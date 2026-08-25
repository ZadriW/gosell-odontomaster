"""Product catalog, Wake sync and admin library listings."""
from __future__ import annotations

import logging
import sqlite3
import unicodedata
from collections import defaultdict
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


def _fold_product_name(name: str) -> str:
    """Nome comparável (minúsculas, sem acento) para detectar produto-base vs variante."""
    text = unicodedata.normalize("NFD", (name or "").strip().lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.split())


_LIKE_ACCENT_PAIRS = (
    ("á", "a"), ("à", "a"), ("â", "a"), ("ã", "a"), ("ä", "a"),
    ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
    ("í", "i"), ("ì", "i"), ("î", "i"), ("ï", "i"),
    ("ó", "o"), ("ò", "o"), ("ô", "o"), ("õ", "o"), ("ö", "o"),
    ("ú", "u"), ("ù", "u"), ("û", "u"), ("ü", "u"),
    ("ç", "c"),
)


def _sql_fold_text(expr: str) -> str:
    """LOWER + troca de acentos comuns em SQL (SQLite sem ICU)."""
    folded = f"LOWER({expr})"
    for src, dst in _LIKE_ACCENT_PAIRS:
        folded = f"REPLACE({folded}, '{src}', '{dst}')"
    return folded


def _like_contains(term: str) -> str:
    escaped = (
        (term or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _product_search_tokens(q: Optional[str]) -> List[str]:
    """Palavras da busca, sem acento; ``#123`` vira o token ``123``."""
    qs = (q or "").strip()
    if qs.startswith("#"):
        qs = qs[1:].strip()
    return [tok for tok in _fold_product_name(qs).split(" ") if tok]


def _product_catalog_like_clause(
    q: Optional[str],
    *,
    alias: str = "p",
    include_sku_aliases: bool = False,
    include_wake_id: bool = False,
) -> Tuple[str, List]:
    """Busca por partes do nome: todas as palavras precisam aparecer.

    Cada token casa em nome, variante ou SKU (não na descrição). Assim
    ``Cimento Angelus`` encontra ``Cimento Endodôntico Bio-C Temp - Angelus``,
    e ``Lima Reciproc`` não lista ``Lima Kendo``. Trecho só numérico
    (opcional ``#``) também casa o ID.
    Retorna ``(clausula_entre_parenteses, params)`` ou ``("", [])``.
    """
    tokens = _product_search_tokens(q)
    if not tokens:
        return "", []

    prefix = f"{alias}." if alias else ""
    id_col = f"{prefix}id"
    name_f = _sql_fold_text(f"COALESCE({prefix}name, '')")
    variant_f = _sql_fold_text(f"COALESCE({prefix}variant_name, '')")
    sku_f = _sql_fold_text(f"COALESCE({prefix}sku, '')")

    token_ands: List[str] = []
    token_params: List = []
    for tok in tokens:
        like = _like_contains(tok)
        or_parts = [
            f"{name_f} LIKE ? ESCAPE '\\'",
            f"{variant_f} LIKE ? ESCAPE '\\'",
            f"{sku_f} LIKE ? ESCAPE '\\'",
        ]
        or_params: List = [like, like, like]
        if include_sku_aliases:
            alias_f = _sql_fold_text("sa.sku")
            or_parts.append(
                "EXISTS (SELECT 1 FROM product_sku_aliases sa "
                f"WHERE sa.product_id = {id_col} AND {alias_f} LIKE ? ESCAPE '\\')"
            )
            or_params.append(like)
        token_ands.append("(" + " OR ".join(or_parts) + ")")
        token_params.extend(or_params)

    clause = "(" + " AND ".join(token_ands) + ")"
    if len(tokens) == 1 and tokens[0].isdigit():
        id_part = tokens[0]
        id_ors = [
            f"{id_col} = ?",
            f"INSTR(CAST({id_col} AS TEXT), ?) > 0",
        ]
        id_params: List = [int(id_part), id_part]
        if include_wake_id:
            id_ors.append(f"{prefix}wake_product_id = ?")
            id_params.append(int(id_part))
        clause = f"({clause} OR ({' OR '.join(id_ors)}))"
        token_params.extend(id_params)
    return clause, token_params


def _retire_variant_parent_ids(conn: sqlite3.Connection, parent_ids: Iterable[int]) -> int:
    """Não retira mais o produto-base. Mantido só por compatibilidade (no-op)."""
    return 0


def _detect_variant_parent_ids(conn: sqlite3.Connection) -> List[int]:
    """IDs de SKU-base quando já existem variantes do mesmo produto (ativos ou não).

    1. ``id = wake_product_id`` com irmãos (importação Wake).
    2. Nome do cadastro é prefixo do nome de outro item, e o ``id`` do base
       é menor que o das variantes.
    """
    found: set[int] = set()
    rows = conn.execute(
        """
        SELECT p.id
          FROM products p
         WHERE p.wake_product_id IS NOT NULL
           AND p.wake_product_id > 0
           AND p.id = p.wake_product_id
           AND EXISTS (
                SELECT 1 FROM products v
                 WHERE v.wake_product_id = p.wake_product_id
                   AND v.id != p.id
           )
        """
    ).fetchall()
    for r in rows:
        found.add(int(r["id"]))

    catalog = conn.execute("SELECT id, name FROM products").fetchall()
    folded = [(int(r["id"]), _fold_product_name(r["name"])) for r in catalog]
    for pid, pname in folded:
        if pid in found or len(pname) < 12:
            continue
        child_ids = [
            cid
            for cid, cname in folded
            if cid != pid and cname.startswith(pname + " ") and len(cname) >= len(pname) + 8
        ]
        if child_ids and pid < min(child_ids):
            found.add(pid)
    return sorted(found)


def detect_unsellable_variant_parent_ids(conn: sqlite3.Connection) -> List[int]:
    """Compatibilidade: a listagem de SKU-base não implica mais bloqueio de venda."""
    return _detect_variant_parent_ids(conn)


def restore_retired_variant_parents_in_conn(conn: sqlite3.Connection) -> Dict[str, int]:
    """Reativa produtos-base desativados e religa-os aos eventos das variantes."""
    ids = _detect_variant_parent_ids(conn)
    if not ids:
        return {"reactivated": 0, "relinked": 0}

    now = _now_iso()
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE products SET active = 1, updated_at = ? "
        f"WHERE id IN ({placeholders}) AND active = 0",
        (now, *ids),
    )
    reactivated = int(cur.rowcount or 0)

    stock_rows = conn.execute(
        f"""
        SELECT event_id, product_id, COALESCE(SUM(delta), 0) AS stock
          FROM stock_movements
         WHERE event_id IS NOT NULL AND product_id IN ({placeholders})
         GROUP BY event_id, product_id
        """,
        ids,
    ).fetchall()
    tx_rows = conn.execute(
        f"""
        SELECT DISTINCT t.event_id AS event_id, ti.product_id AS product_id
          FROM transaction_items ti
          JOIN transactions t ON t.id = ti.transaction_id
         WHERE t.event_id IS NOT NULL AND ti.product_id IN ({placeholders})
        """,
        ids,
    ).fetchall()

    pairs: Dict[Tuple[int, int], int] = {}
    for r in stock_rows:
        pairs[(int(r["event_id"]), int(r["product_id"]))] = max(0, int(r["stock"] or 0))
    for r in tx_rows:
        pairs.setdefault((int(r["event_id"]), int(r["product_id"])), 0)

    catalog = conn.execute("SELECT id, name, wake_product_id FROM products").fetchall()
    folded_by_id = {int(r["id"]): _fold_product_name(r["name"]) for r in catalog}
    wake_by_id = {
        int(r["id"]): int(r["wake_product_id"] or 0) for r in catalog
    }
    for parent_id in ids:
        prefix = folded_by_id.get(parent_id) or ""
        wake_id = wake_by_id.get(parent_id) or 0
        child_ids = [
            int(r["id"])
            for r in catalog
            if int(r["id"]) != parent_id
            and (
                (wake_id > 0 and int(r["wake_product_id"] or 0) == wake_id)
                or (
                    prefix
                    and len(prefix) >= 12
                    and folded_by_id.get(int(r["id"]), "").startswith(prefix + " ")
                    and len(folded_by_id.get(int(r["id"]), "")) >= len(prefix) + 8
                )
            )
        ]
        if not child_ids:
            continue
        child_ph = ",".join("?" * len(child_ids))
        ev_rows = conn.execute(
            f"SELECT DISTINCT event_id FROM event_products WHERE product_id IN ({child_ph})",
            child_ids,
        ).fetchall()
        for ev in ev_rows:
            pairs.setdefault((int(ev["event_id"]), parent_id), 0)

    relinked = 0
    for (event_id, product_id), stock in pairs.items():
        ev_ok = conn.execute(
            "SELECT 1 FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if ev_ok is None:
            continue
        existing = conn.execute(
            "SELECT 1 FROM event_products WHERE event_id = ? AND product_id = ?",
            (event_id, product_id),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO event_products
                (event_id, product_id, stock, min_stock, backorder_limit, created_at, updated_at)
            VALUES (?, ?, ?, ?, -1, ?, ?)
            """,
            (event_id, product_id, stock, DEFAULT_MIN_STOCK, now, now),
        )
        relinked += 1

    if reactivated or relinked:
        log.info(
            "Produtos-base restaurados: reactivated=%s relinked=%s ids=%s",
            reactivated,
            relinked,
            ids[:20],
        )
    return {"reactivated": reactivated, "relinked": relinked}


def retire_unsellable_variant_parents() -> int:
    """Não desativa mais SKUs-base. Mantido por compatibilidade."""
    return 0


def is_unsellable_variant_parent(product_id: int) -> bool:
    """Não bloqueia mais o SKU-base; sempre False."""
    return False


def variant_children_preview(parent_id: int, limit: int = 5) -> List[Dict]:
    """SKUs/nomes das variantes locais de um produto-base (para mensagem ao admin)."""
    pid = int(parent_id)
    with get_conn() as conn:
        parent = conn.execute(
            "SELECT id, name FROM products WHERE id = ?", (pid,)
        ).fetchone()
        if parent is None:
            return []
        prefix = _fold_product_name(parent["name"])
        if not prefix:
            return []
        rows = conn.execute(
            "SELECT id, sku, name FROM products WHERE id != ? AND active = 1 ORDER BY sku",
            (pid,),
        ).fetchall()
    out: List[Dict] = []
    for r in rows:
        folded = _fold_product_name(r["name"])
        if folded.startswith(prefix + " ") and len(folded) >= len(prefix) + 8:
            out.append({"id": int(r["id"]), "sku": r["sku"] or "", "nome": r["name"] or ""})
            if len(out) >= int(limit):
                break
    return out


def _maybe_migrate_legacy_wake_product_id(
    conn: sqlite3.Connection,
    wake_product_id: int,
    variant_id: int,
) -> bool:
    """Antes desativava o SKU-base ao importar variantes; os dois passam a conviver."""
    return False


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
            variant_id = int(p.get("variant_id") or p.get("id") or 0)
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
            ):
                remapped += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "remapped": remapped,
    }


def sync_catalog_from_wake(wake_variants: List[Dict]) -> Dict[str, int]:
    """Atualiza catálogo local a partir de variantes Wake SEM tocar em estoque/evento.

    Campos atualizados: name, sku, category, description, price, image,
    wake_product_id, variant_name, main_variant, subtitle.

    Campos PRESERVADOS: stock, min_stock, active, created_at.
    Tabelas intocadas: event_products, stock_movements, transactions.

    Produtos novos (variant_id inexistente) são inseridos com stock=0, active=1.
    """
    updated = inserted = skipped = 0
    now = _now_iso()

    with get_conn() as conn:
        for p in wake_variants:
            variant_id = int(p.get("variant_id") or p.get("id") or 0)
            if variant_id <= 0:
                skipped += 1
                continue

            wake_product_id = int(p.get("wake_product_id") or variant_id)
            raw_sku = (p.get("sku") or "").strip()
            nome = str(p.get("nome") or "").strip() or "Produto"
            category = str(p.get("categoria") or "Geral")
            price = float(p.get("preco") or 0)
            image = p.get("imagem") or ""
            variant_name = str(p.get("variant_name") or "").strip()
            subtitle = str(p.get("subtitle") or "").strip()
            main_variant = 1 if p.get("main_variant") else 0
            description = f"{nome} — {category}"

            existing = conn.execute(
                "SELECT id, sku FROM products WHERE id = ?", (variant_id,)
            ).fetchone()

            if existing is None:
                if raw_sku:
                    by_sku = conn.execute(
                        "SELECT id, sku FROM products WHERE sku = ? ORDER BY active DESC, id ASC LIMIT 1",
                        (raw_sku,),
                    ).fetchone()
                    if by_sku:
                        existing = by_sku
                        variant_id = int(by_sku["id"])

            if existing is None:
                sku = raw_sku or _default_sku_for_id(variant_id)
                sku = _ensure_distinct_sku(conn, variant_id, sku)
                conn.execute(
                    """
                    INSERT INTO products
                        (id, sku, name, category, description, price, image,
                         stock, min_stock, active, wake_product_id, variant_name,
                         main_variant, subtitle, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        variant_id, sku, nome, category, description, price, image,
                        DEFAULT_MIN_STOCK, wake_product_id, variant_name or None,
                        main_variant, subtitle or None, now, now,
                    ),
                )
                inserted += 1
            else:
                local_id = int(existing["id"])
                ex_sku = (existing["sku"] or "").strip()
                sku = raw_sku if raw_sku else (
                    ex_sku if ex_sku and not _is_generated_fallback_sku(ex_sku, local_id)
                    else _default_sku_for_id(local_id)
                )
                sku = _ensure_distinct_sku(conn, local_id, sku)
                conn.execute(
                    """
                    UPDATE products
                       SET sku = ?, name = ?, category = ?, description = ?,
                           price = ?, image = ?, wake_product_id = ?,
                           variant_name = ?, main_variant = ?, subtitle = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        sku, nome, category, description, price, image,
                        wake_product_id, variant_name or None, main_variant,
                        subtitle or None, now, local_id,
                    ),
                )
                updated += 1

    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def get_distinct_wake_product_ids() -> List[int]:
    """Retorna os wake_product_id distintos (> 0) gravados na biblioteca."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT wake_product_id
              FROM products
             WHERE wake_product_id IS NOT NULL AND wake_product_id > 0
            """
        ).fetchall()
    return [int(r[0]) for r in rows]


def get_local_ids_without_wake_mapping() -> List[int]:
    """IDs locais ativos que não possuem wake_product_id mapeado.

    Esses IDs provavelmente correspondem a productVariantId da Wake,
    inseridos diretamente sem rastreamento de família.
    Usados para enriquecer o sync via busca direta por productVariantId.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id FROM products
             WHERE active = 1
               AND (wake_product_id IS NULL OR wake_product_id = 0)
             ORDER BY id
            """
        ).fetchall()
    return [int(r[0]) for r in rows]


def _find_product_row_local(conn: sqlite3.Connection, q: str) -> Optional[sqlite3.Row]:
    """Busca produto no SQLite (variante, alias ERP ou ``wake_product_id`` legado)."""
    q = (q or "").strip()
    if not q:
        return None

    row = conn.execute(
        "SELECT * FROM products WHERE sku = ? AND active = 1", (q,)
    ).fetchone()
    if row:
        return row

    row = conn.execute(
        """
        SELECT p.* FROM product_sku_aliases a
          JOIN products p ON p.id = a.product_id
         WHERE a.sku = ? AND p.active = 1
        """,
        (q,),
    ).fetchone()
    if row:
        return row

    try:
        num = int(q.lstrip("#").strip())
    except ValueError:
        return None

    row = conn.execute(
        "SELECT * FROM products WHERE id = ? AND active = 1", (num,)
    ).fetchone()
    if row:
        return row

    row = conn.execute(
        """
        SELECT * FROM products
         WHERE wake_product_id = ? AND active = 1 AND id != wake_product_id
         ORDER BY id ASC
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
    try:
        vn = (row["variant_name"] or "").strip()
    except (KeyError, IndexError):
        vn = ""
    try:
        wake_pid = int(row["wake_product_id"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        wake_pid = 0
    try:
        main_variant = bool(int(row["main_variant"] or 0))
    except (KeyError, IndexError, TypeError, ValueError):
        main_variant = False
    try:
        subtitle = (row["subtitle"] or "").strip()
    except (KeyError, IndexError):
        subtitle = ""
    return {
        "id": pid,
        "sku": sku,
        "nome": row["name"],
        "variante": vn,
        "subtitle": subtitle,
        "categoria": row["category"],
        "descricao": row["description"] or "",
        "preco": float(row["price"] or 0),
        "imagem": product_images.resolve_image_url(pid, row["image"]),
        "estoque": int(row["stock"] or 0),
        "estoque_minimo": int(row["min_stock"] or 0),
        "ativo": bool(row["active"]),
        "wake_product_id": wake_pid,
        "main_variant": main_variant,
        "tem_opcoes": False,
        "catalog_oculto": False,
        "opcoes": [],
    }


def _is_name_variant_child(parent_name: str, child_name: str) -> bool:
    pname = _fold_product_name(parent_name)
    cname = _fold_product_name(child_name)
    return bool(
        pname
        and len(pname) >= 12
        and cname.startswith(pname + " ")
        and len(cname) >= len(pname) + 8
    )


def _catalog_children_of_parent(parent: Dict, products: List[Dict], parent_ids: set) -> List[Dict]:
    pid = int(parent["id"])
    wake = int(parent.get("wake_product_id") or 0)
    children: List[Dict] = []
    for cand in products:
        cid = int(cand["id"])
        if cid == pid or cid in parent_ids:
            continue
        cwake = int(cand.get("wake_product_id") or 0)
        same_wake = wake > 0 and cwake == wake
        name_child = _is_name_variant_child(parent.get("nome") or "", cand.get("nome") or "")
        if same_wake or name_child:
            children.append(cand)
    children.sort(key=lambda c: ((c.get("variante") or c.get("nome") or ""), int(c["id"])))
    return children


def _variant_suffix_from_name(parent_name: str, child_name: str) -> str:
    """Extrai a parte diferenciadora do nome da filha em relação ao pai."""
    p = (parent_name or "").strip()
    c = (child_name or "").strip()
    if not p or not c or len(c) <= len(p):
        return ""
    if c.lower().startswith(p.lower()):
        rest = c[len(p):].strip(" -\u2013\u2014/")
        return rest if len(rest) >= 3 else ""
    return ""


def _mark_catalog_family(head: Dict, members: List[Dict], *, include_head: bool) -> None:
    option_ids: List[int] = []
    head_name = head.get("nome") or ""
    if include_head:
        option_ids.append(int(head["id"]))
        if not (head.get("variante") or "").strip():
            head["variante"] = head_name
    for child in members:
        cid = int(child["id"])
        if cid == int(head["id"]):
            continue
        child["catalog_oculto"] = True
        child["opcao_de"] = int(head["id"])
        if not (child.get("variante") or "").strip():
            suffix = _variant_suffix_from_name(head_name, child.get("nome") or "")
            if suffix:
                child["variante"] = suffix
        option_ids.append(cid)
    if not option_ids:
        return
    head["tem_opcoes"] = True
    head["catalog_oculto"] = False
    head["opcoes"] = option_ids
    search_bits = [head.get("nome") or "", head.get("sku") or "", head.get("variante") or ""]
    by_id = {int(m["id"]): m for m in members}
    by_id[int(head["id"])] = head
    for oid in option_ids:
        opt = by_id.get(oid)
        if not opt:
            continue
        search_bits.extend([opt.get("nome") or "", opt.get("sku") or "", opt.get("variante") or ""])
    head["busca_opcoes"] = " ".join(search_bits)


def summarize_catalog_option_groups(products: List[Dict]) -> None:
    """Atualiza preço/estoque resumidos do card-pai a partir das variantes."""
    by_id = {int(p["id"]): p for p in products}
    for p in products:
        ids = [int(i) for i in (p.get("opcoes") or [])]
        if not ids:
            continue
        children = [by_id[i] for i in ids if i in by_id]
        if not children:
            continue
        prices = [float(c.get("preco") or 0) for c in children]
        stocks = [int(c.get("estoque") or 0) for c in children]
        p["opcoes_count"] = len(children)
        p["estoque_opcoes"] = sum(stocks)
        p["preco_a_partir"] = min(prices) if prices else float(p.get("preco") or 0)
        p["precos_opcoes_variam"] = (max(prices) - min(prices) > 0.001) if prices else False
        pending = sum(int(c.get("pending_delivery_units") or 0) for c in children)
        p["pending_delivery_units"] = max(int(p.get("pending_delivery_units") or 0), pending)
        if any(c.get("em_promocao") for c in children) and not p.get("em_promocao"):
            p["em_promocao"] = True
            for c in children:
                if c.get("promo_badge"):
                    p["promo_badge"] = c.get("promo_badge") or ""
                    p["promo_nome"] = c.get("promo_nome") or ""
                    p["promo_tipo"] = c.get("promo_tipo") or ""
                    break


def _detect_variant_parent_ids_from_products(products: List[Dict]) -> set:
    """Mesma regra de ``_detect_variant_parent_ids``, só sobre a lista já carregada.

    Não abre conexão extra — o catálogo na LAN não pode varrer a tabela
    ``products`` a cada request/polling.
    """
    found: set = set()
    by_wake: Dict[int, List[Dict]] = defaultdict(list)
    for p in products:
        wake = int(p.get("wake_product_id") or 0)
        if wake > 0:
            by_wake[wake].append(p)
    for wake, group in by_wake.items():
        if len(group) < 2:
            continue
        for p in group:
            if int(p["id"]) == wake:
                found.add(int(p["id"]))

    folded = [(int(p["id"]), _fold_product_name(p.get("nome") or "")) for p in products]
    for pid, pname in folded:
        if pid in found or len(pname) < 12:
            continue
        child_ids = [
            cid
            for cid, cname in folded
            if cid != pid and cname.startswith(pname + " ") and len(cname) >= len(pname) + 8
        ]
        if child_ids and pid < min(child_ids):
            found.add(pid)
    return found


def prepare_catalog_variant_groups(products: List[Dict]) -> List[Dict]:
    """Marca famílias pai/variante: um card no catálogo, variantes só no modal.

    Mutates ``products`` in place and returns the same list.
    """
    if not products:
        return products

    for p in products:
        p["tem_opcoes"] = False
        p["catalog_oculto"] = False
        p["opcoes"] = []
        p["busca_opcoes"] = ""
        p.pop("opcao_de", None)

    parent_ids = _detect_variant_parent_ids_from_products(products)
    by_id = {int(p["id"]): p for p in products}

    for parent_id in parent_ids:
        parent = by_id.get(int(parent_id))
        if parent is None:
            continue
        children = _catalog_children_of_parent(parent, products, parent_ids)
        if not children:
            continue
        _mark_catalog_family(parent, children, include_head=True)

    by_wake: Dict[int, List[Dict]] = defaultdict(list)
    for p in products:
        if p.get("catalog_oculto") or p.get("tem_opcoes"):
            continue
        wake = int(p.get("wake_product_id") or 0)
        if wake > 0:
            by_wake[wake].append(p)
    for _wake, group in by_wake.items():
        visible = [p for p in group if not p.get("catalog_oculto")]
        if len(visible) < 2:
            continue
        if any(p.get("tem_opcoes") for p in visible):
            continue
        head = min(visible, key=lambda p: (len(p.get("nome") or ""), int(p["id"])))
        _mark_catalog_family(head, visible, include_head=True)

    summarize_catalog_option_groups(products)
    return products


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
    search_sql, search_params = _product_catalog_like_clause(query, alias="")
    if search_sql:
        sql += f" AND {search_sql}"
        params.extend(search_params)
    sql += " ORDER BY category, name"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return prepare_catalog_variant_groups([_product_row_to_client(r) for r in rows])


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

    Com texto em ``q``: todas as palavras precisam aparecer em nome, variante
    ou SKU; se o trecho for só dígitos (opc. ``#``), também o ID.
    """
    parts: List[str] = ["1=1"]
    params: List = []
    ev = "COALESCE(ev_agg.ev_stock_total, 0)"
    search_sql, search_params = _product_catalog_like_clause(
        q, include_sku_aliases=True, include_wake_id=True
    )
    if search_sql:
        parts.append(search_sql)
        params.extend(search_params)
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
        parts.append(f"p.active = 1 AND {ev} > 0 AND {ev} < p.min_stock")
    elif st == "sem_estoque":
        parts.append(f"p.active = 1 AND {ev} <= 0")
    elif st == "inativo":
        parts.append("p.active = 0")
    else:
        parts.append("p.active = 1")
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

    A chave local é ``productVariantId`` quando existir; o SKU-base
    (``productId``) também pode ser gravado e vendido.
    """
    now = _now_iso()
    variant_id = int(p.get("variant_id") or 0)
    product_id = int(p.get("wake_product_id") or p.get("id") or 0)

    local_id = variant_id if variant_id > 0 else product_id
    if local_id <= 0:
        return None

    raw_sku = (p.get("sku") or "").strip()
    name = (p.get("nome") or "").strip() or "Produto"
    category = str(p.get("categoria") or "Geral")
    price = float(p.get("preco") or 0)
    image = p.get("imagem") or ""
    description = f"{name} — {category}"
    variant_name = (p.get("variant_name") or "").strip() or None
    subtitle = (p.get("subtitle") or "").strip() or None
    wake_product_id = int(p.get("wake_product_id") or p.get("id") or local_id)
    main_variant = 1 if p.get("main_variant") else 0

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
                         stock, min_stock, active, wake_product_id, variant_name,
                         main_variant, subtitle, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (local_id, sku, name, category, description,
                     price, image, DEFAULT_MIN_STOCK,
                     wake_product_id, variant_name, main_variant,
                     subtitle, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE products
                       SET sku = ?, name = ?, category = ?, description = ?,
                           price = ?, image = ?, wake_product_id = ?,
                           variant_name = ?, main_variant = ?, subtitle = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (sku, name, category, description, price, image,
                     wake_product_id, variant_name, main_variant,
                     subtitle, now, local_id),
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
            "SELECT stock, min_stock, backorder_limit, price FROM event_products "
            "WHERE event_id = ? AND product_id = ?",
            (int(event_id), int(product_id)),
        ).fetchone()
    if ep is None:
        return None
    est = int(ep["stock"] or 0)
    mn = int(ep["min_stock"] or 0)
    library_price = float(base.get("preco") or 0)
    event_price = ep["price"]
    out = dict(base)
    out["estoque"] = est
    out["estoque_minimo"] = mn
    out["backorder_limit"] = int(
        ep["backorder_limit"] if ep["backorder_limit"] is not None else -1
    )
    out["preco_biblioteca"] = library_price
    out["preco"] = float(event_price) if event_price is not None else library_price
    out["preco_evento_override"] = event_price is not None
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


def update_product_price(product_id: int, price: float) -> bool:
    p = round(float(price), 2)
    if p < 0:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE products SET price = ?, updated_at = ? WHERE id = ?",
            (p, _now_iso(), int(product_id)),
        )
        return cur.rowcount > 0


def set_product_active(product_id: int, active: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE products SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, _now_iso(), int(product_id)),
        )
        return cur.rowcount > 0

