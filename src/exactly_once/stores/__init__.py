"""Store adapters. Import the ``Store`` base and use its factories.

    from exactly_once import Store
    store = Store.sqlite("effects.db")   # or .memory() / .redis(url) / .postgres(dsn)
"""

from __future__ import annotations

from .base import Store

__all__ = ["Store"]
