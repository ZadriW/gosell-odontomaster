"""Caminhos do arquivo SQLite e fábrica de conexão (único lugar para PRAGMA e commit)."""
from __future__ import annotations

import os
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(_ROOT_DIR, "database")
DB_PATH = os.path.join(DB_DIR, "totem.sqlite3")

# Estoque mínimo padrão para produtos novos (biblioteca e eventos).
DEFAULT_MIN_STOCK = 5


def _ensure_dir() -> None:
    os.makedirs(DB_DIR, exist_ok=True)


def fold_search_text(text: Optional[str]) -> str:
    """Minúsculas e sem acento — para busca textual tolerante a caixa/acentuação.

    O ``LOWER()`` nativo do SQLite só normaliza ASCII: "GALVÃO" vira "galvÃo"
    (o "Ã" permanece intacto), então comparar com "galvão" digitado pelo
    usuário falha. Aqui usamos o casefold do Python (via NFD) antes de
    remover os acentos, cobrindo tanto o valor da coluna quanto o termo de
    busca.
    """
    normalized = unicodedata.normalize("NFD", (text or "").strip().casefold())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _connect() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from .products import _fold_product_search_text

    def _product_search_fold_sql(value) -> str:
        return _fold_product_search_text(value if value is not None else "")

    conn.create_function(
        "product_search_fold", 1, _product_search_fold_sql, deterministic=True
    )

    def _search_fold_sql(value) -> str:
        return fold_search_text(value)

    conn.create_function("search_fold", 1, _search_fold_sql, deterministic=True)
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
