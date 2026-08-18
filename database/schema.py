"""Initial DDL and idempotent migrations."""
from __future__ import annotations

import sqlite3
from typing import List

from .connection import DEFAULT_MIN_STOCK, _now_iso, get_conn
from .sku_helpers import _default_sku_for_id

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
    min_stock    INTEGER NOT NULL DEFAULT 5,
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
    username       TEXT    UNIQUE,
    password_hash  TEXT    NOT NULL,
    pin_hash       TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    last_login_at  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    description  TEXT,
    badge_color  TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS event_products (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         INTEGER NOT NULL,
    product_id       INTEGER NOT NULL,
    stock            INTEGER NOT NULL DEFAULT 0,
    min_stock        INTEGER NOT NULL DEFAULT 5,
    backorder_limit  INTEGER NOT NULL DEFAULT -1,
    price            REAL,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    UNIQUE (event_id, product_id),
    FOREIGN KEY (event_id)   REFERENCES events(id)   ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_event_products_event
    ON event_products(event_id);

CREATE TABLE IF NOT EXISTS promotions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    rule_type   TEXT    NOT NULL
        CHECK (rule_type IN ('percent', 'fixed', 'bogo', 'min_bundle', 'exact_bundle')),
    rule_value  REAL    NOT NULL DEFAULT 0,
    min_qty     INTEGER NOT NULL DEFAULT 1,
    free_qty    INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS promotion_products (
    promotion_id INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    PRIMARY KEY (promotion_id, product_id),
    FOREIGN KEY (promotion_id) REFERENCES promotions(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id)   REFERENCES products(id)  ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_promotions_event
    ON promotions(event_id, active);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

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


def _ensure_transactions_card_installments(conn: sqlite3.Connection) -> None:
    if "card_installments" in _table_columns(conn, "transactions"):
        return
    conn.execute("ALTER TABLE transactions ADD COLUMN card_installments INTEGER")


def _ensure_transactions_aut(conn: sqlite3.Connection) -> None:
    if "aut" in _table_columns(conn, "transactions"):
        return
    conn.execute("ALTER TABLE transactions ADD COLUMN aut TEXT")


def _ensure_transactions_event_id(conn: sqlite3.Connection) -> None:
    """Adiciona event_id em transactions para rastrear vendas de eventos."""
    if "event_id" in _table_columns(conn, "transactions"):
        return
    conn.execute("ALTER TABLE transactions ADD COLUMN event_id INTEGER")


def _ensure_transactions_client_columns(conn: sqlite3.Connection) -> None:
    """Adiciona colunas de dados do cliente/vendedor em transactions."""
    cols = _table_columns(conn, "transactions")
    client_fields = [
        "client_name", "client_cpf", "client_email", "client_phone",
        "client_zipcode", "client_address",
        "client_number", "client_complement", "client_city", "client_state"
    ]
    for field in client_fields:
        if field not in cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {field} TEXT")
    if "seller_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN seller_id INTEGER")
    if "seller_name" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN seller_name TEXT")


def _ensure_transactions_cro_columns(conn: sqlite3.Connection) -> None:
    """Garante colunas de UF/número CRO e campos legados (categoria/validação) em transactions."""
    cols = _table_columns(conn, "transactions")
    cro_fields = {
        "client_cro_uf": "TEXT",
        "client_cro_numero": "TEXT",
        "client_cro_categoria": "TEXT",
        "client_cro_validated": "INTEGER DEFAULT 0",
        "client_cro_validation_data": "TEXT",
    }
    for field, ddl in cro_fields.items():
        if field not in cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {field} {ddl}")


def _ensure_events_tables(conn: sqlite3.Connection) -> None:
    """Cria tabelas de eventos caso a base seja anterior à sua introdução."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            description  TEXT,
            badge_color  TEXT,
            active       INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_products (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id         INTEGER NOT NULL,
            product_id       INTEGER NOT NULL,
            stock            INTEGER NOT NULL DEFAULT 0,
            min_stock        INTEGER NOT NULL DEFAULT 5,
            backorder_limit  INTEGER NOT NULL DEFAULT -1,
            price            REAL,
            created_at       TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL,
            UNIQUE (event_id, product_id),
            FOREIGN KEY (event_id)   REFERENCES events(id)   ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_event_products_event
            ON event_products(event_id);
    """)


def _ensure_events_badge_color(conn: sqlite3.Connection) -> None:
    """Acrescenta ``badge_color`` (hex) em ``events`` em bases antigas."""
    if "badge_color" in _table_columns(conn, "events"):
        return
    conn.execute("ALTER TABLE events ADD COLUMN badge_color TEXT")


def _ensure_event_products_price(conn: sqlite3.Connection) -> None:
    """Preço de venda do produto no evento. ``NULL`` herda ``products.price``."""
    if "price" in _table_columns(conn, "event_products"):
        return
    conn.execute("ALTER TABLE event_products ADD COLUMN price REAL")


def _ensure_event_products_backorder_limit(conn: sqlite3.Connection) -> None:
    """Acrescenta ``backorder_limit`` em ``event_products`` em bases antigas.

    ``-1`` (padrão/"sem limite") permite entrega pendente livremente.
    ``0`` bloqueia qualquer entrega pendente do produto no evento.
    Um valor ``> 0`` é o total de unidades pendentes permitidas.
    """
    if "backorder_limit" not in _table_columns(conn, "event_products"):
        conn.execute(
            "ALTER TABLE event_products ADD COLUMN backorder_limit INTEGER NOT NULL DEFAULT -1"
        )
    _ensure_schema_migrations_table(conn)
    migration_name = "event_products_backorder_limit_unlimited_sentinel"
    if _migration_applied(conn, migration_name):
        return
    # A coluna foi introduzida com ``0`` significando "sem limite"; agora ``0``
    # passa a significar "bloqueado", então valores antigos de ``0`` precisam
    # ser convertidos para o novo sentinel de "sem limite" (``-1``).
    conn.execute("UPDATE event_products SET backorder_limit = -1 WHERE backorder_limit = 0")
    _mark_migration_applied(conn, migration_name)


def _ensure_event_products_backorder_omit_insert_unlimited(conn: sqlite3.Connection) -> None:
    """INSERTs antigos omitiam ``backorder_limit`` e o SQLite usava DEFAULT 0.

    Nesta base a coluna foi criada com ``DEFAULT 0`` (quando 0 ainda significava
    'sem limite'). Depois 0 passou a bloquear vendas futuras, então produtos
    importados por planilha (estoque 0 + limite 0) apareciam como bloqueados.
    """
    _ensure_schema_migrations_table(conn)
    migration_name = "event_products_backorder_omit_insert_unlimited"
    if _migration_applied(conn, migration_name):
        return
    conn.execute("UPDATE event_products SET backorder_limit = -1 WHERE backorder_limit = 0")
    _mark_migration_applied(conn, migration_name)


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL
        )
        """
    )


def _migration_applied(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (name,),
    ).fetchone()
    return row is not None


def _mark_migration_applied(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (name, _now_iso()),
    )


def _ensure_event_extensions(conn: sqlite3.Connection) -> None:
    """Adiciona event_id a stock_movements e cria event_sellers (idempotente)."""
    # Adicionar event_id a stock_movements se ainda não existir
    sm_cols = _table_columns(conn, "stock_movements")
    if "event_id" not in sm_cols:
        conn.execute(
            "ALTER TABLE stock_movements ADD COLUMN event_id INTEGER "
            "REFERENCES events(id) ON DELETE SET NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_movements_event "
        "ON stock_movements(event_id)"
    )
    # Tabela de associação evento ↔ vendedor
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_sellers (
            event_id   INTEGER NOT NULL,
            seller_id  INTEGER NOT NULL,
            added_at   TEXT    NOT NULL,
            PRIMARY KEY (event_id, seller_id),
            FOREIGN KEY (event_id)  REFERENCES events(id)  ON DELETE CASCADE,
            FOREIGN KEY (seller_id) REFERENCES sellers(id) ON DELETE CASCADE
        );
    """)
    _ensure_event_sellers_one_event_per_seller(conn)


def _ensure_event_sellers_one_event_per_seller(conn: sqlite3.Connection) -> None:
    """Remove vínculos duplicados por vendedor e garante índice único em ``seller_id``.

    Regra de negócio: cada vendedor pode estar associado a no máximo um evento.
    Em caso de histórico inconsistente, mantém o vínculo mais recente (``added_at``).
    """
    dup_rows = conn.execute(
        """
        SELECT seller_id FROM event_sellers GROUP BY seller_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in dup_rows:
        sid = int(row["seller_id"])
        keep = conn.execute(
            """
            SELECT rowid FROM event_sellers
             WHERE seller_id = ?
             ORDER BY datetime(added_at) DESC, event_id DESC
             LIMIT 1
            """,
            (sid,),
        ).fetchone()
        if not keep:
            continue
        conn.execute(
            "DELETE FROM event_sellers WHERE seller_id = ? AND rowid != ?",
            (sid, int(keep["rowid"])),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_sellers_seller_unique "
        "ON event_sellers(seller_id)"
    )


def _ensure_transaction_items_promo_columns(conn: sqlite3.Connection) -> None:
    """Adiciona colunas de auditoria de promoção em transaction_items (idempotente)."""
    cols = _table_columns(conn, "transaction_items")
    if "original_price" not in cols:
        conn.execute("ALTER TABLE transaction_items ADD COLUMN original_price REAL")
    if "promotion_id" not in cols:
        conn.execute("ALTER TABLE transaction_items ADD COLUMN promotion_id INTEGER")


def _ensure_delivery_columns(conn: sqlite3.Connection) -> None:
    """Colunas de controle de entrega por item (venda com estoque parcial).

    - ``transaction_items.quantity_delivered``: unidades já entregues (estoque baixado).
    - ``transactions.delivery_status``: ``completa`` | ``parcial`` | ``pendente``.

    Backfill: transações já confirmadas/estornadas antes desta migração tiveram
    baixa integral de estoque, logo ``quantity_delivered = quantity``.
    """
    ti_cols = _table_columns(conn, "transaction_items")
    tx_cols = _table_columns(conn, "transactions")
    is_new = "quantity_delivered" not in ti_cols
    if is_new:
        conn.execute(
            "ALTER TABLE transaction_items "
            "ADD COLUMN quantity_delivered INTEGER NOT NULL DEFAULT 0"
        )
    if "delivery_status" not in tx_cols:
        conn.execute(
            "ALTER TABLE transactions "
            "ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'completa'"
        )
    if is_new:
        conn.execute(
            """
            UPDATE transaction_items
               SET quantity_delivered = quantity
             WHERE transaction_id IN (
                       SELECT id FROM transactions
                        WHERE status IN ('confirmado', 'estornado')
                   )
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_delivery_status "
        "ON transactions(event_id, delivery_status)"
    )


def _ensure_transactions_handover_status(conn: sqlite3.Connection) -> None:
    """Confirmação geral de entrega do pedido (retirada no balcão pelo cliente).

    - ``transactions.handover_status``: ``pendente`` | ``entregue``.
    - ``transactions.handover_confirmed_at``: data/hora da confirmação.

    Independente do controle de retirada pendente por item (``delivery_status``
    / ``quantity_delivered``), que continua exigindo baixa de estoque. Este
    campo apenas sinaliza, para fins de acompanhamento, que o pedido como um
    todo já foi entregue ao cliente.
    """
    cols = _table_columns(conn, "transactions")
    if "handover_status" not in cols:
        conn.execute(
            "ALTER TABLE transactions "
            "ADD COLUMN handover_status TEXT NOT NULL DEFAULT 'pendente'"
        )
    if "handover_confirmed_at" not in cols:
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN handover_confirmed_at TEXT"
        )


def _ensure_sellers_columns(conn: sqlite3.Connection) -> None:
    """Migrações leves para contas de vendedores."""
    cols = _table_columns(conn, "sellers")
    for field, ddl in {
        "name": "TEXT NOT NULL DEFAULT 'Vendedor'",
        "email": "TEXT",
        "username": "TEXT",
        "password_hash": "TEXT",
        "pin_hash": "TEXT",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "last_login_at": "TEXT",
    }.items():
        if field not in cols:
            conn.execute(f"ALTER TABLE sellers ADD COLUMN {field} {ddl}")
    used: set[str] = set()
    rows = conn.execute("SELECT id, email, username FROM sellers").fetchall()
    for row in rows:
        current = (row["username"] or "").strip().lower()
        if current:
            used.add(current)
            continue
        email = (row["email"] or "").strip().lower()
        base = email.split("@", 1)[0] if "@" in email else email
        base = "".join(ch for ch in base if ch.isalnum() or ch in "._-") or f"vendedor{int(row['id'])}"
        if len(base) < 3:
            base = f"{base}{int(row['id'])}"
        candidate = base[:40]
        n = 2
        while candidate in used:
            suffix = str(n)
            candidate = f"{base[: max(1, 40 - len(suffix))]}{suffix}"
            n += 1
        used.add(candidate)
        conn.execute(
            "UPDATE sellers SET username = ? WHERE id = ?",
            (candidate, int(row["id"])),
        )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sellers_email ON sellers(email)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sellers_username ON sellers(username)")


def _ensure_products_wake_columns(conn: sqlite3.Connection) -> None:
    """Colunas Wake em ``products`` (variante principal, nome da variante)."""
    cols = _table_columns(conn, "products")
    for field, ddl in {
        "wake_product_id": "INTEGER",
        "variant_name": "TEXT",
        "main_variant": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if field not in cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {field} {ddl}")


def _ensure_product_sku_aliases_table(conn: sqlite3.Connection) -> None:
    """Tabela de SKUs alternativos (ERP / legado) por produto."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS product_sku_aliases (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            sku        TEXT    NOT NULL,
            created_at TEXT    NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            UNIQUE (sku)
        );
        CREATE INDEX IF NOT EXISTS idx_product_sku_aliases_product
            ON product_sku_aliases(product_id);
    """)


def _ensure_promotions_extended_rule_types(conn: sqlite3.Connection) -> None:
    """Recria promotions com CHECK ampliado para incluir min_bundle e exact_bundle.

    Em bases existentes o DDL ``CREATE TABLE IF NOT EXISTS`` não altera a restrição
    CHECK — por isso recriamos a tabela preservando os dados.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='promotions'"
    ).fetchone()
    if row is None:
        return  # tabela ainda não existe; _SCHEMA criará com CHECK correto
    if 'min_bundle' in (row[0] or ''):
        return  # já migrada

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE IF EXISTS promotions_v2")
    conn.execute(
        """
        CREATE TABLE promotions_v2 (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            rule_type   TEXT    NOT NULL
                CHECK (rule_type IN ('percent','fixed','bogo','min_bundle','exact_bundle')),
            rule_value  REAL    NOT NULL DEFAULT 0,
            min_qty     INTEGER NOT NULL DEFAULT 1,
            free_qty    INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        )
        """
    )
    # Normaliza nomes legados de rule_type que possam existir de tentativas anteriores
    conn.execute(
        "INSERT INTO promotions_v2 "
        "SELECT id, event_id, name, "
        "CASE rule_type "
        "  WHEN 'a_partir_de' THEN 'min_bundle' "
        "  WHEN 'na_compra_de' THEN 'exact_bundle' "
        "  WHEN 'min_bundle' THEN 'min_bundle' "
        "  WHEN 'exact_bundle' THEN 'exact_bundle' "
        "  ELSE rule_type END, "
        "rule_value, min_qty, free_qty, active, created_at, updated_at "
        "FROM promotions"
    )
    conn.execute("DROP TABLE promotions")
    conn.execute("ALTER TABLE promotions_v2 RENAME TO promotions")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promotions_event "
        "ON promotions(event_id, active)"
    )
    conn.execute("PRAGMA foreign_keys = ON")


# ---------------------------------------------------------------------------
# Conexão
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inicialização + seed
# ---------------------------------------------------------------------------

def _consolidate_legacy_movement_types(conn: sqlite3.Connection) -> None:
    """Converte tipos legados ``ajuste``/``inicial`` para ``entrada`` ou ``saída``."""
    conn.execute(
        "DELETE FROM stock_movements WHERE movement_type IN ('ajuste', 'inicial') AND delta = 0"
    )
    conn.execute(
        """
        UPDATE stock_movements
           SET movement_type = 'entrada'
         WHERE movement_type IN ('inicial', 'ajuste') AND delta > 0
        """
    )
    conn.execute(
        """
        UPDATE stock_movements
           SET movement_type = 'saida'
         WHERE movement_type = 'ajuste' AND delta < 0
        """
    )


def _ensure_min_stock_default_five(conn: sqlite3.Connection) -> None:
    """Migração única: produtos com mínimo 0 passam a usar o padrão (5 un.)."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 1:
        return
    conn.execute(
        "UPDATE products SET min_stock = ? WHERE min_stock = 0",
        (DEFAULT_MIN_STOCK,),
    )
    conn.execute(
        "UPDATE event_products SET min_stock = ? WHERE min_stock = 0",
        (DEFAULT_MIN_STOCK,),
    )
    conn.execute("PRAGMA user_version = 1")


def init_db() -> None:
    """Cria as tabelas, aplica migrações leves e remove resíduos do seed antigo."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _ensure_products_sku_column(conn)
        _ensure_transaction_items_product_sku_column(conn)
        _ensure_transactions_client_columns(conn)
        _ensure_transactions_cro_columns(conn)
        _ensure_transactions_payment_method(conn)
        _ensure_transactions_card_installments(conn)
        _ensure_transactions_aut(conn)
        _ensure_transactions_event_id(conn)
        _ensure_sellers_columns(conn)
        _ensure_products_wake_columns(conn)
        _ensure_product_sku_aliases_table(conn)
        _ensure_events_tables(conn)
        _ensure_promotions_extended_rule_types(conn)
        _ensure_events_badge_color(conn)
        _ensure_event_extensions(conn)
        _ensure_event_products_backorder_limit(conn)
        _ensure_event_products_backorder_omit_insert_unlimited(conn)
        _ensure_event_products_price(conn)
        _ensure_transaction_items_promo_columns(conn)
        _ensure_delivery_columns(conn)
        _ensure_transactions_handover_status(conn)
        _consolidate_legacy_movement_types(conn)
        _purge_invalid_product_ids(conn)
        _purge_legacy_demo_products(conn)
        _ensure_min_stock_default_five(conn)


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
