"""SQLite store — single-host, durable, zero external dependency.

Atomicity mechanism: ``BEGIN IMMEDIATE`` takes SQLite's write lock, then an
``INSERT`` on the ``key`` PRIMARY KEY either succeeds (we are the ``FRESH`` winner)
or raises ``IntegrityError`` (a record already exists — read it). SQLite serializes
writers via the database file lock, so concurrent processes on the same file get a
strong single-host guarantee. It is **not** for multi-host — see ARCH §3.1.

Durability: WAL journaling with ``synchronous=NORMAL`` survives process death
(our crash model — SIGKILL, not power loss), which is what the crash-injection
suite exercises. A committed record is fsync-durable in the WAL.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from .._types import ClaimRecord, ClaimResult, State
from ..errors import StoreUnavailableError
from .base import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    key         TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    result      BLOB,
    fingerprint TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    token       TEXT
);
"""

_COLS = "key, state, result, fingerprint, created_at, updated_at, token"

# A vanished-record race (INSERT conflicts, then a concurrent process releases the
# row before our SELECT reads it) is retried a bounded number of times — never via
# recursion, which would re-enter the non-reentrant lock and deadlock.
_MAX_CLAIM_ATTEMPTS = 100


class SQLiteStore(Store):
    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        # check_same_thread=False + a lock: we guard the shared connection ourselves,
        # while BEGIN IMMEDIATE + the file lock handle cross-process serialization.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.executescript(_SCHEMA)

    def claim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        now = time.time()
        with self._lock:
            for _attempt in range(_MAX_CLAIM_ATTEMPTS):
                token = uuid.uuid4().hex
                self._conn.execute("BEGIN IMMEDIATE;")
                try:
                    self._conn.execute(
                        f"INSERT INTO effects({_COLS}) VALUES (?, ?, NULL, ?, ?, ?, ?);",
                        (key, State.IN_FLIGHT.value, fingerprint, now, now, token),
                    )
                except sqlite3.IntegrityError:
                    self._conn.execute("ROLLBACK;")
                    row = self._conn.execute(
                        "SELECT state, result, fingerprint, token FROM effects WHERE key = ?;",
                        (key,),
                    ).fetchone()
                    if row is None:
                        continue  # vanished between our conflict and read — retry (no recursion)
                    state, result, stored_fp, stored_token = row
                    return ClaimResult(State(state), key, result, stored_fp, stored_token)
                else:
                    self._conn.execute("COMMIT;")
                    return ClaimResult(State.FRESH, key, None, fingerprint, token)
            raise StoreUnavailableError(
                f"claim for {key!r} did not converge after {_MAX_CLAIM_ATTEMPTS} attempts "
                "under contention; failing closed (effect not run)."
            )

    def commit(self, key: str, result: bytes) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE;")
            row = self._conn.execute(
                "SELECT state FROM effects WHERE key = ?;", (key,)
            ).fetchone()
            if row is not None and row[0] == State.COMMITTED.value:
                self._conn.execute("COMMIT;")
                return  # idempotent
            self._conn.execute(
                f"INSERT INTO effects({_COLS}) VALUES (?, ?, ?, NULL, ?, ?, NULL) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state, result=excluded.result, "
                "updated_at=excluded.updated_at;",
                (key, State.COMMITTED.value, result, now, now),
            )
            self._conn.execute("COMMIT;")

    def release(self, key: str, token: str | None = None) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE;")
            if token is None:
                self._conn.execute(
                    "DELETE FROM effects WHERE key = ? AND state = ?;",
                    (key, State.IN_FLIGHT.value),
                )
            else:
                self._conn.execute(
                    "DELETE FROM effects WHERE key = ? AND state = ? AND token = ?;",
                    (key, State.IN_FLIGHT.value, token),
                )
            self._conn.execute("COMMIT;")

    def get(self, key: str) -> ClaimRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM effects WHERE key = ?;", (key,)
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list(self, state: State | None = None) -> Iterator[ClaimRecord]:
        with self._lock:
            if state is None:
                rows = self._conn.execute(f"SELECT {_COLS} FROM effects;").fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_COLS} FROM effects WHERE state = ?;", (state.value,)
                ).fetchall()
        for row in rows:
            yield _row_to_record(row)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_record(row: tuple[Any, ...]) -> ClaimRecord:
    key, state, result, fingerprint, created_at, updated_at, token = row
    return ClaimRecord(
        key=str(key),
        state=State(state),
        result=None if result is None else bytes(result),
        fingerprint=None if fingerprint is None else str(fingerprint),
        created_at=float(created_at),
        updated_at=float(updated_at),
        token=None if token is None else str(token),
    )
