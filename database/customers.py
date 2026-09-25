"""Perfil analítico de clientes derivado das vendas.

Não existe cadastro de clientes no totem: o cliente é informado no checkout de
cada venda. Este módulo reconstrói a carteira agrupando ``transactions`` por
**identidade** — CPF quando disponível (é o campo mais confiável: preenchido em
100% das vendas confirmadas), com fallback em e-mail, telefone e, por último,
nome normalizado.

O CPF é comparado apenas pelos dígitos (``082.628.475-20`` e ``08262847520``
são o mesmo cliente) e CPFs-sentinela de dígito repetido (``000.000.000-00``)
são ignorados como identidade — senão todas as vendas com CPF de teste virariam
um único "cliente" fantasma.

Receita considera apenas ``status = 'confirmado'`` (mesma convenção do
Financeiro e de ``get_stats``); estornos, cancelamentos e pendências são
contabilizados à parte para não inflar o faturamento.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .connection import get_conn

# Ordem de precedência da identidade: do campo mais confiável para o menos.
IDENTITY_KINDS = ("cpf", "email", "phone", "name")

# CPFs de dígito repetido são placeholder de teste, não identidade real.
_PLACEHOLDER_CPFS = tuple(str(d) * 11 for d in range(10))
# Literais gerados em código (nunca entrada do usuário) — seguro interpolar.
_CPF_PLACEHOLDER_SQL = ", ".join(f"'{cpf}'" for cpf in _PLACEHOLDER_CPFS)

# Normaliza os campos de cliente e deriva (ident_kind, ident_value) por venda.
_IDENT_CTE = f"""
WITH norm AS (
    SELECT
        t.id, t.order_number, t.created_at, t.total, t.items_count,
        t.event_id, t.seller_id, t.seller_name, t.payment_method,
        t.card_installments, t.delivery_status, t.handover_status,
        t.client_name, t.client_cpf, t.client_email, t.client_phone,
        t.client_cro_uf, t.client_cro_numero, t.client_city, t.client_state,
        LOWER(TRIM(COALESCE(t.status, ''))) AS status_norm,
        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(t.client_cpf, ''),
            '.', ''), '-', ''), '/', ''), ' ', '') AS cpf_digits,
        LOWER(TRIM(COALESCE(t.client_email, ''))) AS email_norm,
        REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(t.client_phone, ''),
            '(', ''), ')', ''), '-', ''), ' ', ''), '+', ''), '.', '') AS phone_digits,
        LOWER(TRIM(COALESCE(t.client_name, ''))) AS name_norm
      FROM transactions t
),
ident AS (
    SELECT *,
        CASE
            WHEN LENGTH(cpf_digits) >= 11
             AND cpf_digits NOT IN ({_CPF_PLACEHOLDER_SQL}) THEN 'cpf'
            WHEN email_norm <> ''                           THEN 'email'
            WHEN LENGTH(phone_digits) >= 10                 THEN 'phone'
            WHEN name_norm <> ''                            THEN 'name'
            ELSE ''
        END AS ident_kind,
        CASE
            WHEN LENGTH(cpf_digits) >= 11
             AND cpf_digits NOT IN ({_CPF_PLACEHOLDER_SQL}) THEN cpf_digits
            WHEN email_norm <> ''                           THEN email_norm
            WHEN LENGTH(phone_digits) >= 10                 THEN phone_digits
            WHEN name_norm <> ''                            THEN name_norm
            ELSE ''
        END AS ident_value
      FROM norm
)
"""

# Agregado por cliente sobre vendas confirmadas + contadores dos outros status.
# ``avg_interval_days`` = média de dias entre compras consecutivas; fica NULL
# para quem comprou uma única vez (não existe intervalo a medir).
_AGG_SQL = """
agg AS (
    SELECT
        ident_kind,
        ident_value,
        COUNT(*)                            AS orders_count,
        COALESCE(SUM(total), 0)             AS revenue,
        COALESCE(SUM(items_count), 0)       AS units,
        MIN(created_at)                     AS first_purchase_at,
        MAX(created_at)                     AS last_purchase_at,
        MAX(total)                          AS max_order_total,
        COUNT(DISTINCT event_id)            AS events_count,
        CASE WHEN COUNT(*) > 1
             THEN (julianday(MAX(created_at)) - julianday(MIN(created_at)))
                  / (COUNT(*) - 1)
        END                                 AS avg_interval_days,
        MAX(NULLIF(TRIM(COALESCE(client_email, '')), '')) AS any_email,
        MAX(NULLIF(TRIM(COALESCE(client_phone, '')), '')) AS any_phone
      FROM ident
     WHERE ident_kind <> '' AND status_norm = 'confirmado'{event_clause}
     GROUP BY ident_kind, ident_value
),
latest AS (
    SELECT ident_kind, ident_value, client_name, client_cpf,
           client_email, client_phone, client_cro_uf, client_cro_numero,
           client_city, client_state,
           ROW_NUMBER() OVER (
               PARTITION BY ident_kind, ident_value
               ORDER BY created_at DESC, id DESC
           ) AS rn
      FROM ident
     WHERE ident_kind <> '' AND status_norm = 'confirmado'{event_clause}
),
other AS (
    SELECT
        ident_kind,
        ident_value,
        SUM(CASE WHEN status_norm = 'estornado' THEN 1 ELSE 0 END)     AS refunded_count,
        SUM(CASE WHEN status_norm = 'estornado' THEN total ELSE 0 END)  AS refunded_value,
        SUM(CASE WHEN status_norm = 'pendente'  THEN 1 ELSE 0 END)     AS pending_count,
        SUM(CASE WHEN status_norm = 'cancelado' THEN 1 ELSE 0 END)     AS cancelled_count
      FROM ident
     WHERE ident_kind <> ''{event_clause}
     GROUP BY ident_kind, ident_value
)
"""

# Allowlist de ordenação: a chave vem da querystring, nunca o SQL.
_SORTS = {
    "revenue": "a.revenue DESC, a.orders_count DESC",
    "orders": "a.orders_count DESC, a.revenue DESC",
    "recent": "a.last_purchase_at DESC",
    "oldest": "a.first_purchase_at ASC",
    "ticket": "(a.revenue / a.orders_count) DESC",
    "frequency": "a.avg_interval_days IS NULL, a.avg_interval_days ASC",
    "name": "LOWER(TRIM(COALESCE(l.client_name, ''))) ASC",
}
DEFAULT_CUSTOMER_SORT = "revenue"
CUSTOMER_SORTS = tuple(_SORTS)


def _only_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def normalize_customer_ident(kind: str, ident: Any) -> Tuple[str, str]:
    """Normaliza ``(kind, ident)`` da URL para a forma usada no agrupamento.

    Permite que ``/admin/clientes/cpf/082.628.475-20`` e
    ``/admin/clientes/cpf/08262847520`` resolvam o mesmo perfil.
    """
    k = (kind or "").strip().lower()
    if k not in IDENTITY_KINDS:
        return "", ""
    raw = str(ident or "").strip()
    if k == "cpf":
        return k, _only_digits(raw)
    if k == "phone":
        return k, _only_digits(raw)
    if k == "email":
        return k, raw.lower()
    return k, " ".join(raw.lower().split())


def get_customer_display_name(kind: str, ident: Any) -> str:
    """Nome da venda mais recente do cliente (``""`` se não achar).

    Consulta enxuta para a trilha de navegação — evita montar o perfil inteiro
    só para rotular o breadcrumb.
    """
    norm_kind, norm_ident = normalize_customer_ident(kind, ident)
    if not norm_kind or not norm_ident:
        return ""
    with get_conn() as conn:
        row = conn.execute(
            f"""
            {_IDENT_CTE}
            SELECT client_name
              FROM ident
             WHERE ident_kind = ? AND ident_value = ?
               AND status_norm = 'confirmado'
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (norm_kind, norm_ident),
        ).fetchone()
    return (row["client_name"] or "").strip() if row else ""


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _days_since(value: Any) -> Optional[float]:
    dt = _parse_dt(value)
    if dt is None:
        return None
    return max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)


def _share(part: Any, whole: Any) -> float:
    try:
        total = float(whole or 0)
        if total <= 0:
            return 0.0
        return round(float(part or 0) / total * 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _event_clause(event_id: Optional[int]) -> str:
    return " AND event_id = ?" if event_id else ""


def _event_params(event_id: Optional[int], times: int) -> List[Any]:
    return [int(event_id)] * times if event_id else []


def _scoped_revenue(conn, event_id: Optional[int]) -> Dict[str, Any]:
    """Faturamento confirmado do escopo (evento ou sistema inteiro).

    É o denominador do "percentual do faturamento" de cada cliente.
    """
    clause = " AND event_id = ?" if event_id else ""
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(total), 0)       AS revenue,
               COUNT(*)                      AS orders,
               COALESCE(SUM(items_count), 0) AS units
          FROM transactions
         WHERE LOWER(TRIM(COALESCE(status, ''))) = 'confirmado'{clause}
        """,
        _event_params(event_id, 1),
    ).fetchone()
    return {
        "revenue": float(row["revenue"] or 0),
        "orders": int(row["orders"] or 0),
        "units": int(row["units"] or 0),
    }


def _customer_row(row, total_revenue: float) -> Dict[str, Any]:
    orders = int(row["orders_count"] or 0)
    revenue = float(row["revenue"] or 0)
    name = (row["client_name"] or "").strip()
    interval = row["avg_interval_days"]
    return {
        "kind": row["ident_kind"],
        "ident": row["ident_value"],
        "name": name or "(sem nome)",
        "cpf": (row["client_cpf"] or "").strip(),
        "email": (row["client_email"] or row["any_email"] or "").strip(),
        "phone": (row["client_phone"] or row["any_phone"] or "").strip(),
        "cro_uf": (row["client_cro_uf"] or "").strip(),
        "cro_numero": (row["client_cro_numero"] or "").strip(),
        "city": (row["client_city"] or "").strip(),
        "state": (row["client_state"] or "").strip(),
        "orders_count": orders,
        "units": int(row["units"] or 0),
        "revenue": revenue,
        "avg_ticket": round(revenue / orders, 2) if orders else 0.0,
        "max_order_total": float(row["max_order_total"] or 0),
        "events_count": int(row["events_count"] or 0),
        "first_purchase_at": row["first_purchase_at"],
        "last_purchase_at": row["last_purchase_at"],
        "days_since_last": _days_since(row["last_purchase_at"]),
        "avg_interval_days": float(interval) if interval is not None else None,
        "revenue_share": _share(revenue, total_revenue),
        "refunded_count": int(row["refunded_count"] or 0),
        "refunded_value": float(row["refunded_value"] or 0),
        "pending_count": int(row["pending_count"] or 0),
        "cancelled_count": int(row["cancelled_count"] or 0),
        "is_recurring": orders > 1,
    }


def list_customers(
    *,
    query: Optional[str] = None,
    event_id: Optional[int] = None,
    sort: str = DEFAULT_CUSTOMER_SORT,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    """Carteira de clientes agregada, com paginação, busca e ordenação.

    ``event_id`` restringe **e escopa** as métricas àquele evento (inclusive o
    denominador do percentual de faturamento), o que faz o percentual somar 100%
    dentro da página filtrada.

    Retorna ``{"rows": [...], "total": N, "summary": {...}}``.
    """
    sort_key = sort if sort in _SORTS else DEFAULT_CUSTOMER_SORT
    order_by = _SORTS[sort_key]
    ev_clause = _event_clause(event_id)
    agg_sql = _AGG_SQL.format(event_clause=ev_clause)

    term = " ".join((query or "").lower().split())
    search_clause = ""
    search_params: List[Any] = []
    if term:
        like = f"%{term}%"
        conditions = [
            "LOWER(TRIM(COALESCE(l.client_name, ''))) LIKE ?",
            "LOWER(TRIM(COALESCE(l.client_email, ''))) LIKE ?",
            "LOWER(TRIM(COALESCE(l.client_cro_numero, ''))) LIKE ?",
        ]
        search_params = [like, like, like]
        # CPF/telefone só entram na busca quando o termo tem dígitos — senão
        # um "%%" em cima do campo casaria com todo mundo.
        digits = _only_digits(term)
        if digits:
            digit_like = f"%{digits}%"
            conditions.append(
                "REPLACE(REPLACE(REPLACE(COALESCE(l.client_cpf, ''), '.', ''), '-', ''), ' ', '') LIKE ?"
            )
            conditions.append(
                "REPLACE(REPLACE(REPLACE(COALESCE(l.client_phone, ''), '(', ''), ')', ''), '-', '') LIKE ?"
            )
            search_params += [digit_like, digit_like]
        search_clause = " AND (" + " OR ".join(conditions) + ")"

    # ``event_clause`` aparece 3x no bloco de CTEs (agg, latest, other).
    cte_params = _event_params(event_id, 3)
    base = f"""
        {_IDENT_CTE},
        {agg_sql}
        SELECT a.*, l.client_name, l.client_cpf, l.client_email, l.client_phone,
               l.client_cro_uf, l.client_cro_numero, l.client_city, l.client_state,
               COALESCE(o.refunded_count, 0)  AS refunded_count,
               COALESCE(o.refunded_value, 0)  AS refunded_value,
               COALESCE(o.pending_count, 0)   AS pending_count,
               COALESCE(o.cancelled_count, 0) AS cancelled_count
          FROM agg a
          JOIN latest l
            ON l.ident_kind = a.ident_kind
           AND l.ident_value = a.ident_value
           AND l.rn = 1
          LEFT JOIN other o
            ON o.ident_kind = a.ident_kind
           AND o.ident_value = a.ident_value
         WHERE 1 = 1{search_clause}
    """

    with get_conn() as conn:
        scope = _scoped_revenue(conn, event_id)
        total_revenue = scope["revenue"]

        counted = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({base})",
            cte_params + search_params,
        ).fetchone()
        total = int(counted["n"] or 0)

        sql = f"{base} ORDER BY {order_by}"
        params = cte_params + search_params
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = params + [int(limit), max(0, int(offset))]
        rows = [
            _customer_row(r, total_revenue)
            for r in conn.execute(sql, params).fetchall()
        ]

        # Resumo da carteira inteira (ignora busca/paginação, respeita o evento).
        overview = conn.execute(
            f"""
            {_IDENT_CTE},
            {agg_sql}
            SELECT COUNT(*)                                   AS customers,
                   COALESCE(SUM(a.orders_count), 0)           AS orders,
                   COALESCE(SUM(a.revenue), 0)                AS revenue,
                   COALESCE(SUM(a.units), 0)                  AS units,
                   SUM(CASE WHEN a.orders_count > 1 THEN 1 ELSE 0 END) AS recurring,
                   MAX(a.revenue)                             AS top_revenue,
                   AVG(a.avg_interval_days)                   AS avg_interval_days
              FROM agg a
            """,
            cte_params,
        ).fetchone()

    customers = int(overview["customers"] or 0)
    orders = int(overview["orders"] or 0)
    revenue = float(overview["revenue"] or 0)
    recurring = int(overview["recurring"] or 0)
    avg_interval = overview["avg_interval_days"]
    summary = {
        "customers": customers,
        "orders": orders,
        "revenue": revenue,
        "units": int(overview["units"] or 0),
        "avg_ticket": round(revenue / orders, 2) if orders else 0.0,
        "avg_revenue_per_customer": round(revenue / customers, 2) if customers else 0.0,
        "avg_orders_per_customer": round(orders / customers, 2) if customers else 0.0,
        "recurring": recurring,
        "recurring_share": _share(recurring, customers),
        "one_time": max(0, customers - recurring),
        "top_share": _share(overview["top_revenue"], revenue),
        "avg_interval_days": float(avg_interval) if avg_interval is not None else None,
        "scope_revenue": total_revenue,
        "scope_orders": scope["orders"],
        # Vendas sem identidade aproveitável não entram na carteira.
        "unidentified_orders": max(0, scope["orders"] - orders),
    }
    return {"rows": rows, "total": total, "summary": summary, "sort": sort_key}


def _profile_orders(conn, kind: str, ident: str) -> List[Dict[str, Any]]:
    """Histórico completo do cliente (todos os status), do mais recente ao antigo."""
    rows = conn.execute(
        f"""
        {_IDENT_CTE}
        SELECT i.id, i.order_number, i.created_at, i.total, i.items_count,
               i.status_norm AS status, i.payment_method, i.card_installments,
               i.delivery_status, i.handover_status, i.event_id, i.seller_id,
               i.seller_name, e.name AS event_name, e.badge_color AS event_badge_color
          FROM ident i
          LEFT JOIN events e ON e.id = i.event_id
         WHERE i.ident_kind = ? AND i.ident_value = ?
         ORDER BY i.created_at DESC, i.id DESC
        """,
        (kind, ident),
    ).fetchall()

    orders = [dict(r) for r in rows]
    # Dias desde a compra confirmada anterior (a lista está em ordem decrescente).
    confirmed = [o for o in orders if o["status"] == "confirmado"]
    for newer, older in zip(confirmed, confirmed[1:]):
        a, b = _parse_dt(newer["created_at"]), _parse_dt(older["created_at"])
        newer["days_since_previous"] = (
            round((a - b).total_seconds() / 86400.0, 2) if a and b else None
        )
    return orders


def _profile_breakdowns(conn, kind: str, ident: str) -> Dict[str, List[Dict[str, Any]]]:
    """Top produtos, categorias, formas de pagamento, eventos e vendedores."""
    ident_args = (kind, ident)

    products = conn.execute(
        f"""
        {_IDENT_CTE}
        SELECT ti.product_name,
               MAX(COALESCE(ti.product_sku, ''))  AS product_sku,
               MAX(COALESCE(ti.product_id, ''))   AS product_id,
               MAX(COALESCE(ti.category, ''))     AS category,
               SUM(ti.quantity)                   AS units,
               SUM(ti.subtotal)                   AS revenue,
               COUNT(DISTINCT ti.transaction_id)  AS orders
          FROM ident i
          JOIN transaction_items ti ON ti.transaction_id = i.id
         WHERE i.ident_kind = ? AND i.ident_value = ?
           AND i.status_norm = 'confirmado'
         GROUP BY LOWER(TRIM(ti.product_name))
         ORDER BY units DESC, revenue DESC
        """,
        ident_args,
    ).fetchall()

    categories = conn.execute(
        f"""
        {_IDENT_CTE}
        SELECT COALESCE(NULLIF(TRIM(ti.category), ''), 'Sem categoria') AS category,
               SUM(ti.quantity) AS units,
               SUM(ti.subtotal) AS revenue
          FROM ident i
          JOIN transaction_items ti ON ti.transaction_id = i.id
         WHERE i.ident_kind = ? AND i.ident_value = ?
           AND i.status_norm = 'confirmado'
         GROUP BY LOWER(TRIM(COALESCE(ti.category, '')))
         ORDER BY revenue DESC
        """,
        ident_args,
    ).fetchall()

    payments = conn.execute(
        f"""
        {_IDENT_CTE}
        SELECT COALESCE(NULLIF(TRIM(payment_method), ''), 'cartao') AS method,
               COUNT(*)              AS orders,
               COALESCE(SUM(total), 0) AS revenue
          FROM ident
         WHERE ident_kind = ? AND ident_value = ? AND status_norm = 'confirmado'
         GROUP BY LOWER(TRIM(COALESCE(payment_method, '')))
         ORDER BY revenue DESC
        """,
        ident_args,
    ).fetchall()

    events = conn.execute(
        f"""
        {_IDENT_CTE}
        SELECT i.event_id, e.name AS event_name, e.badge_color,
               COUNT(*)                AS orders,
               COALESCE(SUM(i.total), 0) AS revenue,
               MAX(i.created_at)       AS last_purchase_at
          FROM ident i
          LEFT JOIN events e ON e.id = i.event_id
         WHERE i.ident_kind = ? AND i.ident_value = ? AND i.status_norm = 'confirmado'
         GROUP BY i.event_id
         ORDER BY revenue DESC
        """,
        ident_args,
    ).fetchall()

    sellers = conn.execute(
        f"""
        {_IDENT_CTE}
        SELECT seller_id,
               COALESCE(NULLIF(TRIM(seller_name), ''), 'Sem vendedor') AS seller_name,
               COUNT(*)                AS orders,
               COALESCE(SUM(total), 0) AS revenue
          FROM ident
         WHERE ident_kind = ? AND ident_value = ? AND status_norm = 'confirmado'
         GROUP BY seller_id
         ORDER BY revenue DESC
        """,
        ident_args,
    ).fetchall()

    monthly = conn.execute(
        f"""
        {_IDENT_CTE}
        SELECT strftime('%Y-%m', created_at) AS month,
               COUNT(*)                      AS orders,
               COALESCE(SUM(total), 0)       AS revenue,
               COALESCE(SUM(items_count), 0) AS units
          FROM ident
         WHERE ident_kind = ? AND ident_value = ? AND status_norm = 'confirmado'
         GROUP BY month
         ORDER BY month ASC
        """,
        ident_args,
    ).fetchall()

    return {
        "products": [dict(r) for r in products],
        "categories": [dict(r) for r in categories],
        "payments": [dict(r) for r in payments],
        "events": [dict(r) for r in events],
        "sellers": [dict(r) for r in sellers],
        "monthly": [dict(r) for r in monthly],
    }


def _interval_stats(confirmed_dates: List[datetime]) -> Dict[str, Any]:
    """Estatísticas de periodicidade a partir das datas de compra confirmadas."""
    ordered = sorted(d for d in confirmed_dates if d is not None)
    gaps = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(ordered, ordered[1:])
    ]
    if not gaps:
        return {
            "count": 0,
            "avg_days": None,
            "min_days": None,
            "max_days": None,
            "median_days": None,
        }
    ranked = sorted(gaps)
    mid = len(ranked) // 2
    median = ranked[mid] if len(ranked) % 2 else (ranked[mid - 1] + ranked[mid]) / 2
    return {
        "count": len(gaps),
        "avg_days": round(sum(gaps) / len(gaps), 2),
        "min_days": round(min(gaps), 2),
        "max_days": round(max(gaps), 2),
        "median_days": round(median, 2),
    }


def get_customer_profile(kind: str, ident: Any) -> Optional[Dict[str, Any]]:
    """Perfil analítico completo de um cliente, ou ``None`` se não existir.

    Inclui KPIs (receita, ticket médio, participação no faturamento total),
    periodicidade de compra, top produtos/categorias, formas de pagamento,
    eventos, vendedores que o atenderam e o histórico de pedidos.
    """
    norm_kind, norm_ident = normalize_customer_ident(kind, ident)
    if not norm_kind or not norm_ident:
        return None

    with get_conn() as conn:
        scope = _scoped_revenue(conn, None)
        total_revenue = scope["revenue"]

        head = conn.execute(
            f"""
            {_IDENT_CTE},
            {_AGG_SQL.format(event_clause="")}
            SELECT a.*, l.client_name, l.client_cpf, l.client_email, l.client_phone,
                   l.client_cro_uf, l.client_cro_numero, l.client_city, l.client_state,
                   COALESCE(o.refunded_count, 0)  AS refunded_count,
                   COALESCE(o.refunded_value, 0)  AS refunded_value,
                   COALESCE(o.pending_count, 0)   AS pending_count,
                   COALESCE(o.cancelled_count, 0) AS cancelled_count
              FROM agg a
              JOIN latest l
                ON l.ident_kind = a.ident_kind
               AND l.ident_value = a.ident_value
               AND l.rn = 1
              LEFT JOIN other o
                ON o.ident_kind = a.ident_kind
               AND o.ident_value = a.ident_value
             WHERE a.ident_kind = ? AND a.ident_value = ?
            """,
            (norm_kind, norm_ident),
        ).fetchone()
        if head is None:
            return None

        customer = _customer_row(head, total_revenue)
        orders = _profile_orders(conn, norm_kind, norm_ident)
        parts = _profile_breakdowns(conn, norm_kind, norm_ident)

    revenue = customer["revenue"]
    for item in parts["products"]:
        item["share"] = _share(item["revenue"], revenue)
    for item in parts["categories"]:
        item["share"] = _share(item["revenue"], revenue)
    for item in parts["payments"]:
        item["share"] = _share(item["revenue"], revenue)
    for item in parts["events"]:
        item["share"] = _share(item["revenue"], revenue)

    confirmed_dates = [
        _parse_dt(o["created_at"]) for o in orders if o["status"] == "confirmado"
    ]
    intervals = _interval_stats([d for d in confirmed_dates if d])

    # Projeção simples da próxima compra: última data + periodicidade média.
    next_expected = None
    if intervals["avg_days"] and confirmed_dates:
        last = max(d for d in confirmed_dates if d)
        from datetime import timedelta

        next_expected = (last + timedelta(days=intervals["avg_days"])).isoformat(
            timespec="seconds"
        )

    customer.update(
        {
            "identity": {"kind": norm_kind, "ident": norm_ident},
            "intervals": intervals,
            "next_purchase_expected_at": next_expected,
            "distinct_products": len(parts["products"]),
            "distinct_categories": len(parts["categories"]),
            "top_products": parts["products"][:10],
            "all_products": parts["products"],
            "categories": parts["categories"],
            "payments": parts["payments"],
            "events": parts["events"],
            "sellers": parts["sellers"],
            "monthly": parts["monthly"],
            "orders": orders,
            "total_revenue_all": total_revenue,
        }
    )
    return customer
