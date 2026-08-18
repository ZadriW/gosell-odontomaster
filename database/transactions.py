"""Orders / transactions."""
from __future__ import annotations

import math
import random
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from .connection import _now_iso, get_conn
from .event_stock import _apply_event_movement
from .promotions import (
    apply_list_prices_to_normalized_items,
    apply_promotions_to_items_in_conn,
    build_promo_display_map,
    enrich_product_with_promo,
    get_active_promotions_for_event,
)
from .sku_helpers import _build_sku_by_product_id, _default_sku_for_id, _product_sku_label
from .stock import _apply_movement, _normalize_order_reference

TX_FILTER_STATUSES = frozenset(
    {"confirmado", "pendente", "cancelado", "estornado", "entregue"}
)

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


_MIN_TOTAL_PARCELAS_REAIS = 120.0
_MIN_PARCELA_REAIS = 120.0
_MAX_PARCELAS_CARTAO = 24


def _max_card_installments_allowed(total: float) -> int:
    """Mesma regra do checkout: total > R$120 e cada parcela > R$120 (estrito)."""
    t = float(total)
    if not math.isfinite(t) or t <= _MIN_TOTAL_PARCELAS_REAIS:
        return 1
    max_k = 1
    for k in range(2, _MAX_PARCELAS_CARTAO + 1):
        if t / k > _MIN_PARCELA_REAIS:
            max_k = k
        else:
            break
    return max_k


def _normalize_card_installments_for_db(
    payment_method: Optional[str],
    total: float,
    raw,
) -> Optional[int]:
    pm = (payment_method or "").strip().lower()
    if pm != "cartao":
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Número de parcelas inválido.") from None
    if n < 1:
        raise ValueError("Número de parcelas inválido.")
    max_allowed = _max_card_installments_allowed(total)
    if n > max_allowed:
        raise ValueError("Número de parcelas inválido para o valor do pedido.")
    return n


def _promo_names_for_normalized(conn: sqlite3.Connection, normalized: List[Dict]) -> Dict[int, str]:
    promo_ids = {int(i["promotion_id"]) for i in normalized if i.get("promotion_id") is not None}
    if not promo_ids:
        return {}
    placeholders = ",".join("?" * len(promo_ids))
    rows = conn.execute(
        f"SELECT id, name FROM promotions WHERE id IN ({placeholders})",
        list(promo_ids),
    ).fetchall()
    return {int(r["id"]): str(r["name"] or "") for r in rows}


def _public_items_from_normalized(
    normalized: List[Dict],
    promo_names: Optional[Dict[int, str]] = None,
) -> List[Dict]:
    """Itens formatados para resposta JSON (checkout / cotação)."""
    names = promo_names or {}
    out: List[Dict] = []
    for i in normalized:
        pid = i.get("product_id")
        if pid is None:
            continue
        qty = int(i.get("quantity") or 0)
        list_p = float(i.get("original_price") or i.get("unit_price") or 0)
        subtotal = float(i.get("subtotal") or 0)
        promo_id = i.get("promotion_id")
        has_promo = promo_id is not None and subtotal < round(list_p * qty, 2) - 0.001
        out.append(
            {
                "id": int(pid),
                "quantidade": qty,
                "preco_lista": list_p,
                "preco": float(i.get("unit_price") or 0),
                "subtotal": subtotal,
                "promotion_id": promo_id,
                "em_promocao": has_promo,
                "promo_nome": names.get(int(promo_id), "") if promo_id is not None else "",
                "economia": round(max(0.0, list_p * qty - subtotal), 2),
            }
        )
    return out


def _coerce_seller_total(computed_total: float, seller_total: Optional[object]) -> float:
    """Aplica desconto manual do vendedor ao total já calculado (itens + promoções).

    O valor informado não pode ser negativo nem maior que o subtotal do pedido.
    """
    computed = round(float(computed_total), 2)
    if seller_total is None or seller_total == "":
        return computed
    try:
        requested = round(float(seller_total), 2)
    except (TypeError, ValueError):
        raise ValueError("Valor total informado é inválido.") from None
    if requested < 0:
        raise ValueError("O valor total não pode ser negativo.")
    if requested > computed + 0.009:
        raise ValueError(
            "O valor total não pode ser maior que o subtotal do pedido."
        )
    return requested


def create_transaction(
    items: Iterable[Dict],
    *,
    created_by: str = "totem",
    seller_id: Optional[int] = None,
    seller_name: Optional[str] = None,
    event_id: Optional[int] = None,
    client_name: Optional[str] = None,
    client_cpf: Optional[str] = None,
    client_email: Optional[str] = None,
    client_phone: Optional[str] = None,
    client_zipcode: Optional[str] = None,
    client_address: Optional[str] = None,
    client_number: Optional[str] = None,
    client_complement: Optional[str] = None,
    client_city: Optional[str] = None,
    client_state: Optional[str] = None,
    payment_method: Optional[str] = None,
    card_installments: Optional[int] = None,
    client_cro_uf: Optional[str] = None,
    client_cro_numero: Optional[str] = None,
    seller_total: Optional[float] = None,
) -> Dict:
    """Registra um pedido **pendente** com seus itens (sem baixar estoque).

    Cada item deve conter ``id, nome, categoria, preco, quantidade``; ``sku`` é
    opcional (complementado pelo catálogo quando houver ``id``).
    Estoque insuficiente não bloqueia a criação: a baixa acontece na confirmação
    do AUT (o que houver disponível) e o restante fica pendente de retirada.

    Parâmetros opcionais de ``client_*`` guardam dados do cliente na transação.
    ``client_cro_uf`` e ``client_cro_numero``: registro profissional informado no checkout.

    ``event_id``: Se fornecido, valida os produtos contra ``event_products``
    (venda em evento). Se None, usa o catálogo global de ``products``.

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
    card_installments_store = _normalize_card_installments_for_db(
        payment_method, total, card_installments if card_installments is not None else 1,
    )

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

        # Valida existência do produto no escopo da venda (evento ou catálogo).
        # Estoque insuficiente NÃO bloqueia: o item ficará pendente de retirada
        # e será baixado apenas na entrega (confirm_transaction_with_aut /
        # confirm_item_delivery).
        if event_id is not None:
            for pid in demand:
                ep = conn.execute(
                    "SELECT 1 FROM event_products WHERE event_id = ? AND product_id = ?",
                    (int(event_id), pid),
                ).fetchone()
                if ep is None:
                    raise ValueError(
                        f"Produto {_product_sku_label(pid, sku=sku_by_id.get(pid))} "
                        f"não está disponível neste evento."
                    )
            _check_event_backorder_limits(conn, event_id, demand, sku_by_id)
        else:
            for pid in demand:
                row = conn.execute(
                    "SELECT 1 FROM products WHERE id = ?", (pid,)
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"Produto {_product_sku_label(pid, sku=sku_by_id.get(pid))} "
                        f"não encontrado no catálogo."
                    )

        # Preço de lista do catálogo + promoções ativas do evento.
        if event_id is not None:
            apply_list_prices_to_normalized_items(conn, normalized, event_id=event_id)
            normalized = apply_promotions_to_items_in_conn(conn, event_id, normalized)

        # Recalcula total e items_count após promoções.
        total = round(sum(i["subtotal"] for i in normalized), 2)
        total = _coerce_seller_total(total, seller_total)
        items_count = sum(i["quantity"] for i in normalized)
        card_installments_store = _normalize_card_installments_for_db(
            payment_method, total, card_installments if card_installments is not None else 1,
                    )

        order_number = generate_order_number(conn)
        created_at = _now_iso()

        cur = conn.execute(
            """
            INSERT INTO transactions
                (order_number, created_at, total, items_count, status,
                 client_name, client_cpf, client_email, client_phone,
                 client_zipcode, client_address,
                 client_number, client_complement, client_city, client_state,
                 seller_id, seller_name, payment_method, card_installments,
                 client_cro_uf, client_cro_numero, client_cro_categoria,
                 client_cro_validated, client_cro_validation_data, aut, event_id)
            VALUES (?, ?, ?, ?, 'pendente', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?)
            """,
            (
                order_number, created_at, total, items_count,
                client_name, client_cpf, client_email, client_phone,
                client_zipcode, client_address,
                client_number, client_complement, client_city, client_state,
                seller_id, seller_name, payment_method, card_installments_store,
                client_cro_uf, client_cro_numero, event_id,
            ),
        )
        tx_id = cur.lastrowid

        conn.executemany(
            """
            INSERT INTO transaction_items
                (transaction_id, product_id, product_name, category,
                 unit_price, quantity, subtotal, product_sku, original_price, promotion_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    i.get("original_price"),
                    i.get("promotion_id"),
                )
                for i in normalized
            ],
        )

        # Guarda normalized/demand/event_id no contexto de retorno para uso posterior.
        # O estoque só é baixado em confirm_transaction_with_aut().
        promo_names = _promo_names_for_normalized(conn, normalized)

    return {
        "id": tx_id,
        "order_number": order_number,
        "total": total,
        "items_count": items_count,
        "created_at": created_at,
        "seller_id": seller_id,
        "seller_name": seller_name,
        "payment_method": payment_method,
        "card_installments": card_installments_store,
        "status": "pendente",
        "items": _public_items_from_normalized(normalized, promo_names),
        "subtotal_lista": round(
            sum(
                float(i.get("original_price") or i.get("unit_price") or 0)
                * int(i.get("quantity") or 0)
                for i in normalized
            ),
            2,
        ),
        "economia_total": round(
            max(
                0.0,
                sum(
                    float(i.get("original_price") or i.get("unit_price") or 0)
                    * int(i.get("quantity") or 0)
                    for i in normalized
                )
                - total,
            ),
            2,
        ),
        # Metadados internos necessários para confirm_transaction_with_aut().
        "_normalized": normalized,
        "_demand": demand,
        "_event_id": event_id,
        "_created_by": created_by,
    }


def _pending_tx_merge_client_field(
    incoming: Optional[str],
    existing: Optional[str],
) -> Optional[str]:
    """PATCH de pedido pendente: preserva texto já gravado quando o payload omite o campo.

    Evita apagar dados do cliente quando o front envia ``client: {}`` (ex.: ``sessionStorage``
    vazio na tela de AUT retomada ou em outra aba).
    """
    if incoming is None:
        return existing
    stripped = str(incoming).strip()
    if not stripped:
        return existing
    return stripped


def update_pending_transaction(
    tx_id: int,
    *,
    seller_id: int,
    items: Iterable[Dict],
    client_name: Optional[str] = None,
    client_cpf: Optional[str] = None,
    client_email: Optional[str] = None,
    client_phone: Optional[str] = None,
    client_zipcode: Optional[str] = None,
    client_address: Optional[str] = None,
    client_number: Optional[str] = None,
    client_complement: Optional[str] = None,
    client_city: Optional[str] = None,
    client_state: Optional[str] = None,
    payment_method: Optional[str] = None,
    card_installments: Optional[int] = None,
    client_cro_uf: Optional[str] = None,
    client_cro_numero: Optional[str] = None,
    seller_total: Optional[float] = None,
) -> Dict:
    """Atualiza um pedido **pendente** (itens, totais, cliente e pagamento) sem baixar estoque.

    Mantém ``order_number``, ``created_at``, ``seller_*`` e ``event_id`` da transação original.
    Usado ao retomar o checkout após alterações no carrinho ou na forma de pagamento.

    Campos ``client_*`` e CRO: valores omitidos ou em branco no PATCH **não apagam** dados já
    gravados (merge com a linha atual), para suportar fluxos sem ``sessionStorage`` na tela de AUT.
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
    card_installments_store = _normalize_card_installments_for_db(
        payment_method,
        total,
        card_installments if card_installments is not None else 1,
    )

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (int(tx_id),)
        ).fetchone()
        if row is None:
            raise ValueError("Transação não encontrada.")
        tx_row = dict(row)
        if str(tx_row.get("status") or "").lower() != "pendente":
            raise ValueError("Somente pedidos pendentes podem ser atualizados.")
        if int(tx_row.get("seller_id") or 0) != int(seller_id):
            raise ValueError("Você não pode alterar esta transação.")

        ev_raw = tx_row.get("event_id")
        try:
            event_id = int(ev_raw) if ev_raw is not None else None
        except (TypeError, ValueError):
            event_id = None
        if event_id is not None and event_id <= 0:
            event_id = None

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

        demand: Dict[int, int] = {}
        for i in normalized:
            if i["product_id"] is None:
                continue
            demand[i["product_id"]] = demand.get(i["product_id"], 0) + i["quantity"]

        # Somente existência do produto no escopo; estoque insuficiente vira
        # item pendente de retirada na confirmação do AUT.
        if event_id is not None:
            for pid in demand:
                ep = conn.execute(
                    "SELECT 1 FROM event_products WHERE event_id = ? AND product_id = ?",
                    (int(event_id), pid),
                ).fetchone()
                if ep is None:
                    raise ValueError(
                        f"Produto {_product_sku_label(pid, sku=sku_by_id.get(pid))} "
                        f"não está disponível neste evento."
                    )
            _check_event_backorder_limits(conn, event_id, demand, sku_by_id)
        else:
            for pid in demand:
                pr = conn.execute(
                    "SELECT 1 FROM products WHERE id = ?", (pid,)
                ).fetchone()
                if pr is None:
                    raise ValueError(
                        f"Produto {_product_sku_label(pid, sku=sku_by_id.get(pid))} "
                        f"não encontrado no catálogo."
                    )

        chk = conn.execute(
            """
            SELECT 1 FROM transactions
             WHERE id = ? AND status = 'pendente' AND seller_id = ?
            """,
            (int(tx_id), int(seller_id)),
        ).fetchone()
        if chk is None:
            raise ValueError("Não foi possível atualizar o pedido.")

        merged_name = _pending_tx_merge_client_field(client_name, tx_row.get("client_name"))
        merged_cpf = _pending_tx_merge_client_field(client_cpf, tx_row.get("client_cpf"))
        merged_email = _pending_tx_merge_client_field(client_email, tx_row.get("client_email"))
        merged_phone = _pending_tx_merge_client_field(client_phone, tx_row.get("client_phone"))
        merged_zip = _pending_tx_merge_client_field(client_zipcode, tx_row.get("client_zipcode"))
        merged_addr = _pending_tx_merge_client_field(client_address, tx_row.get("client_address"))
        merged_num = _pending_tx_merge_client_field(client_number, tx_row.get("client_number"))
        merged_comp = _pending_tx_merge_client_field(client_complement, tx_row.get("client_complement"))
        merged_city = _pending_tx_merge_client_field(client_city, tx_row.get("client_city"))
        merged_state = _pending_tx_merge_client_field(client_state, tx_row.get("client_state"))
        merged_cro_n = _pending_tx_merge_client_field(
            client_cro_numero,
            tx_row.get("client_cro_numero"),
        )
        merged_cro_uf_raw = _pending_tx_merge_client_field(
            client_cro_uf,
            tx_row.get("client_cro_uf"),
        )
        merged_cro_uf = merged_cro_uf_raw.strip().upper() if merged_cro_uf_raw else None

        # Preço de lista do catálogo + promoções ativas do evento.
        if event_id is not None:
            apply_list_prices_to_normalized_items(conn, normalized, event_id=event_id)
            normalized = apply_promotions_to_items_in_conn(conn, event_id, normalized)

        total = round(sum(i["subtotal"] for i in normalized), 2)
        total = _coerce_seller_total(total, seller_total)
        items_count = sum(i["quantity"] for i in normalized)
        card_installments_store = _normalize_card_installments_for_db(
            payment_method, total,
            card_installments if card_installments is not None else 1,
        )

        conn.execute(
            """
            UPDATE transactions
               SET total = ?, items_count = ?,
                   client_name = ?, client_cpf = ?, client_email = ?, client_phone = ?,
                   client_zipcode = ?, client_address = ?,
                   client_number = ?, client_complement = ?, client_city = ?, client_state = ?,
                   payment_method = ?, card_installments = ?,
                   client_cro_uf = ?, client_cro_numero = ?
             WHERE id = ? AND status = 'pendente' AND seller_id = ?
            """,
            (
                total,
                items_count,
                merged_name,
                merged_cpf,
                merged_email,
                merged_phone,
                merged_zip,
                merged_addr,
                merged_num,
                merged_comp,
                merged_city,
                merged_state,
                payment_method,
                card_installments_store,
                merged_cro_uf,
                merged_cro_n,
                int(tx_id),
                int(seller_id),
            ),
        )

        conn.execute(
            "DELETE FROM transaction_items WHERE transaction_id = ?",
            (int(tx_id),),
        )
        conn.executemany(
            """
            INSERT INTO transaction_items
                (transaction_id, product_id, product_name, category,
                 unit_price, quantity, subtotal, product_sku, original_price, promotion_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(tx_id),
                    i["product_id_str"],
                    i["product_name"],
                    i["category"],
                    i["unit_price"],
                    i["quantity"],
                    i["subtotal"],
                    i.get("product_sku"),
                    i.get("original_price"),
                    i.get("promotion_id"),
                )
                for i in normalized
            ],
        )

        promo_names = _promo_names_for_normalized(conn, normalized)

    return {
        "id": int(tx_id),
        "order_number": tx_row["order_number"],
        "total": total,
        "items_count": items_count,
        "created_at": tx_row["created_at"],
        "seller_id": int(seller_id),
        "seller_name": tx_row.get("seller_name"),
        "payment_method": payment_method,
        "card_installments": card_installments_store,
        "status": "pendente",
        "items": _public_items_from_normalized(normalized, promo_names),
        "subtotal_lista": round(
            sum(
                float(i.get("original_price") or i.get("unit_price") or 0)
                * int(i.get("quantity") or 0)
                for i in normalized
            ),
            2,
        ),
        "economia_total": round(
            max(
                0.0,
                sum(
                    float(i.get("original_price") or i.get("unit_price") or 0)
                    * int(i.get("quantity") or 0)
                    for i in normalized
                )
                - total,
            ),
            2,
        ),
    }


def _event_id_for_aut_confirmation(
    conn: sqlite3.Connection,
    tx_row: Dict,
    demand: Dict[int, int],
) -> Optional[int]:
    """Define qual saldo usar na confirmação (evento vs catálogo global).

    Usa ``transactions.event_id`` quando válido. Se estiver ausente/nulo mas o
    pedido só contém produtos ligados ao **evento ativo do vendedor**, infere o
    evento — cenário típico de linhas antigas ou falha pontual na persistência,
    que antes faziam o AUT validar ``products.stock`` enquanto o totem exibia
    ``event_products.stock``.
    """
    raw = tx_row.get("event_id")
    try:
        eid = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        eid = None
    if eid is not None and eid <= 0:
        eid = None
    if eid is not None or not demand:
        return eid

    sid_raw = tx_row.get("seller_id")
    if sid_raw is None:
        return None
    try:
        sid = int(sid_raw)
    except (TypeError, ValueError):
        return None

    ev_row = conn.execute(
        """
        SELECT e.id
          FROM event_sellers es
          JOIN events e ON e.id = es.event_id
         WHERE es.seller_id = ? AND e.active = 1
         ORDER BY es.added_at DESC
         LIMIT 1
        """,
        (sid,),
    ).fetchone()
    if ev_row is None:
        return None

    candidate = int(ev_row["id"])
    for pid in demand:
        hit = conn.execute(
            "SELECT 1 FROM event_products WHERE event_id = ? AND product_id = ?",
            (candidate, int(pid)),
        ).fetchone()
        if hit is None:
            return None
    return candidate


def _available_stock_for_product(
    conn: sqlite3.Connection,
    product_id: int,
    event_id: Optional[int],
) -> Optional[int]:
    """Saldo disponível do produto no escopo da venda (evento ou catálogo global).

    Retorna ``None`` se o produto não existir no escopo (não cadastrado no
    evento / inexistente no catálogo).
    """
    if event_id is not None:
        ep = conn.execute(
            "SELECT stock FROM event_products WHERE event_id = ? AND product_id = ?",
            (int(event_id), int(product_id)),
        ).fetchone()
        return int(ep["stock"] or 0) if ep is not None else None
    pr = conn.execute(
        "SELECT stock FROM products WHERE id = ?", (int(product_id),)
    ).fetchone()
    return int(pr["stock"] or 0) if pr is not None else None


def _delivery_status_for_tx(conn: sqlite3.Connection, tx_id: int) -> str:
    """Recalcula o status de entrega a partir dos itens da transação.

    Itens sem ``product_id`` numérico não controlam estoque e contam como entregues.
    """
    rows = conn.execute(
        "SELECT product_id, quantity, quantity_delivered FROM transaction_items "
        "WHERE transaction_id = ?",
        (int(tx_id),),
    ).fetchall()
    any_delivered = False
    any_pending = False
    for r in rows:
        try:
            int(r["product_id"])
        except (TypeError, ValueError):
            continue
        qty = int(r["quantity"] or 0)
        delivered = int(r["quantity_delivered"] or 0)
        if delivered > 0:
            any_delivered = True
        if delivered < qty:
            any_pending = True
    if not any_pending:
        return "completa"
    return "parcial" if any_delivered else "pendente"


def confirm_transaction_with_aut(tx_id: int, aut: str, *, created_by: str = "totem") -> Dict:
    """Confirma uma transação pendente: salva o AUT, baixa o estoque e muda status.

    O pagamento é sempre pelo valor total. Itens sem estoque suficiente **não
    bloqueiam** a confirmação: o sistema entrega (baixa) o que houver disponível
    e marca o restante como pendente de retirada (``delivery_status``).

    Deve ser chamada com o ``tx_id`` retornado por ``create_transaction``.
    Levanta ``ValueError`` se a transação não existir, já estiver confirmada/cancelada
    ou o AUT for inválido.
    """
    aut_clean = (aut or "").strip()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Transação não encontrada.")
        if row["status"] != "pendente":
            raise ValueError("Esta transação já foi processada e não pode ser alterada.")

        tx_row = dict(row)
        pm = (tx_row.get("payment_method") or "").strip().lower()
        if not aut_clean:
            if pm == "dinheiro":
                aut_clean = "DINHEIRO"
            else:
                raise ValueError("O código AUT não pode estar vazio.")

        # Itens gravados (linha a linha, para controlar entrega por item).
        items_rows = conn.execute(
            "SELECT id, product_id, product_name, quantity FROM transaction_items "
            "WHERE transaction_id = ? ORDER BY id",
            (tx_id,),
        ).fetchall()
        demand: Dict[int, int] = {}
        for it in items_rows:
            try:
                pid = int(it["product_id"])
            except (TypeError, ValueError):
                continue
            demand[pid] = demand.get(pid, 0) + int(it["quantity"] or 0)

        event_id = _event_id_for_aut_confirmation(conn, tx_row, demand)
        if event_id is not None and tx_row.get("event_id") is None:
            conn.execute(
                "UPDATE transactions SET event_id = ? WHERE id = ?",
                (int(event_id), int(tx_id)),
            )

        order_number = row["order_number"]

        # Saldo disponível por produto no escopo da venda.
        sku_by_id = _build_sku_by_product_id(conn, demand.keys())
        available: Dict[int, int] = {}
        for pid in demand:
            stock = _available_stock_for_product(conn, pid, event_id)
            if stock is None:
                sku = _product_sku_label(pid, sku=sku_by_id.get(pid))
                if event_id is not None:
                    raise ValueError(f"Produto {sku} não está disponível neste evento.")
                raise ValueError(f"Produto {sku} não encontrado no catálogo.")
            available[pid] = stock

        # Aloca entrega por item (na ordem de inserção) até esgotar o saldo.
        deliver_by_product: Dict[int, int] = {}
        pending_items: List[Dict] = []
        for it in items_rows:
            try:
                pid = int(it["product_id"])
            except (TypeError, ValueError):
                continue
            qty = int(it["quantity"] or 0)
            deliver_now = min(qty, available.get(pid, 0))
            available[pid] = available.get(pid, 0) - deliver_now
            if deliver_now > 0:
                conn.execute(
                    "UPDATE transaction_items SET quantity_delivered = ? WHERE id = ?",
                    (deliver_now, int(it["id"])),
                )
                deliver_by_product[pid] = deliver_by_product.get(pid, 0) + deliver_now
            if deliver_now < qty:
                pending_items.append(
                    {
                        "item_id": int(it["id"]),
                        "product_id": pid,
                        "product_name": it["product_name"],
                        "pending": qty - deliver_now,
                    }
                )

        # Baixa estoque e registra movimentações (apenas do que foi entregue).
        for pid, qty in deliver_by_product.items():
            if event_id is not None:
                _apply_event_movement(
                    conn,
                    event_id=int(event_id),
                    product_id=pid,
                    movement_type="venda",
                    delta=-qty,
                    reason="Venda no totem",
                    reference=order_number,
                    transaction_id=tx_id,
                    created_by=created_by,
                )
            else:
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

        delivery_status = _delivery_status_for_tx(conn, tx_id)
        conn.execute(
            "UPDATE transactions SET status = 'confirmado', aut = ?, delivery_status = ? "
            "WHERE id = ?",
            (aut_clean, delivery_status, tx_id),
        )

    return {
        "id": tx_id,
        "order_number": order_number,
        "aut": aut_clean,
        "status": "confirmado",
        "delivery_status": delivery_status,
        "pending_items": pending_items,
    }


def refund_transaction(
    tx_id: int,
    *,
    created_by: str = "admin",
    expected_event_id: Optional[int] = None,
) -> Dict:
    """Estorna transação confirmada: repõe estoque e marca status ``estornado``.

    Levanta ``ValueError`` se a transação não existir, não estiver confirmada,
    já tiver sido estornada ou não pertencer ao ``expected_event_id`` informado.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if row is None:
            raise ValueError("Transação não encontrada.")
        if row["status"] != "confirmado":
            raise ValueError("Somente transações confirmadas podem ser estornadas.")

        tx_row = dict(row)
        order_number = (tx_row.get("order_number") or "").strip() or f"#{tx_id}"
        reason = f"Estorno"

        dup = conn.execute(
            """
            SELECT 1 FROM stock_movements
             WHERE transaction_id = ? AND delta > 0 AND reason LIKE 'Estorno%'
             LIMIT 1
            """,
            (tx_id,),
        ).fetchone()
        if dup:
            raise ValueError("Esta transação já foi estornada.")

        event_id_raw = tx_row.get("event_id")
        event_id: Optional[int] = (
            int(event_id_raw) if event_id_raw is not None else None
        )
        if event_id is None:
            mov = conn.execute(
                """
                SELECT event_id FROM stock_movements
                 WHERE transaction_id = ? AND movement_type = 'venda' AND event_id IS NOT NULL
                 LIMIT 1
                """,
                (tx_id,),
            ).fetchone()
            if mov and mov["event_id"] is not None:
                event_id = int(mov["event_id"])

        if expected_event_id is not None:
            tx_ev = tx_row.get("event_id")
            resolved = event_id
            if resolved is None and tx_ev is not None:
                resolved = int(tx_ev)
            if resolved is None or int(resolved) != int(expected_event_id):
                raise ValueError("Esta transação não pertence a este evento.")

        # Repõe apenas o que foi de fato entregue (estoque baixado);
        # itens ainda pendentes de retirada nunca saíram do estoque.
        items_rows = conn.execute(
            "SELECT product_id, quantity_delivered FROM transaction_items "
            "WHERE transaction_id = ?",
            (tx_id,),
        ).fetchall()
        demand: Dict[int, int] = {}
        for it in items_rows:
            try:
                pid = int(it["product_id"])
            except (TypeError, ValueError):
                continue
            delivered = int(it["quantity_delivered"] or 0)
            if delivered > 0:
                demand[pid] = demand.get(pid, 0) + delivered

        ref = order_number if order_number.startswith("OM") else None
        if event_id is not None:
            for pid, qty in demand.items():
                _apply_event_movement(
                    conn,
                    event_id=int(event_id),
                    product_id=pid,
                    movement_type="entrada",
                    delta=qty,
                    reason=reason,
                    reference=ref,
                    transaction_id=tx_id,
                    created_by=created_by,
                )
        else:
            for pid, qty in demand.items():
                _apply_movement(
                    conn,
                    product_id=pid,
                    movement_type="entrada",
                    delta=qty,
                    reason=reason,
                    reference=ref,
                    transaction_id=tx_id,
                    created_by=created_by,
                )

        conn.execute(
            "UPDATE transactions SET status = 'estornado' WHERE id = ?",
            (tx_id,),
        )

    return {
        "id": tx_id,
        "order_number": tx_row.get("order_number"),
        "status": "estornado",
    }


def _load_confirmed_tx_for_delivery(
    conn: sqlite3.Connection,
    tx_id: int,
    *,
    seller_id: Optional[int] = None,
    expected_event_id: Optional[int] = None,
) -> Tuple[Dict, Optional[int]]:
    """Valida e retorna ``(tx_row, event_id)`` para registro de entrega."""
    tx = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (int(tx_id),)
    ).fetchone()
    if tx is None:
        raise ValueError("Transação não encontrada.")
    tx_row = dict(tx)
    if str(tx_row.get("status") or "").lower() != "confirmado":
        raise ValueError("Somente transações confirmadas podem ter entrega registrada.")
    if seller_id is not None and int(tx_row.get("seller_id") or 0) != int(seller_id):
        raise ValueError("Você não pode alterar esta transação.")

    event_id_raw = tx_row.get("event_id")
    event_id: Optional[int] = int(event_id_raw) if event_id_raw is not None else None
    if expected_event_id is not None and (
        event_id is None or int(event_id) != int(expected_event_id)
    ):
        raise ValueError("Esta transação não pertence a este evento.")
    return tx_row, event_id


def _confirm_item_delivery_in_conn(
    conn: sqlite3.Connection,
    tx_id: int,
    item_id: int,
    tx_row: Dict,
    event_id: Optional[int],
    *,
    quantity: Optional[int] = None,
    created_by: str = "totem",
    refresh_delivery_status: bool = True,
) -> Dict:
    """Baixa estoque e marca entrega de um item (conexão já aberta)."""
    item = conn.execute(
        "SELECT id, product_id, product_name, quantity, quantity_delivered "
        "FROM transaction_items WHERE id = ? AND transaction_id = ?",
        (int(item_id), int(tx_id)),
    ).fetchone()
    if item is None:
        raise ValueError("Item não encontrado neste pedido.")
    try:
        pid = int(item["product_id"])
    except (TypeError, ValueError):
        raise ValueError("Este item não controla estoque.") from None

    qty_total = int(item["quantity"] or 0)
    delivered = int(item["quantity_delivered"] or 0)
    pending = qty_total - delivered
    if pending <= 0:
        raise ValueError(
            f"Item '{item['product_name']}' já foi totalmente entregue."
        )

    stock = _available_stock_for_product(conn, pid, event_id)
    if stock is None:
        raise ValueError(
            f"Produto '{item['product_name']}' não está mais disponível neste escopo."
        )
    if stock <= 0:
        raise ValueError(
            f"Sem estoque disponível para '{item['product_name']}'. "
            "Registre uma entrada de estoque antes de confirmar a entrega."
        )

    requested = pending if quantity is None else int(quantity)
    if requested <= 0:
        raise ValueError("Quantidade de entrega inválida.")
    qty_to_deliver = min(requested, pending, stock)

    order_number = (tx_row.get("order_number") or "").strip() or f"#{tx_id}"
    if event_id is not None:
        _apply_event_movement(
            conn,
            event_id=int(event_id),
            product_id=pid,
            movement_type="venda",
            delta=-qty_to_deliver,
            reason="entrega pendente",
            reference=order_number,
            transaction_id=int(tx_id),
            created_by=created_by,
        )
    else:
        _apply_movement(
            conn,
            product_id=pid,
            movement_type="venda",
            delta=-qty_to_deliver,
            reason="entrega pendente",
            reference=order_number,
            transaction_id=int(tx_id),
            created_by=created_by,
        )

    conn.execute(
        "UPDATE transaction_items SET quantity_delivered = quantity_delivered + ? "
        "WHERE id = ?",
        (qty_to_deliver, int(item_id)),
    )

    delivery_status = None
    if refresh_delivery_status:
        delivery_status = _delivery_status_for_tx(conn, int(tx_id))
        conn.execute(
            "UPDATE transactions SET delivery_status = ? WHERE id = ?",
            (delivery_status, int(tx_id)),
        )

    return {
        "id": int(tx_id),
        "item_id": int(item_id),
        "product_name": item["product_name"],
        "delivered_now": qty_to_deliver,
        "still_pending": pending - qty_to_deliver,
        "delivery_status": delivery_status,
    }


def confirm_item_delivery(
    tx_id: int,
    item_id: int,
    quantity: Optional[int] = None,
    *,
    seller_id: Optional[int] = None,
    expected_event_id: Optional[int] = None,
    created_by: str = "totem",
) -> Dict:
    """Confirma a entrega (retirada) de um item pendente de transação confirmada.

    Baixa o estoque e registra movimentação ``venda`` com a mesma referência do
    pedido. ``quantity`` omitido entrega tudo que estiver pendente (limitado ao
    estoque disponível). Recalcula ``transactions.delivery_status`` ao final.

    - ``seller_id``: quando informado, exige que a transação pertença ao vendedor.
    - ``expected_event_id``: quando informado, exige que a transação pertença ao evento.
    """
    with get_conn() as conn:
        tx_row, event_id = _load_confirmed_tx_for_delivery(
            conn,
            tx_id,
            seller_id=seller_id,
            expected_event_id=expected_event_id,
        )
        return _confirm_item_delivery_in_conn(
            conn,
            tx_id,
            item_id,
            tx_row,
            event_id,
            quantity=quantity,
            created_by=created_by,
            refresh_delivery_status=True,
        )


def confirm_items_delivery(
    tx_id: int,
    item_ids: Iterable[int],
    *,
    seller_id: Optional[int] = None,
    expected_event_id: Optional[int] = None,
    created_by: str = "totem",
) -> Dict:
    """Confirma a entrega de vários itens pendentes na mesma transação.

    Processa cada item em sequência na mesma conexão SQLite. Itens sem estoque
    (ou inválidos) entram em ``errors`` sem interromper os demais; se nenhum
    item for entregue, levanta ``ValueError``.
    """
    ids: List[int] = []
    seen: set[int] = set()
    for raw in item_ids:
        try:
            iid = int(raw)
        except (TypeError, ValueError):
            continue
        if iid <= 0 or iid in seen:
            continue
        seen.add(iid)
        ids.append(iid)

    if not ids:
        raise ValueError("Selecione ao menos um item para confirmar a entrega.")

    delivered: List[Dict] = []
    errors: List[str] = []

    with get_conn() as conn:
        tx_row, event_id = _load_confirmed_tx_for_delivery(
            conn,
            tx_id,
            seller_id=seller_id,
            expected_event_id=expected_event_id,
        )
        for item_id in ids:
            try:
                delivered.append(
                    _confirm_item_delivery_in_conn(
                        conn,
                        tx_id,
                        item_id,
                        tx_row,
                        event_id,
                        quantity=None,
                        created_by=created_by,
                        refresh_delivery_status=False,
                    )
                )
            except ValueError as exc:
                errors.append(str(exc))

        if not delivered:
            raise ValueError(errors[0] if errors else "Nenhum item pôde ser entregue.")

        delivery_status = _delivery_status_for_tx(conn, int(tx_id))
        conn.execute(
            "UPDATE transactions SET delivery_status = ? WHERE id = ?",
            (delivery_status, int(tx_id)),
        )
        for row in delivered:
            row["delivery_status"] = delivery_status

    return {
        "id": int(tx_id),
        "delivered": delivered,
        "errors": errors,
        "items_count": len(delivered),
        "units_delivered": sum(int(d["delivered_now"]) for d in delivered),
        "delivery_status": delivery_status,
    }


def confirm_transaction_handover(
    tx_id: int,
    *,
    seller_id: Optional[int] = None,
    expected_event_id: Optional[int] = None,
) -> Dict:
    """Marca o pedido inteiro como entregue/retirado pelo cliente no balcão.

    Não altera estoque nem ``delivery_status``/``quantity_delivered`` dos itens:
    a confirmação de retirada pendente por item (com baixa de estoque) continua
    funcionando de forma independente (ver ``confirm_item_delivery``). Este
    controle apenas sinaliza, para acompanhamento, que o pedido foi entregue.

    - ``seller_id``: quando informado, exige que a transação pertença ao vendedor.
    - ``expected_event_id``: quando informado, exige que a transação pertença ao evento.
    """
    with get_conn() as conn:
        tx = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (int(tx_id),)
        ).fetchone()
        if tx is None:
            raise ValueError("Transação não encontrada.")
        tx_row = dict(tx)
        if str(tx_row.get("status") or "").lower() != "confirmado":
            raise ValueError(
                "Somente pedidos pagos (confirmados) podem ser marcados como entregues."
            )
        if seller_id is not None and int(tx_row.get("seller_id") or 0) != int(seller_id):
            raise ValueError("Você não pode alterar esta transação.")

        event_id_raw = tx_row.get("event_id")
        event_id: Optional[int] = int(event_id_raw) if event_id_raw is not None else None
        if expected_event_id is not None and (
            event_id is None or int(event_id) != int(expected_event_id)
        ):
            raise ValueError("Esta transação não pertence a este evento.")

        if str(tx_row.get("handover_status") or "").lower() == "entregue":
            return {
                "id": int(tx_id),
                "handover_status": "entregue",
                "handover_confirmed_at": tx_row.get("handover_confirmed_at"),
                "already_confirmed": True,
            }

        now = _now_iso()
        conn.execute(
            "UPDATE transactions "
            "SET handover_status = 'entregue', handover_confirmed_at = ? "
            "WHERE id = ?",
            (now, int(tx_id)),
        )
        return {
            "id": int(tx_id),
            "handover_status": "entregue",
            "handover_confirmed_at": now,
            "already_confirmed": False,
        }


def count_pending_delivery_transactions(
    seller_id: Optional[int] = None,
    event_id: Optional[int] = None,
) -> int:
    """Conta transações confirmadas com itens aguardando retirada."""
    parts = [
        "LOWER(TRIM(COALESCE(status, ''))) = 'confirmado'",
        "COALESCE(delivery_status, 'completa') IN ('parcial', 'pendente')",
    ]
    params: List = []
    if seller_id is not None:
        parts.append("seller_id = ?")
        params.append(int(seller_id))
    if event_id is not None:
        parts.append("event_id = ?")
        params.append(int(event_id))
    sql = f"SELECT COUNT(*) AS c FROM transactions WHERE {' AND '.join(parts)}"
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["c"] if row else 0)


PENDING_MOVEMENT_TYPE = "pendente"


def _pending_delivery_units_by_product_for_event_conn(
    conn: sqlite3.Connection,
    event_id: int,
    product_ids: Optional[Iterable[int]] = None,
    *,
    seller_id: Optional[int] = None,
) -> Dict[int, int]:
    """Mapa ``product_id → unidades pendentes de retirada`` no evento (usa conexão dada)."""
    params: List = [int(event_id)]
    pid_filter = ""
    if product_ids is not None:
        pids = sorted({int(p) for p in product_ids if p is not None})
        if not pids:
            return {}
        placeholders = ",".join("?" * len(pids))
        pid_filter = f" AND CAST(ti.product_id AS INTEGER) IN ({placeholders})"
        params.extend(pids)
    seller_filter = ""
    if seller_id is not None:
        seller_filter = " AND COALESCE(t.seller_id, -1) = ?"
        params.append(int(seller_id))

    sql = f"""
        SELECT CAST(ti.product_id AS INTEGER) AS product_id,
               SUM(ti.quantity - COALESCE(ti.quantity_delivered, 0)) AS pending_units
          FROM transaction_items ti
          JOIN transactions t ON t.id = ti.transaction_id
         WHERE t.event_id = ?
           AND LOWER(TRIM(COALESCE(t.status, ''))) = 'confirmado'
           AND (ti.quantity - COALESCE(ti.quantity_delivered, 0)) > 0
           AND CAST(ti.product_id AS INTEGER) > 0
           {pid_filter}
           {seller_filter}
         GROUP BY CAST(ti.product_id AS INTEGER)
    """
    out: Dict[int, int] = {}
    for r in conn.execute(sql, params).fetchall():
        out[int(r["product_id"])] = int(r["pending_units"] or 0)
    return out


def pending_delivery_units_by_product_for_event(
    event_id: int,
    product_ids: Optional[Iterable[int]] = None,
    *,
    seller_id: Optional[int] = None,
) -> Dict[int, int]:
    """Mapa ``product_id → unidades pendentes de retirada`` no evento."""
    with get_conn() as conn:
        return _pending_delivery_units_by_product_for_event_conn(
            conn, event_id, product_ids, seller_id=seller_id,
        )


def units_sold_by_product_for_event(
    event_id: int,
    product_ids: Optional[Iterable[int]] = None,
    seller_id: Optional[int] = None,
) -> Dict[int, int]:
    """Mapa ``product_id → unidades vendidas`` em pedidos confirmados do evento.

    Conta ``SUM(transaction_items.quantity)`` apenas em transações com
    ``status = confirmado`` e ``event_id`` correspondente. Estornos não entram.
    Com ``seller_id``, restringe às vendas desse vendedor no evento.
    """
    params: List = [int(event_id)]
    pid_filter = ""
    if product_ids is not None:
        pids = sorted({int(p) for p in product_ids if p is not None})
        if not pids:
            return {}
        placeholders = ",".join("?" * len(pids))
        pid_filter = f" AND CAST(ti.product_id AS INTEGER) IN ({placeholders})"
        params.extend(pids)
    seller_filter = ""
    if seller_id is not None:
        seller_filter = " AND t.seller_id = ?"
        params.append(int(seller_id))

    sql = f"""
        SELECT CAST(ti.product_id AS INTEGER) AS product_id,
               COALESCE(SUM(ti.quantity), 0) AS units_sold
          FROM transaction_items ti
          JOIN transactions t ON t.id = ti.transaction_id
         WHERE t.event_id = ?
           AND LOWER(TRIM(COALESCE(t.status, ''))) = 'confirmado'
           AND CAST(ti.product_id AS INTEGER) > 0
           {pid_filter}
           {seller_filter}
         GROUP BY CAST(ti.product_id AS INTEGER)
    """
    out: Dict[int, int] = {}
    with get_conn() as conn:
        for r in conn.execute(sql, params).fetchall():
            out[int(r["product_id"])] = int(r["units_sold"] or 0)
    return out


def units_sold_by_product_for_seller(
    seller_id: int,
    product_ids: Optional[Iterable[int]] = None,
) -> Dict[int, int]:
    """Mapa ``product_id → unidades vendidas`` pelo vendedor (todos os eventos)."""
    params: List = [int(seller_id)]
    pid_filter = ""
    if product_ids is not None:
        pids = sorted({int(p) for p in product_ids if p is not None})
        if not pids:
            return {}
        placeholders = ",".join("?" * len(pids))
        pid_filter = f" AND CAST(ti.product_id AS INTEGER) IN ({placeholders})"
        params.extend(pids)

    sql = f"""
        SELECT CAST(ti.product_id AS INTEGER) AS product_id,
               COALESCE(SUM(ti.quantity), 0) AS units_sold
          FROM transaction_items ti
          JOIN transactions t ON t.id = ti.transaction_id
         WHERE t.seller_id = ?
           AND LOWER(TRIM(COALESCE(t.status, ''))) = 'confirmado'
           AND CAST(ti.product_id AS INTEGER) > 0
           {pid_filter}
         GROUP BY CAST(ti.product_id AS INTEGER)
    """
    out: Dict[int, int] = {}
    with get_conn() as conn:
        for r in conn.execute(sql, params).fetchall():
            out[int(r["product_id"])] = int(r["units_sold"] or 0)
    return out


def _check_event_backorder_limits(
    conn: sqlite3.Connection,
    event_id: int,
    demand: Dict[int, int],
    sku_by_id: Optional[Dict[int, str]] = None,
) -> None:
    """Impede que um pedido supere o limite de entregas pendentes configurado no evento.

    Para cada produto, ``entrega pendente`` = quantidade que excede o estoque
    disponível no evento (o restante é entregue na hora). ``backorder_limit``:
    ``-1`` (padrão) = sem limite; ``0`` = nenhuma entrega pendente permitida;
    ``> 0`` = total de unidades pendentes permitidas no evento. Levanta
    ``ValueError`` quando (pendentes já confirmados + novas unidades pendentes)
    excede o limite.
    """
    if not demand:
        return
    pids = list(demand.keys())
    placeholders = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""
        SELECT product_id, stock, backorder_limit
          FROM event_products
         WHERE event_id = ? AND product_id IN ({placeholders})
        """,
        (int(event_id), *pids),
    ).fetchall()
    limits_by_pid = {
        int(r["product_id"]): (int(r["stock"] or 0), int(r["backorder_limit"] if r["backorder_limit"] is not None else -1))
        for r in rows
    }

    new_backorder: Dict[int, int] = {}
    for pid, qty in demand.items():
        stock, limit = limits_by_pid.get(pid, (0, -1))
        if limit < 0:
            continue
        needed = qty - stock
        if needed > 0:
            new_backorder[pid] = needed

    if not new_backorder:
        return

    existing_pending = _pending_delivery_units_by_product_for_event_conn(
        conn, event_id, new_backorder.keys(),
    )
    for pid, needed in new_backorder.items():
        _, limit = limits_by_pid.get(pid, (0, -1))
        already = existing_pending.get(pid, 0)
        if already + needed > limit:
            sku = _product_sku_label(pid, sku=(sku_by_id or {}).get(pid))
            if limit == 0:
                raise ValueError(
                    f"Produto {sku}: entrega pendente não é permitida para este produto neste evento."
                )
            raise ValueError(
                f"Produto {sku}: limite de entrega pendente atingido neste evento "
                f"(limite {limit} un., já pendente {already} un.)."
            )


def list_pending_delivery_ledger_rows(
    event_id: int,
    product_id: int,
    *,
    reference: Optional[str] = None,
    seller_id: Optional[int] = None,
) -> List[Dict]:
    """Linhas sintéticas de histórico (tipo ``pendente``) ainda sem baixa de estoque.

    Após a confirmação da entrega, o sistema grava movimentação real ``venda``
    com motivo ``entrega pendente`` — estas linhas deixam de aparecer.
    """
    params: List = [int(event_id), int(product_id)]
    extra = ""
    ref_norm = _normalize_order_reference(reference)
    if ref_norm:
        extra += (
            " AND t.order_number IS NOT NULL "
            "AND INSTR(LOWER(t.order_number), LOWER(?)) > 0"
        )
        params.append(ref_norm)
    if seller_id is not None:
        extra += " AND COALESCE(t.seller_id, -1) = ?"
        params.append(int(seller_id))

    sql = f"""
        SELECT ti.id AS item_id,
               ti.quantity,
               ti.quantity_delivered,
               ti.product_id,
               t.id AS transaction_id,
               t.order_number,
               t.created_at,
               t.seller_name,
               ep.stock AS event_stock
          FROM transaction_items ti
          JOIN transactions t ON t.id = ti.transaction_id
          LEFT JOIN event_products ep
            ON ep.event_id = t.event_id
           AND ep.product_id = CAST(ti.product_id AS INTEGER)
         WHERE t.event_id = ?
           AND CAST(ti.product_id AS INTEGER) = ?
           AND LOWER(TRIM(COALESCE(t.status, ''))) = 'confirmado'
           AND (ti.quantity - COALESCE(ti.quantity_delivered, 0)) > 0
           {extra}
         ORDER BY t.created_at DESC, ti.id DESC
    """
    rows: List[Dict] = []
    with get_conn() as conn:
        for r in conn.execute(sql, params).fetchall():
            pending = int(r["quantity"] or 0) - int(r["quantity_delivered"] or 0)
            if pending <= 0:
                continue
            order_number = (r["order_number"] or "").strip() or f"#{r['transaction_id']}"
            seller_name = (r["seller_name"] or "").strip()
            created_by = f"vendedor:{seller_name}" if seller_name else "totem"
            balance = int(r["event_stock"] or 0) if r["event_stock"] is not None else None
            rows.append(
                {
                    "id": f"pending-{int(r['item_id'])}",
                    "product_id": int(product_id),
                    "event_id": int(event_id),
                    "movement_type": PENDING_MOVEMENT_TYPE,
                    "quantity": pending,
                    "delta": -pending,
                    "balance_after": balance if balance is not None else 0,
                    "unit_cost": None,
                    "reason": "entrega pendente",
                    "reference": order_number,
                    "transaction_id": int(r["transaction_id"]),
                    "created_by": created_by,
                    "created_at": r["created_at"],
                    "is_pending_delivery": True,
                    "pending_item_id": int(r["item_id"]),
                }
            )
    return rows


def _ledger_sort_key(row: Dict) -> Tuple:
    return (str(row.get("created_at") or ""), str(row.get("id") or ""))


def count_event_product_ledger(
    event_id: int,
    product_id: int,
    *,
    movement_type: Optional[str] = None,
    reference: Optional[str] = None,
    seller_id: Optional[int] = None,
) -> int:
    """Conta movimentações reais + linhas sintéticas de entrega pendente."""
    from .stock import count_stock_movements, normalize_movement_type_filter

    mt_raw = (movement_type or "").strip().lower()
    if mt_raw in ("", "todos"):
        mt = None
    elif mt_raw == PENDING_MOVEMENT_TYPE:
        mt = PENDING_MOVEMENT_TYPE
    else:
        mt = normalize_movement_type_filter(mt_raw)

    pending_n = 0
    if mt in (None, PENDING_MOVEMENT_TYPE):
        pending_n = len(
            list_pending_delivery_ledger_rows(
                event_id,
                product_id,
                reference=reference,
                seller_id=seller_id,
            )
        )
    if mt == PENDING_MOVEMENT_TYPE:
        return pending_n

    real_n = count_stock_movements(
        product_id=product_id,
        event_id=event_id,
        movement_type=mt,
        reference=reference,
        seller_id=seller_id,
    )
    if mt is None:
        return real_n + pending_n
    return real_n


def list_event_product_ledger(
    event_id: int,
    product_id: int,
    *,
    movement_type: Optional[str] = None,
    reference: Optional[str] = None,
    seller_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict]:
    """Lista o histórico do produto no evento, incluindo entregas ainda pendentes."""
    from .stock import list_stock_movements, normalize_movement_type_filter

    mt_raw = (movement_type or "").strip().lower()
    if mt_raw in ("", "todos"):
        mt = None
    elif mt_raw == PENDING_MOVEMENT_TYPE:
        mt = PENDING_MOVEMENT_TYPE
    else:
        mt = normalize_movement_type_filter(mt_raw)

    lim = max(0, int(limit))
    off = max(0, int(offset))

    pending: List[Dict] = []
    if mt in (None, PENDING_MOVEMENT_TYPE):
        pending = list_pending_delivery_ledger_rows(
            event_id,
            product_id,
            reference=reference,
            seller_id=seller_id,
        )

    if mt == PENDING_MOVEMENT_TYPE:
        return pending[off : off + lim]

    if mt is not None:
        return list_stock_movements(
            product_id=product_id,
            event_id=event_id,
            movement_type=mt,
            reference=reference,
            seller_id=seller_id,
            limit=lim,
            offset=off,
        )

    # ``todos``: mescla reais + pendentes e pagina em memória (escopo produto×evento).
    real = list_stock_movements(
        product_id=product_id,
        event_id=event_id,
        movement_type=None,
        reference=reference,
        seller_id=seller_id,
        limit=10_000,
        offset=0,
    )
    merged = sorted(
        list(real) + pending,
        key=_ledger_sort_key,
        reverse=True,
    )
    return merged[off : off + lim]


def get_pending_transaction_if_owned(transaction_id: int, seller_id: int) -> Optional[Dict]:
    """Retorna a linha mínima da transação **pendente** se pertencer ao vendedor."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, order_number, status, seller_id
              FROM transactions
             WHERE id = ? AND seller_id = ? AND status = 'pendente'
            """,
            (int(transaction_id), int(seller_id)),
        ).fetchone()
    return dict(row) if row else None


def get_pending_transaction_restore_payload(tx_id: int, seller_id: int) -> Optional[Dict]:
    """Monta carrinho + dados de cliente (sessionStorage) para retomar checkout de um pedido pendente."""
    tx = get_transaction(int(tx_id))
    if not tx:
        return None
    if int(tx.get("seller_id") or 0) != int(seller_id):
        return None
    if str(tx.get("status") or "").lower() != "pendente":
        return None

    event_raw = tx.get("event_id")
    event_id = int(event_raw) if event_raw is not None else None

    promo_map: Dict[int, Dict] = {}
    if event_id is not None:
        promos = get_active_promotions_for_event(event_id)
        promo_map = build_promo_display_map(promos)

    cart_items: List[Dict] = []
    with get_conn() as conn:
        for it in tx.get("items") or []:
            pid_raw = it.get("product_id")
            try:
                pid = int(pid_raw)
            except (TypeError, ValueError):
                continue
            pr = conn.execute(
                """
                SELECT id, sku, name, category, price, image, stock, active
                  FROM products
                 WHERE id = ?
                """,
                (pid,),
            ).fetchone()
            if pr is None:
                continue
            qty = int(it.get("quantity") or 0)
            if qty <= 0:
                continue
            imagem = pr["image"] or ""
            if event_id is not None:
                ep = conn.execute(
                    """
                    SELECT stock FROM event_products
                     WHERE event_id = ? AND product_id = ?
                    """,
                    (int(event_id), pid),
                ).fetchone()
                estoque = int(ep["stock"] or 0) if ep else 0
            else:
                estoque = int(pr["stock"] or 0)

            list_p = float(it.get("original_price") or pr["price"] or 0)
            unit_p = float(it.get("unit_price") or list_p)
            subtotal = float(it.get("subtotal") or round(unit_p * qty, 2))
            promo_nome = (it.get("promotion_name") or "").strip()
            has_promo = it.get("promotion_id") is not None and subtotal < round(list_p * qty, 2) - 0.001

            entry: Dict = {
                    "id": pid,
                    "sku": (pr["sku"] or "").strip(),
                "nome": it.get("product_name") or pr["name"],
                "categoria": it.get("category") or pr["category"] or "",
                "preco_lista": list_p,
                "preco": unit_p,
                "subtotal": subtotal,
                    "imagem": imagem,
                    "estoque": estoque,
                    "quantidade": qty,
                "em_promocao": has_promo or bool(promo_nome),
                "promo_nome": promo_nome,
                "promo_aplicada": has_promo,
                "economia": round(max(0.0, list_p * qty - subtotal), 2),
            }

            if promo_map:
                stub = {
                    "id": pid,
                    "preco": list_p,
                    "preco_original": list_p,
                    "em_promocao": False,
                }
                enriched = enrich_product_with_promo(stub, promo_map)
                if enriched.get("em_promocao"):
                    entry["em_promocao"] = True
                    entry["promo_tipo"] = enriched.get("promo_tipo") or ""
                    entry["promo_rule_value"] = enriched.get("promo_rule_value") or 0
                    entry["promo_min_qty"] = enriched.get("promo_min_qty") or 1
                    entry["promo_free_qty"] = enriched.get("promo_free_qty") or 0
                    entry["promo_badge"] = enriched.get("promo_badge") or ""
                    if not entry["promo_nome"]:
                        entry["promo_nome"] = enriched.get("promo_nome") or ""

            cart_items.append(entry)

    pm = (tx.get("payment_method") or "cartao").strip().lower()
    if pm not in ("pix", "cartao", "dinheiro"):
        pm = "cartao"
    installments_raw = tx.get("card_installments")
    try:
        installments = max(1, int(installments_raw))
    except (TypeError, ValueError):
        installments = 1

    client_payload = {
        "name": (tx.get("client_name") or "").strip(),
        "cpf": (tx.get("client_cpf") or "").strip(),
        "email": (tx.get("client_email") or "").strip(),
        "phone": (tx.get("client_phone") or "").strip(),
        "cro_uf": (tx.get("client_cro_uf") or "").strip(),
        "cro_numero": (tx.get("client_cro_numero") or "").strip(),
        "zipcode": (tx.get("client_zipcode") or "").strip(),
        "address": (tx.get("client_address") or "").strip(),
        "number": (tx.get("client_number") or "").strip(),
        "complement": (tx.get("client_complement") or "").strip(),
        "city": (tx.get("client_city") or "").strip(),
        "state": (tx.get("client_state") or "").strip(),
        "payment_method": pm,
        "installments": installments,
    }

    return {
        "transaction_id": int(tx["id"]),
        "order_number": tx.get("order_number"),
        "cart_items": cart_items,
        "client_data": client_payload,
    }


def cancel_pending_transaction_for_seller(tx_id: int, seller_id: int) -> Dict:
    """Marca uma transação **pendente** como ``cancelado`` (somente o vendedor dono)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, seller_id, status FROM transactions WHERE id = ?",
            (int(tx_id),),
        ).fetchone()
        if row is None:
            raise ValueError("Transação não encontrada.")
        if int(row["seller_id"] or 0) != int(seller_id):
            raise ValueError("Você não pode alterar esta transação.")
        if str(row["status"] or "").lower() != "pendente":
            raise ValueError("Somente pedidos pendentes podem ser descartados.")
        conn.execute(
            "UPDATE transactions SET status = 'cancelado' WHERE id = ?",
            (int(tx_id),),
        )
    return {"id": int(tx_id), "status": "cancelado"}


def _items_for(conn: sqlite3.Connection, tx_id: int) -> List[Dict]:
    rows = conn.execute(
        """
        SELECT ti.id, ti.product_id, ti.product_name, ti.category,
               ti.unit_price, ti.quantity, ti.subtotal, ti.product_sku,
               ti.quantity_delivered,
               ti.original_price, ti.promotion_id,
               pr.name AS promotion_name
          FROM transaction_items ti
          LEFT JOIN promotions pr ON pr.id = ti.promotion_id
         WHERE ti.transaction_id = ?
         ORDER BY ti.id
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
                   seller_id, seller_name, payment_method, card_installments, aut,
                   event_id,
                   client_name, client_cpf, client_email, client_phone, client_zipcode, client_address,
                   client_number, client_complement, client_city, client_state,
                   client_cro_uf, client_cro_numero, delivery_status,
                   handover_status, handover_confirmed_at
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


def _delivery_filter_sql_parts(delivery: Optional[str]) -> List[str]:
    """Cláusulas do filtro de entrega futura (aplicado só a transações confirmadas).

    - ``parcial``: pedidos confirmados com itens aguardando retirada.
    - ``completa``: mantido por compatibilidade com URLs antigas.
    """
    dv = (delivery or "").strip().lower()
    if dv == "completa":
        return [
            "LOWER(TRIM(COALESCE(t.status, ''))) = 'confirmado'",
            "COALESCE(t.delivery_status, 'completa') = 'completa'",
        ]
    if dv == "parcial":
        return [
            "LOWER(TRIM(COALESCE(t.status, ''))) = 'confirmado'",
            "COALESCE(t.delivery_status, 'completa') IN ('parcial', 'pendente')",
        ]
    return []


def _transactions_event_filter_sql_params(
    event_id: int,
    *,
    seller_id: Optional[int] = None,
    order_search: Optional[str] = None,
    status: Optional[str] = None,
    on_date: Optional[str] = None,
    delivery: Optional[str] = None,
) -> Tuple[str, List]:
    """Trecho ``WHERE ...`` (sem a palavra-chave) + parâmetros para transações do evento."""
    parts = ["t.event_id = ?"]
    params: List = [int(event_id)]
    if seller_id is not None:
        parts.append("t.seller_id = ?")
        params.append(int(seller_id))
    ref = _normalize_order_reference(order_search)
    if ref:
        parts.append(
            "("
            "(t.order_number IS NOT NULL AND INSTR(LOWER(t.order_number), LOWER(?)) > 0)"
            " OR "
            "(t.client_name IS NOT NULL AND INSTR(LOWER(t.client_name), LOWER(?)) > 0)"
            ")"
        )
        params.extend([ref, ref])
    st = (status or "").strip().lower()
    if st and st != "todos" and st in TX_FILTER_STATUSES:
        if st == "entregue":
            parts.append(
                "LOWER(TRIM(COALESCE(t.handover_status, ''))) = 'entregue'"
            )
        else:
            parts.append("LOWER(TRIM(COALESCE(t.status, ''))) = ?")
            params.append(st)
    if on_date:
        parts.append("DATE(t.created_at) = DATE(?)")
        params.append(on_date)
    parts.extend(_delivery_filter_sql_parts(delivery))
    return " AND ".join(parts), params


def count_transactions_for_event(
    event_id: int,
    *,
    seller_id: Optional[int] = None,
    order_search: Optional[str] = None,
    status: Optional[str] = None,
    on_date: Optional[str] = None,
    delivery: Optional[str] = None,
) -> int:
    """Conta transações ligadas ao ``event_id`` (coluna ``transactions.event_id``)."""
    wh, params = _transactions_event_filter_sql_params(
        event_id,
        seller_id=seller_id,
        order_search=order_search,
        status=status,
        on_date=on_date,
        delivery=delivery,
    )
    sql = f"SELECT COUNT(*) AS c FROM transactions t WHERE {wh}"
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["c"] if row else 0)


def list_transactions_for_event(
    event_id: int,
    *,
    seller_id: Optional[int] = None,
    order_search: Optional[str] = None,
    status: Optional[str] = None,
    on_date: Optional[str] = None,
    delivery: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
) -> List[Dict]:
    """Lista transações do evento com itens (mesmo formato que ``list_transactions``)."""
    wh, params = _transactions_event_filter_sql_params(
        event_id,
        seller_id=seller_id,
        order_search=order_search,
        status=status,
        on_date=on_date,
        delivery=delivery,
    )
    lim = max(1, int(limit))
    off = max(0, int(offset))
    sql = f"""
        SELECT id, order_number, created_at, total, items_count, status,
               seller_id, seller_name, payment_method, card_installments, aut,
               client_name, client_cpf, client_email, client_phone, client_zipcode, client_address,
               client_number, client_complement, client_city, client_state,
               client_cro_uf, client_cro_numero, delivery_status,
               handover_status, handover_confirmed_at
          FROM transactions t
         WHERE {wh}
         ORDER BY datetime(t.created_at) DESC, t.id DESC
         LIMIT ? OFFSET ?
    """
    qparams = params + [lim, off]
    with get_conn() as conn:
        tx_rows = conn.execute(sql, qparams).fetchall()
        results: List[Dict] = []
        for tx in tx_rows:
            tx_dict = dict(tx)
            tx_dict["items"] = _items_for(conn, tx["id"])
            results.append(tx_dict)
        return results


def _transactions_seller_scope_filter_sql_params(
    seller_id: int,
    *,
    order_search: Optional[str] = None,
    status: Optional[str] = None,
    on_date: Optional[str] = None,
    delivery: Optional[str] = None,
) -> Tuple[str, List]:
    """Trecho ``WHERE ...`` para transações de um único vendedor (qualquer ``event_id``)."""
    parts = ["t.seller_id = ?"]
    params: List = [int(seller_id)]
    ref = _normalize_order_reference(order_search)
    if ref:
        parts.append(
            "("
            "(t.order_number IS NOT NULL AND INSTR(LOWER(t.order_number), LOWER(?)) > 0)"
            " OR "
            "(t.client_name IS NOT NULL AND INSTR(LOWER(t.client_name), LOWER(?)) > 0)"
            ")"
        )
        params.extend([ref, ref])
    st = (status or "").strip().lower()
    if st and st != "todos" and st in TX_FILTER_STATUSES:
        if st == "entregue":
            parts.append(
                "LOWER(TRIM(COALESCE(t.handover_status, ''))) = 'entregue'"
            )
        else:
            parts.append("LOWER(TRIM(COALESCE(t.status, ''))) = ?")
            params.append(st)
    if on_date:
        parts.append("DATE(t.created_at) = DATE(?)")
        params.append(on_date)
    parts.extend(_delivery_filter_sql_parts(delivery))
    return " AND ".join(parts), params


def count_transactions_for_seller(
    seller_id: int,
    *,
    order_search: Optional[str] = None,
    status: Optional[str] = None,
    on_date: Optional[str] = None,
    delivery: Optional[str] = None,
) -> int:
    """Conta transações em que ``seller_id`` coincide (catálogo global / sem filtro de evento)."""
    wh, params = _transactions_seller_scope_filter_sql_params(
        int(seller_id),
        order_search=order_search,
        status=status,
        on_date=on_date,
        delivery=delivery,
    )
    sql = f"SELECT COUNT(*) AS c FROM transactions t WHERE {wh}"
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["c"] if row else 0)


def list_transactions_for_seller(
    seller_id: int,
    *,
    order_search: Optional[str] = None,
    status: Optional[str] = None,
    on_date: Optional[str] = None,
    delivery: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
) -> List[Dict]:
    """Lista transações do vendedor com itens (mesmo formato que ``list_transactions_for_event``)."""
    wh, params = _transactions_seller_scope_filter_sql_params(
        int(seller_id),
        order_search=order_search,
        status=status,
        on_date=on_date,
        delivery=delivery,
    )
    lim = max(1, int(limit))
    off = max(0, int(offset))
    sql = f"""
        SELECT id, order_number, created_at, total, items_count, status,
               seller_id, seller_name, payment_method, card_installments, aut,
               event_id,
               client_name, client_cpf, client_email, client_phone, client_zipcode, client_address,
               client_number, client_complement, client_city, client_state,
               client_cro_uf, client_cro_numero, delivery_status,
               handover_status, handover_confirmed_at
          FROM transactions t
         WHERE {wh}
         ORDER BY datetime(t.created_at) DESC, t.id DESC
         LIMIT ? OFFSET ?
    """
    qparams = params + [lim, off]
    with get_conn() as conn:
        tx_rows = conn.execute(sql, qparams).fetchall()
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
    - Zera ``products.stock`` (cadastro) e ``event_products.stock`` (saldo por evento).
    - Não recria linhas de movimentação após o zeramento (histórico fica vazio).
    """
    with get_conn() as conn:
        n_tx_row = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()
        n_tx_before = int(n_tx_row["c"] or 0)

        conn.execute("DELETE FROM transactions")

        cur = conn.execute("DELETE FROM stock_movements")
        n_mov_deleted = int(cur.rowcount or 0)

        now = _now_iso()
        prod_rows = conn.execute("SELECT id FROM products").fetchall()
        for r in prod_rows:
            pid = int(r["id"])
            conn.execute(
                "UPDATE products SET stock = 0, updated_at = ? WHERE id = ?",
                (now, pid),
            )

        conn.execute(
            "UPDATE event_products SET stock = 0, updated_at = ?",
            (now,),
        )
        ep_rows = conn.execute(
            "SELECT event_id, product_id FROM event_products"
        ).fetchall()

        return {
            "transactions_deleted": n_tx_before,
            "movements_deleted": n_mov_deleted,
            "products_restored": len(prod_rows),
            "event_product_pairs_reset": len(ep_rows),
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
