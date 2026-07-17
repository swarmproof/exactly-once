"""Shared store fixtures. The SpyEffect execution counter lives in ``_spy.py`` so
both this file and the test modules can import it."""

from __future__ import annotations

from pathlib import Path

import pytest

from exactly_once import Store


@pytest.fixture
def memory_store() -> Store:
    return Store.memory()


@pytest.fixture
def sqlite_store(tmp_path: Path) -> Store:
    store = Store.sqlite(str(tmp_path / "effects.db"))
    yield store
    store.close()


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    """Parametrized over the two always-available backends.

    Redis/Postgres get their own testcontainer-gated modules (they need Docker).
    """
    s = Store.memory() if request.param == "memory" else Store.sqlite(str(tmp_path / "effects.db"))
    yield s
    s.close()
