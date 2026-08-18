"""Promoções por evento (descontos e regras de precificação em produtos selecionados)."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional

from .connection import _now_iso, get_conn

_VALID_RULE_TYPES = {"percent", "fixed", "bogo", "min_bundle", "exact_bundle"}

RULE_TYPE_LABELS = {
    "percent": "Desconto (%)",
    "fixed": "Desconto fixo (R$)",
    "bogo": "Compre X, Leve Y",
    "min_bundle": "A partir de (pacote mínimo)",
    "exact_bundle": "Na compra de (pacote exato)",
}


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _pack_groups_and_extra(qty: int, pack_qty: int) -> tuple[int, int]:
    """Quantos pacotes completos e quantas unidades avulsas.

    Ex.: pack_qty=5, qty=6 → (1 pacote, 1 avulsa). qty=10 → (2, 0).
    """
    pack = max(2, int(pack_qty))
    q = max(0, int(qty))
    return q // pack, q % pack


def _pack_subtotal(qty: int, pack_qty: int, pack_total: float, list_price: float) -> float:
    """Pacotes completos pelo valor da promoção; o resto pelo preço de lista.

    Ex.: 5 un. a R$ 15 (lista R$ 75) com kit de 5 por R$ 50 → R$ 50.
    6 un. → R$ 50 + 1×R$ 15 = R$ 65. 10 un. → 2 kits = R$ 100.
    """
    groups, extra = _pack_groups_and_extra(qty, pack_qty)
    return round(groups * max(0.0, float(pack_total)) + extra * float(list_price), 2)


def _compute_effective_subtotal(
    rule_type: str,
    rule_value: float,
    min_qty: int,
    free_qty: int,
    list_price: float,
    qty: int,
) -> float:
    """Calcula o subtotal efetivo (o valor que o cliente paga) para um item com uma promo."""
    if rule_type == "percent":
        pct = max(0.0, min(100.0, float(rule_value)))
        return round(list_price * qty * (1.0 - pct / 100.0), 2)
    if rule_type == "fixed":
        discount = max(0.0, float(rule_value))
        return round(max(0.0, list_price - discount) * qty, 2)
    if rule_type == "bogo":
        min_q = max(1, int(min_qty))
        free_q = max(0, int(free_qty))
        if free_q == 0:
            return round(list_price * qty, 2)
        group = min_q + free_q
        groups = qty // group
        rem = qty % group
        paid = groups * min_q + min(rem, min_q)
        return round(list_price * paid, 2)
    if rule_type == "min_bundle":
        # "A partir de min_qty unidades": exige atingir o mínimo; cada grupo completo
        # de min_qty paga rule_value; unidades excedentes pagam preço de lista.
        min_q = max(2, int(min_qty))
        if qty < min_q:
            return round(list_price * qty, 2)
        eff = _pack_subtotal(qty, min_q, rule_value, list_price)
        if eff >= round(list_price * qty, 2):  # conjunto mais caro → sem desconto
            return round(list_price * qty, 2)
        return eff
    if rule_type == "exact_bundle":
        # "Na compra de min_qty": só pacotes completos usam o valor da promoção.
        # Unidades além do múltiplo (ex.: 6ª de um kit de 5) pagam preço de lista.
        min_q = max(2, int(min_qty))
        groups, _extra = _pack_groups_and_extra(qty, min_q)
        if groups <= 0:
            return round(list_price * qty, 2)
        eff = _pack_subtotal(qty, min_q, rule_value, list_price)
        if eff >= round(list_price * qty, 2):  # kit mais caro → sem desconto
            return round(list_price * qty, 2)
        return eff
    return round(list_price * qty, 2)


def _rows_to_promo(rows, product_rows) -> Dict:
    """Converte row de promoção + linhas de produtos em dict rico."""
    promo = dict(rows)
    promo["rule_label"] = RULE_TYPE_LABELS.get(promo.get("rule_type", ""), "")
    promo["product_ids"] = [int(r["product_id"]) for r in product_rows]
    promo["products"] = [dict(r) for r in product_rows]
    return promo


# ---------------------------------------------------------------------------
# CRUD de promoções
# ---------------------------------------------------------------------------

def create_promotion(
    event_id: int,
    name: str,
    rule_type: str,
    *,
    rule_value: float = 0.0,
    min_qty: int = 1,
    free_qty: int = 0,
    product_ids: List[int],
) -> Dict:
    """Cria uma promoção para o evento e associa os produtos. Retorna o dict criado."""
    name_s = (name or "").strip()
    if not name_s:
        raise ValueError("Informe um nome para a promoção.")
    if rule_type not in _VALID_RULE_TYPES:
        raise ValueError(f"Tipo de regra inválido: {rule_type}")
    if rule_type == "percent":
        if not (0 < rule_value <= 100):
            raise ValueError("O percentual de desconto deve ser entre 1 e 100.")
    elif rule_type == "fixed":
        if rule_value <= 0:
            raise ValueError("O desconto fixo deve ser maior que zero.")
    elif rule_type == "bogo":
        if min_qty < 1:
            raise ValueError("Quantidade mínima deve ser pelo menos 1.")
        if free_qty < 1:
            raise ValueError("Quantidade grátis deve ser pelo menos 1.")
    elif rule_type in ("min_bundle", "exact_bundle"):
        if min_qty < 2:
            raise ValueError("A quantidade do pacote deve ser pelo menos 2.")
        if rule_value <= 0:
            raise ValueError("O valor do pacote deve ser maior que zero.")
    if not product_ids:
        raise ValueError("Selecione ao menos um produto para a promoção.")

    now = _now_iso()
    with get_conn() as conn:
        ev = conn.execute("SELECT 1 FROM events WHERE id = ?", (int(event_id),)).fetchone()
        if ev is None:
            raise ValueError("Evento não encontrado.")
        cur = conn.execute(
            """
            INSERT INTO promotions
                (event_id, name, rule_type, rule_value, min_qty, free_qty, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (int(event_id), name_s, rule_type, float(rule_value),
             int(min_qty), int(free_qty), now, now),
        )
        promo_id = int(cur.lastrowid)
        _sync_promo_products(conn, promo_id, product_ids)
        row = conn.execute("SELECT * FROM promotions WHERE id = ?", (promo_id,)).fetchone()
        p_rows = conn.execute(
            "SELECT pp.product_id, p.name FROM promotion_products pp "
            "JOIN products p ON p.id = pp.product_id WHERE pp.promotion_id = ?",
            (promo_id,),
        ).fetchall()
    return _rows_to_promo(row, p_rows)


def update_promotion(
    promo_id: int,
    name: str,
    rule_type: str,
    *,
    rule_value: float,
    min_qty: int,
    free_qty: int,
    active: bool,
    product_ids: List[int],
) -> Dict:
    """Atualiza nome, regra, produtos e status de uma promoção existente."""
    name_s = (name or "").strip()
    if not name_s:
        raise ValueError("Informe um nome para a promoção.")
    if rule_type not in _VALID_RULE_TYPES:
        raise ValueError(f"Tipo de regra inválido: {rule_type}")
    if rule_type == "percent":
        if not (0 < rule_value <= 100):
            raise ValueError("O percentual de desconto deve ser entre 1 e 100.")
    elif rule_type == "fixed":
        if rule_value <= 0:
            raise ValueError("O desconto fixo deve ser maior que zero.")
    elif rule_type == "bogo":
        if min_qty < 1:
            raise ValueError("Quantidade mínima deve ser pelo menos 1.")
        if free_qty < 1:
            raise ValueError("Quantidade grátis deve ser pelo menos 1.")
    elif rule_type in ("min_bundle", "exact_bundle"):
        if min_qty < 2:
            raise ValueError("A quantidade do pacote deve ser pelo menos 2.")
        if rule_value <= 0:
            raise ValueError("O valor do pacote deve ser maior que zero.")
    if not product_ids:
        raise ValueError("Selecione ao menos um produto para a promoção.")

    now = _now_iso()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM promotions WHERE id = ?", (int(promo_id),)).fetchone()
        if row is None:
            raise ValueError("Promoção não encontrada.")
        conn.execute(
            """
            UPDATE promotions
               SET name = ?, rule_type = ?, rule_value = ?, min_qty = ?,
                   free_qty = ?, active = ?, updated_at = ?
             WHERE id = ?
            """,
            (name_s, rule_type, float(rule_value), int(min_qty),
             int(free_qty), 1 if active else 0, now, int(promo_id)),
        )
        _sync_promo_products(conn, int(promo_id), product_ids)
        row = conn.execute("SELECT * FROM promotions WHERE id = ?", (int(promo_id),)).fetchone()
        p_rows = conn.execute(
            "SELECT pp.product_id, p.name FROM promotion_products pp "
            "JOIN products p ON p.id = pp.product_id WHERE pp.promotion_id = ?",
            (int(promo_id),),
        ).fetchall()
    return _rows_to_promo(row, p_rows)


def toggle_promotion_active(promo_id: int) -> Dict:
    """Inverte o campo ``active`` de uma promoção. Retorna o dict atualizado."""
    now = _now_iso()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM promotions WHERE id = ?", (int(promo_id),)).fetchone()
        if row is None:
            raise ValueError("Promoção não encontrada.")
        new_active = 0 if int(row["active"]) else 1
        conn.execute(
            "UPDATE promotions SET active = ?, updated_at = ? WHERE id = ?",
            (new_active, now, int(promo_id)),
        )
        row = conn.execute("SELECT * FROM promotions WHERE id = ?", (int(promo_id),)).fetchone()
        p_rows = conn.execute(
            "SELECT pp.product_id, p.name FROM promotion_products pp "
            "JOIN products p ON p.id = pp.product_id WHERE pp.promotion_id = ?",
            (int(promo_id),),
        ).fetchall()
    return _rows_to_promo(row, p_rows)


def delete_promotion(promo_id: int) -> None:
    """Remove uma promoção e seus vínculos de produto (CASCADE cuida do FK)."""
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM promotions WHERE id = ?", (int(promo_id),)).fetchone()
        if row is None:
            raise ValueError("Promoção não encontrada.")
        conn.execute("DELETE FROM promotions WHERE id = ?", (int(promo_id),))


def get_promotion(promo_id: int) -> Optional[Dict]:
    """Retorna uma promoção com a lista de produtos, ou None."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM promotions WHERE id = ?", (int(promo_id),)).fetchone()
        if row is None:
            return None
        p_rows = conn.execute(
            "SELECT pp.product_id, p.name FROM promotion_products pp "
            "JOIN products p ON p.id = pp.product_id WHERE pp.promotion_id = ?",
            (int(promo_id),),
        ).fetchall()
    return _rows_to_promo(row, p_rows)


def list_promotions_for_event(event_id: int) -> List[Dict]:
    """Lista todas as promoções do evento com produtos associados."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM promotions WHERE event_id = ? ORDER BY active DESC, created_at DESC",
            (int(event_id),),
        ).fetchall()
        result = []
        for row in rows:
            pid = int(row["id"])
            p_rows = conn.execute(
                "SELECT pp.product_id, p.name FROM promotion_products pp "
                "JOIN products p ON p.id = pp.product_id WHERE pp.promotion_id = ?",
                (pid,),
            ).fetchall()
            result.append(_rows_to_promo(row, p_rows))
    return result


def get_active_promotions_for_event(event_id: int) -> List[Dict]:
    """Promoções ativas do evento com lista de product_ids (usado no catálogo)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM promotions WHERE event_id = ? AND active = 1",
            (int(event_id),),
        ).fetchall()
        result = []
        for row in rows:
            pid = int(row["id"])
            p_rows = conn.execute(
                "SELECT pp.product_id FROM promotion_products pp WHERE pp.promotion_id = ?",
                (pid,),
            ).fetchall()
            d = dict(row)
            d["product_ids"] = [int(r["product_id"]) for r in p_rows]
            d["rule_label"] = RULE_TYPE_LABELS.get(d.get("rule_type", ""), "")
            result.append(d)
    return result


def product_ids_with_active_promotions_for_event(event_id: int) -> set[int]:
    """Conjunto de ``product_id`` com pelo menos uma promoção **ativa** no evento."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT pp.product_id
              FROM promotions pr
              JOIN promotion_products pp ON pp.promotion_id = pr.id
             WHERE pr.event_id = ? AND pr.active = 1
            """,
            (int(event_id),),
        ).fetchall()
    return {int(r["product_id"]) for r in rows}


def _format_promo_tooltip_line(promo: Dict) -> str:
    """Uma linha curta para tooltip/hover (nome + regra)."""
    name = (promo.get("name") or "").strip() or "Promoção"
    rt = promo.get("rule_type") or ""
    rv = float(promo.get("rule_value") or 0)
    min_q = max(1, int(promo.get("min_qty") or 1))
    free_q = max(0, int(promo.get("free_qty") or 0))
    if rt == "percent":
        pct = min(100.0, max(0.0, rv))
        pct_txt = str(int(pct)) if abs(pct - int(pct)) < 1e-9 else f"{pct:.1f}".replace(".", ",")
        return f"{name}: {pct_txt}% de desconto"
    if rt == "fixed":
        brv = f"{rv:.2f}".replace(".", ",")
        return f"{name}: R$ {brv} de desconto no preço unitário"
    if rt == "bogo":
        total = min_q + free_q if free_q > 0 else min_q
        return f"{name}: compre {min_q}, leve {total}"
    if rt == "min_bundle":
        brv = f"{rv:.2f}".replace(".", ",")
        return f"{name}: a partir de {min_q} un. por R$ {brv}"
    if rt == "exact_bundle":
        brv = f"{rv:.2f}".replace(".", ",")
        return f"{name}: kit de {min_q} un. por R$ {brv} (extras no preço normal)"
    return name


def active_promotion_tooltip_by_product_id(event_id: int) -> Dict[int, str]:
    """Por produto, texto único listando todas as promoções ativas que o cobrem (separador · )."""
    promos = get_active_promotions_for_event(event_id)
    promos_sorted = sorted(promos, key=lambda p: int(p.get("id") or 0))
    lines_by_pid: Dict[int, List[str]] = defaultdict(list)
    for pr in promos_sorted:
        line = _format_promo_tooltip_line(pr)
        for pid in pr.get("product_ids") or []:
            lines_by_pid[int(pid)].append(line)
    return {pid: " · ".join(lines) for pid, lines in lines_by_pid.items()}


def active_promotion_names_by_product_id(event_id: int) -> Dict[int, str]:
    """Por produto, nomes das promoções ativas que o cobrem (separador · )."""
    promos = get_active_promotions_for_event(event_id)
    promos_sorted = sorted(promos, key=lambda p: int(p.get("id") or 0))
    names_by_pid: Dict[int, List[str]] = defaultdict(list)
    for pr in promos_sorted:
        name = (pr.get("name") or "").strip() or "Promoção"
        for pid in pr.get("product_ids") or []:
            names_by_pid[int(pid)].append(name)
    return {pid: " · ".join(names) for pid, names in names_by_pid.items()}


# ---------------------------------------------------------------------------
# Aplicação de promoções aos itens da transação
# ---------------------------------------------------------------------------

def apply_promotions_to_items_in_conn(
    conn: sqlite3.Connection,
    event_id: int,
    items: List[Dict],
) -> List[Dict]:
    """Aplica promoções ativas do evento sobre ``items`` normalizados.

    Retorna nova lista com ``unit_price``, ``subtotal``, ``original_price`` e
    ``promotion_id`` ajustados para cada item que possua ``product_id`` e tenha
    ao menos uma promoção ativa cobrindo esse produto.

    Invariante: ``subtotal == round(unit_price * quantity, 2)`` sempre se mantém.
    Se mais de uma promoção cobrir o mesmo produto, aplica a que resulta no menor
    subtotal (melhor desconto).
    """
    # Carrega promos ativas do evento junto com os product_ids de cada uma.
    promo_rows = conn.execute(
        """
        SELECT pr.id, pr.rule_type, pr.rule_value, pr.min_qty, pr.free_qty,
               pp.product_id
          FROM promotions pr
          JOIN promotion_products pp ON pp.promotion_id = pr.id
         WHERE pr.event_id = ? AND pr.active = 1
        """,
        (int(event_id),),
    ).fetchall()

    if not promo_rows:
        return items

    # Agrupa: product_id -> lista de promos
    product_promos: dict = {}
    for r in promo_rows:
        pid = int(r["product_id"])
        product_promos.setdefault(pid, []).append({
            "id": int(r["id"]),
            "rule_type": r["rule_type"],
            "rule_value": float(r["rule_value"]),
            "min_qty": int(r["min_qty"]),
            "free_qty": int(r["free_qty"]),
        })

    result = []
    for item in items:
        pid = item.get("product_id")
        list_price = float(item.get("unit_price") or 0.0)
        qty = int(item.get("quantity") or 0)
        new_item = dict(item)
        new_item["original_price"] = list_price
        new_item["promotion_id"] = None

        if pid is None or pid not in product_promos or qty <= 0:
            result.append(new_item)
            continue

        # Seleciona a promoção com menor subtotal efetivo
        best_subtotal: Optional[float] = None
        best_promo = None
        for promo in product_promos[pid]:
            eff = _compute_effective_subtotal(
                promo["rule_type"], promo["rule_value"],
                promo["min_qty"], promo["free_qty"],
                list_price, qty,
            )
            if best_subtotal is None or eff < best_subtotal:
                best_subtotal = eff
                best_promo = promo

        original_subtotal = round(list_price * qty, 2)
        if best_promo is not None and best_subtotal is not None and best_subtotal < original_subtotal:
            eff_unit = round(best_subtotal / qty, 6) if qty else 0.0
            new_item["unit_price"] = eff_unit
            new_item["subtotal"] = round(best_subtotal, 2)
            new_item["promotion_id"] = int(best_promo["id"])

        result.append(new_item)
    return result


def apply_list_prices_to_normalized_items(
    conn: sqlite3.Connection,
    items: List[Dict],
    event_id: Optional[int] = None,
) -> None:
    """Substitui ``unit_price`` pelo preço de lista (evento, senão biblioteca) antes das promoções."""
    pids = {int(i["product_id"]) for i in items if i.get("product_id") is not None}
    if not pids:
        return
    placeholders = ",".join("?" * len(pids))
    pid_list = list(pids)
    if event_id is not None:
        rows = conn.execute(
            f"""
            SELECT p.id, COALESCE(ep.price, p.price) AS price
              FROM products p
              LEFT JOIN event_products ep
                ON ep.product_id = p.id AND ep.event_id = ?
             WHERE p.id IN ({placeholders})
            """,
            [int(event_id), *pid_list],
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id, price FROM products WHERE id IN ({placeholders})",
            pid_list,
        ).fetchall()
    prices = {int(r["id"]): float(r["price"] or 0) for r in rows}
    for item in items:
        pid = item.get("product_id")
        if pid is None:
            continue
        list_p = prices.get(int(pid))
        if list_p is None:
            continue
        qty = int(item.get("quantity") or 0)
        item["unit_price"] = list_p
        item["subtotal"] = round(list_p * qty, 2)


def quote_cart_items_for_event(event_id: int, cart_items: List[Dict]) -> Dict:
    """Calcula preços promocionais para itens do carrinho (mesma lógica da venda).

    ``cart_items``: lista com ``id``/``product_id`` e ``quantidade``/``quantity``.
    Retorna ``{items, total, subtotal_lista, economia_total}``.
    """
    normalized: List[Dict] = []
    for raw in cart_items or []:
        pid_raw = raw.get("id") if raw.get("id") is not None else raw.get("product_id")
        try:
            product_id = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            product_id = None
        try:
            qty = int(raw.get("quantidade") or raw.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0 or product_id is None:
            continue
        list_p = float(raw.get("preco_lista") or raw.get("preco_original") or raw.get("preco") or 0)
        normalized.append(
            {
                "product_id": product_id,
                "product_id_str": str(product_id),
                "product_name": str(raw.get("nome") or raw.get("product_name") or "Produto"),
                "product_sku": raw.get("sku") or raw.get("product_sku"),
                "category": raw.get("categoria") or raw.get("category"),
                "unit_price": list_p,
                "quantity": qty,
                "subtotal": round(list_p * qty, 2),
            }
        )

    if not normalized:
        return {"items": [], "total": 0.0, "subtotal_lista": 0.0, "economia_total": 0.0}

    promo_names: Dict[int, str] = {}
    with get_conn() as conn:
        apply_list_prices_to_normalized_items(conn, normalized, event_id=int(event_id))
        subtotal_lista = round(sum(i["subtotal"] for i in normalized), 2)
        priced = apply_promotions_to_items_in_conn(conn, int(event_id), normalized)
        promo_ids = {int(i["promotion_id"]) for i in priced if i.get("promotion_id")}
        if promo_ids:
            placeholders = ",".join("?" * len(promo_ids))
            for r in conn.execute(
                f"SELECT id, name FROM promotions WHERE id IN ({placeholders})",
                list(promo_ids),
            ).fetchall():
                promo_names[int(r["id"])] = str(r["name"] or "")

    out_items: List[Dict] = []
    for src, row in zip(normalized, priced):
        pid = int(row["product_id"])
        qty = int(row["quantity"])
        list_p = float(row.get("original_price") or src["unit_price"] or 0)
        eff_unit = float(row.get("unit_price") or 0)
        subtotal = float(row.get("subtotal") or 0)
        promo_id = row.get("promotion_id")
        has_promo = promo_id is not None and subtotal < round(list_p * qty, 2) - 0.001
        out_items.append(
            {
                "id": pid,
                "quantidade": qty,
                "preco_lista": list_p,
                "preco": eff_unit,
                "subtotal": subtotal,
                "em_promocao": has_promo,
                "promotion_id": int(promo_id) if promo_id is not None else None,
                "promo_nome": promo_names.get(int(promo_id), "") if promo_id else "",
                "economia": round(max(0.0, list_p * qty - subtotal), 2),
            }
        )

    total = round(sum(i["subtotal"] for i in out_items), 2)
    economia_total = round(max(0.0, subtotal_lista - total), 2)
    return {
        "items": out_items,
        "total": total,
        "subtotal_lista": subtotal_lista,
        "economia_total": economia_total,
    }


# ---------------------------------------------------------------------------
# Helper de exibição no catálogo
# ---------------------------------------------------------------------------

def build_promo_display_map(promotions: List[Dict]) -> Dict[int, Dict]:
    """Constrói {product_id -> promo_display} para enriquecer o catálogo do vendedor.

    ``promo_display`` contém: ``promo_id``, ``promo_nome``, ``promo_tipo``,
    ``promo_label``, ``rule_value``, ``min_qty``, ``free_qty``.
    """
    best: Dict[int, Dict] = {}
    for promo in promotions:
        for pid in promo.get("product_ids", []):
            # Se um produto tiver múltiplas promos, mantém a mais recente ativa
            # (lista já vem ordenada por created_at DESC da query).
            if pid not in best:
                best[pid] = {
                    "promo_id": promo["id"],
                    "promo_nome": promo["name"],
                    "promo_tipo": promo["rule_type"],
                    "promo_label": promo.get("rule_label", ""),
                    "rule_value": float(promo.get("rule_value") or 0),
                    "min_qty": int(promo.get("min_qty") or 1),
                    "free_qty": int(promo.get("free_qty") or 0),
                }
    return best


def enrich_product_with_promo(product: Dict, promo_map: Dict[int, Dict]) -> Dict:
    """Adiciona campos de promoção a um produto do catálogo (formato cliente).

    Campos adicionados:
    - ``em_promocao`` (bool)
    - ``promo_nome`` (str)
    - ``promo_tipo`` (str)
    - ``promo_label`` (str)
    - ``preco_original`` (float) — preço de lista
    - ``preco`` (float) — preço efetivo (já com desconto para percent/fixed;
      mantém o preço de lista para bogo, pois o desconto é de quantidade)
    - ``promo_badge`` (str) — texto curto para exibição no card, ex. "15% OFF"
    """
    pid = int(product.get("id") or 0)
    p = dict(product)
    p["em_promocao"] = False
    p["preco_original"] = float(p.get("preco") or 0)
    p["promo_nome"] = ""
    p["promo_tipo"] = ""
    p["promo_label"] = ""
    p["promo_badge"] = ""
    p["promo_min_qty"] = 0

    if pid not in promo_map:
        return p

    promo = promo_map[pid]
    p["em_promocao"] = True
    p["promo_nome"] = promo["promo_nome"]
    p["promo_tipo"] = promo["promo_tipo"]
    p["promo_label"] = promo["promo_label"]
    p["promo_min_qty"] = int(promo.get("min_qty") or 1)
    p["promo_rule_value"] = float(promo.get("rule_value") or 0)
    p["promo_free_qty"] = int(promo.get("free_qty") or 0)

    list_price = float(p.get("preco") or 0)
    rule = promo["promo_tipo"]
    val = float(promo.get("rule_value") or 0)
    min_q = int(promo.get("min_qty") or 1)
    free_q = int(promo.get("free_qty") or 0)

    if rule == "percent":
        pct = min(100.0, max(0.0, val))
        eff_price = round(list_price * (1.0 - pct / 100.0), 2)
        p["preco"] = eff_price
        p["promo_badge"] = f"{int(pct) if pct == int(pct) else pct}% OFF"
    elif rule == "fixed":
        eff_price = round(max(0.0, list_price - val), 2)
        p["preco"] = eff_price
        p["promo_badge"] = f"- R$ {val:.2f}".replace(".", ",")
    elif rule == "bogo":
        # Preço unitário não muda; desconto é de quantidade
        p["preco"] = list_price
        p["promo_badge"] = f"Compre {min_q} Leve {min_q + free_q}"
    elif rule == "min_bundle":
        # A partir de min_qty: conjuntos completos custam val; excedentes = preço de lista.
        p["preco"] = list_price
        if min_q >= 2 and val > 0:
            val_fmt = f"{val:.2f}".replace(".", ",")
            p["promo_badge"] = f"A partir de {min_q}: R$ {val_fmt} no conjunto"
        else:
            p["promo_badge"] = ""
    elif rule == "exact_bundle":
        # Kit: cada grupo completo de min_qty custa val; o restante fica no preço de lista.
        p["preco"] = list_price
        if min_q >= 2 and val > 0:
            val_fmt = f"{val:.2f}".replace(".", ",")
            p["promo_badge"] = f"Kit de {min_q}: R$ {val_fmt}"
        else:
            p["promo_badge"] = ""

    return p


# ---------------------------------------------------------------------------
# Sync de produtos (interno)
# ---------------------------------------------------------------------------

def _sync_promo_products(
    conn: sqlite3.Connection,
    promo_id: int,
    product_ids: List[int],
) -> None:
    """Substitui a lista de produtos de uma promoção (DELETE + INSERT)."""
    conn.execute("DELETE FROM promotion_products WHERE promotion_id = ?", (promo_id,))
    unique_ids = list(dict.fromkeys(int(p) for p in product_ids if p))
    if unique_ids:
        conn.executemany(
            "INSERT OR IGNORE INTO promotion_products (promotion_id, product_id) VALUES (?, ?)",
            [(promo_id, pid) for pid in unique_ids],
        )
