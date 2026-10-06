"""Metadados do catálogo para o totem.

O catálogo real vem do **Sankhya** (sincronização via ``erp_sync.py``) e é
persistido no SQLite. Não há mais *seed* de produtos fictícios.

``CATEGORIES`` é uma lista mutável compartilhada com os templates; as
categorias de verdade vêm do banco (``DESCRGRUPOPROD`` do Sankhya).
"""

from __future__ import annotations

from typing import Dict, List

# Inicialmente vazio; mantido por compatibilidade com ``app.py``.
CATEGORIES: List[str] = []


def get_seed_products() -> List[Dict]:
    """Compatibilidade: o banco não é mais populado por produtos locais."""
    return []


def get_products() -> List[Dict]:
    """Compatibilidade retroativa — catálogo vem do banco, não deste módulo."""
    return []
