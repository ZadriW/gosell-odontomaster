"""Cache local de imagens de produto (Wake) para operação offline.

Estratégia:
- A coluna ``products.image`` sempre guarda a URL remota original da Wake.
- Ao baixar para offline, o arquivo é salvo em ``static/product-images/<id>.<ext>``.
- ``resolve_image_url()`` verifica se existe cópia local; se sim retorna o path
  local, senão retorna a URL remota original.
- O admin dispara o download manualmente pelo botão "Salvar imagens (offline)".
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import requests

from database.connection import get_conn

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(_ROOT, "static", "product-images")
LOCAL_URL_PREFIX = "/static/product-images/"

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}


def is_local_path(url: Optional[str]) -> bool:
    u = (url or "").strip()
    return u.startswith(LOCAL_URL_PREFIX) or "/static/product-images/" in u


def _ensure_dir() -> None:
    os.makedirs(IMAGES_DIR, exist_ok=True)


def _ext_from_url_and_type(url: str, content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ctype]
    path = urlparse(url).path or ""
    m = re.search(r"\.(jpe?g|png|webp|gif|svg)$", path, re.I)
    if m:
        ext = m.group(1).lower()
        return ".jpg" if ext == "jpeg" else f".{ext}"
    return ".jpg"


def _local_file_for_product(product_id: int) -> Optional[str]:
    """Retorna o path absoluto do arquivo local se existir, None caso contrário."""
    prefix = os.path.join(IMAGES_DIR, str(int(product_id)))
    for ext in (".jpg", ".png", ".webp", ".gif", ".svg"):
        candidate = prefix + ext
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 32:
            return candidate
    return None


def resolve_image_url(product_id: int, remote_url: Optional[str]) -> str:
    """Retorna a URL para exibição: local se cacheada, remota caso contrário."""
    pid = int(product_id)
    local = _local_file_for_product(pid)
    if local:
        filename = os.path.basename(local)
        return f"{LOCAL_URL_PREFIX}{filename}"
    return (remote_url or "").strip()


def download_image(product_id: int, remote_url: str, *, timeout: int = 20) -> Optional[str]:
    """Baixa a imagem remota para disco. Retorna path local ou None se falhar."""
    url = (remote_url or "").strip()
    pid = int(product_id)
    if pid <= 0 or not url:
        return None
    if not url.startswith("http://") and not url.startswith("https://"):
        return None

    _ensure_dir()
    try:
        res = requests.get(url, timeout=timeout, stream=True)
        res.raise_for_status()
    except Exception as exc:
        log.warning("Falha ao baixar imagem do produto %s: %s", pid, exc)
        return None

    ctype = res.headers.get("Content-Type") or ""
    if ctype and not ctype.lower().startswith("image/") and "octet-stream" not in ctype.lower():
        log.warning("Resposta não é imagem para produto %s (%s)", pid, ctype)
        return None

    ext = _ext_from_url_and_type(url, ctype)
    filename = f"{pid}{ext}"
    dest = os.path.join(IMAGES_DIR, filename)
    try:
        with open(dest, "wb") as fh:
            for chunk in res.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
        if os.path.getsize(dest) < 32:
            os.remove(dest)
            return None
    except OSError as exc:
        log.warning("Não foi possível gravar imagem do produto %s: %s", pid, exc)
        return None

    return f"{LOCAL_URL_PREFIX}{filename}"


def cache_product(product_id: int, image_url: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Tenta baixar imagem de um produto. Retorna (status, local_url).

    status: 'ok' | 'skip' | 'fail'
    """
    pid = int(product_id)
    url = (image_url or "").strip()

    if not url:
        with get_conn() as conn:
            row = conn.execute("SELECT image FROM products WHERE id = ?", (pid,)).fetchone()
        url = (row["image"] if row else "") or ""

    if not url:
        return "skip", None

    if is_local_path(url):
        url = ""
        with get_conn() as conn:
            row = conn.execute("SELECT image FROM products WHERE id = ?", (pid,)).fetchone()
        url = (row["image"] if row else "") or ""
        if not url or is_local_path(url):
            if _local_file_for_product(pid):
                return "skip", None
            return "fail", None

    if _local_file_for_product(pid):
        return "skip", None

    local = download_image(pid, url)
    if local:
        return "ok", local
    return "fail", None


def cache_images_for_products(rows: Iterable[Dict]) -> Dict[str, int]:
    """Baixa imagens remotas de uma lista de produtos."""
    stats = {"ok": 0, "skip": 0, "fail": 0}
    for row in rows:
        pid = row.get("product_id") if row.get("product_id") is not None else row.get("id")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            stats["fail"] += 1
            continue
        url = row.get("image") or row.get("imagem") or ""
        status, _ = cache_product(pid, url)
        stats[status] = stats.get(status, 0) + 1
    return stats


def cache_images_for_event(event_id: int) -> Dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id AS id, p.image AS image
              FROM event_products ep
              JOIN products p ON p.id = ep.product_id
             WHERE ep.event_id = ?
            """,
            (int(event_id),),
        ).fetchall()
    return cache_images_for_products([dict(r) for r in rows])


def cache_images_for_catalog() -> Dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, image FROM products").fetchall()
    return cache_images_for_products([dict(r) for r in rows])
