"""Integração com o ERP Sankhya: configuração, fila de envio e montagem de pedidos.

Só persistência e regras sobre o banco. O transporte HTTP fica em
``sankhya_api.py`` e a orquestração (worker da fila, sincronização do
catálogo) em ``erp_sync.py``.

Fluxo de um pedido
------------------
1. A venda é confirmada com AUT → ``enqueue_order_in_conn`` grava uma linha
   ``pedido`` em ``erp_outbox`` e ``transactions.erp_status`` vira ``na_fila``.
2. O worker pega as linhas vencidas (``claim_due_jobs``), garante o
   ``CODPARC`` do cliente e envia o pedido.
3. Sucesso → ``enviado``. Falha de rede → nova tentativa mais tarde, sem
   limite (o estande pode ficar horas sem internet). Recusa do ERP → até
   ``MAX_BUSINESS_ATTEMPTS`` tentativas e depois ``erro``, à espera do admin.
4. Estorno de venda já enviada gera uma linha ``cancelamento``; se o pedido
   ainda não tinha saído, ele só é retirado da fila.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .connection import _now_iso, get_conn
from .payment_methods import CHECKOUT_METHODS, PAYMENT_METHOD_LABELS

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

#: Chaves editáveis no admin. Credenciais NÃO ficam aqui (vão no ``.env``).
SETTINGS_DEFAULTS: Dict[str, str] = {
    "codemp": "1",
    "codvend": "",
    "orders_enabled": "0",
    "order_ref_field": "",
    "cro_field": "",
}

#: Formas com um único CODTIPVENDA; o crédito tem um código por parcela (1x–10x).
SINGLE_CODE_METHODS = tuple(m for m in CHECKOUT_METHODS if m != "credito")
MAX_CARD_INSTALLMENTS = 10

#: Situação do pedido no ERP (``transactions.erp_status``).
ERP_STATUS_LABELS = {
    "nao_enviado": "Não enviado",
    "na_fila": "Na fila",
    "enviado": "Enviado",
    "erro": "Erro no envio",
    "cancelamento_pendente": "Cancelamento na fila",
    "cancelado": "Cancelado no ERP",
}

OUTBOX_STATUS_LABELS = {
    "pendente": "Aguardando envio",
    "processando": "Enviando",
    "enviado": "Enviado",
    "erro": "Erro",
    "descartado": "Descartado",
}

#: Recusas do ERP (dado inválido, cadastro incompleto) antes de parar e
#: esperar o admin. Falhas de rede não contam: repetem até a internet voltar.
MAX_BUSINESS_ATTEMPTS = 5
#: Espera entre tentativas (minutos), pela ordem; a última se repete.
RETRY_BACKOFF_MINUTES = (1, 5, 30, 120)
#: Linha presa em ``processando`` (processo caiu no meio do envio).
STALE_PROCESSING_MINUTES = 15


def get_erp_settings() -> Dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM erp_settings").fetchall()
    out = dict(SETTINGS_DEFAULTS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def save_erp_settings(values: Dict[str, Any]) -> None:
    now = _now_iso()
    with get_conn() as conn:
        for key, value in values.items():
            if key not in SETTINGS_DEFAULTS:
                continue
            conn.execute(
                """
                INSERT INTO erp_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (key, str(value if value is not None else "").strip(), now),
            )


def orders_readiness(settings: Optional[Dict[str, str]] = None) -> List[str]:
    """O que falta configurar no banco para começar a enviar pedidos."""
    cfg = settings or get_erp_settings()
    missing: List[str] = []
    if not (cfg.get("codemp") or "").strip().isdigit():
        missing.append("Empresa (CODEMP)")
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM erp_payment_types").fetchone()[0]
    if not n:
        missing.append("Tipos de negociação (CODTIPVENDA)")
    if (cfg.get("orders_enabled") or "0") != "1":
        missing.append("Envio de pedidos ativado")
    return missing


# ---------------------------------------------------------------------------
# Formas de pagamento → tipo de negociação (CODTIPVENDA)
# ---------------------------------------------------------------------------

def list_payment_types() -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT payment_method, installments, codtipvenda, description "
            "FROM erp_payment_types"
        ).fetchall()
    return [dict(r) for r in rows]


def payment_type_grid() -> List[Dict]:
    """Uma linha por combinação que o checkout produz.

    PIX, Pix Inter, débito, dinheiro e faturado têm um código cada; o crédito
    tem um por número de parcelas (1x–10x).
    """
    saved = {
        (r["payment_method"], r["installments"]): r for r in list_payment_types()
    }
    grid: List[Dict] = []
    for method in SINGLE_CODE_METHODS:
        row = saved.get((method, None)) or {}
        grid.append({
            "key": method,
            "payment_method": method,
            "installments": None,
            "label": PAYMENT_METHOD_LABELS[method],
            "codtipvenda": row.get("codtipvenda"),
            "description": row.get("description") or "",
        })
    for n in range(1, MAX_CARD_INSTALLMENTS + 1):
        row = saved.get(("credito", n)) or {}
        grid.append({
            "key": f"credito_{n}",
            "payment_method": "credito",
            "installments": n,
            "label": f"Cartão de crédito {n}x" + (" (à vista)" if n == 1 else ""),
            "codtipvenda": row.get("codtipvenda"),
            "description": row.get("description") or "",
        })
    return grid


def save_payment_types(rows: Iterable[Dict]) -> int:
    """Substitui o mapeamento inteiro. Linha sem código é removida."""
    now = _now_iso()
    clean: List[Tuple] = []
    for r in rows:
        method = (r.get("payment_method") or "").strip().lower()
        if method not in CHECKOUT_METHODS:
            continue
        inst = r.get("installments")
        inst = int(inst) if inst not in (None, "") else None
        code = str(r.get("codtipvenda") or "").strip()
        if not code:
            continue
        if not code.isdigit():
            raise ValueError(
                f"CODTIPVENDA inválido para {PAYMENT_METHOD_LABELS[method]}"
                f"{f' {inst}x' if inst else ''}: use só números."
            )
        clean.append((method, inst, int(code), (r.get("description") or "").strip() or None, now))
    with get_conn() as conn:
        conn.execute("DELETE FROM erp_payment_types")
        conn.executemany(
            "INSERT INTO erp_payment_types "
            "(payment_method, installments, codtipvenda, description, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            clean,
        )
    return len(clean)


def resolve_codtipvenda(
    conn: sqlite3.Connection, payment_method: Optional[str], installments: Optional[int]
) -> Optional[int]:
    """Regra mais específica vence: crédito 3x antes de "crédito, qualquer parcela".

    Vendas antigas com ``cartao`` (antes da separação crédito/débito) usam os
    códigos do crédito.
    """
    method = (payment_method or "").strip().lower()
    if method == "cartao":
        method = "credito"
    if method != "credito":
        installments = None
    elif not installments:
        installments = 1
    row = None
    if installments is not None:
        row = conn.execute(
            "SELECT codtipvenda FROM erp_payment_types "
            "WHERE payment_method = ? AND installments = ?",
            (method, int(installments)),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT codtipvenda FROM erp_payment_types "
            "WHERE payment_method = ? AND installments IS NULL",
            (method,),
        ).fetchone()
    return int(row["codtipvenda"]) if row else None


# ---------------------------------------------------------------------------
# Clientes (CPF → CODPARC)
# ---------------------------------------------------------------------------

def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def get_cached_codparc(cpf: Any) -> Optional[int]:
    digits = _digits(cpf)
    if not digits:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT codparc FROM erp_customers WHERE cpf = ?", (digits,)
        ).fetchone()
    return int(row["codparc"]) if row else None


def save_customer_codparc(cpf: Any, codparc: int, name: Optional[str] = None) -> None:
    digits = _digits(cpf)
    if not digits:
        return
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO erp_customers (cpf, codparc, name, synced_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(cpf) DO UPDATE SET codparc = excluded.codparc,
                                           name = excluded.name,
                                           synced_at = excluded.synced_at
            """,
            (digits, int(codparc), name, _now_iso()),
        )


# ---------------------------------------------------------------------------
# Montagem do pedido
# ---------------------------------------------------------------------------

def _valid_cpf(digits: str) -> bool:
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(digits[i]) * (size + 1 - i) for i in range(size))
        check = (total * 10) % 11 % 10
        if check != int(digits[size]):
            return False
    return True


def _erp_city(value: Any) -> str:
    """Cidade no padrão do cadastro do Sankhya: maiúsculas, sem acento nem apóstrofo."""
    text = unicodedata.normalize("NFD", str(value or "").strip())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("'", "").replace("´", "").replace("`", "")
    return " ".join(text.upper().split())


def resolve_codvend(
    conn: sqlite3.Connection, tx: Dict, settings: Dict[str, str]
) -> Tuple[Optional[int], Optional[str]]:
    """``CODVEND`` do vendedor que fez a venda. Devolve ``(codvend, problema)``.

    Vendedor sem código: a venda **não** sai com o código de outra pessoa; fica
    na fila com erro até o admin cadastrar o CODVEND dele. O CODVEND padrão das
    configurações só vale para venda cujo vendedor foi excluído do Totem.
    """
    seller_id = tx.get("seller_id")
    if seller_id:
        row = conn.execute(
            "SELECT name, erp_codvend FROM sellers WHERE id = ?", (int(seller_id),)
        ).fetchone()
        if row is not None:
            if row["erp_codvend"] is not None:
                return int(row["erp_codvend"]), None
            return None, (
                f"Vendedor {row['name']} sem CODVEND do Sankhya "
                "(cadastre em Vendedores e reenvie)"
            )
    fallback = str(settings.get("codvend") or "").strip()
    if fallback.isdigit():
        return int(fallback), None
    who = tx.get("seller_name") or "excluído"
    return None, (
        f"Vendedor {who} não existe mais no Totem e não há CODVEND padrão configurado"
    )


def build_customer_payload(
    tx: Dict, settings: Dict[str, str], codparc: Optional[int], codvend: Optional[int] = None
) -> Tuple[Dict, List[str]]:
    """Corpo de ``/clientes/create-update`` a partir dos dados gravados na venda."""
    problems: List[str] = []
    cpf = _digits(tx.get("client_cpf"))
    name = " ".join(str(tx.get("client_name") or "").split())
    if not _valid_cpf(cpf):
        problems.append("CPF do cliente ausente ou inválido")
    if not name:
        problems.append("nome do cliente")
    address = {
        "ENDERECO": " ".join(str(tx.get("client_address") or "").split()),
        "CEP": _digits(tx.get("client_zipcode")),
        "NUMEND": " ".join(str(tx.get("client_number") or "").split()) or "S/N",
        "COMPLENDERECO": " ".join(str(tx.get("client_complement") or "").split()),
        "BAIRRO": " ".join(str(tx.get("client_neighborhood") or "").split()),
        "CIDADE": _erp_city(tx.get("client_city")),
        "ESTADO": str(tx.get("client_state") or "").strip().upper(),
    }
    labels = {"ENDERECO": "endereço", "BAIRRO": "bairro", "CIDADE": "cidade", "ESTADO": "UF"}
    for key, label in labels.items():
        if not address[key]:
            problems.append(label)
    if len(address["CEP"]) != 8:
        problems.append("CEP")
    phone = _digits(tx.get("client_phone"))
    if phone.startswith("55") and len(phone) > 11:
        phone = phone[2:]
    payload: Dict[str, Any] = {
        "CODPARC": str(codparc) if codparc else "",
        "TIPPESSOA": "F",
        "NOMEPARC": name,
        "RAZAOSOCIAL": name,
        "CGC_CPF": cpf,
        "IDENTINSCESTAD": "ISENTO",
        "CODVEND": str(codvend) if codvend else "",
        "TELEFONE": phone if len(phone) == 10 else "",
        "CELULAR": phone if len(phone) == 11 else "",
        "EMAIL": str(tx.get("client_email") or "").strip(),
        "TIPOENDERECO": "",
        **address,
    }
    cro_field = (settings.get("cro_field") or "").strip()
    cro_num = str(tx.get("client_cro_numero") or "").strip()
    if cro_field and cro_num:
        cro_uf = str(tx.get("client_cro_uf") or "").strip().upper()
        payload[cro_field] = f"{cro_uf} {cro_num}".strip()
    if problems:
        problems = ["Cadastro do cliente incompleto: " + ", ".join(problems)]
    return payload, problems


def _fmt_dtneg(created_at: Any) -> str:
    raw = str(created_at or "").strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.now()
    return dt.strftime("%d/%m/%Y")


def build_order_payload(
    conn: sqlite3.Connection,
    tx: Dict,
    settings: Dict[str, str],
    codparc: int,
    codvend: Optional[int] = None,
) -> Tuple[Dict, List[str]]:
    """Corpo de ``/orders``. Devolve ``(payload, problemas)``; com problema não envia.

    - ``VLRUNIT`` é o preço de lista do item (antes da promoção).
    - ``VLRDESC`` é o desconto total do item: promoção + rateio do desconto
      manual do vendedor (``transactions.total`` abaixo da soma dos itens), de
      modo que o pedido no ERP feche no valor que o cliente pagou.
    """
    problems: List[str] = []
    codtipvenda = resolve_codtipvenda(
        conn, tx.get("payment_method"), tx.get("card_installments")
    )
    if codtipvenda is None:
        pm = (tx.get("payment_method") or "?").strip().lower()
        if pm == "cartao":
            pm = "credito"
        label = PAYMENT_METHOD_LABELS.get(pm, pm)
        if pm == "credito":
            label += f" {int(tx.get('card_installments') or 1)}x"
        problems.append(f"Sem CODTIPVENDA cadastrado para {label}")

    items = conn.execute(
        """
        SELECT ti.id, ti.product_id, ti.product_sku, ti.product_name,
               ti.unit_price, ti.original_price, ti.quantity, ti.subtotal,
               p.erp_codprod, p.erp_codvol
          FROM transaction_items ti
          LEFT JOIN products p ON p.id = CAST(ti.product_id AS INTEGER)
         WHERE ti.transaction_id = ?
         ORDER BY ti.id
        """,
        (int(tx["id"]),),
    ).fetchall()

    lines: List[Dict] = []
    for it in items:
        qty = int(it["quantity"] or 0)
        if qty <= 0:
            continue
        codprod = it["erp_codprod"]
        codvol = it["erp_codvol"]
        if codprod is None and (it["product_sku"] or "").strip():
            alt = conn.execute(
                "SELECT erp_codprod, erp_codvol FROM products "
                "WHERE sku = ? AND erp_codprod IS NOT NULL",
                (it["product_sku"].strip(),),
            ).fetchone()
            if alt:
                codprod, codvol = alt["erp_codprod"], alt["erp_codvol"]
        label = (it["product_sku"] or it["product_name"] or "?").strip()
        if codprod is None:
            problems.append(f"Produto {label} sem CODPROD do ERP")
            continue
        if not (codvol or "").strip():
            problems.append(f"Produto {label} sem unidade (CODVOL); sincronize o catálogo")
        unit = float(it["unit_price"] or 0)
        original = it["original_price"]
        list_price = float(original) if original is not None and float(original) >= unit else unit
        net = float(it["subtotal"] if it["subtotal"] is not None else unit * qty)
        lines.append({
            "codprod": int(codprod),
            "codvol": (codvol or "").strip(),
            "qty": qty,
            "list_price": round(list_price, 2),
            "gross": round(list_price * qty, 2),
            "net": round(net, 2),
        })
    if not lines and not problems:
        problems.append("Pedido sem itens")

    # Desconto manual do vendedor: rateado pelo valor líquido dos itens.
    net_sum = round(sum(line["net"] for line in lines), 2)
    total = round(float(tx.get("total") or 0), 2)
    extra = round(net_sum - total, 2) if net_sum - total > 0.004 else 0.0
    if extra and net_sum > 0:
        remaining = extra
        for i, line in enumerate(lines):
            if i == len(lines) - 1:
                share = remaining
            else:
                share = round(extra * line["net"] / net_sum, 2)
                remaining = round(remaining - share, 2)
            line["net"] = round(line["net"] - share, 2)

    itens = []
    for line in lines:
        desc = round(max(0.0, line["gross"] - line["net"]), 2)
        perc = round(desc / line["gross"] * 100, 4) if line["gross"] > 0 else 0
        itens.append({
            "CODPROD": str(line["codprod"]),
            "QTDNEG": line["qty"],
            "VLRUNIT": line["list_price"],
            "CODVOL": line["codvol"],
            "PERCDESC": perc,
            "VLRDESC": desc,
        })

    payload: Dict[str, Any] = {
        "CODPARC": int(codparc) if codparc else None,
        "DTNEG": _fmt_dtneg(tx.get("created_at")),
        "CODTIPVENDA": codtipvenda,
        "CODVEND": int(codvend) if codvend else None,
        "CODEMP": int(settings["codemp"]) if str(settings.get("codemp") or "").isdigit() else None,
        "VLRFRETE": 0,
        "itens": itens,
    }
    ref_field = (settings.get("order_ref_field") or "").strip()
    if ref_field:
        payload[ref_field] = tx.get("order_number")
    return payload, problems


# ---------------------------------------------------------------------------
# Fila de envio (erp_outbox)
# ---------------------------------------------------------------------------

def enqueue_order_in_conn(conn: sqlite3.Connection, tx_id: int) -> bool:
    """Põe a venda confirmada na fila. Idempotente (``UNIQUE (kind, transaction_id)``)."""
    now = _now_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO erp_outbox
            (kind, transaction_id, status, next_attempt_at, created_at, updated_at)
        VALUES ('pedido', ?, 'pendente', ?, ?, ?)
        """,
        (int(tx_id), now, now, now),
    )
    if cur.rowcount:
        conn.execute(
            "UPDATE transactions SET erp_status = 'na_fila' WHERE id = ?", (int(tx_id),)
        )
    return bool(cur.rowcount)


def handle_refund_in_conn(conn: sqlite3.Connection, tx_id: int) -> str:
    """Estorno: retira da fila o pedido não enviado ou enfileira o cancelamento.

    Retorna ``'fora_da_fila'``, ``'retirado'`` ou ``'cancelamento'``.
    """
    order = conn.execute(
        "SELECT id, status FROM erp_outbox WHERE kind = 'pedido' AND transaction_id = ?",
        (int(tx_id),),
    ).fetchone()
    if order is None:
        return "fora_da_fila"
    now = _now_iso()
    if order["status"] in ("pendente", "erro", "descartado"):
        conn.execute(
            "UPDATE erp_outbox SET status = 'descartado', last_error = ?, updated_at = ? "
            "WHERE id = ?",
            ("Venda estornada antes do envio ao ERP.", now, int(order["id"])),
        )
        conn.execute(
            "UPDATE transactions SET erp_status = 'nao_enviado' WHERE id = ?", (int(tx_id),)
        )
        return "retirado"
    conn.execute(
        """
        INSERT OR IGNORE INTO erp_outbox
            (kind, transaction_id, status, next_attempt_at, created_at, updated_at)
        VALUES ('cancelamento', ?, 'pendente', ?, ?, ?)
        """,
        (int(tx_id), now, now, now),
    )
    conn.execute(
        "UPDATE transactions SET erp_status = 'cancelamento_pendente' WHERE id = ?",
        (int(tx_id),),
    )
    return "cancelamento"


def note_items_changed_in_conn(conn: sqlite3.Connection, tx_id: int) -> Optional[str]:
    """Item trocado depois da confirmação.

    Pedido ainda na fila: nada a fazer, o corpo é montado na hora do envio.
    Pedido já enviado: o ERP não é atualizado sozinho; devolve um aviso.
    """
    row = conn.execute(
        "SELECT erp_status, erp_order_id FROM transactions WHERE id = ?", (int(tx_id),)
    ).fetchone()
    if row and row["erp_status"] == "enviado":
        ref = f" (pedido {row['erp_order_id']})" if row["erp_order_id"] else ""
        return (
            f"Este pedido já foi enviado ao Sankhya{ref}. "
            "Ajuste o item também no ERP."
        )
    return None


def enqueue_unsent_orders(event_id: Optional[int] = None) -> int:
    """Envio explícito do histórico: vendas confirmadas que nunca foram à fila."""
    sql = (
        "SELECT t.id FROM transactions t "
        "WHERE t.status = 'confirmado' AND COALESCE(t.erp_status, 'nao_enviado') = 'nao_enviado' "
        "AND NOT EXISTS (SELECT 1 FROM erp_outbox o "
        "                WHERE o.transaction_id = t.id AND o.kind = 'pedido' "
        "                  AND o.status != 'descartado')"
    )
    params: List[Any] = []
    if event_id is not None:
        sql += " AND t.event_id = ?"
        params.append(int(event_id))
    with get_conn() as conn:
        ids = [int(r[0]) for r in conn.execute(sql, params).fetchall()]
        now = _now_iso()
        for tx_id in ids:
            conn.execute(
                """
                INSERT INTO erp_outbox
                    (kind, transaction_id, status, next_attempt_at, created_at, updated_at)
                VALUES ('pedido', ?, 'pendente', ?, ?, ?)
                ON CONFLICT(kind, transaction_id) DO UPDATE SET
                    status = 'pendente', attempts = 0, last_error = NULL,
                    next_attempt_at = excluded.next_attempt_at,
                    updated_at = excluded.updated_at
                """,
                (tx_id, now, now, now),
            )
            conn.execute(
                "UPDATE transactions SET erp_status = 'na_fila' WHERE id = ?", (tx_id,)
            )
    return len(ids)


def count_unsent_orders(event_id: Optional[int] = None) -> int:
    sql = (
        "SELECT COUNT(*) FROM transactions WHERE status = 'confirmado' "
        "AND COALESCE(erp_status, 'nao_enviado') = 'nao_enviado'"
    )
    params: List[Any] = []
    if event_id is not None:
        sql += " AND event_id = ?"
        params.append(int(event_id))
    with get_conn() as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def reset_stale_processing() -> int:
    cutoff = (datetime.now() - timedelta(minutes=STALE_PROCESSING_MINUTES)).isoformat(
        timespec="seconds"
    )
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE erp_outbox SET status = 'pendente', updated_at = ? "
            "WHERE status = 'processando' AND updated_at < ?",
            (_now_iso(), cutoff),
        )
        return int(cur.rowcount or 0)


def claim_due_jobs(limit: int = 20) -> List[Dict]:
    """Marca como ``processando`` as linhas vencidas e as devolve (pedido antes de cancelamento)."""
    now = _now_iso()
    claimed: List[Dict] = []
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM erp_outbox
             WHERE status = 'pendente'
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
             ORDER BY CASE kind WHEN 'pedido' THEN 0 ELSE 1 END, id
             LIMIT ?
            """,
            (now, int(limit)),
        ).fetchall()
        for r in rows:
            cur = conn.execute(
                "UPDATE erp_outbox SET status = 'processando', updated_at = ? "
                "WHERE id = ? AND status = 'pendente'",
                (now, int(r["id"])),
            )
            if cur.rowcount:
                claimed.append(dict(r))
    return claimed


def load_transaction_for_erp(conn: sqlite3.Connection, tx_id: int) -> Optional[Dict]:
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (int(tx_id),)).fetchone()
    return dict(row) if row else None


def get_order_job(conn: sqlite3.Connection, tx_id: int) -> Optional[Dict]:
    row = conn.execute(
        "SELECT * FROM erp_outbox WHERE kind = 'pedido' AND transaction_id = ?",
        (int(tx_id),),
    ).fetchone()
    return dict(row) if row else None


def mark_job_sent(
    job: Dict,
    *,
    payload: Optional[Dict],
    response: Any,
    erp_order_id: Optional[str] = None,
    codparc: Optional[int] = None,
) -> None:
    now = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE erp_outbox
               SET status = 'enviado', attempts = attempts + 1, payload_json = ?,
                   response_json = ?, last_error = NULL, updated_at = ?
             WHERE id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                json.dumps(response, ensure_ascii=False, default=str),
                now,
                int(job["id"]),
            ),
        )
        tx_id = int(job["transaction_id"])
        if job["kind"] == "pedido":
            current = conn.execute(
                "SELECT erp_status FROM transactions WHERE id = ?", (tx_id,)
            ).fetchone()
            # Estorno durante o envio já pôs o cancelamento na fila: não sobrescreve.
            status = (
                "cancelamento_pendente"
                if current and current["erp_status"] == "cancelamento_pendente"
                else "enviado"
            )
            conn.execute(
                "UPDATE transactions SET erp_status = ?, erp_order_id = ?, "
                "erp_sent_at = ?, erp_codparc = COALESCE(?, erp_codparc) WHERE id = ?",
                (status, erp_order_id, now, codparc, tx_id),
            )
        else:
            conn.execute(
                "UPDATE transactions SET erp_status = 'cancelado' WHERE id = ?", (tx_id,)
            )


def mark_job_failed(
    job: Dict,
    error: str,
    *,
    transient: bool,
    payload: Optional[Dict] = None,
    response: Any = None,
    final: bool = False,
) -> str:
    """Registra a falha e agenda a próxima tentativa. Retorna o novo status da linha.

    ``transient`` (rede/gateway fora do ar): tenta de novo sem limite.
    ``final``: não adianta repetir sem ação do admin (ex.: cadastro incompleto).
    """
    attempts = int(job.get("attempts") or 0) + 1
    give_up = final or (not transient and attempts >= MAX_BUSINESS_ATTEMPTS)
    wait = RETRY_BACKOFF_MINUTES[min(attempts, len(RETRY_BACKOFF_MINUTES)) - 1]
    next_at = (datetime.now() + timedelta(minutes=wait)).isoformat(timespec="seconds")
    status = "erro" if give_up else "pendente"
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE erp_outbox
               SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?,
                   payload_json = COALESCE(?, payload_json),
                   response_json = COALESCE(?, response_json), updated_at = ?
             WHERE id = ?
            """,
            (
                status,
                attempts,
                None if give_up else next_at,
                (error or "")[:2000],
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                json.dumps(response, ensure_ascii=False, default=str) if response is not None else None,
                _now_iso(),
                int(job["id"]),
            ),
        )
        if give_up and job["kind"] == "pedido":
            conn.execute(
                "UPDATE transactions SET erp_status = 'erro' WHERE id = ? "
                "AND erp_status IN ('na_fila', 'erro')",
                (int(job["transaction_id"]),),
            )
    return status


def mark_job_discarded(job_id: int, reason: str) -> None:
    """Admin tira o item da fila.

    Pedido: a venda volta a ``nao_enviado``. Cancelamento: o admin cancelou
    direto no Sankhya, e a venda passa a ``cancelado``.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM erp_outbox WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            raise ValueError("Item da fila não encontrado.")
        if row["status"] in ("enviado", "processando"):
            raise ValueError("Este item já foi enviado ou está sendo enviado.")
        conn.execute(
            "UPDATE erp_outbox SET status = 'descartado', last_error = ?, updated_at = ? "
            "WHERE id = ?",
            (reason, _now_iso(), int(job_id)),
        )
        tx_status = "nao_enviado" if row["kind"] == "pedido" else "cancelado"
        conn.execute(
            "UPDATE transactions SET erp_status = ? WHERE id = ?",
            (tx_status, int(row["transaction_id"])),
        )


def retry_job(job_id: int) -> None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM erp_outbox WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            raise ValueError("Item da fila não encontrado.")
        if row["status"] not in ("erro", "pendente", "descartado"):
            raise ValueError("Só itens com erro, aguardando ou descartados podem ser reenviados.")
        if row["kind"] == "pedido":
            tx = conn.execute(
                "SELECT status FROM transactions WHERE id = ?", (int(row["transaction_id"]),)
            ).fetchone()
            if tx is None or tx["status"] != "confirmado":
                raise ValueError("A venda não está mais confirmada; não há o que enviar.")
        now = _now_iso()
        conn.execute(
            "UPDATE erp_outbox SET status = 'pendente', attempts = 0, next_attempt_at = ?, "
            "updated_at = ? WHERE id = ?",
            (now, now, int(job_id)),
        )
        conn.execute(
            "UPDATE transactions SET erp_status = ? WHERE id = ?",
            (
                "na_fila" if row["kind"] == "pedido" else "cancelamento_pendente",
                int(row["transaction_id"]),
            ),
        )


def outbox_counts() -> Dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM erp_outbox GROUP BY status"
        ).fetchall()
    counts = {k: 0 for k in OUTBOX_STATUS_LABELS}
    counts.update({r["status"]: int(r["n"]) for r in rows})
    return counts


def list_outbox(status: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict]:
    sql = (
        "SELECT o.*, t.order_number, t.client_name, t.total, t.event_id, t.status AS tx_status, "
        "t.erp_status, t.erp_order_id, e.name AS event_name "
        "FROM erp_outbox o JOIN transactions t ON t.id = o.transaction_id "
        "LEFT JOIN events e ON e.id = t.event_id"
    )
    params: List[Any] = []
    if status and status in OUTBOX_STATUS_LABELS:
        sql += " WHERE o.status = ?"
        params.append(status)
    sql += " ORDER BY CASE o.status WHEN 'erro' THEN 0 WHEN 'pendente' THEN 1 " \
           "WHEN 'processando' THEN 2 ELSE 3 END, o.updated_at DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(max(0, offset))]
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status_label"] = OUTBOX_STATUS_LABELS.get(d["status"], d["status"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Histórico de sincronizações
# ---------------------------------------------------------------------------

def start_sync_run(kind: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO erp_sync_runs (kind, started_at, status) VALUES (?, ?, 'executando')",
            (kind, _now_iso()),
        )
        return int(cur.lastrowid)


def finish_sync_run(
    run_id: int,
    status: str,
    *,
    stats: Optional[Dict] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    s = stats or {}
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE erp_sync_runs
               SET finished_at = ?, status = ?, inserted = ?, updated = ?, unchanged = ?,
                   warnings = ?, images = ?, message = ?, error = ?
             WHERE id = ?
            """,
            (
                _now_iso(), status, int(s.get("inserted") or 0), int(s.get("updated") or 0),
                int(s.get("unchanged") or 0), int(s.get("warnings") or 0),
                int(s.get("images") or 0), message, (error or None) and error[:2000],
                int(run_id),
            ),
        )


def abandon_running_syncs() -> None:
    """Na subida do app: sincronização que estava rodando quando o processo caiu."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE erp_sync_runs SET status = 'erro', finished_at = ?, "
            "error = 'Interrompida (o sistema foi reiniciado durante a sincronização).' "
            "WHERE status = 'executando'",
            (_now_iso(),),
        )


def list_sync_runs(limit: int = 10) -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM erp_sync_runs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]
