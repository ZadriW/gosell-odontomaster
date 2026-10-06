"""Cliente do ERP Sankhya via gateway da 4R Tech (``portal-repres``).

Configuração (somente ``.env`` / ambiente — nunca no código ou no banco):

- ``SANKHYA_API_URL``: base do gateway (padrão abaixo).
- ``SANKHYA_API_KEY``: chave hexadecimal que vai no fim de cada caminho.
- ``SANKHYA_LOGIN`` / ``SANKHYA_PASSWORD``: usuário do próprio Sankhya.
- ``SANKHYA_TIMEOUT``: segundos por requisição (padrão 60; catálogo usa 5x).
- ``SANKHYA_PRODUCT_FILTER_PARAM``: nome do parâmetro que filtra ``/products``
  por ``CODPROD`` (busca de um produto só). Sem ele, a busca avulsa por código
  fica desligada e o produto entra pela sincronização completa.

Toda resposta do gateway tem o formato ``{message, success, data, error}``.
O token (JWT) vale 6 h; é renovado sozinho antes de vencer ou após um 401.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app.4rtech.com.br/ftapi/v1/portal-repres"
_TOKEN_MARGIN_SECONDS = 300
_TOKEN_FALLBACK_TTL = 6 * 3600


class SankhyaError(Exception):
    """Falha na integração. ``transient`` = vale tentar de novo mais tarde."""

    transient = False

    def __init__(self, message: str, *, response: Any = None):
        super().__init__(message)
        self.response = response


class SankhyaConfigError(SankhyaError):
    """Credenciais ou endereço do gateway ausentes no ``.env``."""


class SankhyaNetworkError(SankhyaError):
    """Sem internet, gateway fora do ar ou lento demais."""

    transient = True


class SankhyaRejected(SankhyaError):
    """O gateway respondeu e recusou o pedido (dado inválido, regra do ERP)."""


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def base_url() -> str:
    return (_env("SANKHYA_API_URL") or DEFAULT_BASE_URL).rstrip("/")


def missing_config() -> List[str]:
    """Variáveis obrigatórias ainda vazias no ambiente."""
    return [
        name for name in ("SANKHYA_API_KEY", "SANKHYA_LOGIN", "SANKHYA_PASSWORD")
        if not _env(name)
    ]


def is_configured() -> bool:
    return not missing_config()


def product_filter_param() -> str:
    return _env("SANKHYA_PRODUCT_FILTER_PARAM")


def _timeout() -> float:
    try:
        return max(5.0, float(_env("SANKHYA_TIMEOUT") or 60))
    except ValueError:
        return 60.0


def _url(path: str) -> str:
    key = _env("SANKHYA_API_KEY")
    if not key:
        raise SankhyaConfigError("SANKHYA_API_KEY não configurada no .env.")
    return f"{base_url()}/{path.strip('/')}/{key}"


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
_token: Optional[str] = None
_token_expires_at = 0.0


def _jwt_exp(token: str) -> Optional[float]:
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body)).get("exp")
        return float(exp) if exp else None
    except (IndexError, ValueError, TypeError):
        return None


def find_value(obj: Any, keys: Iterable[str]) -> Any:
    """Primeiro valor não vazio de uma das chaves, em qualquer nível do JSON."""
    wanted = {k.lower() for k in keys}
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in wanted and v not in (None, "", [], {}):
                    return v
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def _authenticate() -> str:
    global _token, _token_expires_at
    missing = missing_config()
    if missing:
        raise SankhyaConfigError(
            "Integração Sankhya não configurada. Preencha no .env: " + ", ".join(missing)
        )
    try:
        resp = requests.post(
            _url("autenticacao"),
            json={
                "authenticationType": "usuario-snk",
                "login": _env("SANKHYA_LOGIN"),
                "senha": _env("SANKHYA_PASSWORD"),
            },
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        raise SankhyaNetworkError(f"Sem conexão com o gateway do Sankhya: {exc}") from exc
    if resp.status_code >= 500:
        raise SankhyaNetworkError(f"Gateway do Sankhya fora do ar (HTTP {resp.status_code}).")
    try:
        body = resp.json()
    except ValueError:
        body = None
    token = find_value(body, ("token", "accessToken", "access_token")) if body else None
    if resp.status_code >= 400 or not token:
        msg = find_value(body, ("message", "error")) if body else None
        raise SankhyaRejected(
            f"Login no Sankhya recusado (HTTP {resp.status_code})"
            + (f": {msg}" if msg else ". Confira SANKHYA_LOGIN e SANKHYA_PASSWORD."),
            response=body,
        )
    _token = str(token)
    _token_expires_at = _jwt_exp(_token) or (time.time() + _TOKEN_FALLBACK_TTL)
    return _token


def _get_token(force: bool = False) -> str:
    with _token_lock:
        if force or not _token or time.time() > _token_expires_at - _TOKEN_MARGIN_SECONDS:
            return _authenticate()
        return _token


# ---------------------------------------------------------------------------
# Transporte
# ---------------------------------------------------------------------------

def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict] = None,
    params: Optional[Dict] = None,
    timeout: Optional[float] = None,
) -> Any:
    """Chama o gateway e devolve ``data``. Levanta ``SankhyaError`` nas falhas."""
    url = _url(path)
    for attempt in (1, 2):
        token = _get_token(force=attempt == 2)
        try:
            resp = requests.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout or _timeout(),
            )
        except requests.RequestException as exc:
            raise SankhyaNetworkError(f"Sem conexão com o gateway do Sankhya: {exc}") from exc
        if resp.status_code == 401 and attempt == 1:
            continue  # token vencido/revogado: renova e tenta uma vez
        break
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 500 or resp.status_code in (408, 429):
        raise SankhyaNetworkError(
            f"Gateway do Sankhya indisponível (HTTP {resp.status_code}).", response=body
        )
    if resp.status_code >= 400 or not isinstance(body, dict) or body.get("success") is False:
        msg = None
        if isinstance(body, dict):
            msg = body.get("error") or body.get("message")
            if isinstance(msg, (dict, list)):
                msg = json.dumps(msg, ensure_ascii=False)
        elif resp.text:
            msg = resp.text[:300]
        raise SankhyaRejected(
            f"Sankhya recusou ({method} {path}, HTTP {resp.status_code})"
            + (f": {msg}" if msg else "."),
            response=body if body is not None else resp.text[:2000],
        )
    return body.get("data")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def fetch_products(codprod: Optional[int] = None) -> List[Dict]:
    """Catálogo (só ativos). Com ``codprod``, exige ``SANKHYA_PRODUCT_FILTER_PARAM``."""
    params = None
    if codprod is not None:
        name = product_filter_param()
        if not name:
            raise SankhyaConfigError(
                "Busca de produto avulso desativada: defina SANKHYA_PRODUCT_FILTER_PARAM no .env."
            )
        params = {name: int(codprod)}
    data = _request("GET", "products", params=params, timeout=_timeout() * 5)
    return [d for d in (data or []) if isinstance(d, dict)]


def fetch_prices() -> List[Dict]:
    """Linhas ``{CODPROD, CODEMP, PRECO}``: uma por produto × empresa."""
    data = _request("GET", "products/price", timeout=_timeout() * 5)
    return [d for d in (data or []) if isinstance(d, dict)]


def create_update_client(payload: Dict) -> Dict:
    """Cria ou atualiza o parceiro (o gateway acha pelo CPF). Devolve ``{codparc, raw}``."""
    data = _request("POST", "clientes/create-update", json_body=payload)
    codparc = find_value(data, ("CODPARC",))
    try:
        codparc = int(codparc)
    except (TypeError, ValueError):
        raise SankhyaRejected(
            "Cadastro do cliente sem CODPARC na resposta do Sankhya.", response=data
        ) from None
    return {"codparc": codparc, "raw": data}


def create_order(payload: Dict) -> Dict:
    """Cadastra o pedido como orçamento. Devolve ``{order_id, raw}``."""
    data = _request("POST", "orders", json_body=payload)
    order_id = find_value(data, ("NUNOTA", "nunota", "NUMNOTA", "numnota", "id"))
    return {"order_id": str(order_id) if order_id is not None else None, "raw": data}


def cancel_order(order_id: Optional[str], order_number: str) -> Dict:
    """Cancelamento no ERP. O integrador ainda não passou o formato do endpoint."""
    raise SankhyaRejected(
        "Endpoint de cancelamento do Sankhya ainda não foi informado pelo integrador. "
        f"Cancele o pedido {order_id or order_number} direto no Sankhya e marque aqui como cancelado."
    )
