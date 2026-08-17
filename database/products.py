"""Product catalog, Wake sync and admin library listings."""
from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

from .connection import DEFAULT_MIN_STOCK, _now_iso, get_conn
from .sku_helpers import (
    _default_sku_for_id,
    _ensure_distinct_sku,
    _is_generated_fallback_sku,
    _is_placeholder_product_name,
)
import product_images

log = logging.getLogger(__name__)


def _row_to_product_dict(row: sqlite3.Row) -> Dict:
    """Converte row SQLite de ``products`` para dict interno."""
    return dict(row)


def _remap_product_id_references(conn: sqlite3.Connection, old_id: int, new_id: int) -> None:
    """Redireciona FKs de cadastros legados (``id`` = ``productId``) para variante Wake."""
    if old_id == new_id:
        return
    conn.execute(
        "UPDATE event_products SET product_id = ? WHERE product_id = ?",
        (new_id, old_id),
    )
    conn.execute(
        "UPDATE stock_movements SET product_id = ? WHERE product_id = ?",
        (new_id, old_id),
    )
    conn.execute(
        "UPDATE promotion_products SET product_id = ? WHERE product_id = ?",
        (new_id, old_id),
    )
    conn.execute(
        "UPDATE transaction_items SET product_id = ? WHERE product_id = ?",
        (str(new_id), str(old_id)),
    )
    conn.execute(
        "UPDATE product_sku_aliases SET product_id = ? WHERE product_id = ?",
        (new_id, old_id),
    )


def _maybe_migrate_legacy_wake_product_id(
    conn: sqlite3.Connection,
    wake_product_id: int,
    variant_id: int,
    *,
    is_main_variant: bool,
) -> bool:
    """Se existir linha legada com ``id = wake_product_id``, migra referências."""
    if not is_main_variant or wake_product_id <= 0 or wake_product_id == variant_id:
        return False
    legacy = conn.execute(
        "SELECT id FROM products WHERE id = ?",
        (wake_product_id,),
    ).fetchone()
    if legacy is None:
        return False
    _remap_product_id_references(conn, wake_product_id, variant_id)
    conn.execute("DELETE FROM products WHERE id = ?", (wake_product_id,))
    return True


def sync_products_from_wake(
    products: Iterable[Dict],
    *,
    remap_legacy: bool = True,
) -> Dict[str, int]:
    """Sincroniza a biblioteca local com variantes Wake (``id`` = ``productVariantId``).

    - Produto novo → insere com estoque ``0``.
    - Produto existente → atualiza catálogo; preserva estoque/mínimo/ativo locais.
    - Com ``remap_legacy``, cadastros antigos indexados por ``productId`` são
      redirecionados para a variante principal quando aplicável.

    Retorna ``{"inserted": N, "updated": N, "skipped": N, "remapped": N}``.
    """
    inserted = updated = skipped = remapped = 0
    now = _now_iso()

    with get_conn() as conn:
        for p in products:
            variant_id = int(p["id"])
            if variant_id <= 0:
                skipped += 1
                continue

            wake_product_id = int(p.get("wake_product_id") or variant_id)
            raw_sku_wake = (p.get("sku") or "").strip()
            nome_wake = str(p.get("nome") or "").strip()
            name = nome_wake if nome_wake else "Produto"
            category = str(p.get("categoria") or "Geral")
            price = float(p.get("preco") or 0)
            image = p.get("imagem") or ""
            variant_name = str(p.get("variant_name") or "").strip()
            main_variant = 1 if p.get("main_variant") else 0

            existing = conn.execute(
                "SELECT id, name, sku FROM products WHERE id = ?", (variant_id,)
            ).fetchone()

            if existing is None:
                sku = raw_sku_wake or _default_sku_for_id(variant_id)
            else:
                ex_name = (existing["name"] or "").strip()
                ex_sku = (existing["sku"] or "").strip()
                if _is_placeholder_product_name(name) and not _is_placeholder_product_name(
                    ex_name
                ):
                    name = ex_name
                if raw_sku_wake:
                    sku = raw_sku_wake
                elif ex_sku and not _is_generated_fallback_sku(ex_sku, variant_id):
                    sku = ex_sku
                else:
                    sku = _default_sku_for_id(variant_id)

            sku = _ensure_distinct_sku(conn, variant_id, sku)
            description = f"{name} — {category}"

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO products
                        (id, sku, name, category, description, price, image,
                         stock, min_stock, active, wake_product_id, variant_name,
                         main_variant, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        variant_id, sku, name, category, description, price, image,
                        0, DEFAULT_MIN_STOCK, 1, wake_product_id, variant_name or None,
                        main_variant, now, now,
                    ),
                )
                inserted += 1
            else:
                conn.execute(
                    """
                    UPDATE products
                       SET sku = ?, name = ?, category = ?, description = ?,
                           price = ?, image = ?, wake_product_id = ?,
                           variant_name = ?, main_variant = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        sku, name, category, description, price, image,
                        wake_product_id, variant_name or None, main_variant,
                        now, variant_id,
                    ),
                )
                updated += 1

            if remap_legacy and _maybe_migrate_legacy_wake_product_id(
                conn,
                wake_product_id,
                variant_id,
                is_main_variant=bool(main_variant),
            ):
                remapped += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "remapped": remapped,
    }


def _find_product_row_local(conn: sqlite3.Connection, q: str) -> Optional[sqlite3.Row]:
    """Busca produto no SQLite (variante, alias ERP ou ``wake_product_id`` legado)."""
    q = (q or "").strip()
    if not q:
        return None

    row = conn.execute("SELECT * FROM products WHERE sku = ?", (q,)).fetchone()
    if row:
        return row

    row = conn.execute(
        """
        SELECT p.* FROM product_sku_aliases a
          JOIN products p ON p.id = a.product_id
         WHERE a.sku = ?
        """,
        (q,),
    ).fetchone()
    if row:
        return row

    try:
        num = int(q.lstrip("#").strip())
    except ValueError:
        return None

    row = conn.execute("SELECT * FROM products WHERE id = ?", (num,)).fetchone()
    if row:
        return row

    row = conn.execute(
        """
        SELECT * FROM products
         WHERE wake_product_id = ?
         ORDER BY main_variant DESC, id ASC
         LIMIT 1
        """,
        (num,),
    ).fetchone()
    return row


def resolve_product_by_sku_or_id(
    q: str,
    *,
    fetch_wake: bool = True,
) -> Optional[Dict]:
    """Resolve produto por SKU/ID local; fallback Wake on-demand se configurado.

    O fallback Wake consulta a API apenas quando o SKU não existe no SQLite,
    upserta a variante encontrada e retorna o cadastro local.
    """
    q = (q or "").strip()
    if not q:
        return None

    with get_conn() as conn:
        row = _find_product_row_local(conn, q)
        if row:
            return _row_to_product_dict(row)

    if not fetch_wake:
        return None

    try:
        import wake_api
    except ImportError:
        return None

    if not wake_api.wake_token_configured():
        return None

    try:
        wake_rows = wake_api.fetch_variants_by_sku(q)
    except Exception as exc:
        log.warning("Wake lookup SKU %s falhou: %s", q, exc)
        return None

    if not wake_rows:
        return None

    sync_products_from_wake(wake_rows, remap_legacy=True)

    with get_conn() as conn:
        row = _find_product_row_local(conn, q)
        return _row_to_product_dict(row) if row else None


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
        "imagem": product_images.resolve_image_url(pid, row["image"]),
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


def list_distinct_product_categories() -> List[str]:
    """Valores distintos de categoria na biblioteca de produtos (filtro admin)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT TRIM(category)
              FROM products
             WHERE TRIM(COALESCE(category, '')) != ''
             ORDER BY LOWER(TRIM(category))
            """
        ).fetchall()
    # Índice posicional: compatível com todos os sqlite3.Row / builds onde alias falha.
    return [str(row[0]) for row in rows if row[0] is not None and str(row[0]).strip()]


def _admin_products_library_filter_clause(
    q: Optional[str],
    categoria: str,
    status: str,
) -> Tuple[str, List]:
    """Filtros da biblioteca de produtos (saldos agregados em todos os eventos).

    Com texto em ``q``: nome, descrição, SKU (LIKE) e, se o trecho for só dígitos (opc. ``#``), ID do produto.
    """
    parts: List[str] = ["1=1"]
    params: List = []
    ev = "COALESCE(ev_agg.ev_stock_total, 0)"
    if q:
        qs = q.strip()
        like = f"%{qs.lower()}%"
        or_parts = [
            "LOWER(p.name) LIKE ?",
            "LOWER(COALESCE(p.description, '')) LIKE ?",
            "LOWER(COALESCE(p.sku, '')) LIKE ?",
            "EXISTS (SELECT 1 FROM product_sku_aliases a "
            "WHERE a.product_id = p.id AND LOWER(a.sku) LIKE ?)",
        ]
        or_params: List = [like, like, like, like]
        id_part = qs.lstrip("#").strip()
        if id_part.isdigit():
            or_parts.append("p.id = ?")
            or_params.append(int(id_part))
            or_parts.append("p.wake_product_id = ?")
            or_params.append(int(id_part))
            or_parts.append("INSTR(CAST(p.id AS TEXT), ?) > 0")
            or_params.append(id_part)
        parts.append("(" + " OR ".join(or_parts) + ")")
        params.extend(or_params)
    if categoria and categoria.lower() != "todos":
        parts.append("LOWER(p.category) = LOWER(?)")
        params.append(categoria)
    st = (status or "todos").strip().lower()
    if st == "ok":
        parts.append(
            f"p.active = 1 AND {ev} > 0 AND "
            f"(p.min_stock <= 0 OR {ev} >= p.min_stock)"
        )
    elif st == "baixo":
        parts.append(f"{ev} > 0 AND {ev} < p.min_stock")
    elif st == "sem_estoque":
        parts.append(f"{ev} <= 0")
    elif st == "inativo":
        parts.append("p.active = 0")
    return " AND ".join(parts), params


_EVT_PRODUCTS_JOIN = """
FROM products p
LEFT JOIN (
    SELECT product_id, COALESCE(SUM(stock), 0) AS ev_stock_total
      FROM event_products
     GROUP BY product_id
) ev_agg ON ev_agg.product_id = p.id
"""


def _admin_products_library_row_to_admin_product(row: sqlite3.Row) -> Dict:
    rd = dict(row)
    ev_total = int(rd.pop("stock_events_total") or 0)
    d = _product_row_to_client(rd)  # type: ignore[arg-type]
    d["estoque"] = ev_total
    d["abaixo_minimo"] = d["estoque_minimo"] > 0 and ev_total < d["estoque_minimo"]
    d["sem_estoque"] = ev_total <= 0
    return d


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
    """Conta produtos na biblioteca admin (filtros sobre saldo agregado nos eventos)."""
    where, params = _admin_products_library_filter_clause(q, categoria, status)
    sql = f"SELECT COUNT(*) AS c {_EVT_PRODUCTS_JOIN} WHERE {where}"
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
    """Página da biblioteca de produtos com saldo total nos eventos."""
    where, params = _admin_products_library_filter_clause(q, categoria, status)
    sql = (
        f"SELECT p.*, COALESCE(ev_agg.ev_stock_total, 0) AS stock_events_total "
        f"{_EVT_PRODUCTS_JOIN} WHERE {where} "
        "ORDER BY p.category, p.name LIMIT ? OFFSET ?"
    )
    qparams = list(params) + [int(limit), int(max(0, offset))]
    with get_conn() as conn:
        rows = conn.execute(sql, qparams).fetchall()
    return [_admin_products_library_row_to_admin_product(r) for r in rows]


def upsert_wake_variant(p: Dict) -> Optional[Dict]:
    """Persiste uma variante Wake no catálogo local e retorna seu dict.

    Regra de chave local:
    - Variante secundária (``is_variant=True``): usa ``variant_id``
      (``productVariantId`` da Wake) como ``id`` local — cria uma linha
      independente, sem sobrescrever o registro da variante principal
      (``productId``) já sincronizado.
    - Variante principal ou produto sem variante: usa ``id`` (``productId``).

    Se o ``local_id`` já existir no SQLite, os campos de catálogo são
    atualizados (nome, sku, categoria, preço, imagem) sem tocar em estoque,
    min_stock ou active — mesmo comportamento de ``sync_products_from_wake``.

    Retorna None se os IDs forem inválidos ou ocorrer erro de persistência.
    """
    now = _now_iso()
    variant_id = int(p.get("variant_id") or 0)
    product_id = int(p.get("id") or 0)
    is_variant = bool(p.get("is_variant")) and variant_id > 0

    local_id = variant_id if is_variant else product_id
    if local_id <= 0:
        return None

    raw_sku = (p.get("sku") or "").strip()
    name = (p.get("nome") or "").strip() or "Produto"
    category = str(p.get("categoria") or "Geral")
    price = float(p.get("preco") or 0)
    image = p.get("imagem") or ""
    description = f"{name} — {category}"

    try:
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT id, sku, name FROM products WHERE id = ?", (local_id,)
            ).fetchone()

            sku = raw_sku or _default_sku_for_id(local_id)
            sku = _ensure_distinct_sku(conn, local_id, sku)

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO products
                        (id, sku, name, category, description, price, image,
                         stock, min_stock, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?)
                    """,
                    (local_id, sku, name, category, description,
                     price, image, DEFAULT_MIN_STOCK, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE products
                       SET sku = ?, name = ?, category = ?, description = ?,
                           price = ?, image = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (sku, name, category, description, price, image,
                     now, local_id),
                )

            row = conn.execute(
                "SELECT * FROM products WHERE id = ?", (local_id,)
            ).fetchone()
    except Exception:
        return None

    return _product_row_to_client(row) if row else None


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


def get_product_in_event(event_id: int, product_id: int) -> Optional[Dict]:
    """Catálogo + saldos do produto dentro do evento (formato compatível com ``get_product``)."""
    base = get_product(product_id)
    if base is None:
        return None
    with get_conn() as conn:
        ep = conn.execute(
            "SELECT stock, min_stock, backorder_limit FROM event_products "
            "WHERE event_id = ? AND product_id = ?",
            (int(event_id), int(product_id)),
        ).fetchone()
    if ep is None:
        return None
    est = int(ep["stock"] or 0)
    mn = int(ep["min_stock"] or 0)
    out = dict(base)
    out["estoque"] = est
    out["estoque_minimo"] = mn
    out["backorder_limit"] = int(ep["backorder_limit"] or 0)
    out["abaixo_minimo"] = mn > 0 and est < mn
    out["sem_estoque"] = est <= 0
    return out


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

