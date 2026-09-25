"""Cliente Wake Commerce — consulta on-demand por SKU (Storefront GraphQL).

Usado ao adicionar produtos por SKU ou importar planilha quando o item
ainda não existe no catálogo local. Não há sincronização em massa do catálogo.

Endpoint: https://storefront-api.fbits.net/graphql
Header:   TCS-Access-Token (variável de ambiente ``WAKE_TOKEN``)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

ENDPOINT = "https://storefront-api.fbits.net/graphql"


def wake_token_configured() -> bool:
    """True se ``WAKE_TOKEN`` estiver definido e não-vazio no ambiente."""
    return bool((os.environ.get("WAKE_TOKEN") or "").strip())


def _require_wake_token() -> str:
    """Retorna o TCS-Access-Token da Storefront API (Wake Commerce).

    O valor vem **somente** de ``WAKE_TOKEN`` no ambiente — sem fallback no código.
    """
    raw = (os.environ.get("WAKE_TOKEN") or "").strip()
    if not raw:
        raise PermissionError(
            "Configure a variável de ambiente WAKE_TOKEN com o TCS-Access-Token "
            "da Storefront API (painel Wake Commerce). O token não deve ficar no código-fonte."
        )
    return raw


# ---------------------------------------------------------------------------
# Transporte HTTP
# ---------------------------------------------------------------------------

def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "TCS-Access-Token": _require_wake_token(),
    }


def _graphql(
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict:
    """Executa uma query GraphQL contra a Storefront API da Wake.

    Levanta ``ConnectionError`` se a API não responder ou retornar erro HTTP.
    Levanta ``RuntimeError`` se o payload contiver erros GraphQL.
    """
    payload: Dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        resp = requests.post(
            ENDPOINT, json=payload, headers=_headers(), timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ConnectionError(f"Falha de rede ao acessar Wake API: {exc}") from exc

    if resp.status_code >= 500:
        raise ConnectionError(
            f"Wake API retornou HTTP {resp.status_code}. "
            "Verifique se o token TCS-Access-Token esta correto e ativo "
            "na plataforma Wake Commerce."
        )

    resp.raise_for_status()
    data = resp.json()

    errors = data.get("errors")
    if errors:
        auth_errors = [e for e in errors if "AUTH_NOT_AUTHENTICATED" in str(e)]
        if auth_errors:
            raise PermissionError(
                "Token nao autorizado pela Wake API. "
                "Verifique se o TCS-Access-Token esta correto e ativo."
            )
        msgs = "; ".join(e.get("message", str(e)) for e in errors)
        raise RuntimeError(f"Wake GraphQL: {msgs}")

    return data.get("data") or {}


# ---------------------------------------------------------------------------
# Helpers e consulta por SKU
# ---------------------------------------------------------------------------

def _primary_category(categories: list) -> str:
    """Extrai o nome da categoria mais específica (último nível da hierarquia)."""
    if not categories:
        return "Geral"
    best = categories[0]
    for c in categories:
        hier = c.get("hierarchy") or c.get("name") or ""
        best_hier = best.get("hierarchy") or best.get("name") or ""
        if hier.count("/") > best_hier.count("/"):
            best = c
    return (best.get("name") or "Geral").strip()


def _display_product_name(node: Dict[str, Any]) -> str:
    """Nome exibível: API às vezes preenche só ``variantName`` ou ``alias``."""
    for key in ("productName", "variantName"):
        v = (node.get(key) or "").strip()
        if v:
            return v
    alias = (node.get("alias") or "").strip()
    if alias:
        return alias
    return ""


def _display_product_sku(node: Dict[str, Any]) -> str:
    """SKU da linha Storefront; EAN como fallback quando ``sku`` vem vazio."""
    s = (node.get("sku") or "").strip()
    if s:
        return s
    ean = (node.get("ean") or "").strip()
    if ean:
        return ean
    return ""


def _build_subtitle_from_attributes(attributes: List[Dict[str, Any]]) -> str:
    """Monta subtitle a partir dos atributos retornados pela Wake para um produto.

    Cada ``ProductAttribute`` no nó já pertence àquela variante específica.
    Formato resultante: "CALIBRE LIMA: 25 - 25mm | COMPRIMENTO: 25mm".
    """
    if not attributes:
        return ""
    parts: List[str] = []
    for attr in attributes:
        attr_name = (attr.get("name") or "").strip()
        val_text = (attr.get("value") or "").strip()
        if not attr_name or not val_text:
            continue
        parts.append(f"{attr_name}: {val_text}")
    return " | ".join(parts)


def _normalize_wake_node(node: Dict[str, Any]) -> Dict:
    """Normaliza um nó bruto da Storefront API para o formato interno do Totem.

    Combina ``productName`` + ``variantName`` no nome quando a variante não é
    principal, para que entradas como "Lima Hedstroem — Nº45/80 25mm" sejam
    distintas de "Lima Hedstroem" (variante principal).
    """
    prices = node.get("prices") or {}
    price = prices.get("price") or prices.get("listPrice") or 0
    images = node.get("images") or []
    image_url = images[0].get("url", "") if images else ""
    cats = node.get("productCategories") or []
    category = _primary_category(cats)

    product_name = (node.get("productName") or "").strip()
    variant_name = (node.get("variantName") or "").strip()
    is_main = bool(node.get("mainVariant"))

    if not is_main and variant_name and variant_name.lower() not in product_name.lower():
        full_name = f"{product_name} {variant_name}".strip()
    else:
        full_name = product_name or variant_name or "Produto"

    sku_wake = _display_product_sku(node)
    product_id = int(node.get("productId") or 0)
    variant_id = int(node.get("productVariantId") or 0)
    parent_id = int(node.get("parentId") or 0) or product_id

    attributes = node.get("attributes") or []
    subtitle = _build_subtitle_from_attributes(attributes)

    return {
        "id": variant_id or product_id,
        "variant_id": variant_id,
        "parent_id": parent_id,
        "is_variant": not is_main,
        "main_variant": is_main,
        "is_variant_parent": False,
        "sku": sku_wake,
        "nome": full_name,
        "variant_name": variant_name,
        "subtitle": subtitle,
        "wake_product_id": product_id,
        "categoria": category,
        "descricao": f"{full_name} — {category}",
        "preco": float(price),
        "imagem": image_url,
    }


def _count_wake_family_variants(product_id: int) -> int:
    """Quantas variantes a Storefront devolve para o mesmo produto pai."""
    pid = int(product_id or 0)
    if pid <= 0:
        return 0
    seen: set[int] = set()
    for filt in (
        f'ignoreDisplayRules: true, sameParentAs: [{pid}]',
        f'ignoreDisplayRules: true, productId: [{pid}]',
    ):
        query = f"""
        query {{
          products(first: 30, filters: {{ {filt} }}) {{
            nodes {{
              productVariantId
              productId
            }}
          }}
        }}
        """
        try:
            data = _graphql(query)
        except Exception as exc:
            log.info("Contagem de variantes Wake (productId=%s) falhou: %s", pid, exc)
            continue
        nodes = (data.get("products") or {}).get("nodes") or []
        for n in nodes:
            vid = int(n.get("productVariantId") or n.get("productId") or 0)
            if vid > 0:
                seen.add(vid)
        if len(seen) > 1:
            return len(seen)
    return len(seen)


def _mark_unsellable_parent(normalized: Dict) -> Dict:
    """Produto-base (variante principal) de um item com várias opções: não é vendável."""
    if not normalized.get("main_variant"):
        return normalized
    family_id = int(normalized.get("parent_id") or normalized.get("wake_product_id") or 0)
    if family_id <= 0:
        return normalized
    if _count_wake_family_variants(family_id) > 1:
        normalized["is_variant_parent"] = True
    return normalized


def _escape_graphql_str(value: str) -> str:
    """Escapa string para uso literal em query GraphQL inline."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def fetch_product_by_sku(sku: str) -> Optional[Dict]:
    """Busca uma variante/produto na Wake pelo SKU exato (on-demand).

    A Storefront API da Wake não aceita variável GraphQL no filtro ``sku``
    (retorna HTTP 400); por isso o SKU é embutido na query com escape seguro.

    Usa ``ignoreDisplayRules: true`` para localizar variantes secundárias que
    não aparecem na vitrine pública. Retorna None se não encontrado ou se a
    API estiver indisponível — nunca lança exceção ao chamador.
    """
    q = (sku or "").strip()
    if not q:
        return None
    if not wake_token_configured():
        log.warning("fetch_product_by_sku(%s): WAKE_TOKEN não configurado.", q)
        return None
    if not re.fullmatch(r"[A-Za-z0-9._\-/#]+", q):
        log.warning("fetch_product_by_sku(%s): SKU com caracteres inválidos.", q)
        return None

    safe_sku = _escape_graphql_str(q)
    filt = f'ignoreDisplayRules: true, sku: "{safe_sku}"'
    nodes: List[Dict[str, Any]] = []
    try:
        nodes = _fetch_nodes_for_filter(filt, include_attributes=True, first=5)
    except PermissionError:
        log.warning("fetch_product_by_sku(%s): token Wake inválido ou ausente.", q)
        return None
    except Exception:
        try:
            nodes = _fetch_nodes_for_filter(filt, include_attributes=False, first=5)
        except PermissionError:
            log.warning("fetch_product_by_sku(%s): token Wake inválido ou ausente.", q)
            return None
        except Exception as exc:
            log.warning("fetch_product_by_sku(%s): falha na API Wake: %s", q, exc)
            return None
    if not nodes:
        return None
    exact = next(
        (n for n in nodes if _display_product_sku(n).strip() == q),
        None,
    )
    node = exact if exact is not None else nodes[0]
    return _normalize_wake_node(node)


def fetch_variants_by_sku(sku: str) -> List[Dict]:
    """Compatível com o fallback de catálogo: uma linha, ou lista vazia."""
    row = fetch_product_by_sku(sku)
    if not row:
        return []
    return [row]


_PRODUCT_FIELDS_BASIC = """
              productId
              parentId
              productVariantId
              mainVariant
              productName
              variantName
              sku
              ean
              prices {
                price
                listPrice
              }
              images {
                url
                fileName
              }
              productCategories {
                name
                hierarchy
              }
"""

_PRODUCT_FIELDS_ATTRIBUTES = """
              attributes {
                name
                value
              }
"""


def _fetch_nodes_for_filter(
    filt: str, include_attributes: bool = True, first: int = 50
) -> List[Dict[str, Any]]:
    """Executa a query de produtos para um filtro, com fallback sem attributes."""
    fields = _PRODUCT_FIELDS_BASIC + (_PRODUCT_FIELDS_ATTRIBUTES if include_attributes else "")
    query = f"""
    query {{
      products(first: {first}, filters: {{ {filt} }}) {{
        nodes {{
{fields}
        }}
      }}
    }}
    """
    data = _graphql(query)
    return (data.get("products") or {}).get("nodes") or []


def fetch_family_variants(product_id: int) -> List[Dict]:
    """Todas as variantes do produto-pai Wake (inclusive a principal).

    Consulta ``sameParentAs`` + ``productId`` para cobrir a variante principal
    que a Wake não inclui em ``sameParentAs``.

    Tenta buscar com ``attributes`` primeiro; se falhar, busca sem (subtitle vazio).
    """
    pid = int(product_id or 0)
    if pid <= 0:
        return []
    if not wake_token_configured():
        return []

    nodes_by_vid: Dict[int, Dict[str, Any]] = {}
    filters = (
        f"ignoreDisplayRules: true, sameParentAs: [{pid}]",
        f"ignoreDisplayRules: true, productId: [{pid}]",
    )

    for filt in filters:
        try:
            nodes = _fetch_nodes_for_filter(filt, include_attributes=True)
        except Exception as exc:
            log.info(
                "fetch_family_variants(pid=%s) com attributes falhou (%s): %s — tentando sem attributes",
                pid, filt, exc,
            )
            try:
                nodes = _fetch_nodes_for_filter(filt, include_attributes=False)
            except Exception as exc2:
                log.warning("fetch_family_variants(pid=%s) falhou (%s): %s", pid, filt, exc2)
                continue
        for n in nodes:
            vid = int(n.get("productVariantId") or n.get("productId") or 0)
            if vid > 0:
                nodes_by_vid[vid] = n

    family = [_normalize_wake_node(n) for n in nodes_by_vid.values()]
    family.sort(key=lambda p: (not p.get("main_variant"), int(p.get("id") or 0)))
    return family


def fetch_all_local_families_from_wake(
    wake_product_ids: List[int],
) -> List[Dict]:
    """Busca variantes Wake para cada família presente no catálogo local.

    Itera sobre os ``wake_product_id`` distintos gravados em ``products`` e
    retorna a lista completa (flat) de todas as variantes normalizadas.
    Falhas individuais são logadas mas não interrompem o processo.
    """
    all_variants: List[Dict] = []
    seen_pids: set = set()
    for pid in wake_product_ids:
        pid = int(pid or 0)
        if pid <= 0 or pid in seen_pids:
            continue
        seen_pids.add(pid)
        try:
            family = fetch_family_variants(pid)
        except Exception as exc:
            log.warning("Sync Wake família %s falhou: %s", pid, exc)
            continue
        all_variants.extend(family)
    log.info(
        "fetch_all_local_families_from_wake: %d famílias consultadas, %d variantes obtidas",
        len(seen_pids), len(all_variants),
    )
    return all_variants


def fetch_variants_by_ids(variant_ids: List[int], batch_size: int = 30) -> List[Dict]:
    """Busca variantes Wake por productVariantId em batches.

    Usado para enriquecer produtos locais cujo ``id`` corresponde a um
    ``productVariantId`` Wake, mas que não possuem ``wake_product_id`` mapeado.
    Retorna lista normalizada com subtitle quando disponível.
    """
    if not variant_ids or not wake_token_configured():
        return []

    all_nodes: List[Dict] = []
    for i in range(0, len(variant_ids), batch_size):
        chunk = variant_ids[i : i + batch_size]
        ids_str = ", ".join(str(vid) for vid in chunk)
        filt = f"ignoreDisplayRules: true, productVariantId: [{ids_str}]"
        try:
            nodes = _fetch_nodes_for_filter(filt, include_attributes=True, first=50)
        except Exception:
            try:
                nodes = _fetch_nodes_for_filter(filt, include_attributes=False, first=50)
            except Exception as exc:
                log.warning("fetch_variants_by_ids batch %d-%d falhou: %s", i, i + len(chunk), exc)
                continue
        all_nodes.extend(_normalize_wake_node(n) for n in nodes)

    log.info("fetch_variants_by_ids: %d IDs solicitados, %d variantes retornadas", len(variant_ids), len(all_nodes))
    return all_nodes
