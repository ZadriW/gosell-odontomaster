"""Totem Odonto Master - aplicação Flask (boas-vindas + painéis admin/vendedor).

- ``/``                Tela de boas-vindas; o catálogo público não é autoatendimento.
- ``/catalogo`` etc. Rotas antigas redirecionam para ``/`` (venda só com vendedor logado).
- ``/api/...``         Endpoints JSON (vendas exigem sessão do vendedor).
- ``/admin/...``       Painel administrativo.
- ``/vendedor/...``    Painel do vendedor (catálogo/venda, minhas vendas, estoque, movimentações, transações).
"""

from __future__ import annotations

import csv
import io
import os
import re
import secrets
import subprocess
import sys

import totem_env  # noqa: F401 — carrega .env / totem.env antes da integração Wake
from receipt_tokens import sign_receipt_token, verify_receipt_token
from functools import wraps
from datetime import date, datetime

try:
    import xlrd as _xlrd  # .xls legacy (BIFF)
    _XLRD_AVAILABLE = True
except ImportError:
    _xlrd = None
    _XLRD_AVAILABLE = False


def _pip_install(package: str) -> None:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", package],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _load_xlsx_workbook(file_bytes: bytes):
    """Abre um .xlsx com openpyxl, instalando a lib neste Python se ainda faltar."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        try:
            _pip_install("openpyxl>=3.1.0,<4")
            from openpyxl import load_workbook
        except Exception as exc:
            raise RuntimeError(
                "Não foi possível usar o openpyxl neste Python. "
                f"Execute: {sys.executable} -m pip install openpyxl"
            ) from exc
    return load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)

from flask import (
    Flask,
    Response,
    current_app,
    flash,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from itsdangerous import BadSignature, URLSafeSerializer
from werkzeug.routing.exceptions import BuildError
from werkzeug.security import check_password_hash, generate_password_hash

from data.products import CATEGORIES
from database import (
    RULE_TYPE_LABELS,
    active_promotion_names_by_product_id,
    active_promotion_tooltip_by_product_id,
    build_promo_display_map,
    create_promotion,
    delete_promotion,
    enrich_product_with_promo,
    get_active_promotions_for_event,
    get_promotion,
    list_promotions_for_event,
    product_ids_with_active_promotions_for_event,
    quote_cart_items_for_event,
    toggle_promotion_active,
    update_promotion,
    add_product_to_event,
    add_seller_to_event,
    archive_event,
    count_products_admin_filtered,
    create_event,
    create_seller_account,
    confirm_item_delivery,
    confirm_items_delivery,
    confirm_transaction_handover,
    confirm_transaction_with_aut,
    count_event_product_ledger,
    count_pending_delivery_transactions,
    EXPORT_STOCK_CSV_CAP,
    create_transaction,
    update_pending_transaction,
    delete_seller,
    ensure_seller_account,
    event_badge_style_pairs,
    find_product_by_sku_or_id,
    get_active_event_for_seller,
    get_event,
    get_event_financial_report,
    get_event_sales_dashboard,
    get_event_stats,
    get_event_stock_stats,
    get_product,
    get_product_events_stock_total,
    get_product_in_event,
    get_products_library_stats,
    cancel_pending_transaction_for_seller,
    get_pending_transaction_if_owned,
    get_pending_transaction_restore_payload,
    get_seller,
    get_seller_admin_event_selection_id,
    get_seller_by_email,
    get_seller_by_username,
    get_stats,
    get_transaction_by_order_number,
    init_db,
    DEFAULT_MIN_STOCK,
    list_active_event_product_stocks,
    list_active_product_stocks,
    list_distinct_product_categories,
    count_event_products_filtered,
    count_stock_movements,
    count_transactions_for_event,
    count_transactions_for_seller,
    list_event_product_ledger,
    list_event_products,
    list_event_products_filtered_for_client,
    list_event_products_for_client,
    list_event_products_slice,
    list_event_sellers,
    list_event_stock_movements,
    list_events,
    list_products_admin,
    list_products_admin_slice,
    list_products_for_client,
    list_sellers,
    list_stock_movements,
    list_transaction_items_for_event_period,
    list_transactions,
    list_transactions_for_event,
    list_transactions_for_seller,
    list_transactions_summary_for_event_period,
    normalize_event_badge_color,
    pending_delivery_units_by_product_for_event,
    units_sold_by_product_for_event,
    units_sold_by_product_for_seller,
    refund_transaction,
    register_event_stock_adjustment,
    register_event_stock_entry,
    register_event_stock_exit,
    remove_product_from_event,
    replace_seller_event_assignment,
    reset_totem_to_default_state,
    restore_event,
    set_product_active,
    upsert_wake_variant,
    update_event,
    update_event_product_backorder_limit,
    update_event_product_price,
    update_event_product_stock,
    update_seller_account,
    update_seller_last_login,
    validate_seller_username,
)
import product_images
import wake_api


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

app = Flask(__name__)

# SECRET_KEY persistida em totem.env (totem_env.ensure_persistent_secret_key).
app.secret_key = os.environ.get("TOTEM_SECRET_KEY") or secrets.token_hex(32)

# CSRF (Flask-WTF): mesma chave da sessão; sem limite de tempo para o token na sessão atual.
app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(_e):
    accept = (request.headers.get("Accept") or "").lower()
    wants_json = (
        request.path.startswith("/api/")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in accept
    )
    if wants_json:
        return jsonify(
            error=(
                "Sessão expirada ou token de segurança inválido. "
                "Recarregue a página e tente novamente."
            ),
        ), 400
    flash(
        "Sessão expirada ou token de segurança inválido. Recarregue a página e tente novamente.",
        "error",
    )
    return redirect(request.referrer or url_for("welcome"))

# Credenciais do admin — sobrescreva em produção via variável de ambiente.
ADMIN_USERNAME = os.environ.get("TOTEM_ADMIN_USER", "adminmaster")
ADMIN_PASSWORD = os.environ.get("TOTEM_ADMIN_PASS", "adminmaster430@")

# Conta inicial do painel de vendedores. Em produção, sobrescreva por ambiente.
SELLER_DEFAULT_NAME = os.environ.get("TOTEM_SELLER_NAME", "Vendedor")
SELLER_DEFAULT_USERNAME = os.environ.get("TOTEM_SELLER_USER", "vendedor")
SELLER_DEFAULT_EMAIL = os.environ.get("TOTEM_SELLER_EMAIL", "vendedor@odontomaster.local")
SELLER_DEFAULT_PASSWORD = os.environ.get("TOTEM_SELLER_PASS", "vendedor123")
ADMIN_AUTH_COOKIE = "totem_admin_auth"
SELLER_AUTH_COOKIE = "totem_seller_auth"
ADMIN_AUTH_SALT = "totem-admin-auth"
SELLER_AUTH_SALT = "totem-seller-auth"
AUTH_COOKIE_OPTIONS = {
    "httponly": True,
    "samesite": "Lax",
}


def _url_if_registered(endpoint: str, *, fallback: str) -> str:
    """Evita BuildError se o endpoint ainda não existir no mapa de rotas."""
    try:
        views = current_app.view_functions
    except RuntimeError:
        return fallback
    if endpoint in views:
        return url_for(endpoint)
    return fallback


def _seller_pending_cancel_url(tx_id: int) -> str:
    """POST para descartar pedido pendente; fallback seguro se ``url_for`` falhar."""
    eid = int(tx_id)

    def fallback_path() -> str:
        prefix = (
            (request.script_root or "").rstrip("/") if has_request_context() else ""
        )
        return f"{prefix}/vendedor/pedido/{eid}/cancelar-pendente"

    try:
        views = current_app.view_functions
    except RuntimeError:
        return fallback_path()
    if "seller_cancel_pending_transaction" not in views:
        return fallback_path()
    try:
        return url_for("seller_cancel_pending_transaction", tx_id=eid)
    except BuildError:
        return fallback_path()


def _normalize_payment_method_for_db(value) -> str:
    v = (value or "").strip().lower()
    if v in ("pix", "cartao", "dinheiro"):
        return v
    return "cartao"


def _payment_method_label(value, card_installments=None) -> str:
    v = (value or "").strip().lower()
    if v == "pix":
        return "PIX"
    if v == "dinheiro":
        return "Dinheiro"
    if v == "cartao":
        try:
            n = int(card_installments)
        except (TypeError, ValueError):
            n = None
        if n is not None and n > 1:
            return f"Cartão em {n}x"
        return "Cartão"
    return "—"


def _format_brl(value) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    formatted = f"{n:,.2f}"
    formatted = formatted.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"R$ {formatted}"


def _card_installment_plan_text(total, payment_method, card_installments):
    """Texto do parcelamento no cartão (ex.: ``2x de R$187,25``)."""
    pm = (payment_method or "").strip().lower()
    if pm != "cartao":
        return None
    try:
        n = int(card_installments)
    except (TypeError, ValueError):
        return None
    if n < 2:
        return None
    try:
        t = float(total or 0)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    per = round(t / n, 2)
    return f"{n}x de {_format_brl(per)}"


def _display_created_by(value) -> str:
    """Texto exibível do campo created_by (remove prefixo interno ``vendedor:``)."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower().startswith("vendedor:"):
        return s[9:].strip()
    if s.lower().startswith("seller:"):
        tail = s[7:].strip()
        if tail.isdigit():
            row = get_seller(int(tail))
            if row and row.get("name"):
                return str(row["name"]).strip()
        return tail or s
    return s


def _event_badge_style_filter(raw) -> str:
    """CSS inline para badge de evento (fundo + texto legível)."""
    bg, fg = event_badge_style_pairs(raw)
    if not bg or not fg:
        return ""
    return f"background-color:{bg};color:{fg};"


def _parcelas_cartao_filter(total, payment_method, installments):
    """Filtro Jinja: ``{{ total | parcelas_cartao(pm, inst) }}``."""
    return _card_installment_plan_text(total, payment_method, installments) or ""


# Registro imediato: garante o filtro Jinja mesmo com importações parciais / reload.
app.add_template_filter(_display_created_by, "display_created_by")
app.add_template_filter(_event_badge_style_filter, "event_badge_style")
app.add_template_filter(_parcelas_cartao_filter, "parcelas_cartao")

# Inicializa o schema e popula o catálogo inicial (se vazio).
init_db()

# Conta inicial só se o usuário padrão ainda não existir — nunca regrava senha.
try:
    if (
        get_seller_by_username(SELLER_DEFAULT_USERNAME) is None
        and get_seller_by_email(SELLER_DEFAULT_EMAIL) is None
    ):
        ensure_seller_account(
            SELLER_DEFAULT_NAME,
            SELLER_DEFAULT_USERNAME,
            generate_password_hash(SELLER_DEFAULT_PASSWORD),
            pin_hash=None,
        )
except Exception as exc:
    app.logger.warning("Conta inicial de vendedor indisponivel: %s", exc)


def _auth_serializer(salt: str) -> URLSafeSerializer:
    return URLSafeSerializer(app.secret_key, salt=salt)


def _load_auth_cookie(cookie_name: str, salt: str) -> dict | None:
    token = request.cookies.get(cookie_name)
    if not token:
        return None
    try:
        data = _auth_serializer(salt).loads(token)
    except BadSignature:
        return None
    return data if isinstance(data, dict) else None


def _set_auth_cookie(response, cookie_name: str, salt: str, payload: dict):
    response.set_cookie(
        cookie_name,
        _auth_serializer(salt).dumps(payload),
        **AUTH_COOKIE_OPTIONS,
    )
    return response


def _delete_auth_cookie(response, cookie_name: str):
    response.delete_cookie(cookie_name, samesite=AUTH_COOKIE_OPTIONS["samesite"])
    return response


def _admin_auth() -> dict | None:
    data = _load_auth_cookie(ADMIN_AUTH_COOKIE, ADMIN_AUTH_SALT)
    if data and data.get("is_admin"):
        return data
    if session.get("is_admin"):
        return {
            "is_admin": True,
            "admin_user": session.get("admin_user", "admin"),
        }
    return None


def _seller_auth() -> dict | None:
    data = _load_auth_cookie(SELLER_AUTH_COOKIE, SELLER_AUTH_SALT)
    if data and data.get("is_seller"):
        try:
            sid = int(data.get("seller_id"))
        except (TypeError, ValueError):
            sid = 0
        if sid > 0:
            out = dict(data)
            out["seller_id"] = sid
            return out
    if session.get("is_seller"):
        try:
            sid = int(session.get("seller_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid > 0:
            return {
                "is_seller": True,
                "seller_id": sid,
                "seller_name": session.get("seller_name", "Vendedor"),
                "seller_username": session.get("seller_username") or session.get("seller_email", ""),
            }
    return None


def _is_admin_logged_in() -> bool:
    return bool(_admin_auth())


def _is_seller_logged_in() -> bool:
    return bool(_seller_auth())


def _totem_theme_scope() -> str | None:
    """Escopo de preferência visual: admin ou vendedor, por login."""
    path = (request.path or "").lower()
    if path.startswith("/admin"):
        if _is_admin_logged_in():
            return f"admin:{_current_admin_user()}"
        return "admin:_guest"
    if path.startswith("/vendedor"):
        auth = _seller_auth()
        if auth and auth.get("seller_id"):
            return f"seller:{int(auth['seller_id'])}"
        return "seller:_guest"
    return None


@app.context_processor
def _inject_totem_theme_scope():
    return {"totem_theme_scope": _totem_theme_scope()}


def _clear_admin_session() -> None:
    """Remove apenas credenciais do painel admin (preserva vendedor na mesma sessão)."""
    for key in ("is_admin", "admin_user"):
        session.pop(key, None)


def _clear_seller_session() -> None:
    """Remove apenas credenciais do painel do vendedor (preserva admin na mesma sessão)."""
    for key in ("is_seller", "seller_id", "seller_name", "seller_email", "seller_username"):
        session.pop(key, None)


def _current_admin_user() -> str:
    auth = _admin_auth() or {}
    return auth.get("admin_user") or "admin"


def _current_seller_id() -> int:
    auth = _seller_auth() or {}
    return int(auth["seller_id"])


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_admin_logged_in():
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def seller_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = _seller_auth()
        if not auth:
            return redirect(url_for("seller_login", next=request.path))
        seller = get_seller(int(auth["seller_id"]))
        if seller is None or not seller.get("active"):
            response = redirect(url_for("seller_login"))
            _clear_seller_session()
            return _delete_auth_cookie(response, SELLER_AUTH_COOKIE)
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Rotas do cliente
# ---------------------------------------------------------------------------

@app.route("/")
def welcome():
    return render_template("welcome.html")


@app.route("/catalogo")
def catalog():
    return redirect(url_for("welcome"))


@app.route("/pagamento")
def payment():
    return redirect(url_for("welcome"))


@app.route("/pagamento/aguardando")
def payment_waiting():
    return redirect(url_for("welcome"))


# ---------------------------------------------------------------------------
# API pública (consumida pelo front do totem)
# ---------------------------------------------------------------------------

def _parse_seller_total(payload: dict, client: dict):
    """Total negociado pelo vendedor (desconto manual). None = usar total calculado."""
    raw = payload.get("seller_total")
    if raw is None:
        raw = client.get("seller_total")
    if raw is None or raw == "":
        return None
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        raise ValueError("Valor total informado é inválido.") from None


@app.route("/api/produtos")
def api_products():
    category = request.args.get("categoria", "").strip()
    query = request.args.get("q", "").strip().lower()
    products = list_products_for_client(category=category or None, query=query or None)
    return jsonify(products)


@app.route("/api/transacoes", methods=["POST"])
def api_create_transaction():
    """Cria uma transação pendente (estoque ainda não baixado) e retorna o ``id``.

    O estoque só é decrescido após o vendedor digitar e confirmar o AUT via
    ``PATCH /api/transacoes/<id>/aut``.
    """
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or payload.get("itens") or []
    client = payload.get("client") or {}
    payment_method = _normalize_payment_method_for_db(
        payload.get("payment_method") or client.get("payment_method"),
    )

    # CRO: UF e número informados no checkout (sem API externa)
    cro_uf = (client.get("cro_uf") or "").strip().upper()
    cro_numero = (client.get("cro_numero") or "").strip()

    try:
        inst_raw = payload.get("installments")
        if inst_raw is None:
            inst_raw = client.get("installments")
        card_installments = None
        if inst_raw not in (None, ""):
            try:
                card_installments = int(inst_raw)
            except (TypeError, ValueError):
                raise ValueError("Número de parcelas inválido.") from None

        auth = _seller_auth()
        if not auth:
            raise ValueError(
                "É necessário estar logado como vendedor para registrar a venda."
            )
        row = get_seller(int(auth["seller_id"]))
        if row is None or not row.get("active"):
            raise ValueError("Sessão de vendedor inválida ou inativa.")
        seller = {"id": int(row["id"]), "name": row["name"]}

        # Busca o evento ativo do vendedor (se houver)
        active_event = get_active_event_for_seller(int(seller["id"]))
        event_id = int(active_event["id"]) if active_event else None

        result = create_transaction(
            items,
            created_by=f"vendedor:{seller['name']}",
            seller_id=int(seller["id"]),
            seller_name=seller["name"],
            event_id=event_id,
            client_name=client.get("name"),
            client_cpf=client.get("cpf"),
            client_email=client.get("email"),
            client_phone=client.get("phone"),
            client_zipcode=client.get("zipcode"),
            client_address=client.get("address"),
            client_number=client.get("number"),
            client_complement=client.get("complement"),
            client_city=client.get("city"),
            client_state=client.get("state"),
            payment_method=payment_method,
            card_installments=card_installments,
            client_cro_uf=cro_uf or None,
            client_cro_numero=cro_numero or None,
            seller_total=_parse_seller_total(payload, client),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Falha ao criar transação pendente")
        return jsonify({"error": "Não foi possível registrar a transação."}), 500

    # Remove chaves internas (_normalized, _demand, _event_id, _created_by) da resposta.
    public = {k: v for k, v in result.items() if not k.startswith("_")}
    return jsonify(public), 201


@app.route("/api/transacoes/<int:tx_id>", methods=["PATCH"])
def api_update_pending_transaction(tx_id: int):
    """Atualiza pedido pendente (ex.: retomada após alterar carrinho ou pagamento)."""
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or payload.get("itens") or []
    client = payload.get("client") or {}
    payment_method = _normalize_payment_method_for_db(
        payload.get("payment_method") or client.get("payment_method"),
    )

    cro_uf = (client.get("cro_uf") or "").strip().upper()
    cro_numero = (client.get("cro_numero") or "").strip()

    try:
        inst_raw = payload.get("installments")
        if inst_raw is None:
            inst_raw = client.get("installments")
        card_installments = None
        if inst_raw not in (None, ""):
            try:
                card_installments = int(inst_raw)
            except (TypeError, ValueError):
                raise ValueError("Número de parcelas inválido.") from None

        auth = _seller_auth()
        if not auth:
            raise ValueError(
                "É necessário estar logado como vendedor para atualizar o pedido."
            )
        row = get_seller(int(auth["seller_id"]))
        if row is None or not row.get("active"):
            raise ValueError("Sessão de vendedor inválida ou inativa.")

        result = update_pending_transaction(
            int(tx_id),
            seller_id=int(row["id"]),
            items=items,
            client_name=client.get("name"),
            client_cpf=client.get("cpf"),
            client_email=client.get("email"),
            client_phone=client.get("phone"),
            client_zipcode=client.get("zipcode"),
            client_address=client.get("address"),
            client_number=client.get("number"),
            client_complement=client.get("complement"),
            client_city=client.get("city"),
            client_state=client.get("state"),
            payment_method=payment_method,
            card_installments=card_installments,
            client_cro_uf=cro_uf or None,
            client_cro_numero=cro_numero or None,
            seller_total=_parse_seller_total(payload, client),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Falha ao atualizar transação pendente")
        return jsonify({"error": "Não foi possível atualizar o pedido."}), 500

    return jsonify(result), 200


@app.route("/api/transacoes/<int:tx_id>/aut", methods=["PATCH"])
def api_confirm_transaction_aut(tx_id: int):
    """Salva o AUT, confirma a transação e baixa o estoque.

    Body JSON: ``{"aut": "123456"}``
    """
    payload = request.get_json(silent=True) or {}
    aut = (payload.get("aut") or "").strip()

    try:
        auth = _seller_auth()
        if not auth:
            raise ValueError(
                "É necessário estar logado como vendedor para confirmar a venda."
            )
        row = get_seller(int(auth["seller_id"]))
        if row is None or not row.get("active"):
            raise ValueError("Sessão de vendedor inválida ou inativa.")
        seller_name = row["name"]

        result = confirm_transaction_with_aut(
            tx_id, aut, created_by=f"vendedor:{seller_name}"
        )
        order_number = (result.get("order_number") or "").strip()
        if order_number:
            result["receipt_token"] = sign_receipt_token(
                order_number, app.secret_key
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Falha ao confirmar transação com AUT")
        return jsonify({"error": "Não foi possível confirmar a transação."}), 500

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Painel administrativo — autenticação
# ---------------------------------------------------------------------------

def _admin_home_url() -> str:
    """Página inicial do painel administrativo (lista de eventos)."""
    return url_for("admin_events")


@app.route("/admin")
def admin_index():
    if not _is_admin_logged_in():
        return redirect(url_for("admin_login"))
    return redirect(_admin_home_url())


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if _is_admin_logged_in():
        return redirect(_admin_home_url())

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            next_url = request.args.get("next") or _admin_home_url()
            if not next_url.startswith("/"):
                next_url = _admin_home_url()
            response = redirect(next_url)
            return _set_auth_cookie(
                response,
                ADMIN_AUTH_COOKIE,
                ADMIN_AUTH_SALT,
                {"is_admin": True, "admin_user": username},
            )
        error = "Usuário ou senha inválidos."
    return render_template("admin/login.html", error=error)


@app.route("/admin/logout", methods=["POST", "GET"])
def admin_logout():
    _clear_admin_session()
    response = redirect(url_for("admin_login"))
    return _delete_auth_cookie(response, ADMIN_AUTH_COOKIE)


# ---------------------------------------------------------------------------
# Painel do vendedor — autenticação e leitura
# ---------------------------------------------------------------------------

@app.route("/vendedor")
def seller_index():
    if not _is_seller_logged_in():
        return redirect(url_for("seller_login"))
    return redirect(_seller_home_url())


@app.route("/vendedor/login", methods=["GET", "POST"])
def seller_login():
    if _is_seller_logged_in():
        return redirect(_seller_home_url())

    error = None
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        seller = get_seller_by_username(username)
        if seller and seller.get("active") and check_password_hash(
            seller["password_hash"], password
        ):
            update_seller_last_login(int(seller["id"]))
            next_url = request.args.get("next") or _seller_home_url()
            if not next_url.startswith("/vendedor"):
                next_url = _seller_home_url()
            login_id = seller.get("username") or seller.get("email") or username
            response = redirect(next_url)
            return _set_auth_cookie(
                response,
                SELLER_AUTH_COOKIE,
                SELLER_AUTH_SALT,
                {
                    "is_seller": True,
                    "seller_id": int(seller["id"]),
                    "seller_name": seller["name"],
                    "seller_username": login_id,
                    "seller_email": login_id,
                },
            )
        error = "Usuário ou senha inválidos."

    return render_template("seller/login.html", error=error, username=username)


@app.route("/vendedor/logout", methods=["POST", "GET"])
def seller_logout():
    _clear_seller_session()
    response = redirect(url_for("seller_login"))
    return _delete_auth_cookie(response, SELLER_AUTH_COOKIE)


def _seller_home_url() -> str:
    """Página inicial do painel do vendedor (catálogo / venda)."""
    url = _url_if_registered("seller_sale", fallback="/vendedor/venda")
    return (url or "").strip() or "/vendedor/venda"


def _seller_shell_context(**extra):
    auth = _seller_auth() or {}
    seller_id_raw = auth.get("seller_id")
    seller_event = (
        get_active_event_for_seller(int(seller_id_raw))
        if seller_id_raw
        else None
    )
    ctx = {
        "seller_name": auth.get("seller_name", "Vendedor"),
        "seller_username": auth.get("seller_username") or auth.get("seller_email", ""),
        "now": datetime.now(),
        "seller_event": seller_event,
        "seller_pending_cancel_url": _seller_pending_cancel_url,
        "seller_home_url": _seller_home_url(),
    }
    ctx.update(extra)
    venda_url = _seller_home_url()
    ctx["seller_venda_url"] = venda_url
    return ctx


def _get_seller_event():
    """Retorna o evento ativo do vendedor logado, ou None."""
    try:
        seller_id = _current_seller_id()
    except (KeyError, TypeError):
        return None
    return get_active_event_for_seller(seller_id)


def _seller_pending_sales_count(seller_id: int, seller_ev) -> int:
    """Conta vendas pendentes do vendedor (escopo do evento quando houver)."""
    if seller_ev:
        return count_transactions_for_event(
            int(seller_ev["id"]),
            seller_id=seller_id,
            status="pendente",
        )
    return count_transactions_for_seller(seller_id, status="pendente")


def _seller_totem_flow() -> dict:
    """URLs do fluxo de venda no painel (consumido pelo JS)."""
    return {
        "payment": _url_if_registered(
            "seller_payment", fallback="/vendedor/pagamento"
        ),
        "paymentWaiting": _url_if_registered(
            "seller_payment_waiting", fallback="/vendedor/pagamento/aguardando"
        ),
        "catalog": _seller_home_url(),
        "home": _seller_home_url(),
    }


def _seller_payment_page_context() -> dict:
    flow = _seller_totem_flow()
    return {
        "totem_flow": flow,
        "payment_catalog_url": _url_if_registered(
            "seller_sale", fallback="/vendedor/venda"),
        "payment_home_url": _seller_home_url(),
        "resume_pending_aut": None,
        "seller_backorder": True,
    }


def _attach_pending_delivery_units(
    products: list,
    event_id: int,
    *,
    seller_id: int | None = None,
) -> None:
    """Anexa ``pending_delivery_units`` (retirada pendente) a cada produto do evento."""
    if not products:
        return
    pending_map = pending_delivery_units_by_product_for_event(
        event_id,
        product_ids=[int(p["id"]) for p in products],
        seller_id=seller_id,
    )
    for p in products:
        p["pending_delivery_units"] = int(pending_map.get(int(p["id"]), 0))


@app.route("/vendedor/venda", endpoint="seller_sale")
@seller_required
def seller_sale():
    seller_ev = _get_seller_event()
    if seller_ev:
        products = list_event_products_for_client(seller_ev["id"])
        # Enriquece cada produto com dados de promoção ativa do evento.
        promos = get_active_promotions_for_event(seller_ev["id"])
        promo_map = build_promo_display_map(promos)
        products = [enrich_product_with_promo(p, promo_map) for p in products]
        _attach_pending_delivery_units(
            products, seller_ev["id"], seller_id=_current_seller_id(),
        )
        # No modo evento, estoque + preços + promoções vêm do mesmo polling (15s).
        catalog_stock_api_url = ""
        catalog_promo_refresh_api_url = url_for(
            "seller_api_event_catalog_promos_refresh",
        )
    else:
        products = list_products_for_client()
        catalog_stock_api_url = _url_if_registered(
            "seller_api_catalog_stock",
            fallback="/vendedor/api/catalogo/estoque",
        )
        catalog_promo_refresh_api_url = ""
    return render_template(
        "seller/catalog.html",
        products=products,
        totem_flow=_seller_totem_flow(),
        catalog_stock_api_url=catalog_stock_api_url,
        catalog_promo_refresh_api_url=catalog_promo_refresh_api_url,
        **_seller_shell_context(active_section="venda"),
    )


@app.route("/vendedor/pagamento", endpoint="seller_payment")
@seller_required
def seller_payment():
    return render_template("payment.html", **_seller_payment_page_context())


@app.route("/vendedor/pagamento/aguardando", endpoint="seller_payment_waiting")
@seller_required
def seller_payment_waiting():
    ctx = dict(_seller_payment_page_context())
    resume = None
    raw = (request.args.get("pendente") or "").strip()
    if raw.isdigit():
        sid = _current_seller_id()
        row = get_pending_transaction_if_owned(int(raw), sid)
        if row:
            resume = {
                "transaction_id": row["id"],
                "order_number": row["order_number"],
            }
    ctx["resume_pending_aut"] = resume
    return render_template("payment_waiting.html", **ctx)


@app.route("/vendedor/pedido/<int:tx_id>/refazer-checkout", endpoint="seller_restore_pending_checkout")
@seller_required
def seller_restore_pending_checkout(tx_id: int):
    """Restaura carrinho + formulário do cliente e envia à tela de pagamento (pedido pendente de AUT)."""
    sid = _current_seller_id()
    payload = get_pending_transaction_restore_payload(tx_id, sid)
    if not payload or not payload.get("cart_items"):
        flash(
            "Não foi possível restaurar este pedido. Verifique se está pendente de AUT e se os produtos ainda existem.",
            "error",
        )
        return redirect(url_for("seller_dashboard"))
    return render_template(
        "seller/restore_checkout.html",
        restore_payload=payload,
        payment_url=url_for("seller_payment"),
    )


@app.route(
    "/vendedor/pedido/<int:tx_id>/cancelar-pendente",
    methods=["POST"],
    endpoint="seller_cancel_pending_transaction",
)
@seller_required
def seller_cancel_pending_transaction(tx_id: int):
    """Descarta pedido pendente (marca como cancelado); não altera estoque nem faturamento."""
    try:
        cancel_pending_transaction_for_seller(tx_id, _current_seller_id())
        flash("Pedido pendente descartado.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("seller_dashboard"))


def _seller_dashboard_transactions_view() -> tuple:
    """Lista paginada de transações do vendedor logado (escopo do evento quando houver)."""
    seller_id = _current_seller_id()
    seller_ev = _get_seller_event()

    pedido = (request.args.get("pedido") or "").strip()
    status_raw = (request.args.get("status") or "").strip().lower()
    valid_status = frozenset({"todos", "confirmado", "pendente", "cancelado", "estornado", "entregue"})
    status_norm = status_raw if status_raw in valid_status else "todos"

    entrega_raw = (request.args.get("entrega") or "").strip().lower()
    entrega_norm = entrega_raw if entrega_raw == "parcial" else "todos"
    delivery_api = None if entrega_norm == "todos" else entrega_norm

    date_arg_raw = (request.args.get("data") or "").strip()
    on_date = _parse_tx_filter_date_arg(date_arg_raw)
    filter_date_display = on_date or ""

    per_page = SELLER_DASHBOARD_TX_PER_PAGE
    page = max(1, _parse_int(request.args.get("page"), 1))
    status_api = None if status_norm == "todos" else status_norm

    if seller_ev:
        ev_id = int(seller_ev["id"])
        total = count_transactions_for_event(
            ev_id,
            seller_id=seller_id,
            order_search=pedido or None,
            status=status_api,
            on_date=on_date,
            delivery=delivery_api,
        )
    else:
        total = count_transactions_for_seller(
            seller_id,
            order_search=pedido or None,
            status=status_api,
            on_date=on_date,
            delivery=delivery_api,
        )

    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    if seller_ev:
        transactions = list_transactions_for_event(
            int(seller_ev["id"]),
            seller_id=seller_id,
            order_search=pedido or None,
            status=status_api,
            on_date=on_date,
            delivery=delivery_api,
            limit=per_page,
            offset=offset,
        )
    else:
        transactions = list_transactions_for_seller(
            seller_id,
            order_search=pedido or None,
            status=status_api,
            on_date=on_date,
            delivery=delivery_api,
            limit=per_page,
            offset=offset,
        )

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(transactions), total) if total > 0 else 0

    filters = {
        "pedido": pedido,
        "status": status_norm,
        "entrega": entrega_norm,
        "data": filter_date_display,
    }
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "showing_from": showing_from,
        "showing_to": showing_to,
    }
    return transactions, filters, pagination


@app.route("/vendedor/dashboard")
@seller_required
def seller_dashboard():
    seller_id = _current_seller_id()
    seller_ev = _get_seller_event()
    stats = get_stats(seller_id=seller_id)
    transactions, filters, pagination = _seller_dashboard_transactions_view()
    pending_sales_count = _seller_pending_sales_count(seller_id, seller_ev)
    pending_delivery_count = count_pending_delivery_transactions(
        seller_id=seller_id,
        event_id=int(seller_ev["id"]) if seller_ev else None,
    )
    return render_template(
        "seller/dashboard.html",
        stats=stats,
        pending_sales_count=pending_sales_count,
        pending_delivery_count=pending_delivery_count,
        transactions=transactions,
        filters=filters,
        pagination=pagination,
        seller_id=seller_id,
        **_seller_shell_context(active_section="dashboard"),
    )


@app.route("/vendedor/estoque")
@seller_required
def seller_stock():
    seller_ev = _get_seller_event()
    if seller_ev:
        ev_id = seller_ev["id"]
        seller_id = _current_seller_id()
        products, filters, pagination = _seller_event_stock_page_view(ev_id, seller_id)
        ev_stats = get_event_stock_stats(ev_id)
        stock = {
            "products_count": ev_stats["products_count"],
            "products_active": ev_stats["products_count"],
            "units_in_stock": ev_stats["units_in_stock"],
            "stock_value": ev_stats["stock_value"],
            "below_min": ev_stats["below_min"],
            "out_of_stock": ev_stats["sem_estoque"],
        }
        stock_api_url = url_for(
            "seller_api_event_stock",
            q=filters["q"],
            status=filters["status"],
            per_page=filters["per_page"],
            page=pagination["page"],
        )
        promo_product_ids = product_ids_with_active_promotions_for_event(ev_id)
        promo_tooltips = active_promotion_tooltip_by_product_id(ev_id)
        promo_names = active_promotion_names_by_product_id(ev_id)
        return render_template(
            "seller/stock.html",
            products=products,
            stock=stock,
            categories=[],
            filters=filters,
            pagination=pagination,
            allowed_per_page=ALLOWED_ADMIN_STOCK_PER_PAGE,
            stock_api_url=stock_api_url,
            promo_product_ids=promo_product_ids,
            promo_tooltips=promo_tooltips,
            promo_names=promo_names,
            **_seller_shell_context(active_section="estoque"),
        )
    products, filters, pagination = _admin_stock_page_view()
    seller_id = _current_seller_id()
    product_ids = [int(p["id"]) for p in products]
    sold_map = units_sold_by_product_for_seller(seller_id, product_ids=product_ids)
    for p in products:
        p["units_sold"] = int(sold_map.get(int(p["id"]), 0))
    return render_template(
        "seller/stock.html",
        products=products,
        stock=get_products_library_stats(),
        categories=_admin_stock_library_category_options(),
        filters=filters,
        pagination=pagination,
        allowed_per_page=ALLOWED_ADMIN_STOCK_PER_PAGE,
        stock_api_url=url_for("seller_api_stock",
                              q=filters["q"], categoria=filters["categoria"],
                              status=filters["status"],
                              per_page=filters["per_page"], page=pagination["page"]),
        promo_product_ids=frozenset(),
        promo_tooltips={},
        promo_names={},
        **_seller_shell_context(active_section="estoque"),
    )


@app.route("/vendedor/estoque/<int:product_id>")
@seller_required
def seller_stock_product(product_id: int):
    """Página de produto individual para vendedor: vendas e retiradas pendentes dele."""
    seller_id = _current_seller_id()
    seller_ev = _get_seller_event()

    if seller_ev:
        product = get_product_in_event(int(seller_ev["id"]), product_id)
        if product is None:
            flash("Produto não encontrado neste evento.", "error")
            return redirect(url_for("seller_stock"))
        product = _enrich_product_promo_for_event(product, int(seller_ev["id"]))
    else:
        product = get_product(product_id)
        if product is None:
            flash("Produto não encontrado.", "error")
            return redirect(url_for("seller_stock"))

    pedido = (request.args.get("pedido") or "").strip()
    per_page = EVENT_PRODUCT_MOVEMENTS_PER_PAGE
    page = max(1, _parse_int(request.args.get("page"), 1))

    if seller_ev:
        # Mescla vendas confirmadas + linhas sintéticas de retirada pendente (como no admin).
        ev_id = int(seller_ev["id"])
        total = count_event_product_ledger(
            ev_id,
            product_id,
            reference=pedido or None,
            seller_id=seller_id,
        )
        total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        movements = list_event_product_ledger(
            ev_id,
            product_id,
            reference=pedido or None,
            seller_id=seller_id,
            limit=per_page,
            offset=offset,
        )
    else:
        total = count_stock_movements(
            product_id=product_id,
            movement_type="venda",
            reference=pedido or None,
            seller_id=seller_id,
        )
        total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        movements = list_stock_movements(
            product_id=product_id,
            movement_type="venda",
            reference=pedido or None,
            seller_id=seller_id,
            limit=per_page,
            offset=offset,
        )

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(movements), total) if total > 0 else 0

    filters = {
        "pedido": pedido,
    }
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "showing_from": showing_from,
        "showing_to": showing_to,
    }

    return render_template(
        "seller/stock_product.html",
        product=product,
        movements=movements,
        filters=filters,
        pagination=pagination,
        **_seller_shell_context(active_section="estoque"),
    )


@app.route("/vendedor/transacoes")
@seller_required
def seller_transactions():
    """Redireciona para o dashboard (histórico de transações unificado)."""
    return redirect(url_for("seller_dashboard", **request.args.to_dict()))


def _seller_transaction_api(tx: dict) -> dict:
    """Serializa transação para o endpoint JSON do vendedor (somente leitura)."""
    items = tx.get("items") or []
    return {
        "id": int(tx["id"]),
        "order_number": tx["order_number"],
        "created_at_display": datahora_filter(tx["created_at"]),
        "items_count": int(tx["items_count"] or 0),
        "total_display": brl_filter(tx["total"]),
        "status": tx["status"],
        "delivery_status": tx.get("delivery_status") or "completa",
        "payment_method": tx.get("payment_method"),
        "payment_method_label": _payment_method_label(
            tx.get("payment_method"),
            tx.get("card_installments"),
        ),
        "parcelas_cartao": _card_installment_plan_text(
            tx.get("total"),
            tx.get("payment_method"),
            tx.get("card_installments"),
        ),
        "aut": tx.get("aut") or None,
        "items": [
            {
                "product_name": it.get("product_name") or "",
                "product_sku": it.get("product_sku"),
                "category": it.get("category") or "-",
                "quantity": int(it.get("quantity") or 0),
                "unit_price_display": brl_filter(it.get("unit_price")),
                "subtotal_display": brl_filter(it.get("subtotal")),
            }
            for it in items
        ],
    }


@app.route("/vendedor/api/estoque")
@seller_required
def seller_api_stock():
    seller_ev = _get_seller_event()
    if seller_ev:
        ev_id = seller_ev["id"]
        seller_id = _current_seller_id()
        products = list_event_products_for_client(ev_id)
        sold_map = units_sold_by_product_for_event(
            ev_id, product_ids=[int(p["id"]) for p in products], seller_id=seller_id,
        )
        for p in products:
            p["units_sold"] = int(sold_map.get(int(p["id"]), 0))
        ev_stats = get_event_stock_stats(ev_id)
        stock = {
            "products_count": ev_stats["products_count"],
            "products_active": ev_stats["products_count"],
            "units_in_stock": ev_stats["units_in_stock"],
            "stock_value": ev_stats["stock_value"],
            "below_min": ev_stats["below_min"],
            "out_of_stock": ev_stats["sem_estoque"],
        }
        return jsonify({
            "stock": stock,
            "pagination": {"page": 1, "per_page": len(products), "total": len(products), "total_pages": 1},
            "products": [{**p, "status": _event_product_status({
                "stock": p["estoque"], "min_stock": p["estoque_minimo"], "product_active": p["ativo"],
            })} for p in products],
        })
    products, _filters, pagination = _admin_stock_page_view()
    seller_id = _current_seller_id()
    product_ids = [int(p["id"]) for p in products]
    sold_map = units_sold_by_product_for_seller(seller_id, product_ids=product_ids)
    for p in products:
        p["units_sold"] = int(sold_map.get(int(p["id"]), 0))
    return jsonify({
        "stock": get_products_library_stats(),
        "pagination": {
            "page": pagination["page"],
            "per_page": pagination["per_page"],
            "total": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
        "products": [
            {**p, "status": _product_status(p)}
            for p in products
        ],
    })


@app.route("/vendedor/api/evento/estoque")
@seller_required
def seller_api_event_stock():
    """API de polling para a listagem de estoque do evento no painel do vendedor."""
    seller_ev = _get_seller_event()
    if seller_ev is None:
        return jsonify({"error": "Não associado a nenhum evento ativo."}), 404
    ev_id = seller_ev["id"]
    seller_id = _current_seller_id()
    products, _filters, pagination = _seller_event_stock_page_view(ev_id, seller_id)
    ev_stats = get_event_stock_stats(ev_id)
    promo_ids = product_ids_with_active_promotions_for_event(ev_id)
    promo_tooltips = active_promotion_tooltip_by_product_id(ev_id)
    promo_names = active_promotion_names_by_product_id(ev_id)
    stock = {
        "products_count": ev_stats["products_count"],
        "products_active": ev_stats["products_count"],
        "units_in_stock": ev_stats["units_in_stock"],
        "stock_value": ev_stats["stock_value"],
        "below_min": ev_stats["below_min"],
        "out_of_stock": ev_stats["sem_estoque"],
    }
    return jsonify({
        "stock": stock,
        "pagination": {
            "page": pagination["page"],
            "per_page": pagination["per_page"],
            "total": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
        "products": [
            {
                **p,
                "status": _event_product_status({
                    "stock": p["estoque"],
                    "min_stock": p["estoque_minimo"],
                    "product_active": p["ativo"],
                }),
                "active_promo": int(p["id"]) in promo_ids,
                "promo_tooltip": promo_tooltips.get(int(p["id"]), ""),
                "promo_name": promo_names.get(int(p["id"]), ""),
            }
            for p in products
        ],
    })


@app.route(
    "/vendedor/api/catalogo/estoque",
    endpoint="seller_api_catalog_stock",
)
@seller_required
def seller_api_catalog_stock():
    """Payload mínimo para atualizar estoque nos cards do catálogo (polling)."""
    return jsonify({"products": list_active_product_stocks()})


@app.route("/vendedor/api/evento/catalogo/estoque", endpoint="seller_api_event_catalog_stock")
@seller_required
def seller_api_event_catalog_stock():
    """Polling de estoque do catálogo em modo evento."""
    seller_ev = _get_seller_event()
    if seller_ev is None:
        return jsonify({"products": list_active_product_stocks()})
    products = list_active_event_product_stocks(seller_ev["id"])
    _attach_pending_delivery_units(
        products, seller_ev["id"], seller_id=_current_seller_id(),
    )
    return jsonify({"products": products})


@app.route(
    "/vendedor/api/evento/catalogo/precos-promocoes",
    endpoint="seller_api_event_catalog_promos_refresh",
)
@seller_required
def seller_api_event_catalog_promos_refresh():
    """Catálogo do evento com preços e promoções recalculados (polling ~15s no front).

    Retorna o mesmo formato que ``list_event_products_for_client``, já enriquecido
    com ``em_promocao``, ``preco_original``, ``promo_badge``, etc.
    """
    seller_ev = _get_seller_event()
    if seller_ev is None:
        return jsonify(
            {"error": "Este endpoint só se aplica a vendedores associados a um evento."}
        ), 404
    products = list_event_products_for_client(seller_ev["id"])
    promos = get_active_promotions_for_event(seller_ev["id"])
    promo_map = build_promo_display_map(promos)
    products = [enrich_product_with_promo(p, promo_map) for p in products]
    _attach_pending_delivery_units(
        products, seller_ev["id"], seller_id=_current_seller_id(),
    )
    return jsonify({"products": products})


@app.route("/api/carrinho/cotacao", methods=["POST"])
@seller_required
def api_cart_promo_quote():
    """Cotação promocional do carrinho (mesma lógica da criação de transação)."""
    seller_ev = _get_seller_event()
    if seller_ev is None:
        return jsonify({"error": "Cotação disponível apenas para vendas em evento."}), 404
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or payload.get("itens") or []
    try:
        quote = quote_cart_items_for_event(int(seller_ev["id"]), items)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(quote)


@app.route("/vendedor/api/dashboard")
@seller_required
def seller_api_dashboard():
    """JSON com os mesmos dados do dashboard."""
    seller_id = _current_seller_id()
    seller_ev = _get_seller_event()
    transactions = list_transactions(limit=100, seller_id=seller_id)
    latest_tx_id = max((int(t["id"]) for t in transactions), default=0)
    pending_sales_count = _seller_pending_sales_count(seller_id, seller_ev)
    pending_delivery_count = count_pending_delivery_transactions(
        seller_id=seller_id,
        event_id=int(seller_ev["id"]) if seller_ev else None,
    )
    return jsonify({
        "stats": get_stats(seller_id=seller_id),
        "pending_sales_count": pending_sales_count,
        "pending_delivery_count": pending_delivery_count,
        "latest_tx_id": latest_tx_id,
        "transactions": [_seller_transaction_api(t) for t in transactions],
    })


# ---------------------------------------------------------------------------
# Painel administrativo — vendedores
# ---------------------------------------------------------------------------

_SELLER_FORM_FIELD_ORDER = ("name", "username", "event_id", "password")


def _first_seller_form_error_message(errors: dict[str, str]) -> str:
    for key in _SELLER_FORM_FIELD_ORDER:
        if key in errors:
            return errors[key]
    return next(iter(errors.values()), "Verifique os campos destacados.")


def _parse_new_seller_post(form) -> tuple[dict[str, str], dict[str, str]]:
    """Validação do cadastro de vendedor. Retorna (erros_por_campo, valores_para_reexibir)."""
    name = (form.get("name") or "").strip()
    username = (form.get("username") or form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    event_id_raw = (form.get("event_id") or "").strip()
    event_id = _parse_int(event_id_raw, 0)

    errors: dict[str, str] = {}
    if not name:
        errors["name"] = "Nome do vendedor é obrigatório."
    try:
        username = validate_seller_username(username)
    except ValueError as exc:
        errors["username"] = str(exc)

    if len(password) < 6:
        errors["password"] = "A senha deve ter pelo menos 6 caracteres."

    if event_id <= 0:
        errors["event_id"] = "Selecione o evento ao qual este vendedor será associado."
    elif get_event(event_id) is None:
        errors["event_id"] = "Evento não encontrado."

    if "username" not in errors and username and get_seller_by_username(username):
        errors["username"] = "Já existe um vendedor com este usuário."

    def keep(field: str, value: str) -> str:
        return "" if field in errors else value

    repop = {
        "name": keep("name", name),
        "username": keep("username", username),
        "password": keep("password", password),
        "event_id": "" if "event_id" in errors else event_id_raw,
    }
    return errors, repop


def _parse_edit_seller_post(form, _seller_id: int) -> tuple[dict[str, str], dict]:
    """Validação da edição de vendedor. Retorna (erros_por_campo, valores_para_reexibir)."""
    name = (form.get("name") or "").strip()
    username = (form.get("username") or form.get("email") or "").strip().lower()
    active = form.get("active") == "1"
    password = form.get("password") or ""
    event_id_raw = (form.get("event_id") or "").strip()

    errors: dict[str, str] = {}
    if not name:
        errors["name"] = "Nome do vendedor é obrigatório."
    try:
        username = validate_seller_username(username)
    except ValueError as exc:
        errors["username"] = str(exc)

    if password and len(password) < 6:
        errors["password"] = "A nova senha deve ter pelo menos 6 caracteres."

    if not event_id_raw:
        pass  # sem evento permitido
    else:
        eid = _parse_int(event_id_raw, 0)
        if eid <= 0:
            errors["event_id"] = "Selecione um evento válido ou «Sem evento»."
        elif get_event(eid) is None:
            errors["event_id"] = "Evento não encontrado."

    repop = {
        "name": "" if "name" in errors else name,
        "username": "" if "username" in errors else username,
        "active": active,
        "password": "" if "password" in errors else password,
        "event_id": "" if "event_id" in errors else event_id_raw,
    }
    return errors, repop


@app.route("/admin/vendedores", methods=["GET", "POST"])
@admin_required
def admin_sellers():
    seller_form = None
    seller_form_errors: dict[str, str] = {}
    if request.method == "POST":
        seller_form_errors, seller_form = _parse_new_seller_post(request.form)
        if not seller_form_errors:
            try:
                event_id = _parse_int(request.form.get("event_id") or "", 0)
                seller = create_seller_account(
                    (request.form.get("name") or "").strip(),
                    (request.form.get("username") or request.form.get("email") or "").strip().lower(),
                    generate_password_hash(request.form.get("password") or ""),
                    pin_hash=None,
                )
                add_seller_to_event(event_id, int(seller["id"]))
                ev = get_event(event_id)
                ev_label = (ev or {}).get("name") or f"#{event_id}"
                flash(
                    f"Vendedor {seller['name']} criado e associado ao evento «{ev_label}».",
                    "success",
                )
                return redirect(url_for("admin_seller_detail", seller_id=seller["id"]))
            except ValueError as exc:
                msg = str(exc)
                if "usuário" in msg.lower() and ("já" in msg.lower() or "existente" in msg.lower()):
                    seller_form_errors = {"username": msg}
                    seller_form = _parse_new_seller_post(request.form)[1]
                    seller_form["username"] = ""
                else:
                    flash(msg, "error")
                    seller_form = _parse_new_seller_post(request.form)[1]
                    seller_form_errors = {}
        if seller_form_errors:
            flash(_first_seller_form_error_message(seller_form_errors), "error")

    sellers = list_sellers()
    events_for_seller_form = list_events(include_archived=True)
    return render_template(
        "admin/sellers.html",
        sellers=sellers,
        seller_form=seller_form,
        seller_form_errors=seller_form_errors,
        events_for_seller_form=events_for_seller_form,
        **_admin_shell_context(active_section="vendedores"),
    )


def _admin_seller_transactions_view(seller_id: int):
    """Lista paginada de vendas do vendedor (admin) com filtros de status/entrega."""
    pedido = (request.args.get("pedido") or "").strip()
    status_raw = (request.args.get("status") or "").strip().lower()
    valid_status = frozenset({"todos", "confirmado", "pendente", "cancelado", "estornado", "entregue"})
    status_norm = status_raw if status_raw in valid_status else "todos"
    status_api = None if status_norm == "todos" else status_norm

    entrega_raw = (request.args.get("entrega") or "").strip().lower()
    entrega_norm = entrega_raw if entrega_raw == "parcial" else "todos"
    delivery_api = None if entrega_norm == "todos" else entrega_norm

    date_arg_raw = (request.args.get("data") or "").strip()
    on_date = _parse_tx_filter_date_arg(date_arg_raw)
    filter_date_display = on_date or ""

    per_page = _parse_int(request.args.get("per_page"), DEFAULT_ADMIN_MOVEMENTS_PER_PAGE)
    if per_page not in ALLOWED_ADMIN_STOCK_PER_PAGE:
        per_page = DEFAULT_ADMIN_MOVEMENTS_PER_PAGE
    page = max(1, _parse_int(request.args.get("page"), 1))

    total = count_transactions_for_seller(
        seller_id,
        order_search=pedido or None,
        status=status_api,
        on_date=on_date,
        delivery=delivery_api,
    )
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    transactions = list_transactions_for_seller(
        seller_id,
        order_search=pedido or None,
        status=status_api,
        on_date=on_date,
        delivery=delivery_api,
        limit=per_page,
        offset=offset,
    )

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(transactions), total) if total > 0 else 0

    filters = {
        "pedido": pedido,
        "status": status_norm,
        "entrega": entrega_norm,
        "data": filter_date_display,
        "per_page": per_page,
    }
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "showing_from": showing_from,
        "showing_to": showing_to,
    }
    return transactions, filters, pagination


@app.route("/admin/vendedores/<int:seller_id>")
@admin_required
def admin_seller_detail(seller_id: int):
    seller = get_seller(seller_id)
    if seller is None:
        flash("Vendedor não encontrado.", "error")
        return redirect(url_for("admin_sellers"))
    stats = get_stats(seller_id=seller_id)
    transactions, filters, pagination = _admin_seller_transactions_view(seller_id)
    events_for_seller_form = list_events(include_archived=True)
    seller_primary_event_id = get_seller_admin_event_selection_id(seller_id)
    return render_template(
        "admin/seller_detail.html",
        seller=seller,
        stats=stats,
        transactions=transactions,
        filters=filters,
        pagination=pagination,
        allowed_per_page=ALLOWED_ADMIN_STOCK_PER_PAGE,
        seller_form=None,
        seller_form_errors={},
        events_for_seller_form=events_for_seller_form,
        seller_primary_event_id=seller_primary_event_id,
        **_admin_shell_context(active_section="vendedores"),
    )


@app.route("/admin/vendedores/<int:seller_id>/editar", methods=["POST"])
@admin_required
def admin_seller_update(seller_id: int):
    seller = get_seller(seller_id)
    if seller is None:
        flash("Vendedor não encontrado.", "error")
        return redirect(url_for("admin_sellers"))

    seller_form_errors, seller_form = _parse_edit_seller_post(request.form, seller_id)
    if seller_form_errors:
        flash(_first_seller_form_error_message(seller_form_errors), "error")
        stats = get_stats(seller_id=seller_id)
        transactions, filters, pagination = _admin_seller_transactions_view(seller_id)
        events_for_seller_form = list_events(include_archived=True)
        seller_primary_event_id = get_seller_admin_event_selection_id(seller_id)
        return render_template(
            "admin/seller_detail.html",
            seller=seller,
            stats=stats,
            transactions=transactions,
            filters=filters,
            pagination=pagination,
            allowed_per_page=ALLOWED_ADMIN_STOCK_PER_PAGE,
            seller_form=seller_form,
            seller_form_errors=seller_form_errors,
            events_for_seller_form=events_for_seller_form,
            seller_primary_event_id=seller_primary_event_id,
            **_admin_shell_context(active_section="vendedores"),
        )

    name = (request.form.get("name") or "").strip()
    username = (request.form.get("username") or request.form.get("email") or "").strip().lower()
    active = request.form.get("active") == "1"
    password = request.form.get("password") or ""
    event_raw = (request.form.get("event_id") or "").strip()
    assigned_event_id = None if not event_raw else _parse_int(event_raw, 0)
    password_hash = None
    if password:
        password_hash = generate_password_hash(password)
    try:
        seller = update_seller_account(
            seller_id,
            name=name,
            username=username,
            active=active,
            password_hash=password_hash,
            clear_pin_hash=True,
        )
    except ValueError as exc:
        msg = str(exc)
        seller_form_errors: dict[str, str] = {}
        if "usuário" in msg.lower() or "usuario" in msg.lower():
            seller_form_errors["username"] = msg
        else:
            flash(msg, "error")
            return redirect(url_for("admin_seller_detail", seller_id=seller_id))
        seller_form = _parse_edit_seller_post(request.form, seller_id)[1]
        seller_form["username"] = ""
        flash(_first_seller_form_error_message(seller_form_errors), "error")
        stats = get_stats(seller_id=seller_id)
        transactions, filters, pagination = _admin_seller_transactions_view(seller_id)
        events_for_seller_form = list_events(include_archived=True)
        seller_primary_event_id = get_seller_admin_event_selection_id(seller_id)
        return render_template(
            "admin/seller_detail.html",
            seller=seller,
            stats=stats,
            transactions=transactions,
            filters=filters,
            pagination=pagination,
            allowed_per_page=ALLOWED_ADMIN_STOCK_PER_PAGE,
            seller_form=seller_form,
            seller_form_errors=seller_form_errors,
            events_for_seller_form=events_for_seller_form,
            seller_primary_event_id=seller_primary_event_id,
            **_admin_shell_context(active_section="vendedores"),
        )

    try:
        replace_seller_event_assignment(seller_id, assigned_event_id)
    except ValueError as exc:
        flash(
            f"Dados de {seller['name']} foram atualizados, mas o vínculo com o evento não foi alterado: {exc}",
            "error",
        )
    else:
        flash(f"Dados de {seller['name']} atualizados.", "success")
    return redirect(url_for("admin_seller_detail", seller_id=seller_id))


@app.route(
    "/admin/vendedores/<int:seller_id>/transacoes/<int:tx_id>/itens/<int:item_id>/entregar",
    methods=["POST"],
)
@admin_required
def admin_seller_confirm_item_delivery(seller_id: int, tx_id: int, item_id: int):
    """Confirma a retirada de um item pendente (escopo do vendedor no admin)."""
    seller = get_seller(seller_id)
    if seller is None:
        flash("Vendedor não encontrado.", "error")
        return redirect(url_for("admin_sellers"))
    try:
        result = confirm_item_delivery(
            tx_id,
            item_id,
            seller_id=seller_id,
            created_by=_current_admin_user(),
        )
        msg = (
            f"Entrega confirmada: {result['delivered_now']} un. de "
            f"'{result['product_name']}'."
        )
        if result["still_pending"] > 0:
            msg += f" Ainda pendente: {result['still_pending']} un."
        flash(msg, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        request.referrer or url_for("admin_seller_detail", seller_id=seller_id)
    )


@app.route(
    "/admin/vendedores/<int:seller_id>/transacoes/<int:tx_id>/entregar",
    methods=["POST"],
)
@admin_required
def admin_seller_confirm_items_delivery(seller_id: int, tx_id: int):
    """Confirma a retirada de vários itens pendentes (escopo do vendedor no admin)."""
    seller = get_seller(seller_id)
    if seller is None:
        flash("Vendedor não encontrado.", "error")
        return redirect(url_for("admin_sellers"))
    item_ids = request.form.getlist("item_ids")
    try:
        result = confirm_items_delivery(
            tx_id,
            item_ids,
            seller_id=seller_id,
            created_by=_current_admin_user(),
        )
        msg = (
            f"Entrega confirmada: {result['items_count']} produto(s), "
            f"{result['units_delivered']} un."
        )
        if result.get("errors"):
            msg += " Alguns itens não puderam ser entregues (verifique o estoque)."
            flash(msg, "warning")
            for err in result["errors"][:5]:
                flash(err, "error")
        else:
            flash(msg, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        request.referrer or url_for("admin_seller_detail", seller_id=seller_id)
    )


@app.route(
    "/admin/vendedores/<int:seller_id>/transacoes/<int:tx_id>/entregue",
    methods=["POST"],
)
@admin_required
def admin_seller_confirm_transaction_handover(seller_id: int, tx_id: int):
    """Marca o pedido inteiro como entregue/retirado (escopo do vendedor no admin)."""
    seller = get_seller(seller_id)
    if seller is None:
        flash("Vendedor não encontrado.", "error")
        return redirect(url_for("admin_sellers"))
    try:
        confirm_transaction_handover(tx_id, seller_id=seller_id)
        flash("Pedido marcado como entregue.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        request.referrer or url_for("admin_seller_detail", seller_id=seller_id)
    )


@app.route(
    "/admin/vendedores/<int:seller_id>/excluir",
    methods=["POST"],
    endpoint="admin_seller_delete",
)
@admin_required
def admin_seller_delete(seller_id: int):
    try:
        deleted = delete_seller(seller_id)
        flash(
            f"Cadastro de {deleted['name']} excluído. "
            "Vendas antigas permanecem no histórico, sem vínculo a este vendedor.",
            "success",
        )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin_sellers"))


# ---------------------------------------------------------------------------
# Financeiro
# ---------------------------------------------------------------------------

def _parse_fin_filters():
    """Lê parâmetros de filtro da rota Financeiro."""
    event_id_raw = request.args.get("evento") or ""
    date_from = (request.args.get("de") or "").strip()
    date_to = (request.args.get("ate") or "").strip()
    # validação mínima de formato
    import re
    _date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if date_from and not _date_re.match(date_from):
        date_from = ""
    if date_to and not _date_re.match(date_to):
        date_to = ""
    try:
        ev_id = int(event_id_raw) if event_id_raw else None
    except ValueError:
        ev_id = None
    return ev_id, date_from or None, date_to or None


@app.route("/admin/financeiro")
@admin_required
def admin_financeiro():
    all_events = list_events(include_archived=True)
    ev_id, date_from, date_to = _parse_fin_filters()
    if ev_id is None and all_events:
        ev_id = int(all_events[0]["id"])
    report = None
    selected_event = None
    if ev_id is not None:
        selected_event = get_event(ev_id)
        if selected_event:
            report = get_event_financial_report(
                ev_id, date_from=date_from, date_to=date_to
            )
    return render_template(
        "admin/financeiro.html",
        selected_event_id=ev_id,
        date_from=date_from or "",
        date_to=date_to or "",
        report=report,
        **_admin_shell_context(active_section="financeiro"),
    )


@app.route("/admin/financeiro/pdf")
@admin_required
def admin_financeiro_pdf():
    """Renderiza a versão para impressão/PDF do relatório financeiro."""
    ev_id, date_from, date_to = _parse_fin_filters()
    if ev_id is None:
        return redirect(url_for("admin_financeiro"))
    event = get_event(ev_id)
    if event is None:
        flash("Evento não encontrado.", "error")
        return redirect(url_for("admin_financeiro"))
    report = get_event_financial_report(ev_id, date_from=date_from, date_to=date_to)
    return render_template(
        "admin/financeiro_pdf.html",
        report=report,
        all_events=list_events(include_archived=True),
        selected_event_id=ev_id,
        date_from=date_from or "",
        date_to=date_to or "",
        now=datetime.now(),
    )


@app.route("/admin/reiniciar-sistema", methods=["POST"])
@admin_required
def admin_reset_system():
    """Apaga vendas, dados de clientes, histórico de estoque e zera todos os produtos."""
    if request.form.get("confirm_reset") != "1":
        flash(
            "Para reiniciar o sistema, marque a caixa de confirmação e tente novamente.",
            "error",
        )
        return redirect(_admin_home_url())
    try:
        result = reset_totem_to_default_state()
        flash(
            "Sistema reiniciado com sucesso. "
            f"{result['transactions_deleted']} venda(s) e dados de cliente removidos; "
            f"{result['movements_deleted']} movimentação(ões) apagadas; "
            f"estoque de {result['products_restored']} produto(s) zerado no cadastro; "
            f"{result['event_product_pairs_reset']} vínculo(s) produto×evento com saldo zerado.",
            "success",
        )
    except Exception:
        app.logger.exception("Falha ao reiniciar o sistema")
        flash(
            "Não foi possível reiniciar o sistema. Verifique os logs ou tente novamente.",
            "error",
        )
    return redirect(_admin_home_url())


# ---------------------------------------------------------------------------
# Painel administrativo — estoque
# ---------------------------------------------------------------------------

def _admin_shell_context(**extra):
    """Contexto comum para todas as telas do painel (topbar/nav)."""
    return {
        "admin_user": _current_admin_user(),
        "now": datetime.now(),
        "all_events": list_events(include_archived=True),
        "admin_home_url": _admin_home_url(),
        **extra,
    }


ALLOWED_ADMIN_STOCK_PER_PAGE = (10, 25, 50, 100)
DEFAULT_ADMIN_STOCK_PER_PAGE = 25
DEFAULT_ADMIN_MOVEMENTS_PER_PAGE = 25
EVENT_PRODUCT_MOVEMENTS_PER_PAGE = 20
SELLER_DASHBOARD_TX_PER_PAGE = 20
EVENT_IMPORT_XLS_MOTIVO_REF = "Importação por Planilha"


def _admin_stock_list_query_params():
    """Parâmetros GET compartilhados entre biblioteca admin/vendedor e suas APIs JSON."""
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("categoria") or "").strip()
    status = (request.args.get("status") or "").strip()
    per_page = _parse_int(request.args.get("per_page"), DEFAULT_ADMIN_STOCK_PER_PAGE)
    if per_page not in ALLOWED_ADMIN_STOCK_PER_PAGE:
        per_page = DEFAULT_ADMIN_STOCK_PER_PAGE
    page = max(1, _parse_int(request.args.get("page"), 1))
    return q, category, status, per_page, page


def _admin_stock_page_view(*, ignore_status_filter: bool = False):
    q_display, category, status, per_page, page = _admin_stock_list_query_params()
    q_lower = q_display.lower() if q_display else ""
    q_filter = q_lower or None
    cat_norm = category or "todos"
    stat_norm = "todos" if ignore_status_filter else (status or "todos")

    total = count_products_admin_filtered(q_filter, cat_norm, stat_norm)
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    products = list_products_admin_slice(
        q_filter,
        cat_norm,
        stat_norm,
        limit=per_page,
        offset=offset,
    )

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(products), total) if total > 0 else 0

    filters = {
        "q": q_display,
        "categoria": cat_norm,
        "status": stat_norm,
        "per_page": per_page,
    }
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "showing_from": showing_from,
        "showing_to": showing_to,
    }
    return products, filters, pagination


def _seller_event_stock_page_view(event_id: int, seller_id: int):
    """Lista paginada do estoque do evento no painel do vendedor (busca + situação, sem categoria)."""
    q_display, _category, status, per_page, page = _admin_stock_list_query_params()
    q_lower = q_display.lower() if q_display else ""
    q_filter = q_lower or None
    stat_norm = (status or "todos").strip().lower()
    if stat_norm not in {"todos", "ok", "baixo", "sem_estoque", "inativo"}:
        stat_norm = "todos"

    total = count_event_products_filtered(event_id, q_filter, "todos", stat_norm)
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    products = list_event_products_filtered_for_client(
        event_id,
        q_filter,
        stat_norm,
        limit=per_page,
        offset=offset,
    )
    product_ids = [int(p["id"]) for p in products]
    sold_map = units_sold_by_product_for_event(
        event_id,
        product_ids=product_ids,
        seller_id=seller_id,
    )
    for p in products:
        p["units_sold"] = int(sold_map.get(int(p["id"]), 0))

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(products), total) if total > 0 else 0

    filters = {
        "q": q_display,
        "status": stat_norm,
        "per_page": per_page,
    }
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "showing_from": showing_from,
        "showing_to": showing_to,
    }
    return products, filters, pagination


def _admin_event_stock_page_view(event_id: int):
    """Lista paginada de produtos no evento com os mesmos filtros GET da biblioteca geral."""
    q_display, category, status, per_page, page = _admin_stock_list_query_params()
    entrega_raw = (request.args.get("entrega") or "todos").strip().lower()
    entrega_norm = entrega_raw if entrega_raw in {"todos", "pendente"} else "todos"
    q_lower = q_display.lower() if q_display else ""
    q_filter = q_lower or None
    cat_norm = category or "todos"
    stat_norm = status or "todos"

    total = count_event_products_filtered(
        event_id, q_filter, cat_norm, stat_norm, entrega=entrega_norm,
    )
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    products = list_event_products_slice(
        event_id,
        q_filter,
        cat_norm,
        stat_norm,
        limit=per_page,
        offset=offset,
        entrega=entrega_norm,
    )
    product_ids = [int(p["product_id"]) for p in products]
    pending_map = pending_delivery_units_by_product_for_event(
        event_id,
        product_ids=product_ids,
    )
    sold_map = units_sold_by_product_for_event(
        event_id,
        product_ids=product_ids,
    )
    for p in products:
        pid = int(p["product_id"])
        p["pending_delivery_units"] = int(pending_map.get(pid, 0))
        p["units_sold"] = int(sold_map.get(pid, 0))

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(products), total) if total > 0 else 0

    filters = {
        "q": q_display,
        "categoria": cat_norm,
        "status": stat_norm,
        "entrega": entrega_norm,
        "per_page": per_page,
    }
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "showing_from": showing_from,
        "showing_to": showing_to,
    }
    return products, filters, pagination


def _event_stock_return_filters_from_form() -> dict:
    """Lê ret_* dos POSTs para preservar filtros ao redirecionar."""
    per_page = _parse_int(request.form.get("ret_per_page"), DEFAULT_ADMIN_STOCK_PER_PAGE)
    if per_page not in ALLOWED_ADMIN_STOCK_PER_PAGE:
        per_page = DEFAULT_ADMIN_STOCK_PER_PAGE
    entrega = (request.form.get("ret_entrega") or "todos").strip().lower()
    if entrega not in {"todos", "pendente"}:
        entrega = "todos"
    return {
        "q": (request.form.get("ret_q") or "").strip(),
        "categoria": (request.form.get("ret_categoria") or "todos").strip(),
        "status": (request.form.get("ret_status") or "todos").strip(),
        "entrega": entrega,
        "per_page": per_page,
        "page": max(1, _parse_int(request.form.get("ret_page"), 1)),
    }


def _url_for_admin_event_stock_list(event_id: int, filters: dict, *, page_override: int | None = None) -> str:
    per_page = _parse_int(filters.get("per_page"), DEFAULT_ADMIN_STOCK_PER_PAGE)
    if per_page not in ALLOWED_ADMIN_STOCK_PER_PAGE:
        per_page = DEFAULT_ADMIN_STOCK_PER_PAGE
    if page_override is not None:
        pg = max(1, int(page_override))
    else:
        pg = max(1, _parse_int(filters.get("page"), 1))
    kw = {
        "event_id": event_id,
        "categoria": (filters.get("categoria") or "todos").strip(),
        "status": (filters.get("status") or "todos").strip(),
        "entrega": (filters.get("entrega") or "todos").strip(),
        "per_page": per_page,
        "page": pg,
    }
    q = (filters.get("q") or "").strip()
    if q:
        kw["q"] = q
    return url_for("admin_event_stock", **kw)


@app.route("/admin/estoque")
@admin_required
def admin_stock_legacy_redirect():
    qs = request.query_string.decode()
    target = url_for("admin_products")
    return redirect(f"{target}?{qs}" if qs else target, code=302)


def _admin_stock_library_category_options() -> list[str]:
    """Opções do filtro Categoria: banco (fonte de verdade) + cache Wake após sync."""
    db_cats = list_distinct_product_categories()
    seen = {c.casefold() for c in db_cats}
    merged = list(db_cats)
    for c in CATEGORIES:
        label = str(c).strip() if c is not None else ""
        if label and label.casefold() not in seen:
            seen.add(label.casefold())
            merged.append(label)
    return sorted(merged, key=lambda s: s.casefold())


@app.route("/admin/produtos")
@admin_required
def admin_products():
    products, filters, pagination = _admin_stock_page_view(ignore_status_filter=True)
    return render_template(
        "admin/products.html",
        products=products,
        stock=get_products_library_stats(),
        categories=_admin_stock_library_category_options(),
        filters=filters,
        pagination=pagination,
        allowed_per_page=ALLOWED_ADMIN_STOCK_PER_PAGE,
        events_for_modal=list_events(include_archived=False),
        **_admin_shell_context(active_section="produtos"),
    )


@app.route("/admin/produtos/<int:product_id>/adicionar-ao-evento", methods=["POST"])
@admin_required
def admin_product_add_to_event(product_id: int):
    product = get_product(product_id)
    if product is None:
        return jsonify({"error": "Produto não encontrado."}), 404
    event_id = _parse_int(request.form.get("event_id") or "", 0)
    if event_id <= 0:
        return jsonify({"error": "Selecione um evento."}), 400
    event = get_event(event_id)
    if event is None:
        return jsonify({"error": "Evento não encontrado."}), 400
    initial_stock = max(0, _parse_int(request.form.get("initial_stock") or "", 0))
    min_stock = max(0, _parse_int(request.form.get("min_stock") or "", DEFAULT_MIN_STOCK))
    link_note = (request.form.get("link_note") or "").strip()
    if not link_note:
        return jsonify({"error": "Informe Motivo / Ref."}), 400
    try:
        add_product_to_event(
            event_id,
            product_id,
            initial_stock,
            min_stock,
            link_audit_reason=link_note,
            link_audit_reference=None,
            created_by=_current_admin_user(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({
        "ok": True,
        "message": f"\u00ab{product['name']}\u00bb adicionado ao evento \u00ab{event['name']}\u00bb.",
    })


@app.route("/admin/estoque/<int:product_id>")
@admin_required
def admin_stock_product_legacy_redirect(product_id: int):
    return redirect(url_for("admin_product_detail", product_id=product_id), code=302)


@app.route("/admin/produtos/<int:product_id>")
@admin_required
def admin_product_detail(product_id: int):
    product_base = get_product(product_id)
    if product_base is None:
        flash("Produto não encontrado.", "error")
        return redirect(url_for("admin_products"))
    ev_total = get_product_events_stock_total(product_id)
    product = dict(product_base)
    product["estoque"] = ev_total
    product["abaixo_minimo"] = product["estoque_minimo"] > 0 and ev_total < product["estoque_minimo"]
    product["sem_estoque"] = ev_total <= 0
    movements = list_stock_movements(product_id=product_id, limit=100)
    return render_template(
        "admin/product_detail.html",
        product=product,
        movements=movements,
        **_admin_shell_context(active_section="produtos"),
    )


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_tx_filter_date_arg(value) -> str | None:
    """``YYYY-MM-DD`` para filtro por dia em ``transactions.created_at``; inválido/absento → ``None``."""
    s = (value or "").strip()
    if len(s) != 10:
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s


def _wants_json_response() -> bool:
    """Detecta chamadas AJAX/API sem mudar o fluxo HTML existente."""
    return (
        request.accept_mimetypes.best == "application/json"
        or request.headers.get("X-Requested-With") == "fetch"
        or request.args.get("format") == "json"
    )


def _product_status(product: dict) -> dict:
    if not product["ativo"]:
        return {"label": "Inativo", "kind": "neutral"}
    if product["sem_estoque"]:
        return {"label": "Sem estoque", "kind": "danger"}
    if product["abaixo_minimo"]:
        return {"label": "Abaixo do mínimo", "kind": "warn"}
    return {"label": "OK", "kind": "success"}


def _cro_pedido_fields(row: dict | None) -> dict[str, str] | None:
    """UF e número do registro CRO salvos na transação (formulário de pagamento)."""
    if not row:
        return None

    def _strip_val(v):
        if v is None:
            return ""
        return str(v).strip()

    uf_s = _strip_val(row.get("client_cro_uf"))
    num_s = _strip_val(row.get("client_cro_numero"))
    if not uf_s and not num_s:
        return None
    out: dict[str, str] = {}
    if uf_s:
        out["uf"] = uf_s
    if num_s:
        out["numero_registro"] = num_s
    return out if out else None


def _movement_payload(movement: dict) -> dict:
    movement_type = movement.get("movement_type")
    ev_bg, ev_fg = event_badge_style_pairs(movement.get("event_badge_color"))
    try:
        d_raw = int(movement.get("delta") or 0)
    except (TypeError, ValueError):
        d_raw = 0
    if d_raw > 0:
        delta_kind = "positive"
    elif d_raw < 0:
        delta_kind = "negative"
    else:
        delta_kind = "neutral"
    return {
        **movement,
        "event_badge_bg": ev_bg,
        "event_badge_fg": ev_fg,
        "created_by_display": _display_created_by(movement.get("created_by")),
        "created_at_display": datahora_filter(movement.get("created_at")),
        "movement_label": mov_label_filter(movement_type),
        "delta_display": signed_filter(movement.get("delta")),
        "delta_kind": delta_kind,
        "product_url": url_for("admin_product_detail", product_id=movement["product_id"]),
    }


def _products_library_detail_payload(product_id: int, *, limit: int = 100) -> dict:
    product_base = get_product(product_id)
    if product_base is None:
        raise ValueError("Produto não encontrado.")
    ev_total = get_product_events_stock_total(product_id)
    product = dict(product_base)
    product["estoque"] = ev_total
    product["abaixo_minimo"] = product["estoque_minimo"] > 0 and ev_total < product["estoque_minimo"]
    product["sem_estoque"] = ev_total <= 0
    movements = list_stock_movements(product_id=product_id, limit=limit)
    return {
        "product": {
            **product,
            "status": _product_status(product),
            "stock_value_display": brl_filter(ev_total * product["preco"]),
        },
        "movements": [_movement_payload(m) for m in movements],
    }


def _json_products_library_success(message: str, product_id: int, status_code: int = 200):
    payload = _products_library_detail_payload(product_id)
    payload["message"] = message
    return jsonify(payload), status_code


def _enrich_product_promo_for_event(product: dict, event_id: int) -> dict:
    """Acrescenta ``em_promocao`` / ``promo_nome`` ao produto no contexto do evento."""
    promos = get_active_promotions_for_event(event_id)
    promo_map = build_promo_display_map(promos)
    return enrich_product_with_promo(dict(product), promo_map)


def _attach_promo_name_to_product(product: dict, event_id: int) -> dict:
    """Anexa campos de promoção ao produto sem alterar o preço de lista exibido."""
    enriched = _enrich_product_promo_for_event(product, event_id)
    out = dict(product)
    out["em_promocao"] = bool(enriched.get("em_promocao"))
    out["promo_nome"] = enriched.get("promo_nome") or ""
    out["promo_badge"] = enriched.get("promo_badge") or ""
    return out


def _event_stock_product_payload(event_id: int, product_id: int, *, limit: int = 100) -> dict:
    product = get_product_in_event(event_id, product_id)
    if product is None:
        raise ValueError("Produto não encontrado neste evento.")
    pending_units = pending_delivery_units_by_product_for_event(
        event_id, product_ids=[product_id],
    ).get(int(product_id), 0)
    product = dict(product)
    product["pending_delivery_units"] = int(pending_units)
    product = _attach_promo_name_to_product(product, event_id)
    movements = list_event_product_ledger(
        event_id, product_id, limit=limit, offset=0,
    )
    return {
        "product": {
            **product,
            "status": _event_product_status({
                "stock": product["estoque"],
                "min_stock": product["estoque_minimo"],
                "product_active": product["ativo"],
            }),
            "stock_value_display": brl_filter(product["estoque"] * product["preco"]),
        },
        "movements": [_event_movement_payload(m, event_id=event_id) for m in movements],
    }


def _json_event_stock_success(message: str, event_id: int, product_id: int, status_code: int = 200):
    payload = _event_stock_product_payload(event_id, product_id)
    payload["message"] = message
    return jsonify(payload), status_code


@app.route("/admin/produtos/<int:product_id>/ativar", methods=["POST"])
@app.route("/admin/estoque/<int:product_id>/ativar", methods=["POST"])
@admin_required
def admin_product_toggle_active(product_id: int):
    active = request.form.get("active") == "1"
    if set_product_active(product_id, active):
        message = (
            "Produto ativado e disponível no totem." if active
            else "Produto desativado — não aparecerá no totem."
        )
        if _wants_json_response():
            return _json_products_library_success(message, product_id)
        flash(message, "success")
    else:
        if _wants_json_response():
            return jsonify({"error": "Não foi possível atualizar o produto."}), 400
        flash("Não foi possível atualizar o produto.", "error")
    return redirect(request.referrer or url_for("admin_product_detail", product_id=product_id))


def _admin_api_products_list_payload():
    products, _filters, pagination = _admin_stock_page_view(ignore_status_filter=True)
    return jsonify({
        "stock": get_products_library_stats(),
        "pagination": {
            "page": pagination["page"],
            "per_page": pagination["per_page"],
            "total": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
        "products": [
            {**p, "status": _product_status(p)}
            for p in products
        ],
    })


@app.route("/admin/api/produtos")
@admin_required
def admin_api_products():
    return _admin_api_products_list_payload()


@app.route("/admin/api/estoque")
@admin_required
def admin_api_stock_legacy():
    return _admin_api_products_list_payload()


@app.route("/admin/api/produtos/<int:product_id>")
@admin_required
def admin_api_products_detail(product_id: int):
    try:
        return jsonify(_products_library_detail_payload(product_id))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404


@app.route("/admin/api/estoque/<int:product_id>")
@admin_required
def admin_api_stock_product_legacy(product_id: int):
    try:
        return jsonify(_products_library_detail_payload(product_id))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404


_EXPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _csv_cell(value):
    if value is None:
        return ""
    return value


def _csv_fmt_date(value) -> str:
    """Converte ISO datetime (AAAA-MM-DD...) para DD/MM/AAAA; retorna vazio se inválido."""
    s = str(value or "").strip().replace("T", " ")
    date_part = s[:10]  # AAAA-MM-DD
    try:
        return datetime.strptime(date_part, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return date_part


def _csv_fmt_time(value) -> str:
    """Extrai HH:MM de ISO datetime; retorna vazio se inválido."""
    s = str(value or "").strip().replace("T", " ")
    time_part = s[11:16] if len(s) >= 16 else ""  # HH:MM
    if len(time_part) == 5 and time_part[2] == ":":
        return time_part
    return ""


def _csv_fmt_brl(value) -> str:
    """Formata valor numérico como decimal com 2 casas (ponto como separador decimal)."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _csv_fmt_delta(value) -> str:
    """Formata variação de estoque com sinal explícito (+5 / -3)."""
    if value is None or value == "":
        return ""
    try:
        n = int(value)
        return f"+{n}" if n >= 0 else str(n)
    except (TypeError, ValueError):
        return str(value)


def _csv_fmt_status(value) -> str:
    """Capitaliza status da transação."""
    s = str(value or "").strip()
    return s.capitalize() if s else ""


def _csv_attachment_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    body = buf.getvalue().encode("utf-8-sig")
    return Response(
        body,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/admin/eventos/<int:event_id>/vendas/export.csv")
@admin_required
def admin_event_sales_export_csv(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))

    raw_from = (request.args.get("data_inicio") or "").strip()
    raw_to = (request.args.get("data_fim") or "").strip()
    for label, raw in (("Data inicial", raw_from), ("Data final", raw_to)):
        if raw and not _EXPORT_DATE_RE.fullmatch(raw):
            flash(f"{label}: use o formato AAAA-MM-DD.", "error")
            return redirect(url_for("admin_event_detail", event_id=event_id))

    nivel = (request.args.get("nivel") or "pedidos").strip().lower()
    if nivel not in ("pedidos", "itens"):
        nivel = "pedidos"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ev = re.sub(r"[^a-zA-Z0-9_-]+", "_", (event.get("name") or str(event_id)))[:40].strip("_") or str(event_id)

    if nivel == "pedidos":
        rows_data = list_transactions_summary_for_event_period(
            event_id,
            date_from=raw_from or None,
            date_to=raw_to or None,
        )
        # Ordem lógica: identificação → data/hora → situação → vendedor →
        #               financeiro → pagamento → cliente → CRO → ID interno
        header = [
            "Código do Pedido",
            "Data",
            "Hora",
            "Status",
            "Vendedor",
            "Qtd. de Itens",
            "Valor Total (R$)",
            "Forma de Pagamento",
            "AUT",
            "Nome do Cliente",
            "CPF",
            "CEP",
            "Endereço",
            "Número",
            "Complemento",
            "Cidade",
            "UF",
            "CRO UF",
            "Nº CRO",
            "ID Interno",
        ]
        rows = []
        for t in rows_data:
            created = t.get("created_at")
            rows.append(
                [
                    _csv_cell(t.get("order_number")),
                    _csv_fmt_date(created),
                    _csv_fmt_time(created),
                    _csv_fmt_status(t.get("status")),
                    _csv_cell(t.get("seller_name")),
                    _csv_cell(t.get("items_count")),
                    _csv_fmt_brl(t.get("total")),
                    _csv_cell(
                        _payment_method_label(
                            t.get("payment_method"),
                            t.get("card_installments"),
                        )
                    ),
                    _csv_cell(t.get("aut")),
                    _csv_cell(t.get("client_name")),
                    _csv_cell(t.get("client_cpf")),
                    _csv_cell(t.get("client_zipcode")),
                    _csv_cell(t.get("client_address")),
                    _csv_cell(t.get("client_number")),
                    _csv_cell(t.get("client_complement")),
                    _csv_cell(t.get("client_city")),
                    _csv_cell(t.get("client_state")),
                    _csv_cell(t.get("client_cro_uf")),
                    _csv_cell(t.get("client_cro_numero")),
                    _csv_cell(t.get("id")),
                ]
            )
        fname = f"vendas_evento_{event_id}_{safe_ev}_pedidos_{ts}.csv"
    else:
        rows_data = list_transaction_items_for_event_period(
            event_id,
            date_from=raw_from or None,
            date_to=raw_to or None,
        )
        # Ordem lógica: pedido → data/hora → vendedor → pagamento →
        #               produto → quantidades → valores → IDs
        header = [
            "Código do Pedido",
            "Data",
            "Hora",
            "Vendedor",
            "Forma de Pagamento",
            "AUT",
            "Produto",
            "SKU",
            "Categoria",
            "Qtd.",
            "Preço Unitário (R$)",
            "Subtotal (R$)",
            "ID Pedido",
            "ID Item",
        ]
        rows = []
        for ti in rows_data:
            created = ti.get("created_at")
            rows.append(
                [
                    _csv_cell(ti.get("order_number")),
                    _csv_fmt_date(created),
                    _csv_fmt_time(created),
                    _csv_cell(ti.get("seller_name")),
                    _csv_cell(
                        _payment_method_label(
                            ti.get("payment_method"),
                            ti.get("card_installments"),
                        )
                    ),
                    _csv_cell(ti.get("aut")),
                    _csv_cell(ti.get("product_name")),
                    _csv_cell(ti.get("product_sku")),
                    _csv_cell(ti.get("category")),
                    _csv_cell(ti.get("quantity")),
                    _csv_fmt_brl(ti.get("unit_price")),
                    _csv_fmt_brl(ti.get("subtotal")),
                    _csv_cell(ti.get("transaction_id")),
                    _csv_cell(ti.get("item_id")),
                ]
            )
        fname = f"vendas_evento_{event_id}_{safe_ev}_itens_{ts}.csv"

    return _csv_attachment_response(fname, header, rows)


# ---------------------------------------------------------------------------
# Eventos de estoque — helpers
# ---------------------------------------------------------------------------

def _event_or_404(event_id: int):
    """Retorna o evento ou faz flash+redirect para a lista."""
    ev = get_event(event_id)
    if ev is None:
        flash("Evento não encontrado.", "error")
    return ev


def _event_badge_color_from_form(form) -> str | None:
    """Interpreta checkbox \"cor padrão\" + color picker do formulário de evento."""
    use_default = (form.get("badge_use_default") or "").strip().lower() in ("1", "on", "true", "yes")
    if use_default:
        return None
    return normalize_event_badge_color((form.get("badge_color") or "").strip())


def _event_subnav_context(event_id: int, active_tab: str) -> dict:
    """Contexto compartilhado para todas as sub-páginas do evento."""
    ev = get_event(event_id)
    stats = get_event_stock_stats(event_id) if ev else {}
    return {
        "event": ev,
        "stats": stats,
        "event_id": event_id,
        "active_event_tab": active_tab,
    }


def _event_movement_payload(movement: dict, *, event_id: int) -> dict:
    """Serializa uma movimentação de evento para JSON (polling / live forms)."""
    movement_type = movement.get("movement_type")
    ev_bg, ev_fg = event_badge_style_pairs(movement.get("event_badge_color"))
    try:
        d_raw = int(movement.get("delta") or 0)
    except (TypeError, ValueError):
        d_raw = 0
    if d_raw > 0:
        delta_kind = "positive"
    elif d_raw < 0:
        delta_kind = "negative"
    else:
        delta_kind = "neutral"
    try:
        product_id = int(movement["product_id"])
    except (TypeError, ValueError, KeyError):
        product_id = 0
    reference = (movement.get("reference") or "").strip()
    try:
        tx_id = int(movement["transaction_id"]) if movement.get("transaction_id") else 0
    except (TypeError, ValueError):
        tx_id = 0
    tx_url = ""
    if reference and tx_id > 0 and event_id:
        tx_url = (
            url_for("admin_event_transactions", event_id=int(event_id), pedido=reference)
            + f"#tx-{tx_id}"
        )
    return {
        **movement,
        "event_badge_bg": ev_bg,
        "event_badge_fg": ev_fg,
        "created_by_display": _display_created_by(movement.get("created_by")),
        "created_at_display": datahora_filter(movement.get("created_at")),
        "movement_label": mov_label_filter(movement_type),
        "delta_display": signed_filter(movement.get("delta")),
        "delta_kind": delta_kind,
        "product_url": url_for("admin_product_detail", product_id=product_id) if product_id else "",
        "is_pending_delivery": bool(movement.get("is_pending_delivery")),
        "tx_url": tx_url,
    }


def _event_product_status(ep: dict) -> dict:
    """Calcula situação de um produto dentro do evento."""
    if not ep.get("product_active", True):
        return {"label": "Inativo no catálogo", "kind": "neutral"}
    if int(ep.get("stock") or 0) == 0:
        return {"label": "Sem estoque", "kind": "danger"}
    if int(ep.get("min_stock") or 0) > 0 and int(ep["stock"]) < int(ep["min_stock"]):
        return {"label": "Abaixo do mínimo", "kind": "warn"}
    return {"label": "OK", "kind": "success"}


# ---------------------------------------------------------------------------
# Eventos de estoque — lista e CRUD principal
# ---------------------------------------------------------------------------

@app.route("/admin/eventos")
@admin_required
def admin_events():
    active = list_events(include_archived=False)
    archived = list_events(include_archived=True)
    archived = [e for e in archived if not e["active"]]
    return render_template(
        "admin/events.html",
        events=active,
        events_archived=archived,
        **_admin_shell_context(active_section="eventos"),
    )


@app.route("/admin/eventos/novo", methods=["POST"])
@admin_required
def admin_event_create():
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not name:
        flash("O nome do evento é obrigatório.", "error")
        return redirect(url_for("admin_events"))
    badge_color = _event_badge_color_from_form(request.form)
    event_id = create_event(name, description, badge_color=badge_color)
    flash(f"Evento \"{name}\" criado com sucesso.", "success")
    return redirect(url_for("admin_event_detail", event_id=event_id))


@app.route("/admin/eventos/<int:event_id>")
@admin_required
def admin_event_detail(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    stats = get_event_stock_stats(event_id)
    sales_day_page = max(1, _parse_int(request.args.get("sales_day_page"), 1))
    sales_dashboard = get_event_sales_dashboard(event_id, sales_days_page=sales_day_page)
    sellers = list_event_sellers(event_id)
    return render_template(
        "admin/event_detail.html",
        event=event,
        stats=stats,
        sales_dashboard=sales_dashboard,
        sellers=sellers,
        active_event_tab="dashboard",
        **_admin_shell_context(active_section="eventos"),
    )


@app.route("/admin/eventos/<int:event_id>/editar", methods=["POST"])
@admin_required
def admin_event_edit(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not name:
        flash("O nome do evento é obrigatório.", "error")
        return redirect(url_for("admin_event_detail", event_id=event_id))
    badge_color = _event_badge_color_from_form(request.form)
    update_event(event_id, name, description, badge_color=badge_color)
    flash("Evento atualizado.", "success")
    return redirect(url_for("admin_event_detail", event_id=event_id))


@app.route("/admin/eventos/<int:event_id>/arquivar", methods=["POST"])
@admin_required
def admin_event_archive(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    archive_event(event_id)
    flash(f"Evento \"{event['name']}\" arquivado.", "success")
    return redirect(url_for("admin_events"))


@app.route("/admin/eventos/<int:event_id>/restaurar", methods=["POST"])
@admin_required
def admin_event_restore(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    restore_event(event_id)
    flash(f"Evento \"{event['name']}\" reativado.", "success")
    return redirect(url_for("admin_events"))


@app.route(
    "/admin/eventos/<int:event_id>/excluir",
    methods=["POST"],
    endpoint="admin_event_delete",
)
@admin_required
def admin_event_delete(event_id: int):
    from database import delete_event

    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    try:
        summary = delete_event(event_id)
        flash(
            f"Evento \"{summary['name']}\" excluído permanentemente "
            f"({summary['transactions']} transação(ões), "
            f"{summary['stock_movements']} movimentação(ões) de estoque, "
            f"{summary['products']} produto(s) no evento, "
            f"{summary['promotions']} promoção(ões), "
            f"{summary['sellers']} vínculo(s) com vendedores).",
            "success",
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_event_detail", event_id=event_id))
    return redirect(url_for("admin_events"))


# ---------------------------------------------------------------------------
# Eventos — sub-página Estoque
# ---------------------------------------------------------------------------

@app.route("/admin/eventos/<int:event_id>/estoque")
@admin_required
def admin_event_stock(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    products, filters, pagination = _admin_event_stock_page_view(event_id)
    stats = get_event_stock_stats(event_id)
    promo_product_ids = product_ids_with_active_promotions_for_event(event_id)
    promo_tooltips = active_promotion_tooltip_by_product_id(event_id)
    promo_names = active_promotion_names_by_product_id(event_id)
    return render_template(
        "admin/event_stock.html",
        event=event,
        products=products,
        stats=stats,
        filters=filters,
        pagination=pagination,
        promo_product_ids=promo_product_ids,
        promo_tooltips=promo_tooltips,
        promo_names=promo_names,
        categories=_admin_stock_library_category_options(),
        allowed_per_page=ALLOWED_ADMIN_STOCK_PER_PAGE,
        active_event_tab="estoque",
        **_admin_shell_context(active_section="eventos"),
    )


@app.route("/admin/eventos/<int:event_id>/estoque/export.csv")
@admin_required
def admin_event_stock_export_csv(event_id: int):
    """Exporta a grade de estoque do evento em CSV, respeitando os filtros ativos."""
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))

    q_display, category, status, _per_page, _page = _admin_stock_list_query_params()
    entrega_raw = (request.args.get("entrega") or "todos").strip().lower()
    entrega_norm = entrega_raw if entrega_raw in {"todos", "pendente"} else "todos"
    q_filter = q_display.lower() if q_display else None
    cat_norm = category or "todos"
    stat_norm = status or "todos"

    products = list_event_products_slice(
        event_id,
        q_filter,
        cat_norm,
        stat_norm,
        limit=EXPORT_STOCK_CSV_CAP,
        offset=0,
        entrega=entrega_norm,
    )
    product_ids = [int(p["product_id"]) for p in products]
    sold_map = units_sold_by_product_for_event(event_id, product_ids=product_ids)
    pending_map = pending_delivery_units_by_product_for_event(event_id, product_ids=product_ids)

    header = [
        "Produto",
        "Vendas",
        "Estoque",
        "Mínimo",
        "Situação",
        "SKU",
        "Categoria",
        "Preço (R$)",
        "Valor em Estoque (R$)",
        "Retirada Pendente (un.)",
        "ID Interno",
    ]
    rows = []
    for p in products:
        pid = int(p["product_id"])
        stock = int(p.get("stock") or 0)
        min_stock = int(p.get("min_stock") or 0)
        price = float(p.get("price") or 0)
        status_info = _event_product_status({
            "stock": stock,
            "min_stock": min_stock,
            "product_active": p.get("product_active", True),
        })
        rows.append([
            _csv_cell(p.get("name")),
            int(sold_map.get(pid, 0)),
            stock,
            min_stock,
            _csv_cell(status_info["label"]),
            _csv_cell(p.get("sku")),
            _csv_cell(p.get("category")),
            _csv_fmt_brl(price),
            _csv_fmt_brl(stock * price),
            int(pending_map.get(pid, 0)),
            pid,
        ])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ev = re.sub(r"[^a-zA-Z0-9_-]+", "_", (event.get("name") or str(event_id)))[:40].strip("_") or str(event_id)
    fname = f"estoque_evento_{event_id}_{safe_ev}_{ts}.csv"
    return _csv_attachment_response(fname, header, rows)


@app.route("/admin/eventos/<int:event_id>/estoque/<int:product_id>")
@admin_required
def admin_event_stock_product(event_id: int, product_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    product = get_product_in_event(event_id, product_id)
    if product is None:
        flash("Produto não encontrado neste evento.", "error")
        return redirect(url_for("admin_event_stock", event_id=event_id))

    pending_units = pending_delivery_units_by_product_for_event(
        event_id, product_ids=[product_id],
    ).get(int(product_id), 0)
    product = dict(product)
    product["pending_delivery_units"] = int(pending_units)
    product = _attach_promo_name_to_product(product, event_id)

    pedido = (request.args.get("pedido") or "").strip()
    tipo_raw = (request.args.get("tipo") or "").strip().lower()
    valid_tipo = frozenset({"todos", "entrada", "venda", "saida", "pendente"})
    tipo_norm = tipo_raw if tipo_raw in valid_tipo else "todos"
    movement_type_api = None if tipo_norm == "todos" else tipo_norm

    event_sellers_rows = list_event_sellers(event_id)
    event_seller_ids = {int(s["id"]) for s in event_sellers_rows}
    seller_raw = _parse_int(request.args.get("vendedor"), 0)
    seller_filter = seller_raw if seller_raw > 0 and seller_raw in event_seller_ids else None
    if seller_raw > 0 and seller_filter is None:
        seller_raw = 0

    per_page = EVENT_PRODUCT_MOVEMENTS_PER_PAGE
    page = max(1, _parse_int(request.args.get("page"), 1))

    total = count_event_product_ledger(
        event_id,
        product_id,
        movement_type=movement_type_api,
        reference=pedido or None,
        seller_id=seller_filter,
    )
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    movements = list_event_product_ledger(
        event_id,
        product_id,
        movement_type=movement_type_api,
        reference=pedido or None,
        seller_id=seller_filter,
        limit=per_page,
        offset=offset,
    )

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(movements), total) if total > 0 else 0

    filters = {
        "pedido": pedido,
        "tipo": tipo_norm,
        "vendedor": seller_raw,
    }
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "showing_from": showing_from,
        "showing_to": showing_to,
    }

    return render_template(
        "admin/event_stock_product.html",
        event=event,
        product=product,
        movements=movements,
        movement_filter_sellers=event_sellers_rows,
        filters=filters,
        pagination=pagination,
        active_event_tab="estoque",
        **_admin_shell_context(active_section="eventos"),
    )


def _wake_token_help_message() -> str:
    return (
        "Configure WAKE_TOKEN com o TCS-Access-Token da Storefront API Wake Commerce. "
        "Crie um arquivo .env na raiz do projeto (copie de .env.example) ou defina "
        "a variável de ambiente do sistema."
    )


def _find_or_fetch_product(sku_or_id: str) -> tuple[dict | None, bool, str | None]:
    """Localiza um produto por SKU/ID, com fallback Wake quando ausente localmente.

    Retorna ``(produto_dict | None, veio_da_wake, erro)``.
    ``erro`` é preenchido quando a busca falha por token Wake ausente
    (``"wake_token"``) — distinto de produto simplesmente não encontrado.
    """
    q = (sku_or_id or "").strip()
    if not q:
        return None, False, None

    local = find_product_by_sku_or_id(q)
    if local is not None:
        return local, False, None

    if not wake_api.wake_token_configured():
        return None, False, "wake_token"

    wake_data = wake_api.fetch_product_by_sku(q)
    if wake_data is None:
        return None, False, None

    if not upsert_wake_variant(wake_data):
        return None, False, None

    saved = find_product_by_sku_or_id(q)
    if saved is None:
        vid = wake_data.get("variant_id") or wake_data.get("id")
        if vid:
            saved = find_product_by_sku_or_id(str(vid))
    if saved is None:
        return None, False, None

    app.logger.info(
        "Variante Wake importada on-demand: SKU=%s id=%s nome=%s",
        saved.get("sku"), saved.get("id"), saved.get("name"),
    )
    return saved, True, None


@app.route("/admin/eventos/<int:event_id>/produtos/adicionar", methods=["POST"])
@admin_required
def admin_event_add_product(event_id: int):
    event = _event_or_404(event_id)
    preserved = _event_stock_return_filters_from_form()
    if event is None:
        return redirect(url_for("admin_events"))
    q = (request.form.get("sku_or_id") or "").strip()
    if not q:
        flash("Informe o SKU ou ID do produto.", "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))
    product, from_wake, lookup_err = _find_or_fetch_product(q)
    if lookup_err == "wake_token":
        flash(_wake_token_help_message(), "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))
    if product is None:
        flash(f"Produto \"{q}\" não encontrado no catálogo nem na Wake Commerce.", "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))
    try:
        add_product_to_event(event_id, int(product["id"]), 0)
        suffix = " (variante importada da Wake)" if from_wake else ""
        flash(f"Produto \"{product['name']}\" adicionado ao evento{suffix}.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(_url_for_admin_event_stock_list(event_id, preserved, page_override=1))


def _flash_image_cache_stats(stats: dict) -> None:
    ok = int(stats.get("ok") or 0)
    skip = int(stats.get("skip") or 0)
    fail = int(stats.get("fail") or 0)
    if ok:
        flash(
            f"{ok} imagem(ns) salva(s) neste computador. "
            f"{skip} já estavam locais. {fail} falha(s).",
            "success" if fail == 0 else "error",
        )
    elif fail:
        flash(
            f"Nenhuma imagem nova foi salva ({fail} falha(s)). "
            "Verifique a internet e as URLs da Wake.",
            "error",
        )
    else:
        flash(
            f"Todas as imagens já estavam salvas neste computador ({skip} produto(s)).",
            "success",
        )


@app.route("/admin/eventos/<int:event_id>/produtos/baixar-imagens", methods=["POST"])
@admin_required
def admin_event_cache_product_images(event_id: int):
    """Copia as fotos da Wake para disco, para o catálogo funcionar sem internet."""
    event = _event_or_404(event_id)
    preserved = _event_stock_return_filters_from_form()
    if event is None:
        return redirect(url_for("admin_events"))
    try:
        stats = product_images.cache_images_for_event(event_id)
    except Exception:
        app.logger.exception("Falha ao baixar imagens do evento %s", event_id)
        flash("Não foi possível baixar as imagens. Tente novamente com internet.", "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))
    _flash_image_cache_stats(stats)
    return redirect(_url_for_admin_event_stock_list(event_id, preserved))


@app.route("/admin/produtos/baixar-imagens", methods=["POST"])
@admin_required
def admin_products_cache_images():
    """Copia as fotos de toda a biblioteca para disco (preparação do evento)."""
    try:
        stats = product_images.cache_images_for_catalog()
    except Exception:
        app.logger.exception("Falha ao baixar imagens da biblioteca")
        flash("Não foi possível baixar as imagens. Tente novamente com internet.", "error")
        return redirect(url_for("admin_products"))
    _flash_image_cache_stats(stats)
    return redirect(url_for("admin_products"))


_XLS_SKU_COL_NAMES = frozenset({
    "produto", "cód. produto", "cod. produto", "codigo", "código",
    "sku", "cod produto", "cód produto", "item", "ref", "referência",
    "referencia", "cod.", "cód.",
})

_XLS_STOCK_COL_NAMES = frozenset({
    "qtd. disponivel", "qtd disponivel", "qtd. disponível", "qtd disponível",
    "qtd. estoque", "qtd estoque", "quantidade", "estoque", "disponivel",
    "disponível", "saldo", "qty", "stock",
})

_XLS_PRICE_COL_NAMES = frozenset({
    "preço", "preco", "preço unitário", "preco unitario", "preço unit.",
    "preco unit.", "vlr. unitário", "vlr unitário", "vlr. unitario",
    "vlr unitario", "valor", "valor unitário", "valor unitario",
    "valor unidade", "vlr unidade", "vlr. unidade",
    "price", "unit_price", "unit price",
})


def _spreadsheet_kind(file_bytes: bytes, filename: str = "") -> str:
    name = (filename or "").lower()
    if file_bytes.startswith(b"PK"):
        return "xlsx"
    if file_bytes.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if name.endswith(".xlsx"):
        return "xlsx"
    return "xls"


def _grid_cell(rows: list[list], r: int, c: int):
    if r < 0 or r >= len(rows) or c < 0:
        return None
    row = rows[r]
    if c >= len(row):
        return None
    return row[c]


def _is_numeric_value(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _header_text(value) -> str:
    if value is None or isinstance(value, (datetime, date)):
        return ""
    return str(value).strip().lower()


def _value_to_str(value) -> str:
    """Converte célula para SKU/texto limpo, sem '.0' em inteiros."""
    if value is None or isinstance(value, (datetime, date, bool)):
        return ""
    if _is_numeric_value(value):
        return str(int(value)) if float(value) == int(value) else str(value)
    return str(value).strip()


def _value_to_int(value) -> int:
    """Converte célula de quantidade para inteiro (>=0). Retorna 0 se vazia/inválida."""
    if _is_numeric_value(value):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip().replace(",", "."))))
        except (ValueError, TypeError):
            return 0
    return 0


def _value_to_float_or_none(value) -> float | None:
    """Converte célula de preço. Retorna None se vazia, zero ou inválida."""
    if _is_numeric_value(value) and value:
        v = round(float(value), 2)
        return v if v > 0 else None
    if isinstance(value, str):
        raw = value.strip().replace(",", ".")
        if not raw:
            return None
        try:
            v = round(float(raw), 2)
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None
    return None


def _load_spreadsheet_grids(file_bytes: bytes, filename: str = "") -> list[list[list]]:
    """Carrega cada aba como grade de valores Python (linhas × colunas)."""
    kind = _spreadsheet_kind(file_bytes, filename)
    grids: list[list[list]] = []
    if kind == "xlsx":
        wb = _load_xlsx_workbook(file_bytes)
        try:
            for ws in wb.worksheets:
                rows = [list(row) for row in ws.iter_rows(values_only=True)]
                if rows:
                    grids.append(rows)
        finally:
            wb.close()
        return grids
    if not _XLRD_AVAILABLE:
        try:
            _pip_install("xlrd>=2.0.1,<3")
            import xlrd as _xlrd_mod
            globals()["_xlrd"] = _xlrd_mod
            globals()["_XLRD_AVAILABLE"] = True
        except Exception as exc:
            raise RuntimeError(
                "Biblioteca xlrd não instalada. "
                f"Execute: {sys.executable} -m pip install xlrd"
            ) from exc
    wb = _xlrd.open_workbook(file_contents=file_bytes)
    for sheet_idx in range(wb.nsheets):
        sh = wb.sheet_by_index(sheet_idx)
        rows = []
        for r in range(sh.nrows):
            rows.append([sh.cell_value(r, c) if sh.cell_type(r, c) not in (0, 6) else None
                         for c in range(sh.ncols)])
        if rows:
            grids.append(rows)
    return grids


def _detect_spreadsheet_header(rows: list[list]) -> tuple[int | None, int, int | None, int | None]:
    """Retorna (header_row, sku_col, price_col_or_none, stock_col_or_none).

    Procura nas primeiras 15 linhas um cabeçalho de SKU/código em qualquer coluna.
    Preço e estoque só são usados se o nome da coluna bater; senão:
    - layout legado (SKU na coluna A): preço = C, estoque = E;
    - outros layouts (ex.: código na B): preço/estoque só pelos nomes.
    """
    nrows = len(rows)
    ncols = max((len(row) for row in rows), default=0)
    for r in range(min(15, nrows)):
        sku_col = None
        price_col = None
        stock_col = None
        for c in range(ncols):
            h = _header_text(_grid_cell(rows, r, c))
            if not h:
                continue
            if sku_col is None and h in _XLS_SKU_COL_NAMES:
                sku_col = c
            if h in _XLS_PRICE_COL_NAMES:
                price_col = c
            if h in _XLS_STOCK_COL_NAMES:
                stock_col = c
        if sku_col is not None:
            if sku_col == 0:
                if price_col is None:
                    price_col = 2
                if stock_col is None:
                    stock_col = 4
            return r, sku_col, price_col, stock_col
    return None, 0, 2, 4


def _parse_xls_sku_stock(
    file_bytes: bytes,
    filename: str = "",
) -> list[tuple[str, float | None, int]]:
    """Lê planilha .xls/.xlsx e retorna lista de (sku, unit_price_or_none, stock_qty).

    1. Procura cabeçalho de código/SKU em qualquer coluna das primeiras 15 linhas.
       Preço: coluna nomeada (ex. 'Valor Unidade') ou, no layout legado, coluna C.
       Quantidade: coluna nomeada de estoque ou, no layout legado, coluna E.
       Sem quantidade reconhecida, o produto sobe com estoque 0.
    2. Sem cabeçalho: primeira linha numérica na coluna A; preço C; estoque E.

    SKUs duplicados somam estoque; o preço é o da primeira aparição.
    """
    grids = _load_spreadsheet_grids(file_bytes, filename)
    result_qty: dict[str, int] = {}
    result_price: dict[str, float | None] = {}

    for rows in grids:
        if len(rows) < 2:
            continue
        ncols = max((len(row) for row in rows), default=0)
        header_row, sku_col, price_col, stock_col = _detect_spreadsheet_header(rows)

        if header_row is not None:
            data_start = header_row + 1
        else:
            data_start = None
            for r in range(len(rows)):
                if _is_numeric_value(_grid_cell(rows, r, 0)):
                    data_start = r
                    break
            if data_start is None:
                continue

        for r in range(data_start, len(rows)):
            sku = _value_to_str(_grid_cell(rows, r, sku_col))
            if not sku or " " in sku or not re.search(r"\d", sku):
                continue
            qty = 0
            if stock_col is not None and stock_col < ncols:
                qty = _value_to_int(_grid_cell(rows, r, stock_col))
            price = None
            if price_col is not None and price_col < ncols:
                price = _value_to_float_or_none(_grid_cell(rows, r, price_col))
            if sku in result_qty:
                result_qty[sku] += qty
            else:
                result_qty[sku] = qty
                result_price[sku] = price

    return [(sku, result_price.get(sku), qty) for sku, qty in result_qty.items()]


@app.route("/admin/eventos/<int:event_id>/produtos/importar-xls", methods=["POST"])
@admin_required
def admin_event_import_xls(event_id: int):
    """Importa produtos em lote para o evento a partir de planilha .xls ou .xlsx.

    Lê o código/SKU (coluna A no layout legado, ou a coluna do cabeçalho CODIGO),
    o preço unitário (coluna C ou 'Valor Unidade') e a quantidade (coluna E, se houver).
    Sem preço reconhecido, herda o preço-base da biblioteca.
    Sem quantidade reconhecida, adiciona o produto com estoque 0.
    SKUs duplicados têm seus estoques somados antes da importação.
    """
    event = _event_or_404(event_id)
    preserved = _event_stock_return_filters_from_form()
    if event is None:
        return redirect(url_for("admin_events"))

    uploaded = request.files.get("xls_file")
    if not uploaded or not uploaded.filename:
        flash("Selecione uma planilha (.xls ou .xlsx) para importar.", "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))

    fname = (uploaded.filename or "").lower()
    if not (fname.endswith(".xls") or fname.endswith(".xlsx")):
        flash("Formato inválido. Envie um arquivo .xls ou .xlsx.", "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))

    try:
        file_bytes = uploaded.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            flash("Arquivo muito grande (máx. 5 MB).", "error")
            return redirect(_url_for_admin_event_stock_list(event_id, preserved))
        sku_stock_pairs = _parse_xls_sku_stock(file_bytes, uploaded.filename or "")
    except RuntimeError as exc:
        flash(str(exc), "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))
    except Exception as exc:
        flash(f"Não foi possível ler a planilha: {exc}", "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))

    if not sku_stock_pairs:
        flash("Nenhum código encontrado na coluna A da planilha.", "error")
        return redirect(_url_for_admin_event_stock_list(event_id, preserved))

    added: list[str] = []
    already: list[str] = []
    not_found: list[str] = []
    wake_fetched: list[str] = []   # SKUs que vieram da Wake on-demand
    import_actor = _current_admin_user()
    total_units_imported = 0

    price_set_count = 0

    for sku, unit_price, qty in sku_stock_pairs:
        product, from_wake, lookup_err = _find_or_fetch_product(sku)
        if lookup_err == "wake_token":
            flash(_wake_token_help_message(), "error")
            return redirect(_url_for_admin_event_stock_list(event_id, preserved))
        if product is None:
            not_found.append(sku)
            continue
        if from_wake:
            wake_fetched.append(sku)
        try:
            add_product_to_event(
                event_id,
                int(product["id"]),
                qty,
                link_audit_reason=EVENT_IMPORT_XLS_MOTIVO_REF,
                link_audit_reference=None,
                created_by=import_actor,
            )
            if unit_price is not None:
                update_event_product_price(event_id, int(product["id"]), unit_price)
                price_set_count += 1
            qty_label = f" ({qty} un.)" if qty > 0 else ""
            price_label = f" · R$ {unit_price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if unit_price is not None else ""
            added.append(f"{product['name']}{qty_label}{price_label}")
            total_units_imported += qty
        except ValueError:
            already.append(product["name"])

    # Monta mensagem de resultado
    parts: list[str] = []
    if added:
        units_txt = f" · {total_units_imported} unidade(s) em estoque" if total_units_imported > 0 else ""
        price_txt = f" · {price_set_count} preço(s) definido(s)" if price_set_count > 0 else ""
        parts.append(f"{len(added)} produto(s) adicionado(s) com sucesso{units_txt}{price_txt}")
    if wake_fetched:
        parts.append(
            f"{len(wake_fetched)} variante(s) importada(s) da Wake e adicionada(s) ao catálogo: "
            f"{', '.join(wake_fetched[:5])}{'…' if len(wake_fetched) > 5 else ''}"
        )
    if already:
        parts.append(f"{len(already)} já estavam no evento")
    if not_found:
        short = not_found[:10]
        tail = "…" if len(not_found) > 10 else ""
        parts.append(f"{len(not_found)} código(s) não encontrado(s) no catálogo nem na Wake: {', '.join(short)}{tail}")

    summary = " · ".join(parts) if parts else "Nenhuma alteração realizada."
    category = "success" if added else ("error" if not_found and not already else "info")
    flash(summary, category)

    return redirect(_url_for_admin_event_stock_list(event_id, preserved, page_override=1))


@app.route("/admin/eventos/<int:event_id>/produtos/<int:product_id>/remover", methods=["POST"])
@admin_required
def admin_event_remove_product(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    preserved = _event_stock_return_filters_from_form()
    remove_product_from_event(event_id, product_id)
    flash("Produto removido do evento.", "success")
    return redirect(_url_for_admin_event_stock_list(event_id, preserved))


@app.route("/admin/eventos/<int:event_id>/produtos/<int:product_id>/entrada", methods=["POST"])
@admin_required
def admin_event_stock_entry(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    qty = _parse_int(request.form.get("quantity"), 0)
    unit_cost = _parse_float(request.form.get("unit_cost"))
    reason = (request.form.get("reason") or "").strip() or None
    fallback = url_for("admin_event_stock_product", event_id=event_id, product_id=product_id)
    try:
        register_event_stock_entry(
            event_id,
            product_id,
            qty,
            unit_cost=unit_cost,
            reason=reason,
            created_by=_current_admin_user(),
        )
        message = f"Entrada de {qty} un. registrada."
        if _wants_json_response():
            return _json_event_stock_success(message, event_id, product_id)
        flash(message, "success")
    except ValueError as exc:
        if _wants_json_response():
            return jsonify({"error": str(exc)}), 400
        flash(str(exc), "error")
    return redirect(request.referrer or fallback)


@app.route("/admin/eventos/<int:event_id>/produtos/entrada-em-lote", methods=["POST"])
@admin_required
def admin_event_stock_bulk_entry(event_id: int):
    """Registra entrada de estoque para vários produtos do evento de uma só vez.

    Espera ``product_ids`` (lista de IDs marcados) + um campo ``qty_<id>`` por
    produto + um único ``reason`` (Motivo / Ref) aplicado a todas as entradas.
    """
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    preserved = _event_stock_return_filters_from_form()
    fallback = _url_for_admin_event_stock_list(event_id, preserved)

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Informe o Motivo / Ref. da entrada em lote.", "error")
        return redirect(fallback)

    product_ids = request.form.getlist("product_ids")
    if not product_ids:
        flash("Selecione ao menos um produto para adicionar estoque.", "error")
        return redirect(fallback)

    admin_user = _current_admin_user()
    ok_count = 0
    ok_units = 0
    errors: list[str] = []
    for raw_id in product_ids:
        product_id = _parse_int(raw_id, 0)
        if product_id <= 0:
            continue
        qty = _parse_int(request.form.get(f"qty_{product_id}"), 0)
        if qty <= 0:
            errors.append(f"#{product_id}: quantidade inválida.")
            continue
        try:
            register_event_stock_entry(
                event_id,
                product_id,
                qty,
                reason=reason,
                created_by=admin_user,
            )
            ok_count += 1
            ok_units += qty
        except ValueError as exc:
            errors.append(f"#{product_id}: {exc}")

    if ok_count:
        flash(f"Entrada registrada em {ok_count} produto(s), {ok_units} unidade(s) no total.", "success")
    if errors:
        short = errors[:5]
        tail = f" (+{len(errors) - 5})" if len(errors) > 5 else ""
        flash("Falha em: " + "; ".join(short) + tail, "error")
    elif not ok_count:
        flash("Nenhuma entrada registrada.", "error")

    return redirect(fallback)


@app.route("/admin/eventos/<int:event_id>/produtos/<int:product_id>/saida", methods=["POST"])
@admin_required
def admin_event_stock_exit(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    qty = _parse_int(request.form.get("quantity"), 0)
    reason = (request.form.get("reason") or "").strip()
    fallback = url_for("admin_event_stock_product", event_id=event_id, product_id=product_id)
    try:
        register_event_stock_exit(
            event_id,
            product_id,
            qty,
            reason=reason or "Saída manual",
            created_by=_current_admin_user(),
        )
        message = f"Saída de {qty} un. registrada."
        if _wants_json_response():
            return _json_event_stock_success(message, event_id, product_id)
        flash(message, "success")
    except ValueError as exc:
        if _wants_json_response():
            return jsonify({"error": str(exc)}), 400
        flash(str(exc), "error")
    return redirect(request.referrer or fallback)


@app.route("/admin/eventos/<int:event_id>/produtos/<int:product_id>/ajuste", methods=["POST"])
@admin_required
def admin_event_stock_adjust(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    new_qty = _parse_int(request.form.get("new_stock"), 0)
    reason = (request.form.get("reason") or "").strip()
    fallback = url_for("admin_event_stock_product", event_id=event_id, product_id=product_id)
    try:
        register_event_stock_adjustment(
            event_id,
            product_id,
            new_qty,
            reason=reason or "Ajuste manual",
            created_by=_current_admin_user(),
        )
        message = f"Estoque ajustado para {new_qty} un."
        if _wants_json_response():
            return _json_event_stock_success(message, event_id, product_id)
        flash(message, "success")
    except ValueError as exc:
        if _wants_json_response():
            return jsonify({"error": str(exc)}), 400
        flash(str(exc), "error")
    return redirect(request.referrer or fallback)


@app.route("/admin/eventos/<int:event_id>/produtos/<int:product_id>/preco", methods=["POST"])
@admin_required
def admin_event_stock_price(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    fallback = url_for("admin_event_stock_product", event_id=event_id, product_id=product_id)
    restore = (request.form.get("restore_base") or "").strip() in {"1", "true", "on"}
    if restore:
        stored = None
        message = "Preço do evento restaurado para o valor da biblioteca."
    else:
        raw = (request.form.get("price") or "").strip().replace(",", ".")
        try:
            stored = round(float(raw), 2)
            if stored < 0:
                raise ValueError("negativo")
        except (TypeError, ValueError):
            if _wants_json_response():
                return jsonify({"error": "Informe um valor numérico válido para o preço."}), 400
            flash("Informe um valor numérico válido para o preço.", "error")
            return redirect(request.referrer or fallback)
        message = (
            f"Preço do evento atualizado para R$ {stored:,.2f}"
            .replace(",", "X").replace(".", ",").replace("X", ".")
        )
    if update_event_product_price(event_id, product_id, stored):
        if _wants_json_response():
            return _json_event_stock_success(message, event_id, product_id)
        flash(message, "success")
    else:
        if _wants_json_response():
            return jsonify({"error": "Não foi possível atualizar o preço neste evento."}), 400
        flash("Não foi possível atualizar o preço neste evento.", "error")
    return redirect(request.referrer or fallback)


@app.route("/admin/eventos/<int:event_id>/produtos/<int:product_id>/minimo", methods=["POST"])
@admin_required
def admin_event_stock_min(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    min_val = _parse_int(request.form.get("min_stock"), 0)
    fallback = url_for("admin_event_stock_product", event_id=event_id, product_id=product_id)
    update_event_product_stock(
        event_id,
        product_id,
        _get_event_product_stock(event_id, product_id),
        min_val,
    )
    message = f"Estoque mínimo atualizado para {max(0, min_val)} un."
    if _wants_json_response():
        return _json_event_stock_success(message, event_id, product_id)
    flash(message, "success")
    return redirect(request.referrer or fallback)


@app.route(
    "/admin/eventos/<int:event_id>/produtos/<int:product_id>/limite-entrega-pendente",
    methods=["POST"],
)
@admin_required
def admin_event_stock_backorder_limit(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return redirect(url_for("admin_events"))
    fallback = url_for("admin_event_stock_product", event_id=event_id, product_id=product_id)
    action = (request.form.get("backorder_action") or "set").strip().lower()
    if action == "unlimited":
        limit_val = -1
        message = "Limite de entregas pendentes removido (livre)."
    else:
        limit_val = max(0, _parse_int(request.form.get("backorder_limit"), 0))
        if limit_val == 0:
            message = "Entregas pendentes bloqueadas para este produto neste evento."
        else:
            message = f"Limite de entregas pendentes atualizado para {limit_val} un."
    update_event_product_backorder_limit(event_id, product_id, limit_val)
    if _wants_json_response():
        return _json_event_stock_success(message, event_id, product_id)
    flash(message, "success")
    return redirect(request.referrer or fallback)


def _get_event_product_stock(event_id: int, product_id: int) -> int:
    """Lê o saldo atual de um produto no evento (helper interno)."""
    for ep in list_event_products(event_id):
        if int(ep["product_id"]) == int(product_id):
            return int(ep["stock"] or 0)
    return 0


# ---------------------------------------------------------------------------
# Eventos — sub-página Promoções
# ---------------------------------------------------------------------------

@app.route("/admin/eventos/<int:event_id>/promocoes")
@admin_required
def admin_event_promotions(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    promotions = list_promotions_for_event(event_id)
    event_products = list_event_products(event_id)
    return render_template(
        "admin/event_promotions.html",
        event=event,
        promotions=promotions,
        event_products=event_products,
        rule_type_labels=RULE_TYPE_LABELS,
        active_event_tab="promocoes",
        **_admin_shell_context(active_section="eventos"),
    )


def _parse_promotion_form_fields(form) -> tuple[str, float, int, int]:
    """Extrai parâmetros da regra a partir do POST do admin.

    Cada tipo de promoção usa campos com nomes distintos no formulário para evitar
    colisão entre seções ocultas (ex.: ``rule_value`` de percent vs. bundle).
    """
    rule_type = (form.get("rule_type") or "").strip()
    if rule_type in ("percent", "fixed"):
        rule_value = float(form.get("rule_value_pf") or 0)
        min_qty = 1
        free_qty = 0
    elif rule_type == "bogo":
        rule_value = 0.0
        min_qty = int(form.get("min_qty_bogo") or 1)
        free_qty = int(form.get("free_qty") or 0)
    elif rule_type in ("min_bundle", "exact_bundle"):
        rule_value = float(form.get("rule_value_bundle") or 0)
        min_qty = int(form.get("min_qty_bundle") or 2)
        free_qty = 0
    else:
        raise ValueError(f"Tipo de regra inválido: {rule_type}")
    return rule_type, rule_value, min_qty, free_qty


@app.route("/admin/eventos/<int:event_id>/promocoes/nova", methods=["POST"])
@admin_required
def admin_event_promotion_create(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    try:
        name = (request.form.get("name") or "").strip()
        rule_type, rule_value, min_qty, free_qty = _parse_promotion_form_fields(request.form)
        product_ids = [int(p) for p in request.form.getlist("product_ids") if p]
        create_promotion(
            event_id, name, rule_type,
            rule_value=rule_value, min_qty=min_qty, free_qty=free_qty,
            product_ids=product_ids,
        )
        flash("Promoção criada com sucesso.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin_event_promotions", event_id=event_id))


@app.route("/admin/eventos/<int:event_id>/promocoes/<int:promo_id>")
@admin_required
def admin_event_promotion_detail(event_id: int, promo_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    promo = get_promotion(promo_id)
    if promo is None or int(promo["event_id"]) != event_id:
        flash("Promoção não encontrada.", "error")
        return redirect(url_for("admin_event_promotions", event_id=event_id))
    event_products = list_event_products(event_id)
    return render_template(
        "admin/event_promotion_detail.html",
        event=event,
        promo=promo,
        event_products=event_products,
        rule_type_labels=RULE_TYPE_LABELS,
        active_event_tab="promocoes",
        **_admin_shell_context(active_section="eventos"),
    )


@app.route("/admin/eventos/<int:event_id>/promocoes/<int:promo_id>/editar", methods=["POST"])
@admin_required
def admin_event_promotion_edit(event_id: int, promo_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))
    promo = get_promotion(promo_id)
    if promo is None or int(promo["event_id"]) != event_id:
        flash("Promoção não encontrada.", "error")
        return redirect(url_for("admin_event_promotions", event_id=event_id))
    try:
        name = (request.form.get("name") or "").strip()
        rule_type, rule_value, min_qty, free_qty = _parse_promotion_form_fields(request.form)
        # Ativar/desativar é só pelo botão dedicado (admin_event_promotion_toggle).
        active = bool(int(promo.get("active") or 0))
        product_ids = [int(p) for p in request.form.getlist("product_ids") if p]
        update_promotion(
            promo_id, name, rule_type,
            rule_value=rule_value, min_qty=min_qty, free_qty=free_qty,
            active=active, product_ids=product_ids,
        )
        flash("Promoção atualizada.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin_event_promotion_detail", event_id=event_id, promo_id=promo_id))


@app.route("/admin/eventos/<int:event_id>/promocoes/<int:promo_id>/ativar", methods=["POST"])
@admin_required
def admin_event_promotion_toggle(event_id: int, promo_id: int):
    promo = get_promotion(promo_id)
    if promo is None or int(promo["event_id"]) != event_id:
        flash("Promoção não encontrada.", "error")
        return redirect(url_for("admin_event_promotions", event_id=event_id))
    try:
        p = toggle_promotion_active(promo_id)
        status = "ativada" if p["active"] else "desativada"
        flash(f"Promoção «{p['name']}» {status}.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(request.referrer or url_for("admin_event_promotions", event_id=event_id))


@app.route("/admin/eventos/<int:event_id>/promocoes/<int:promo_id>/excluir", methods=["POST"])
@admin_required
def admin_event_promotion_delete(event_id: int, promo_id: int):
    promo = get_promotion(promo_id)
    if promo is None or int(promo["event_id"]) != event_id:
        flash("Promoção não encontrada.", "error")
        return redirect(url_for("admin_event_promotions", event_id=event_id))
    try:
        delete_promotion(promo_id)
        flash(f"Promoção «{promo['name']}» excluída.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin_event_promotions", event_id=event_id))


# ---------------------------------------------------------------------------
# Eventos — sub-página Transações
# ---------------------------------------------------------------------------

def _admin_event_transactions_page_data(event_id: int) -> dict:
    """Filtros, lista e paginação das transações do evento (página + polling)."""
    pedido = (request.args.get("pedido") or "").strip()
    status_raw = (request.args.get("status") or "").strip().lower()
    valid_status = frozenset({"todos", "confirmado", "pendente", "cancelado", "estornado", "entregue"})
    status_norm = status_raw if status_raw in valid_status else "todos"

    entrega_raw = (request.args.get("entrega") or "").strip().lower()
    entrega_norm = entrega_raw if entrega_raw == "parcial" else "todos"
    delivery_api = None if entrega_norm == "todos" else entrega_norm

    date_arg_raw = (request.args.get("data") or "").strip()
    on_date = _parse_tx_filter_date_arg(date_arg_raw)
    filter_date_display = on_date or ""

    event_sellers_rows = list_event_sellers(event_id)
    event_seller_ids = {int(s["id"]) for s in event_sellers_rows}
    seller_raw = _parse_int(request.args.get("vendedor"), 0)
    seller_filter = seller_raw if seller_raw > 0 and seller_raw in event_seller_ids else None
    if seller_raw > 0 and seller_filter is None:
        seller_raw = 0

    per_page = _parse_int(request.args.get("per_page"), DEFAULT_ADMIN_MOVEMENTS_PER_PAGE)
    if per_page not in ALLOWED_ADMIN_STOCK_PER_PAGE:
        per_page = DEFAULT_ADMIN_MOVEMENTS_PER_PAGE
    page = max(1, _parse_int(request.args.get("page"), 1))

    status_api = None if status_norm == "todos" else status_norm

    total = count_transactions_for_event(
        event_id,
        seller_id=seller_filter,
        order_search=pedido or None,
        status=status_api,
        on_date=on_date,
        delivery=delivery_api,
    )
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    transactions = list_transactions_for_event(
        event_id,
        seller_id=seller_filter,
        order_search=pedido or None,
        status=status_api,
        on_date=on_date,
        delivery=delivery_api,
        limit=per_page,
        offset=offset,
    )

    showing_from = offset + 1 if total > 0 else 0
    showing_to = min(offset + len(transactions), total) if total > 0 else 0

    return {
        "transactions": transactions,
        "movement_filter_sellers": event_sellers_rows,
        "filters": {
            "pedido": pedido,
            "status": status_norm,
            "entrega": entrega_norm,
            "vendedor": seller_raw,
            "per_page": per_page,
            "data": filter_date_display,
        },
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "showing_from": showing_from,
            "showing_to": showing_to,
        },
        "allowed_per_page": ALLOWED_ADMIN_STOCK_PER_PAGE,
    }


@app.route("/admin/eventos/<int:event_id>/transacoes")
@admin_required
def admin_event_transactions(event_id: int):
    event = _event_or_404(event_id)
    if event is None:
        return redirect(url_for("admin_events"))

    page_data = _admin_event_transactions_page_data(event_id)
    return render_template(
        "admin/event_transactions.html",
        event=event,
        active_event_tab="transacoes",
        **page_data,
        **_admin_shell_context(active_section="eventos"),
    )


@app.route(
    "/admin/eventos/<int:event_id>/transacoes/<int:tx_id>/estornar",
    methods=["POST"],
)
@admin_required
def admin_event_transaction_refund(event_id: int, tx_id: int):
    if _event_or_404(event_id) is None:
        flash("Evento não encontrado.", "error")
        return redirect(url_for("admin_events"))
    try:
        result = refund_transaction(
            tx_id,
            created_by=_current_admin_user(),
            expected_event_id=event_id,
        )
        order_label = result.get("order_number") or f"#{tx_id}"
        flash(
            f"Pedido {order_label} estornado. Estoque reposto e totais de vendas atualizados.",
            "success",
        )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        request.referrer or url_for("admin_event_transactions", event_id=event_id)
    )


@app.route(
    "/admin/eventos/<int:event_id>/transacoes/<int:tx_id>/itens/<int:item_id>/entregar",
    methods=["POST"],
)
@admin_required
def admin_event_confirm_item_delivery(event_id: int, tx_id: int, item_id: int):
    """Confirma a retirada de um item pendente (baixa o estoque na entrega)."""
    if _event_or_404(event_id) is None:
        flash("Evento não encontrado.", "error")
        return redirect(url_for("admin_events"))
    try:
        result = confirm_item_delivery(
            tx_id,
            item_id,
            expected_event_id=event_id,
            created_by=_current_admin_user(),
        )
        msg = (
            f"Entrega confirmada: {result['delivered_now']} un. de "
            f"'{result['product_name']}'."
        )
        if result["still_pending"] > 0:
            msg += f" Ainda pendente: {result['still_pending']} un."
        flash(msg, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        request.referrer or url_for("admin_event_transactions", event_id=event_id)
    )


@app.route(
    "/admin/eventos/<int:event_id>/transacoes/<int:tx_id>/entregar",
    methods=["POST"],
)
@admin_required
def admin_event_confirm_items_delivery(event_id: int, tx_id: int):
    """Confirma a retirada de vários itens pendentes de um pedido."""
    if _event_or_404(event_id) is None:
        flash("Evento não encontrado.", "error")
        return redirect(url_for("admin_events"))
    item_ids = request.form.getlist("item_ids")
    try:
        result = confirm_items_delivery(
            tx_id,
            item_ids,
            expected_event_id=event_id,
            created_by=_current_admin_user(),
        )
        msg = (
            f"Entrega confirmada: {result['items_count']} produto(s), "
            f"{result['units_delivered']} un."
        )
        if result.get("errors"):
            msg += " Alguns itens não puderam ser entregues (verifique o estoque)."
            flash(msg, "warning")
            for err in result["errors"][:5]:
                flash(err, "error")
        else:
            flash(msg, "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        request.referrer or url_for("admin_event_transactions", event_id=event_id)
    )


@app.route(
    "/admin/eventos/<int:event_id>/transacoes/<int:tx_id>/entregue",
    methods=["POST"],
)
@admin_required
def admin_event_confirm_transaction_handover(event_id: int, tx_id: int):
    """Marca o pedido inteiro como entregue/retirado pelo cliente."""
    if _event_or_404(event_id) is None:
        flash("Evento não encontrado.", "error")
        return redirect(url_for("admin_events"))
    try:
        confirm_transaction_handover(tx_id, expected_event_id=event_id)
        flash("Pedido marcado como entregue.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        request.referrer or url_for("admin_event_transactions", event_id=event_id)
    )


# ---------------------------------------------------------------------------
# Eventos — API de polling (estoque / transações)
# ---------------------------------------------------------------------------

@app.route("/admin/api/eventos/<int:event_id>/transacoes")
@admin_required
def admin_api_event_transactions(event_id: int):
    """Fragmento HTML da listagem de transações (polling ~20s no admin)."""
    event = _event_or_404(event_id)
    if event is None:
        return ("", 404)
    page_data = _admin_event_transactions_page_data(event_id)
    return render_template(
        "admin/partials/event_transactions_live.html",
        event=event,
        **page_data,
    )


@app.route("/admin/api/eventos/<int:event_id>/estoque")
@admin_required
def admin_api_event_stock(event_id: int):
    if _event_or_404(event_id) is None:
        return jsonify({"error": "Evento não encontrado."}), 404
    products, _filters, pagination = _admin_event_stock_page_view(event_id)
    promo_ids = product_ids_with_active_promotions_for_event(event_id)
    promo_tooltips = active_promotion_tooltip_by_product_id(event_id)
    promo_names = active_promotion_names_by_product_id(event_id)
    return jsonify({
        "stats": get_event_stock_stats(event_id),
        "pagination": {
            "page": pagination["page"],
            "per_page": pagination["per_page"],
            "total": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
        "products": [
            {
                **p,
                "id": p["product_id"],
                "estoque": p["stock"],
                "estoque_minimo": p["min_stock"],
                "status": _event_product_status(p),
                "active_promo": int(p["product_id"]) in promo_ids,
                "promo_tooltip": promo_tooltips.get(int(p["product_id"]), ""),
                "promo_name": promo_names.get(int(p["product_id"]), ""),
                "pending_delivery_units": int(p.get("pending_delivery_units") or 0),
                "units_sold": int(p.get("units_sold") or 0),
            }
            for p in products
        ],
    })


@app.route("/admin/api/eventos/<int:event_id>/estoque/<int:product_id>")
@admin_required
def admin_api_event_stock_product(event_id: int, product_id: int):
    if _event_or_404(event_id) is None:
        return jsonify({"error": "Evento não encontrado."}), 404
    try:
        return jsonify(_event_stock_product_payload(event_id, product_id))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404


# ---------------------------------------------------------------------------
# Nota não fiscal
# ---------------------------------------------------------------------------

def _receipt_public_url(order_number: str, *, autoprint: bool = False) -> str:
    """URL assinada da nota de retirada (``/nota/OM...?t=...``)."""
    on = (order_number or "").strip()
    if not on:
        return ""
    token = sign_receipt_token(on, app.secret_key)
    extra = {"print": "1"} if autoprint else {}
    return url_for("receipt", order_number=on, t=token, **extra)


app.add_template_global(_receipt_public_url, "receipt_url")


@app.route("/nota/<path:order_number>")
def receipt(order_number: str):
    """Exibe a nota não fiscal (requer parâmetro ``t`` com assinatura válida)."""
    token = (request.args.get("t") or "").strip()
    if not verify_receipt_token(order_number, token, app.secret_key):
        return render_template(
            "nota.html",
            tx=None,
            order_number=order_number,
            autoprint=False,
            receipt_error="invalid_link",
        ), 403

    tx = get_transaction_by_order_number(order_number)
    autoprint = request.args.get("print", "").lower() in ("1", "true", "yes")
    if tx is None:
        return render_template(
            "nota.html",
            tx=None,
            order_number=order_number,
            autoprint=False,
            receipt_error="not_found",
        ), 404
    return render_template(
        "nota.html",
        tx=tx,
        order_number=order_number,
        autoprint=autoprint,
        receipt_error=None,
    )


# ---------------------------------------------------------------------------
# Filtros Jinja
# ---------------------------------------------------------------------------

@app.template_filter("brl")
def brl_filter(value):
    return _format_brl(value)


@app.template_filter("datahora")
def datahora_filter(value):
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    return dt.strftime("%d/%m/%Y %H:%M")


@app.template_filter("paymethod")
def paymethod_filter(value, installments=None):
    return _payment_method_label(value, installments)


@app.template_filter("cro_pedido")
def cro_pedido_filter(row):
    """Extrai UF e número do registro CRO da linha de movimentação / transação."""
    return _cro_pedido_fields(row if isinstance(row, dict) else None)


@app.template_filter("signed")
def signed_filter(value):
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return f"+{n}"
    return str(n)


@app.template_filter("movlabel")
def mov_label_filter(value):
    return {
        "entrada": "Entrada",
        "saida": "Saída",
        "venda": "Venda",
        "pendente": "Pendente",
        "ajuste": "Correção",
        "inicial": "Entrada",
    }.get(value, value or "-")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
