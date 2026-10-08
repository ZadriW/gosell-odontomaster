"""Cliente do ERP Sankhya via gateway da 4R Tech (``portal-repres``).

Configuração (somente ``.env`` / ambiente — nunca no código ou no banco):

- ``SANKHYA_API_URL``: base do gateway (padrão abaixo), sem endpoint nem chave;
  ``url_problems`` aponta os erros de montagem.
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
from urllib3.exceptions import NewConnectionError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app.4rtech.com.br/ftapi/v1/portal-repres"
_TOKEN_MARGIN_SECONDS = 300
_TOKEN_FALLBACK_TTL = 6 * 3600


class SankhyaError(Exception):
    """Falha na integração. ``transient`` = vale tentar de novo mais tarde.

    A mensagem passa por ``redact``: ela vai para o banco, o painel e os logs,
    e o ``requests`` repete a URL (com a chave no caminho) nos erros de rede.
    """

    transient = False

    def __init__(self, message: str, *, response: Any = None):
        super().__init__(redact(message))
        self.response = response


class SankhyaConfigError(SankhyaError):
    """Credenciais ou endereço do gateway ausentes ou errados no ``.env``.

    Não é problema de um pedido: a fila inteira para até alguém corrigir.
    """


class SankhyaAuthError(SankhyaConfigError):
    """O gateway recusou o login (``SANKHYA_LOGIN`` / ``SANKHYA_PASSWORD``)."""


class SankhyaNetworkError(SankhyaError):
    """Sem internet, gateway fora do ar ou lento demais."""

    transient = True


class SankhyaRejected(SankhyaError):
    """O gateway respondeu e recusou o pedido (dado inválido, regra do ERP)."""


class SankhyaUncertain(SankhyaError):
    """O pedido pode ter chegado ao ERP, mas a resposta se perdeu.

    Só vale para chamadas que não podem ser repetidas às cegas (``create_order``):
    reenviar sozinho duplicaria o pedido no Sankhya. Alguém confere no ERP antes.
    """


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


#: Trechos que só aparecem numa URL de endpoint, nunca na base do gateway.
_ENDPOINT_SEGMENTS = ("autenticacao", "products", "orders", "clientes")


def url_problems() -> List[str]:
    """Erros de montagem do ``SANKHYA_API_URL`` (vazio = ok ou padrão).

    O caso clássico é colar a URL de login inteira (``.../autenticacao/<chave>``):
    o sistema acrescenta ``/autenticacao/<chave>`` de novo e o gateway responde 404.
    """
    raw = _env("SANKHYA_API_URL")
    if not raw:
        return []
    problems: List[str] = []
    if not raw.lower().startswith(("https://", "http://")):
        problems.append("SANKHYA_API_URL precisa começar com https://")
    key = _env("SANKHYA_API_KEY")
    if key and len(key) >= 6 and key.lower() in raw.lower():
        problems.append(
            "SANKHYA_API_URL contém a chave; ela vai só em SANKHYA_API_KEY"
        )
    path = raw.split("://", 1)[-1].split("?", 1)[0].lower()
    segments = path.strip("/").split("/")[1:]
    found = [s for s in _ENDPOINT_SEGMENTS if s in segments]
    if found:
        problems.append(
            f"SANKHYA_API_URL termina num endpoint (/{found[0]}); use só a base, "
            "ex.: https://<host>/ftapi/v1/portal-repres"
        )
    return problems


def check_connection() -> float:
    """Faz um login novo no gateway e devolve quanto levou (segundos).

    Só o login: os endpoints de dados devolvem o catálogo inteiro. Levanta a
    ``SankhyaError`` correspondente (endereço, login, rede).
    """
    started = time.monotonic()
    with _token_lock:
        _authenticate()
    return time.monotonic() - started


def product_filter_param() -> str:
    return _env("SANKHYA_PRODUCT_FILTER_PARAM")


def _timeout() -> float:
    try:
        return max(5.0, float(_env("SANKHYA_TIMEOUT") or 60))
    except ValueError:
        return 60.0


def redact(text: Any) -> str:
    """Tira a ``SANKHYA_API_KEY`` de um texto que vai ser gravado ou exibido."""
    out = str(text or "")
    key = _env("SANKHYA_API_KEY")
    if key and len(key) >= 6:
        out = out.replace(key, "***")
    return out


def _url(path: str) -> str:
    key = _env("SANKHYA_API_KEY")
    if not key:
        raise SankhyaConfigError("SANKHYA_API_KEY não configurada no .env.")
    problems = url_problems()
    if problems:
        # Falha aqui, com o motivo, em vez de um 404 confuso vindo do gateway.
        raise SankhyaConfigError("Endereço do gateway mal configurado: " + "; ".join(problems) + ".")
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


def _error_detail(body: Any) -> Optional[str]:
    """Motivo da recusa: ``message`` e ``error`` juntos, sem repetição.

    O gateway põe o resumo em ``message`` e o motivo real em ``error`` (texto ou
    ``{status, message}``), ex.: "Falha ao autenticar..." + "Dados do cliente inválidos".
    """
    if not isinstance(body, dict):
        return None
    parts: List[str] = []

    def add(value: Any) -> None:
        if value in (None, "", [], {}):
            return
        if isinstance(value, dict):
            inner = value.get("message") or value.get("mensagem")
            text = inner if inner else json.dumps(value, ensure_ascii=False)
            if not isinstance(text, str):
                add(text)
                return
        elif isinstance(value, list):
            for item in value:
                add(item)
            return
        else:
            text = str(value)
        text = " ".join(text.split()).rstrip(".")
        if text and text not in parts:
            parts.append(text)

    add(body.get("message"))
    add(body.get("error"))
    return " — ".join(parts) or None


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
            headers=_extra_headers(),
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        # ``from None``: a exceção original repete a URL com a chave nos logs.
        raise SankhyaNetworkError(f"Sem conexão com o gateway do Sankhya: {exc}") from None
    _raise_if_unreachable(resp)
    if resp.status_code >= 500 or resp.status_code in (408, 429):
        raise SankhyaNetworkError(f"Gateway do Sankhya fora do ar (HTTP {resp.status_code}).")
    try:
        body = resp.json()
    except ValueError:
        body = None
    token = find_value(body, ("token", "accessToken", "access_token")) if body else None
    if resp.status_code >= 400 or not token:
        detail = _error_detail(body)
        raise SankhyaAuthError(
            f"Login no Sankhya recusado (HTTP {resp.status_code})"
            + (f": {detail}." if detail else ".")
            + " Confira SANKHYA_LOGIN e SANKHYA_PASSWORD no .env.",
            response=body,
        )
    _token = str(token)
    _token_expires_at = _jwt_exp(_token) or (time.time() + _TOKEN_FALLBACK_TTL)
    return _token


#: Erros do ngrok em que a requisição comprovadamente não chegou ao gateway:
#: 3200 = túnel offline, 8012 = agente sem conexão com o gateway,
#: 6024 = página de aviso do plano gratuito.
_NGROK_NOT_DELIVERED = frozenset({"ERR_NGROK_3200", "ERR_NGROK_8012", "ERR_NGROK_6024"})


def _extra_headers() -> Dict[str, str]:
    """Túnel ngrok gratuito: pula a página de aviso que ele mostra a navegadores."""
    if "ngrok" in base_url().lower():
        return {"ngrok-skip-browser-warning": "1"}
    return {}


def _raise_if_unreachable(
    resp: requests.Response, *, retry_safe: bool = True, endpoint: Optional[str] = None
) -> None:
    """Resposta que não veio do gateway: túnel fora do ar ou endereço errado.

    - Erro do ngrok (cabeçalho ``ngrok-error-code``): falha temporária, tenta de
      novo depois. Em chamada não repetível (pedido), só os códigos de
      ``_NGROK_NOT_DELIVERED`` garantem que nada foi entregue; os outros (ex.
      ``ERR_NGROK_3004``, resposta inválida do gateway) viram ``SankhyaUncertain``.
    - 404 sem JSON, ou o "Cannot POST /..." (JSON) do servidor do gateway: a
      rota não existe nesse endereço; é configuração (``SANKHYA_API_URL``).
    """
    ngrok_code = resp.headers.get("ngrok-error-code")
    if ngrok_code:
        if not retry_safe and ngrok_code not in _NGROK_NOT_DELIVERED:
            raise SankhyaUncertain(
                f"Túnel do gateway do Sankhya falhou depois do envio "
                f"({ngrok_code}, HTTP {resp.status_code})."
            )
        raise SankhyaNetworkError(
            f"Túnel do gateway do Sankhya fora do ar ({ngrok_code}, HTTP {resp.status_code})."
        )
    if resp.status_code != 404:
        return
    if "json" not in (resp.headers.get("Content-Type") or ""):
        raise SankhyaConfigError(
            "Endereço do gateway não encontrado (HTTP 404). Confira SANKHYA_API_URL no .env."
        )
    try:
        body = resp.json()
    except ValueError:
        return
    msg = body.get("message") if isinstance(body, dict) else None
    if isinstance(msg, str) and msg.startswith("Cannot ") and endpoint:
        # O login acabou de passar nesta mesma base: o endereço está certo e é
        # só esta rota que o gateway não publica (ex.: "Cannot POST .../orders/***").
        raise SankhyaConfigError(
            f"O gateway do Sankhya não tem a rota /{endpoint.strip('/')} (HTTP 404: {msg}). "
            "O SANKHYA_API_URL está certo (o login funcionou); confirme com o integrador "
            "o caminho deste endpoint ou se ele já foi publicado."
        )  # sem ``response``: o corpo repete o caminho com a chave
    if isinstance(msg, str) and msg.startswith("Cannot "):
        # Ex.: "Cannot POST /ftapi/v1/autenticacao/***/autenticacao/***".
        raise SankhyaConfigError(
            f"Rota inexistente no gateway do Sankhya (HTTP 404: {msg}). Confira "
            "SANKHYA_API_URL no .env: ele leva só a base (ex.: .../ftapi/v1/portal-repres), "
            "sem /autenticacao nem a chave."
        )  # sem ``response``: o corpo repete o caminho com a chave


def _get_token(force: bool = False) -> str:
    with _token_lock:
        if force or not _token or time.time() > _token_expires_at - _TOKEN_MARGIN_SECONDS:
            return _authenticate()
        return _token


# ---------------------------------------------------------------------------
# Transporte
# ---------------------------------------------------------------------------

def _never_sent(exc: requests.RequestException) -> bool:
    """A conexão nem abriu (sem internet, DNS, recusa, TLS): o corpo não saiu."""
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.SSLError)):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    seen = set()
    cur: Any = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, NewConnectionError):
            return True
        nested = cur.args[0] if getattr(cur, "args", None) else None
        cur = getattr(cur, "reason", None) or (
            nested if isinstance(nested, BaseException) else None
        )
    return False


def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict] = None,
    params: Optional[Dict] = None,
    timeout: Optional[float] = None,
    retry_safe: bool = True,
) -> Any:
    """Chama o gateway e devolve ``data``. Levanta ``SankhyaError`` nas falhas.

    ``retry_safe=False`` (criação de pedido): falha depois que o corpo já pode
    ter saído vira ``SankhyaUncertain`` em vez de ``SankhyaNetworkError``.
    """
    url = _url(path)
    for attempt in (1, 2):
        token = _get_token(force=attempt == 2)
        try:
            resp = requests.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={"Authorization": f"Bearer {token}", **_extra_headers()},
                timeout=timeout or _timeout(),
            )
        except requests.RequestException as exc:
            if retry_safe or _never_sent(exc):
                raise SankhyaNetworkError(
                    f"Sem conexão com o gateway do Sankhya: {exc}"
                ) from None
            raise SankhyaUncertain(
                f"Sem resposta do Sankhya depois do envio ({exc.__class__.__name__})."
            ) from None
        if resp.status_code == 401 and attempt == 1:
            continue  # token vencido/revogado: renova e tenta uma vez
        break
    _raise_if_unreachable(resp, retry_safe=retry_safe, endpoint=path)
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 500 or resp.status_code in (408, 429):
        # 408/429/503: o gateway nem processou. 500/502/504: pode ter gravado.
        if not retry_safe and resp.status_code in (500, 502, 504):
            raise SankhyaUncertain(
                f"Gateway do Sankhya respondeu HTTP {resp.status_code} depois do envio.",
                response=body,
            )
        raise SankhyaNetworkError(
            f"Gateway do Sankhya indisponível (HTTP {resp.status_code}).", response=body
        )
    if resp.status_code == 401:
        # Token recém-emitido e ainda recusado: é o acesso, não o pedido.
        detail = _error_detail(body)
        raise SankhyaAuthError(
            f"Sankhya recusou o acesso ({method} {path}, HTTP 401)"
            + (f": {detail}." if detail else ".")
            + " Confira SANKHYA_LOGIN e SANKHYA_PASSWORD no .env.",
            response=body,
        )
    if resp.status_code >= 400 or not isinstance(body, dict) or body.get("success") is False:
        msg = _error_detail(body)
        if msg is None and not isinstance(body, dict) and resp.text:
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
    """Lote de produtos com cadastro "Aguard. Integração" (até 20; só ativos).

    Confirme com ``ack_products`` para receber o próximo lote. Com ``codprod``
    (busca avulsa), exige ``SANKHYA_PRODUCT_FILTER_PARAM``.
    """
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
    """Lote de preços "Aguard. Integração": ``{CODPROD, CODEMP, PRECO}``.

    Confirme com ``ack_prices`` para receber o próximo lote.
    """
    data = _request("GET", "products/price", timeout=_timeout() * 5)
    return [d for d in (data or []) if isinstance(d, dict)]


# Filas de integração (manual FTAPI / Odontomaster, 06/10/2026)
# ---------------------------------------------------------------------------
# Cada produto tem no Sankhya um status por assunto (cadastro, estoque, preço).
# Toda alteração o põe em "Aguard. Integração"; o GET do assunto devolve um
# lote (20 itens) desses pendentes e o POST com os ``códigos`` marca o lote
# como "Integrado". Sem a confirmação, o GET devolve sempre o mesmo lote.

def _ack(path: str, codes: Iterable[int]) -> None:
    unique = sorted({int(c) for c in codes})
    if unique:
        # Chave com acento, como no manual do integrador.
        _request("POST", path, json_body={"códigos": unique})


def ack_products(codes: Iterable[int]) -> None:
    """Marca o cadastro dos ``CODPROD`` como integrado (some de ``fetch_products``)."""
    _ack("products", codes)


def ack_prices(codes: Iterable[int]) -> None:
    """Marca o preço dos ``CODPROD`` como integrado (some de ``fetch_prices``)."""
    _ack("products/price", codes)


def fetch_stock() -> List[Dict]:
    """Lote de estoque "Aguard. Integração": ``{CODPROD, CODLOCAL, DESCRLOCAL,
    CONTROLE, ESTOQUE, RESERVADO, DISPONIVEL}``, uma linha por local × lote.

    ``CONTROLE`` é o lote (``" "`` = produto sem controle de lote). Confirme
    com ``ack_stock`` para receber o próximo lote.
    """
    data = _request("GET", "products/stock", timeout=_timeout() * 5)
    return [d for d in (data or []) if isinstance(d, dict)]


def ack_stock(codes: Iterable[int]) -> None:
    """Marca o estoque dos ``CODPROD`` como integrado (some de ``fetch_stock``)."""
    _ack("products/stock", codes)


def create_update_client(payload: Dict) -> Dict:
    """Cria ou atualiza o parceiro (o gateway acha pelo CPF). Devolve ``{codparc, raw}``.

    ``create-update-geral``: a versão que preenche os campos de controle do
    gateway (``AD_FTPRTREP*``) e grava ``CRO`` em ``AD_CRO``. A resposta traz
    ``data: [{CODPARC: "76139", ...}]``.
    """
    data = _request("POST", "clientes/create-update-geral", json_body=payload)
    codparc = find_value(data, ("CODPARC",))
    try:
        codparc = int(codparc)
    except (TypeError, ValueError):
        raise SankhyaRejected(
            "Cadastro do cliente sem CODPARC na resposta do Sankhya.", response=data
        ) from None
    return {"codparc": codparc, "raw": data}


def create_order(payload: Dict) -> Dict:
    """Cadastra o pedido como orçamento. Devolve ``{order_id, raw}``.

    Não é repetível: falha depois do envio levanta ``SankhyaUncertain``.
    ``order_id`` só sai de ``NUNOTA``; um ``id`` genérico poderia ser de um item.
    Caminho ``orders/create-order`` confirmado pelo integrador em 08/10/2026
    (o ``orders`` do primeiro exemplo responde 404 no gateway).
    """
    data = _request("POST", "orders/create-order", json_body=payload, retry_safe=False)
    order_id = find_value(data, ("NUNOTA",))
    if order_id is None:
        log.warning("Pedido aceito pelo Sankhya sem NUNOTA na resposta: %r", data)
    return {"order_id": str(order_id) if order_id is not None else None, "raw": data}


def cancel_order(order_id: Optional[str], order_number: str) -> Dict:
    """Cancelamento no ERP. O integrador ainda não passou o formato do endpoint."""
    raise SankhyaRejected(
        "Endpoint de cancelamento do Sankhya ainda não foi informado pelo integrador. "
        f"Cancele o pedido {order_id or order_number} direto no Sankhya e marque aqui como cancelado."
    )
