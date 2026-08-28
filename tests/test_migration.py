"""Schema migration — upgrading a durable store's database across versions (v0.2.1).

Regression for the bug where `CREATE TABLE IF NOT EXISTS` silently left an older
table intact, so a returning user's first call hit "no such column". CI always
started from a fresh DB and never saw it; these tests seed old schemas on purpose.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exactly_once import State, Store, once

# The two historical SQLite schemas a user might have on disk.
_SCHEMA_V010 = """
CREATE TABLE effects (
    key TEXT PRIMARY KEY, state TEXT NOT NULL, result BLOB, fingerprint TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, token TEXT
);
"""  # v0.1.0: has token, but no lease_expires_at
_SCHEMA_PRE_TOKEN = """
CREATE TABLE effects (
    key TEXT PRIMARY KEY, state TEXT NOT NULL, result BLOB, fingerprint TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""  # earliest: no token, no lease_expires_at


@pytest.mark.parametrize(
    "old_schema", [_SCHEMA_V010, _SCHEMA_PRE_TOKEN], ids=["v0.1.0", "pre-token"]
)
def test_sqlite_upgrades_an_old_database(tmp_path: Path, old_schema: str) -> None:
    path = str(tmp_path / "effects.db")
    seed = sqlite3.connect(path)
    seed.executescript(old_schema)
    seed.commit()
    seed.close()

    store = Store.sqlite(path)  # opening must migrate the schema, not crash
    n = {"c": 0}

    @once(store, key="charge:o1", lease_ttl=30.0)
    def charge() -> str:
        n["c"] += 1
        return "ok"

    assert charge() == charge() == "ok"
    assert n["c"] == 1
    rec = store.get("charge:o1")
    assert rec is not None and rec.state is State.COMMITTED and rec.token is not None
    store.close()


def test_sqlite_migration_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "effects.db")
    Store.sqlite(path).close()  # creates the current schema
    Store.sqlite(path).close()  # re-opening must not try to re-add existing columns
    # a third open still works end to end
    store = Store.sqlite(path)
    assert store.claim("k").state is State.FRESH
    store.close()
