"""Camada de persistência do totem.

Usa SQLite (stdlib) para armazenar **produtos**, **movimentações de estoque**
e **vendas** realizadas no totem. O catálogo é sincronizado a partir da
**Wake Commerce**; o estoque operacional é gerido no painel administrativo.

Esquema
-------

- ``products`` — catálogo. Inclui **sku** (código interno, único), preço,
  categoria, imagem, estoque atual (``stock``) e estoque mínimo (``min_stock``).
  ``active = 0`` oculta o produto do cliente sem apagar o histórico.

- ``stock_movements`` — toda variação de estoque (entrada, saída manual,
  venda automática, ajuste, estoque inicial). Guarda ``quantity`` (módulo),
  ``delta`` (sinalizado) e ``balance_after`` (saldo após a movimentação),
  de modo que o histórico sobreviva a qualquer recálculo futuro.

- ``transactions`` — uma linha por venda confirmada no totem.
- ``transaction_items`` — itens com *snapshot* de nome/preço/categoria
  (e **product_sku** quando disponível).
- ``sellers`` — credenciais dos vendedores que acessam o painel somente
  leitura de apoio aos totens.

Invariante: toda alteração de ``products.stock`` é feita na mesma conexão
que insere a ``stock_movements`` correspondente, garantindo consistência.
"""

from __future__ import annotations

import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from database.sku_helpers import _product_sku_label


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "database")
DB_PATH = os.path.join(DB_DIR, "totem.sqlite3")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY,
    sku          TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    description  TEXT,
    price        REAL    NOT NULL DEFAULT 0,
    image        TEXT,
    stock        INTEGER NOT NULL DEFAULT 0,
    min_stock    INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (sku)
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number    TEXT    UNIQUE NOT NULL,
    created_at      TEXT    NOT NULL,
    total           REAL    NOT NULL,
    items_count     INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'confirmado',
    client_name     TEXT,
    client_cpf      TEXT,
    client_zipcode  TEXT,
    client_address  TEXT,
    client_number   TEXT,
    client_complement TEXT,
    client_city     TEXT,
    client_state    TEXT,
    seller_id       INTEGER,
    seller_name     TEXT,
    FOREIGN KEY (seller_id) REFERENCES sellers(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS transaction_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    product_id     TEXT,
    product_name   TEXT    NOT NULL,
    category       TEXT,
    unit_price     REAL    NOT NULL,
    quantity       INTEGER NOT NULL,
    subtotal       REAL    NOT NULL,
    product_sku    TEXT,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     INTEGER NOT NULL,
    movement_type  TEXT    NOT NULL
        CHECK (movement_type IN ('entrada','saida','venda','ajuste','inicial')),
    quantity       INTEGER NOT NULL,
    delta          INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    unit_cost      REAL,
    reason         TEXT,
    reference      TEXT,
    transaction_id INTEGER,
    created_by     TEXT,
    created_at     TEXT    NOT NULL,
    FOREIGN KEY (product_id)     REFERENCES products(id),
    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS sellers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    email          TEXT    UNIQUE NOT NULL,
    password_hash  TEXT    NOT NULL,
    pin_hash       TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    last_login_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_created_at
    ON transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_products_category
    ON products(category);
CREATE INDEX IF NOT EXISTS idx_stock_movements_product_created
    ON stock_movements(product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_movements_created_at
    ON stock_movements(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sellers_email
    ON sellers(email);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _default_sku_for_id(product_id: int) -> str:
    """SKU de fallback quando o cadastro não possui código (formato ``OM-`` + id)."""
    pid = int(product_id)
    if pid <= 0:
        return f"OM-invalid-{abs(pid)}"
    return f"OM-{pid:05d}"


def _is_placeholder_product_name(name: str) -> bool:
    n = (name or "").strip()
    return not n or n == "Produto"


def _is_generated_fallback_sku(sku: str, product_id: int) -> bool:
    return (sku or "").strip() == _default_sku_for_id(int(product_id))


def _ensure_distinct_sku(conn: sqlite3.Connection, pid: int, sku: str) -> str:
    """Garante ``sku`` único na tabela (a Wake pode repetir SKU entre produtos)."""
    base = (sku or "").strip() or _default_sku_for_id(pid)
    clash = conn.execute(
        "SELECT id FROM products WHERE sku = ? AND id != ?",
        (base, pid),
    ).fetchone()
    if not clash:
        return base
    return _default_sku_for_id(pid)


def _ensure_products_sku_column(conn: sqlite3.Connection) -> None:
    """Bases antigas: adiciona ``sku``; preenche valores; garante índice único."""
    cols = _table_columns(conn, "products")
    if "sku" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN sku TEXT")
    for row in conn.execute("SELECT id, sku FROM products").fetchall():
        pid = int(row["id"])
        s = (row["sku"] or "").strip() if row["sku"] is not None else ""
        if not s:
            conn.execute(
                "UPDATE products SET sku = ?, updated_at = ? WHERE id = ?",
                (_default_sku_for_id(pid), _now_iso(), pid),
            )
    # Índice único (não conflita com UNIQUE de tabelas novas — idempotente)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")


def _ensure_transaction_items_product_sku_column(conn: sqlite3.Connection) -> None:
    if "product_sku" in _table_columns(conn, "transaction_items"):
        return
    conn.execute("ALTER TABLE transaction_items ADD COLUMN product_sku TEXT")


def _ensure_transactions_payment_method(conn: sqlite3.Connection) -> None:
    if "payment_method" in _table_columns(conn, "transactions"):
        return
    conn.execute("ALTER TABLE transactions ADD COLUMN payment_method TEXT")


def _ensure_transactions_client_columns(conn: sqlite3.Connection) -> None:
    """Adiciona colunas de dados do cliente/vendedor em transactions."""
    cols = _table_columns(conn, "transactions")
    client_fields = [
        "client_name", "client_cpf", "client_zipcode", "client_address",
        "client_number", "client_complement", "client_city", "client_state"
    ]
    for field in client_fields:
        if field not in cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {field} TEXT")
    if "seller_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN seller_id INTEGER")
    if "seller_name" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN seller_name TEXT")


def _ensure_sellers_columns(conn: sqlite3.Connection) -> None:
    """Migrações leves para contas de vendedores."""
    cols = _table_columns(conn, "sellers")
    for field, ddl in {
        "name": "TEXT NOT NULL DEFAULT 'Vendedor'",
        "email": "TEXT",
        "password_hash": "TEXT",
        "pin_hash": "TEXT",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "last_login_at": "TEXT",
    }.items():
        if field not in cols:
            conn.execute(f"ALTER TABLE sellers ADD COLUMN {field} {ddl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sellers_email ON sellers(email)")


# ---------------------------------------------------------------------------
# Conexão
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    os.makedirs(DB_DIR, exist_ok=True)


def _connect() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Inicialização + seed
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Cria as tabelas, aplica migrações leves e remove resíduos do seed antigo."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _ensure_products_sku_column(conn)
        _ensure_transaction_items_product_sku_column(conn)
        _ensure_transactions_client_columns(conn)
        _ensure_transactions_payment_method(conn)
        _ensure_sellers_columns(conn)
        _purge_invalid_product_ids(conn)
        _purge_legacy_demo_products(conn)


def _purge_invalid_product_ids(conn: sqlite3.Connection) -> None:
    """Remove cadastros com ``id`` não positivo (resíduos de integrações Wake)."""
    rows = conn.execute("SELECT id FROM products WHERE id < 1").fetchall()
    if not rows:
        return
    ids = [int(r["id"]) for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM stock_movements WHERE product_id IN ({placeholders})",
        ids,
    )
    conn.execute(
        f"DELETE FROM products WHERE id IN ({placeholders})",
        ids,
    )


def _purge_legacy_demo_products(conn: sqlite3.Connection) -> None:
    """Remove produtos do catálogo fictício inicial (imagens picsum.photos).

    O catálogo passou a vir apenas da Wake Commerce; estes registros eram
    identificáveis pela URL de placeholder usada no seed antigo.
    """
    rows = conn.execute(
        "SELECT id FROM products WHERE image LIKE ?",
        ("%picsum.photos%",),
    ).fetchall()
    if not rows:
        return
    ids = [int(r["id"]) for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM stock_movements WHERE product_id IN ({placeholders})",
        ids,
    )
    conn.execute(
        f"DELETE FROM products WHERE id IN ({placeholders})",
        ids,
    )


# ---------------------------------------------------------------------------
# Sincronização com Wake Commerce
# ---------------------------------------------------------------------------

def sync_products_from_wake(products: Iterable[Dict]) -> Dict[str, int]:
    """Sincroniza a biblioteca local de produtos com a Wake Commerce.

    Faz *upsert* por ``id`` (productId da Wake):
    - Produto novo → insere com estoque ``0`` para o admin configurar.
    - Produto existente → atualiza dados de catálogo (nome, categoria, preço,
      imagem e sku), preservando estoque, estoque mínimo e status ativo locais.
    - Produtos locais que **não** vieram da Wake permanecem intactos.

    A Wake é tratada como biblioteca de produtos; o estoque operacional do
    totem é sempre gerido pelo painel administrativo.

    Retorna contadores ``{"inserted": N, "updated": N, "skipped": N}``.
    """
    inserted = updated = skipped = 0
    now = _now_iso()

    with get_conn() as conn:
        for p in products:
            pid = int(p["id"])
            if pid <= 0:
                skipped += 1
                continue

            raw_sku_wake = (p.get("sku") or "").strip()
            nome_wake = str(p.get("nome") or "").strip()
            name = nome_wake if nome_wake else "Produto"
            category = str(p.get("categoria") or "Geral")
            price = float(p.get("preco") or 0)
            image = p.get("imagem") or ""

            existing = conn.execute(
                "SELECT id, name, sku FROM products WHERE id = ?", (pid,)
            ).fetchone()

            if existing is None:
                sku = raw_sku_wake or _default_sku_for_id(pid)
            else:
                ex_name = (existing["name"] or "").strip()
                ex_sku = (existing["sku"] or "").strip()
                if _is_placeholder_product_name(name) and not _is_placeholder_product_name(
                    ex_name
                ):
                    name = ex_name
                if raw_sku_wake:
                    sku = raw_sku_wake
                elif ex_sku and not _is_generated_fallback_sku(ex_sku, pid):
                    sku = ex_sku
                else:
                    sku = _default_sku_for_id(pid)

            sku = _ensure_distinct_sku(conn, pid, sku)

            description = f"{name} — {category}"

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO products
                        (id, sku, name, category, description, price, image,
                         stock, min_stock, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (pid, sku, name, category, description, price, image,
                     0, 5, 1, now, now),
                )
                inserted += 1
            else:
                conn.execute(
                    """
                    UPDATE products
                       SET sku = ?, name = ?, category = ?, description = ?,
                           price = ?, image = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (sku, name, category, description, price, image,
                     now, pid),
                )
                updated += 1

    return {"inserted": inserted, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------------------
# Catálogo (produtos)
# ---------------------------------------------------------------------------

def _product_row_to_client(row: sqlite3.Row) -> Dict:
    """Converte um row em dict com os nomes usados pelo front (pt-BR)."""
    pid = int(row["id"])
    try:
        sku_val = row["sku"]
    except (KeyError, IndexError):
        sku_val = None
    sku = (sku_val or "").strip() if sku_val is not None else ""
    if not sku:
        sku = _default_sku_for_id(pid)
    return {
        "id": pid,
        "sku": sku,
        "nome": row["name"],
        "categoria": row["category"],
        "descricao": row["description"] or "",
        "preco": float(row["price"] or 0),
        "imagem": row["image"],
        "estoque": int(row["stock"] or 0),
        "estoque_minimo": int(row["min_stock"] or 0),
        "ativo": bool(row["active"]),
    }


def list_products_for_client(
    category: Optional[str] = None,
    query: Optional[str] = None,
    include_out_of_stock: bool = True,
    include_inactive: bool = False,
) -> List[Dict]:
    """Produtos para consumo do front do totem/cliente."""
    sql = "SELECT * FROM products WHERE 1=1"
    params: List = []
    if not include_inactive:
        sql += " AND active = 1"
    if not include_out_of_stock:
        sql += " AND stock > 0"
    if category and category.lower() != "todos":
        sql += " AND LOWER(category) = LOWER(?)"
        params.append(category)
    if query:
        like = f"%{query.lower()}%"
        sql += (
            " AND (LOWER(name) LIKE ? OR LOWER(description) LIKE ? "
            "OR LOWER(COALESCE(sku, '')) LIKE ?)"
        )
        params.extend([like, like, like])
    sql += " ORDER BY category, name"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_product_row_to_client(r) for r in rows]


def list_active_product_stocks() -> List[Dict[str, int]]:
    """Id e estoque dos produtos ativos (mesmo conjunto base do catálogo ao cliente)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, stock FROM products WHERE active = 1 ORDER BY id"
        ).fetchall()
    return [
        {"id": int(r["id"]), "estoque": int(r["stock"] or 0)} for r in rows
    ]


def list_products_admin() -> List[Dict]:
    """Todos os produtos para o painel administrativo (inclui inativos)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM products ORDER BY category, name"
        ).fetchall()
    out: List[Dict] = []
    for r in rows:
        d = _product_row_to_client(r)
        d.update(
            {
                "abaixo_minimo": d["estoque"] < d["estoque_minimo"],
                "sem_estoque": d["estoque"] <= 0,
            }
        )
        out.append(d)
    return out


def _admin_stock_filter_clause(
    q: Optional[str],
    categoria: str,
    status: str,
) -> Tuple[str, List]:
    """Monta ``WHERE`` para listagem paginada do estoque admin (mesma semântica do filtro em memória)."""
    parts: List[str] = ["1=1"]
    params: List = []
    if q:
        like = f"%{q.lower()}%"
        parts.append(
            "(LOWER(name) LIKE ? OR LOWER(COALESCE(description, '')) LIKE ? "
            "OR LOWER(COALESCE(sku, '')) LIKE ?)"
        )
        params.extend([like, like, like])
    if categoria and categoria.lower() != "todos":
        parts.append("LOWER(category) = LOWER(?)")
        params.append(categoria)
    st = (status or "todos").strip().lower()
    if st == "baixo":
        parts.append("stock > 0 AND stock < min_stock")
    elif st == "sem_estoque":
        parts.append("stock <= 0")
    elif st == "inativo":
        parts.append("active = 0")
    return " AND ".join(parts), params


def _row_to_admin_product(row) -> Dict:
    d = _product_row_to_client(row)
    d.update(
        {
            "abaixo_minimo": d["estoque"] < d["estoque_minimo"],
            "sem_estoque": d["estoque"] <= 0,
        }
    )
    return d


def count_products_admin_filtered(
    q: Optional[str],
    categoria: str = "todos",
    status: str = "todos",
) -> int:
    """Conta produtos que obedecem aos filtros da tela de estoque admin."""
    where, params = _admin_stock_filter_clause(q, categoria, status)
    sql = f"SELECT COUNT(*) AS c FROM products WHERE {where}"
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["c"] if row else 0)


def list_products_admin_slice(
    q: Optional[str],
    categoria: str = "todos",
    status: str = "todos",
    *,
    limit: int,
    offset: int,
) -> List[Dict]:
    """Página de produtos admin com filtros (SQL ``LIMIT`` / ``OFFSET``)."""
    where, params = _admin_stock_filter_clause(q, categoria, status)
    sql = (
        f"SELECT * FROM products WHERE {where} "
        "ORDER BY category, name LIMIT ? OFFSET ?"
    )
    qparams = list(params) + [int(limit), int(max(0, offset))]
    with get_conn() as conn:
        rows = conn.execute(sql, qparams).fetchall()
    return [_row_to_admin_product(r) for r in rows]


def get_product(product_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ?", (int(product_id),)
        ).fetchone()
    if not row:
        return None
    d = _product_row_to_client(row)
    d.update(
        {
            "abaixo_minimo": d["estoque"] < d["estoque_minimo"],
            "sem_estoque": d["estoque"] <= 0,
        }
    )
    return d


def update_product_min_stock(product_id: int, min_stock: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE products SET min_stock = ?, updated_at = ? WHERE id = ?",
            (max(0, int(min_stock)), _now_iso(), int(product_id)),
        )
        return cur.rowcount > 0


def set_product_active(product_id: int, active: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE products SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, _now_iso(), int(product_id)),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Vendedores (autenticação do painel somente leitura)
# ---------------------------------------------------------------------------

def ensure_seller_account(
    name: str,
    email: str,
    password_hash: str,
    pin_hash: Optional[str] = None,
) -> Dict:
    """Cria uma conta de vendedor se o e-mail ainda não existir."""
    normalized_email = (email or "").strip().lower()
    seller_name = (name or "").strip() or "Vendedor"
    if not normalized_email:
        raise ValueError("E-mail do vendedor é obrigatório.")
    now = _now_iso()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sellers WHERE LOWER(email) = LOWER(?)",
            (normalized_email,),
        ).fetchone()
        if row:
            if pin_hash and not row["pin_hash"]:
                conn.execute(
                    "UPDATE sellers SET pin_hash = ?, updated_at = ? WHERE id = ?",
                    (pin_hash, _now_iso(), int(row["id"])),
                )
                row = conn.execute(
                    "SELECT * FROM sellers WHERE id = ?",
                    (int(row["id"]),),
                ).fetchone()
            return dict(row)
        cur = conn.execute(
            """
            INSERT INTO sellers
                (name, email, password_hash, pin_hash, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (seller_name, normalized_email, password_hash, pin_hash, now, now),
        )
        created = conn.execute(
            "SELECT * FROM sellers WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return dict(created)


def create_seller_account(
    name: str,
    email: str,
    password_hash: str,
    pin_hash: str,
) -> Dict:
    """Cria uma conta de vendedor, falhando se o e-mail já estiver em uso."""
    normalized_email = (email or "").strip().lower()
    seller_name = (name or "").strip()
    if not seller_name:
        raise ValueError("Nome do vendedor é obrigatório.")
    if not normalized_email:
        raise ValueError("E-mail do vendedor é obrigatório.")
    if "@" not in normalized_email:
        raise ValueError("Informe um e-mail válido.")
    if not (password_hash or "").strip():
        raise ValueError("Senha do vendedor é obrigatória.")
    if not (pin_hash or "").strip():
        raise ValueError("PIN do vendedor é obrigatório.")
    now = _now_iso()
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sellers WHERE LOWER(email) = LOWER(?)",
            (normalized_email,),
        ).fetchone()
        if exists:
            raise ValueError("Já existe um vendedor com este e-mail.")
        cur = conn.execute(
            """
            INSERT INTO sellers
                (name, email, password_hash, pin_hash, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (seller_name, normalized_email, password_hash, pin_hash, now, now),
        )
        row = conn.execute(
            "SELECT * FROM sellers WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)


def list_sellers() -> List[Dict]:
    """Lista vendedores com métricas agregadas de vendas vinculadas."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                s.*,
                COALESCE(COUNT(t.id), 0) AS transactions_count,
                COALESCE(SUM(t.total), 0) AS total_revenue,
                COALESCE(SUM(t.items_count), 0) AS items_sold,
                MAX(t.created_at) AS last_sale_at
              FROM sellers s
              LEFT JOIN transactions t
                ON t.seller_id = s.id AND t.status = 'confirmado'
             GROUP BY s.id
             ORDER BY s.active DESC, LOWER(s.name), LOWER(s.email)
            """
        ).fetchall()
    return [dict(r) for r in rows]


def list_seller_pin_hashes() -> List[Dict]:
    """Retorna hashes de PIN para validação/autenticação por PIN."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, name, email, active, pin_hash
              FROM sellers
             WHERE pin_hash IS NOT NULL AND pin_hash <> ''
             ORDER BY id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_seller_by_email(email: str) -> Optional[Dict]:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sellers WHERE LOWER(email) = LOWER(?)",
            (normalized_email,),
        ).fetchone()
    return dict(row) if row else None


def get_seller(seller_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sellers WHERE id = ?",
            (int(seller_id),),
        ).fetchone()
    return dict(row) if row else None


def delete_seller(seller_id: int) -> Dict:
    """Remove o cadastro do vendedor. Transações ligadas ficam com ``seller_id`` nulo (FK)."""
    sid = int(seller_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name FROM sellers WHERE id = ?",
            (sid,),
        ).fetchone()
        if row is None:
            raise ValueError("Vendedor não encontrado.")
        name = str(row["name"] or "")
        conn.execute("DELETE FROM sellers WHERE id = ?", (sid,))
    return {"id": sid, "name": name}


def update_seller_last_login(seller_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sellers SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (_now_iso(), _now_iso(), int(seller_id)),
        )


def update_seller_account(
    seller_id: int,
    *,
    name: str,
    email: str,
    active: bool,
    password_hash: Optional[str] = None,
    pin_hash: Optional[str] = None,
) -> Dict:
    """Atualiza dados principais do vendedor e opcionalmente redefine a senha."""
    normalized_email = (email or "").strip().lower()
    seller_name = (name or "").strip()
    if not seller_name:
        raise ValueError("Nome do vendedor é obrigatório.")
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("Informe um e-mail válido.")
    now = _now_iso()
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM sellers WHERE LOWER(email) = LOWER(?) AND id <> ?",
            (normalized_email, int(seller_id)),
        ).fetchone()
        if exists:
            raise ValueError("Já existe outro vendedor com este e-mail.")
        updates = [
            "name = ?",
            "email = ?",
            "active = ?",
        ]
        params: List = [seller_name, normalized_email, 1 if active else 0]
        if password_hash:
            updates.append("password_hash = ?")
            params.append(password_hash)
        if pin_hash:
            updates.append("pin_hash = ?")
            params.append(pin_hash)
        updates.append("updated_at = ?")
        params.extend([now, int(seller_id)])
        conn.execute(
            f"""
            UPDATE sellers
               SET {", ".join(updates)}
             WHERE id = ?
            """,
            params,
        )
        row = conn.execute(
            "SELECT * FROM sellers WHERE id = ?",
            (int(seller_id),),
        ).fetchone()
    if row is None:
        raise ValueError("Vendedor não encontrado.")
    return dict(row)


# ---------------------------------------------------------------------------
# Núcleo de estoque (operação atômica)
# ---------------------------------------------------------------------------

_VALID_TYPES = {"entrada", "saida", "venda", "ajuste", "inicial"}


def _apply_movement(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    movement_type: str,
    delta: int,
    reason: Optional[str] = None,
    unit_cost: Optional[float] = None,
    reference: Optional[str] = None,
    transaction_id: Optional[int] = None,
    created_by: Optional[str] = None,
) -> Dict:
    """Aplica uma movimentação e atualiza o saldo do produto.

    - ``delta`` é **sinalizado** (positivo para entrada/ajuste+,
      negativo para saída/venda/ajuste-).
    - Levanta ``ValueError`` se o saldo resultante ficaria negativo.
    - Deve ser chamado dentro de uma conexão já aberta (transação SQLite).
    """
    if movement_type not in _VALID_TYPES:
        raise ValueError(f"Tipo de movimentação inválido: {movement_type}")
    if delta == 0:
        raise ValueError("Movimentação com quantidade zero.")

    row = conn.execute(
        "SELECT id, name, stock FROM products WHERE id = ?", (int(product_id),)
    ).fetchone()
    if row is None:
        sku = _product_sku_label(product_id, conn=conn)
        raise ValueError(f"Produto {sku} não encontrado.")

    current = int(row["stock"] or 0)
    new_stock = current + int(delta)
    if new_stock < 0:
        raise ValueError(
            f"Estoque insuficiente para '{row['name']}': "
            f"disponível {current}, necessário {abs(delta)}."
        )

    now = _now_iso()
    conn.execute(
        "UPDATE products SET stock = ?, updated_at = ? WHERE id = ?",
        (new_stock, now, int(product_id)),
    )
    cur = conn.execute(
        """
        INSERT INTO stock_movements
            (product_id, movement_type, quantity, delta, balance_after,
             unit_cost, reason, reference, transaction_id, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(product_id),
            movement_type,
            abs(int(delta)),
            int(delta),
            new_stock,
            float(unit_cost) if unit_cost is not None else None,
            reason,
            reference,
            transaction_id,
            created_by,
            now,
        ),
    )
    return {
        "id": cur.lastrowid,
        "product_id": int(product_id),
        "movement_type": movement_type,
        "delta": int(delta),
        "balance_after": new_stock,
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# Entrada, saída e ajuste (API de alto nível do painel admin)
# ---------------------------------------------------------------------------

def register_stock_entry(
    product_id: int,
    quantity: int,
    *,
    unit_cost: Optional[float] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict:
    """Entrada de estoque (compra/reposição)."""
    qty = int(quantity or 0)
    if qty <= 0:
        raise ValueError("Quantidade da entrada deve ser maior que zero.")
    with get_conn() as conn:
        return _apply_movement(
            conn,
            product_id=product_id,
            movement_type="entrada",
            delta=qty,
            unit_cost=unit_cost,
            reason=reason or None,
            created_by=created_by,
        )


def register_stock_exit(
    product_id: int,
    quantity: int,
    *,
    reason: str,
    created_by: Optional[str] = None,
) -> Dict:
    """Saída manual (perda, quebra, vencimento, devolução ao fornecedor...)."""
    qty = int(quantity or 0)
    if qty <= 0:
        raise ValueError("Quantidade da saída deve ser maior que zero.")
    if not (reason or "").strip():
        raise ValueError("Informe o motivo da saída.")
    with get_conn() as conn:
        return _apply_movement(
            conn,
            product_id=product_id,
            movement_type="saida",
            delta=-qty,
            reason=reason.strip(),
            created_by=created_by,
        )


def register_stock_adjustment(
    product_id: int,
    new_stock: int,
    *,
    reason: str,
    created_by: Optional[str] = None,
) -> Dict:
    """Ajusta o estoque para um valor absoluto (conferência/inventário)."""
    target = int(new_stock)
    if target < 0:
        raise ValueError("O estoque final não pode ser negativo.")
    if not (reason or "").strip():
        raise ValueError("Informe o motivo do ajuste.")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT stock FROM products WHERE id = ?", (int(product_id),)
        ).fetchone()
        if row is None:
            sku = _product_sku_label(product_id, conn=conn)
            raise ValueError(f"Produto {sku} não encontrado.")
        delta = target - int(row["stock"] or 0)
        if delta == 0:
            raise ValueError("O estoque informado é igual ao atual.")
        return _apply_movement(
            conn,
            product_id=product_id,
            movement_type="ajuste",
            delta=delta,
            reason=reason.strip(),
            created_by=created_by,
        )


# ---------------------------------------------------------------------------
# Movimentações (leitura para o painel)
# ---------------------------------------------------------------------------

def _normalize_order_reference(value: Optional[str]) -> str:
    """Remove espaços e ``#`` inicial do código de pedido (ex.: ``#OM...``)."""
    s = (value or "").strip()
    if s.startswith("#"):
        s = s[1:].strip()
    return s


def list_stock_movements(
    *,
    product_id: Optional[int] = None,
    movement_type: Optional[str] = None,
    reference: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """Lista movimentações. ``reference`` filtra pelo código do pedido (vendas no totem)."""
    sql = (
        "SELECT m.*, p.name AS product_name, p.category AS product_category, "
        "t.client_name, t.client_cpf, t.client_zipcode, t.client_address, "
        "t.client_number, t.client_complement, t.client_city, t.client_state, "
        "t.payment_method "
        "FROM stock_movements m "
        "LEFT JOIN products p ON p.id = m.product_id "
        "LEFT JOIN transactions t ON t.id = m.transaction_id "
        "WHERE 1=1"
    )
    params: List = []
    if product_id is not None:
        sql += " AND m.product_id = ?"
        params.append(int(product_id))
    if movement_type and movement_type in _VALID_TYPES:
        sql += " AND m.movement_type = ?"
        params.append(movement_type)
    ref_norm = _normalize_order_reference(reference)
    if ref_norm:
        # Vendas do totem gravam o nº em ``reference`` (ex.: OM260422-1234).
        # ``INSTR`` faz busca por subtexto sem tratar ``%``/``_`` como curingas.
        sql += (
            " AND m.reference IS NOT NULL "
            "AND INSTR(LOWER(m.reference), LOWER(?)) > 0"
        )
        params.append(ref_norm)
    sql += " ORDER BY datetime(m.created_at) DESC, m.id DESC LIMIT ?"
    params.append(int(limit))

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_stock_stats() -> Dict:
    """Métricas agregadas de catálogo/estoque para o dashboard."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS products_count,
                COALESCE(SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END), 0) AS products_active,
                COALESCE(SUM(stock), 0) AS units_in_stock,
                COALESCE(SUM(stock * price), 0) AS stock_value,
                COALESCE(SUM(CASE WHEN stock < min_stock THEN 1 ELSE 0 END), 0) AS below_min,
                COALESCE(SUM(CASE WHEN stock <= 0 THEN 1 ELSE 0 END), 0) AS out_of_stock
              FROM products
            """
        ).fetchone()
    return {
        "products_count": int(row["products_count"] or 0),
        "products_active": int(row["products_active"] or 0),
        "units_in_stock": int(row["units_in_stock"] or 0),
        "stock_value": float(row["stock_value"] or 0.0),
        "below_min": int(row["below_min"] or 0),
        "out_of_stock": int(row["out_of_stock"] or 0),
    }


# ---------------------------------------------------------------------------
# Transações (vendas)
# ---------------------------------------------------------------------------

def generate_order_number(conn: Optional[sqlite3.Connection] = None) -> str:
    """Formato ``OMyymmdd-####`` (único por data + aleatório)."""
    now = datetime.now()
    prefix = f"OM{now.strftime('%y%m%d')}"

    def _exists(c: sqlite3.Connection, number: str) -> bool:
        return c.execute(
            "SELECT 1 FROM transactions WHERE order_number = ?", (number,)
        ).fetchone() is not None

    if conn is not None:
        for _ in range(10):
            number = f"{prefix}-{random.randint(1000, 9999)}"
            if not _exists(conn, number):
                return number
        return f"{prefix}-{int(datetime.now().timestamp())}"

    with get_conn() as c:
        for _ in range(10):
            number = f"{prefix}-{random.randint(1000, 9999)}"
            if not _exists(c, number):
                return number
    return f"{prefix}-{int(datetime.now().timestamp())}"


def create_transaction(
    items: Iterable[Dict],
    *,
    created_by: str = "totem",
    seller_id: Optional[int] = None,
    seller_name: Optional[str] = None,
    client_name: Optional[str] = None,
    client_cpf: Optional[str] = None,
    client_zipcode: Optional[str] = None,
    client_address: Optional[str] = None,
    client_number: Optional[str] = None,
    client_complement: Optional[str] = None,
    client_city: Optional[str] = None,
    client_state: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> Dict:
    """Registra uma venda, seus itens e **decrementa o estoque atomicamente**.

    Cada item deve conter ``id, nome, categoria, preco, quantidade``; ``sku`` é
    opcional (complementado pelo catálogo quando houver ``id``).
    Se qualquer produto não tiver estoque suficiente, **nada é gravado**.

    Parâmetros opcionais de ``client_*`` guardam dados do cliente na transação.

    Retorna ``{id, order_number, total, items_count, created_at}``.
    """
    normalized: List[Dict] = []
    for raw in items or []:
        try:
            qty = int(raw.get("quantidade", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        try:
            price = float(raw.get("preco", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0

        pid_raw = raw.get("id")
        try:
            product_id = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            product_id = None

        sku_in = raw.get("sku")
        product_sku: Optional[str] = None
        if sku_in is not None and str(sku_in).strip():
            product_sku = str(sku_in).strip()

        name = str(raw.get("nome") or "Produto sem nome")
        normalized.append(
            {
                "product_id": product_id,
                "product_id_str": str(pid_raw) if pid_raw is not None else None,
                "product_name": name,
                "product_sku": product_sku,
                "category": raw.get("categoria"),
                "unit_price": price,
                "quantity": qty,
                "subtotal": round(price * qty, 2),
            }
        )

    if not normalized:
        raise ValueError("Nenhum item válido na transação.")

    total = round(sum(i["subtotal"] for i in normalized), 2)
    items_count = sum(i["quantity"] for i in normalized)

    with get_conn() as conn:
        pids = {i["product_id"] for i in normalized if i["product_id"] is not None}
        sku_by_id: Dict[int, str] = {}
        if pids:
            placeholders = ",".join("?" * len(pids))
            for r in conn.execute(
                f"SELECT id, sku FROM products WHERE id IN ({placeholders})",
                list(pids),
            ).fetchall():
                sku_by_id[int(r["id"])] = (r["sku"] or "").strip() or _default_sku_for_id(
                    int(r["id"])
                )
        for i in normalized:
            if i["product_id"] is not None and not (i.get("product_sku") or "").strip():
                i["product_sku"] = sku_by_id.get(i["product_id"])
            elif (i.get("product_sku") or "").strip():
                i["product_sku"] = str(i["product_sku"]).strip()

        # Agrupa quantidade por produto (caso venha duplicado) e checa estoque.
        demand: Dict[int, int] = {}
        for i in normalized:
            if i["product_id"] is None:
                # Itens sem id numérico não afetam estoque (não há vínculo
                # com a tabela products).
                continue
            demand[i["product_id"]] = demand.get(i["product_id"], 0) + i["quantity"]

        for pid, qty in demand.items():
            row = conn.execute(
                "SELECT name, stock FROM products WHERE id = ?", (pid,)
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Produto {_product_sku_label(pid, sku=sku_by_id.get(pid))} "
                    f"não encontrado no catálogo."
                )
            if int(row["stock"] or 0) < qty:
                raise ValueError(
                    f"Estoque insuficiente para '{row['name']}': "
                    f"disponível {int(row['stock'] or 0)}, pedido {qty}."
                )

        order_number = generate_order_number(conn)
        created_at = _now_iso()

        cur = conn.execute(
            """
            INSERT INTO transactions
                (order_number, created_at, total, items_count, status,
                 client_name, client_cpf, client_zipcode, client_address,
                 client_number, client_complement, client_city, client_state,
                 seller_id, seller_name, payment_method)
            VALUES (?, ?, ?, ?, 'confirmado', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_number, created_at, total, items_count,
                client_name, client_cpf, client_zipcode, client_address,
                client_number, client_complement, client_city, client_state,
                seller_id, seller_name, payment_method,
            ),
        )
        tx_id = cur.lastrowid

        conn.executemany(
            """
            INSERT INTO transaction_items
                (transaction_id, product_id, product_name, category,
                 unit_price, quantity, subtotal, product_sku)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    tx_id,
                    i["product_id_str"],
                    i["product_name"],
                    i["category"],
                    i["unit_price"],
                    i["quantity"],
                    i["subtotal"],
                    i.get("product_sku"),
                )
                for i in normalized
            ],
        )

        # Registra a saída de estoque para cada produto (agrupado).
        for pid, qty in demand.items():
            _apply_movement(
                conn,
                product_id=pid,
                movement_type="venda",
                delta=-qty,
                reason="Venda no totem",
                reference=order_number,
                transaction_id=tx_id,
                created_by=created_by,
            )

    return {
        "id": tx_id,
        "order_number": order_number,
        "total": total,
        "items_count": items_count,
        "created_at": created_at,
        "seller_id": seller_id,
        "seller_name": seller_name,
        "payment_method": payment_method,
    }


def _items_for(conn: sqlite3.Connection, tx_id: int) -> List[Dict]:
    rows = conn.execute(
        """
        SELECT id, product_id, product_name, category,
               unit_price, quantity, subtotal, product_sku
          FROM transaction_items
         WHERE transaction_id = ?
         ORDER BY id
        """,
        (tx_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_transactions(limit: int = 200, seller_id: Optional[int] = None) -> List[Dict]:
    """Retorna as transações mais recentes com seus itens agrupados."""
    with get_conn() as conn:
        params: List = []
        where = ""
        if seller_id is not None:
            where = "WHERE seller_id = ?"
            params.append(int(seller_id))
        params.append(int(limit))
        tx_rows = conn.execute(
            f"""
            SELECT id, order_number, created_at, total, items_count, status,
                   seller_id, seller_name, payment_method
              FROM transactions
             {where}
             ORDER BY datetime(created_at) DESC, id DESC
             LIMIT ?
            """,
            params,
        ).fetchall()
        results: List[Dict] = []
        for tx in tx_rows:
            tx_dict = dict(tx)
            tx_dict["items"] = _items_for(conn, tx["id"])
            results.append(tx_dict)
        return results


def get_stats(seller_id: Optional[int] = None) -> Dict:
    """Total de vendas e montante arrecadado (apenas transações confirmadas)."""
    with get_conn() as conn:
        seller_clause = ""
        params: List = []
        today_params: List = []
        if seller_id is not None:
            seller_clause = " AND seller_id = ?"
            params.append(int(seller_id))
            today_params.append(int(seller_id))
        row = conn.execute(
            f"""
            SELECT
                COUNT(*)                      AS transactions_count,
                COALESCE(SUM(total), 0)       AS total_revenue,
                COALESCE(SUM(items_count), 0) AS items_sold
              FROM transactions
             WHERE status = 'confirmado'
               {seller_clause}
            """,
            params,
        ).fetchone()
        today = conn.execute(
            f"""
            SELECT
                COUNT(*)                AS transactions_today,
                COALESCE(SUM(total), 0) AS revenue_today
              FROM transactions
             WHERE status = 'confirmado'
               AND date(created_at) = date('now','localtime')
               {seller_clause}
            """,
            today_params,
        ).fetchone()

    return {
        "transactions_count": int(row["transactions_count"] or 0),
        "total_revenue": float(row["total_revenue"] or 0.0),
        "items_sold": int(row["items_sold"] or 0),
        "transactions_today": int(today["transactions_today"] or 0),
        "revenue_today": float(today["revenue_today"] or 0.0),
    }


def reset_totem_to_default_state() -> Dict[str, int]:
    """Reinicia o totem ao estado padrão: **estoque zerado** e histórico limpo.

    - Apaga **todas** as transações (itens inclusos por ``ON DELETE CASCADE``),
      removendo vendas e dados de cliente.
    - Apaga **todas** as movimentações de estoque.
    - Zera ``stock`` de **todos** os produtos e registra uma nova linha
      ``inicial`` com saldo **0** por produto (baseline para o administrador
      reabastecer manualmente). Não reutiliza saldos antigos da Wake nem de
      sincronizações passadas.
    """
    with get_conn() as conn:
        n_tx_row = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()
        n_tx_before = int(n_tx_row["c"] or 0)

        conn.execute("DELETE FROM transactions")

        cur = conn.execute("DELETE FROM stock_movements")
        n_mov_deleted = int(cur.rowcount or 0)

        now = _now_iso()
        prod_rows = conn.execute("SELECT id FROM products").fetchall()
        reason = "Estado padrão (reinício — estoque zerado)"
        for r in prod_rows:
            pid = int(r["id"])
            conn.execute(
                "UPDATE products SET stock = 0, updated_at = ? WHERE id = ?",
                (now, pid),
            )
            conn.execute(
                """
                INSERT INTO stock_movements
                    (product_id, movement_type, quantity, delta,
                     balance_after, reason, created_by, created_at)
                VALUES (?, 'inicial', 0, 0, 0, ?, ?, ?)
                """,
                (pid, reason, "system", now),
            )

        return {
            "transactions_deleted": n_tx_before,
            "movements_deleted": n_mov_deleted,
            "products_restored": len(prod_rows),
        }


def get_transaction(tx_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["items"] = _items_for(conn, tx_id)
        return data


def get_transaction_by_order_number(order_number: str) -> Optional[Dict]:
    """Busca uma transação pelo número do pedido (ex: OM260424-1234), incluindo dados do cliente."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE order_number = ?",
            (order_number.strip(),),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["items"] = _items_for(conn, int(row["id"]))
        return data
