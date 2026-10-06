"""Orquestração da integração Sankhya: catálogo e fila de pedidos.

- ``sync_catalog``: produtos + preços (+ imagens) do ERP para a biblioteca.
- ``import_single_product``: traz um ``CODPROD`` que ainda não existe aqui.
- ``process_outbox_once`` / ``start_worker``: envia clientes e pedidos da fila
  em segundo plano, sem travar o pagamento no estande.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import threading
import time
from typing import Dict, List, Optional

import sankhya_api
from database import erp as erp_db
from database.connection import get_conn
from database.products import find_local_product_for_codprod, upsert_products_from_erp

log = logging.getLogger(__name__)

WORKER_INTERVAL_SECONDS = 30


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------

def _decode_image(raw) -> Optional[bytes]:
    """``IMAGEM`` em base64 (com ou sem prefixo ``data:``). Vazio = não altera a foto."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError):
        return None


def _prices_for_company(rows: List[Dict], codemp: int) -> Dict[int, float]:
    prices: Dict[int, float] = {}
    for r in rows:
        try:
            if int(r.get("CODEMP")) != codemp:
                continue
            prices[int(r.get("CODPROD"))] = float(r.get("PRECO"))
        except (TypeError, ValueError):
            continue
    return prices


def _to_items(products: List[Dict], prices: Dict[int, float]) -> List[Dict]:
    items = []
    for p in products:
        try:
            codprod = int(p.get("CODPROD"))
        except (TypeError, ValueError):
            continue
        items.append({
            "codprod": codprod,
            "name": p.get("DESCRPROD"),
            "category": p.get("DESCRGRUPOPROD"),
            "group_code": p.get("CODGRUPOPROD"),
            "brand": p.get("MARCA"),
            "codvol": p.get("CODVOL"),
            "anvisa_code": p.get("CODANVISA"),
            "supplier_ref": p.get("REFFORN"),
            "supplier_name": p.get("FORNECEDOR"),
            "description": p.get("CARACTERISTICAS"),
            "price": prices.get(codprod),
            "image": _decode_image(p.get("IMAGEM")),
        })
    return items


def _codemp() -> int:
    raw = (erp_db.get_erp_settings().get("codemp") or "1").strip()
    return int(raw) if raw.isdigit() else 1


_PRICE_CACHE_SECONDS = 600
_price_cache: Dict[str, object] = {"at": 0.0, "rows": None}


def _price_rows_cached() -> List[Dict]:
    """Lista de preços para buscas avulsas (importação de planilha chama várias vezes)."""
    now = time.time()
    if _price_cache["rows"] is None or now - float(_price_cache["at"]) > _PRICE_CACHE_SECONDS:
        _price_cache["rows"] = sankhya_api.fetch_prices()
        _price_cache["at"] = now
    return _price_cache["rows"]  # type: ignore[return-value]


def sync_catalog(run_id: Optional[int] = None) -> Dict:
    """Sincronização completa. Não mexe em estoque, eventos, vendas nem promoções."""
    run_id = run_id or erp_db.start_sync_run("catalogo")
    try:
        products = sankhya_api.fetch_products()
        prices = _prices_for_company(sankhya_api.fetch_prices(), _codemp())
        stats = upsert_products_from_erp(_to_items(products, prices))
    except Exception as exc:
        log.exception("Sincronização do catálogo Sankhya falhou")
        erp_db.finish_sync_run(run_id, "erro", error=str(exc))
        raise
    stats["received"] = len(products)
    message = "\n".join(stats["messages"]) or None
    erp_db.finish_sync_run(
        run_id, "parcial" if stats["warnings"] else "ok", stats=stats, message=message
    )
    return stats


_sync_lock = threading.Lock()


def start_catalog_sync_async() -> Optional[int]:
    """Roda a sincronização numa thread. ``None`` se já houver uma em andamento."""
    if not _sync_lock.acquire(blocking=False):
        return None
    try:
        run_id = erp_db.start_sync_run("catalogo")
    except Exception:
        _sync_lock.release()
        raise

    def _run() -> None:
        try:
            sync_catalog(run_id)
        except Exception:
            pass  # já registrado em erp_sync_runs
        finally:
            _sync_lock.release()

    threading.Thread(target=_run, name="sankhya-catalog-sync", daemon=True).start()
    return run_id


def catalog_sync_running() -> bool:
    return _sync_lock.locked()


def import_single_product(code: str) -> Optional[int]:
    """Busca um ``CODPROD`` no ERP e cadastra/atualiza aqui. Devolve o ``id`` local.

    Levanta ``SankhyaConfigError`` quando a busca avulsa não está configurada.
    """
    try:
        codprod = int(str(code).strip().lstrip("#"))
    except ValueError:
        return None
    products = [
        p for p in sankhya_api.fetch_products(codprod)
        if str(p.get("CODPROD")).strip() == str(codprod)
    ]
    if not products:
        return None
    prices = _prices_for_company(_price_rows_cached(), _codemp())
    upsert_products_from_erp(_to_items(products, prices))
    with get_conn() as conn:
        row = find_local_product_for_codprod(conn, codprod)
    return int(row["id"]) if row else None


# ---------------------------------------------------------------------------
# Fila de pedidos
# ---------------------------------------------------------------------------

def _process_order(job: Dict, settings: Dict[str, str]) -> None:
    with get_conn() as conn:
        tx = erp_db.load_transaction_for_erp(conn, int(job["transaction_id"]))
    if tx is None or tx.get("status") != "confirmado":
        erp_db.mark_job_discarded(int(job["id"]), "A venda não está mais confirmada.")
        return

    with get_conn() as conn:
        codvend, codvend_problem = erp_db.resolve_codvend(conn, tx, settings)
    if codvend_problem:
        erp_db.mark_job_failed(job, codvend_problem, transient=False, final=True)
        return

    codparc = tx.get("erp_codparc") or erp_db.get_cached_codparc(tx.get("client_cpf"))
    customer, problems = erp_db.build_customer_payload(tx, settings, codparc, codvend)
    if problems:
        erp_db.mark_job_failed(job, "; ".join(problems), transient=False, final=True,
                               payload={"cliente": customer})
        return
    if not codparc:
        try:
            result = sankhya_api.create_update_client(customer)
        except sankhya_api.SankhyaError as exc:
            erp_db.mark_job_failed(job, f"Cliente: {exc}", transient=exc.transient,
                                   payload={"cliente": customer}, response=exc.response)
            return
        codparc = result["codparc"]
        erp_db.save_customer_codparc(tx.get("client_cpf"), codparc, customer.get("NOMEPARC"))

    with get_conn() as conn:
        payload, problems = erp_db.build_order_payload(conn, tx, settings, codparc, codvend)
    if problems:
        erp_db.mark_job_failed(job, "; ".join(problems), transient=False, final=True,
                               payload=payload)
        return
    try:
        result = sankhya_api.create_order(payload)
    except sankhya_api.SankhyaError as exc:
        erp_db.mark_job_failed(job, str(exc), transient=exc.transient,
                               payload=payload, response=exc.response)
        return
    erp_db.mark_job_sent(job, payload=payload, response=result["raw"],
                         erp_order_id=result["order_id"], codparc=codparc)
    log.info("Pedido %s enviado ao Sankhya (%s)", tx.get("order_number"), result["order_id"])


def _process_cancel(job: Dict) -> None:
    with get_conn() as conn:
        tx = erp_db.load_transaction_for_erp(conn, int(job["transaction_id"]))
        order_job = erp_db.get_order_job(conn, int(job["transaction_id"]))
    if tx is None or order_job is None or order_job["status"] != "enviado":
        if order_job and order_job["status"] in ("pendente", "processando"):
            # Pedido ainda saindo: espera ele terminar antes de cancelar.
            erp_db.mark_job_failed(job, "Aguardando o envio do pedido.", transient=True)
            return
        erp_db.mark_job_discarded(int(job["id"]), "O pedido nunca chegou ao ERP.")
        return
    try:
        result = sankhya_api.cancel_order(tx.get("erp_order_id"), tx.get("order_number"))
    except sankhya_api.SankhyaError as exc:
        erp_db.mark_job_failed(job, str(exc), transient=exc.transient,
                               final=not exc.transient, response=exc.response)
        return
    erp_db.mark_job_sent(job, payload=None, response=result)


def process_outbox_once(limit: int = 20) -> Dict[str, int]:
    """Uma passada pela fila. Sem configuração completa, não envia nada."""
    out = {"processed": 0, "skipped": 0}
    if not sankhya_api.is_configured():
        return out
    settings = erp_db.get_erp_settings()
    if erp_db.orders_readiness(settings):
        return out
    erp_db.reset_stale_processing()
    for job in erp_db.claim_due_jobs(limit):
        try:
            if job["kind"] == "pedido":
                _process_order(job, settings)
            else:
                _process_cancel(job)
            out["processed"] += 1
        except Exception as exc:  # nunca derruba o worker
            log.exception("Falha inesperada ao processar item %s da fila ERP", job["id"])
            erp_db.mark_job_failed(job, f"Erro interno: {exc}", transient=False)
    return out


_wake = threading.Event()
_worker_started = False
_worker_lock = threading.Lock()


def notify_worker() -> None:
    """Acorda o worker (venda confirmada, reenvio pedido pelo admin)."""
    _wake.set()


def _worker_loop() -> None:
    while True:
        try:
            while process_outbox_once()["processed"]:
                pass
        except Exception:
            log.exception("Worker da fila ERP falhou; tentando de novo no próximo ciclo")
        _wake.wait(WORKER_INTERVAL_SECONDS)
        _wake.clear()


def start_worker() -> bool:
    """Sobe o worker uma vez por processo. Desligue com ``TOTEM_ERP_WORKER=0``."""
    global _worker_started
    if (os.environ.get("TOTEM_ERP_WORKER") or "1").strip() == "0":
        return False
    with _worker_lock:
        if _worker_started:
            return False
        _worker_started = True
    threading.Thread(target=_worker_loop, name="sankhya-outbox", daemon=True).start()
    return True
