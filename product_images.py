"""Cópia local das imagens de produto (Wake) para operação offline.

Com internet, baixa a URL remota gravada em ``products.image`` para
``static/product-images/<id>.<ext>`` e atualiza o campo para um caminho
servido pelo Flask (``/static/product-images/...``). No evento, o catálogo
e o estoque passam a usar o arquivo do disco, sem acessar a Wake.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import requests

from database.connection import _now_iso, get_conn

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


def is_local_product_image(url: Optional[str]) -> bool:
    u = (url or "").strip()
    if not u:
        return False
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


def _safe_filename(product_id: int, ext: str) -> str:
    return f"{int(product_id)}{ext}"


def cache_remote_image(product_id: int, remote_url: str, *, timeout: int = 20) -> Optional[str]:
    """Baixa a imagem remota e devolve a URL local, ou None em caso de falha."""
    url = (remote_url or "").strip()
    pid = int(product_id)
    if pid <= 0 or not url:
        return None
    if is_local_product_image(url):
        return url
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
    filename = _safe_filename(pid, ext)
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

    local_url = f"{LOCAL_URL_PREFIX}{filename}"
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE products SET image = ?, updated_at = ? WHERE id = ?",
                (local_url, _now_iso(), pid),
            )
    except Exception as exc:
        log.warning("Imagem salva, mas falhou atualizar o banco do produto %s: %s", pid, exc)
        return local_url
    return local_url


def cache_product_if_remote(product_id: int, image_url: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """``ok`` | ``skip`` | ``fail``. Segundo valor é a URL local quando ``ok``."""
    pid = int(product_id)
    url = (image_url or "").strip()
    if not url:
        with get_conn() as conn:
            row = conn.execute("SELECT image FROM products WHERE id = ?", (pid,)).fetchone()
        url = (row["image"] if row else "") or ""
    if not url:
        return "skip", None
    if is_local_product_image(url) and os.path.isfile(
        os.path.join(_ROOT, url.lstrip("/").replace("/", os.sep))
    ):
        return "skip", url
    local = cache_remote_image(pid, url)
    if local:
        return "ok", local
    return "fail", None


def cache_images_for_products(rows: Iterable[Dict]) -> Dict[str, int]:
    """Baixa imagens remotas de uma lista com ``id``/``product_id`` e ``image``/``imagem``."""
    stats = {"ok": 0, "skip": 0, "fail": 0}
    for row in rows:
        pid = row.get("product_id") if row.get("product_id") is not None else row.get("id")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            stats["fail"] += 1
            continue
        url = row.get("image") or row.get("imagem") or ""
        status, _ = cache_product_if_remote(pid, url)
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
