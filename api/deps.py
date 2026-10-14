"""Dépendances partagées par l'API REST et l'interface web."""
from __future__ import annotations

from functools import lru_cache

from datasentinel.service import DataSentinel


@lru_cache
def get_service() -> DataSentinel:
    return DataSentinel()
