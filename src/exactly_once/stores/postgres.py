"""Postgres store — the strongest offered: true multi-writer, linearizable claim.

Atomicity mechanism: ``INSERT ... ON CONFLICT (key) DO NOTHING RETURNING`` under
``SERIALIZABLE`` isolation. The insert returns a row only for the winning writer;
everyone else sees the conflict and reads the existing record. The unique PRIMARY
KEY plus serializable isolation gives a linearizable claim across many
writers/hosts (ARCH §3.1).

Serialization failures surface as claim *contention*, not effect duplication: the
effect has not run yet, so retrying the whole claim transaction is safe.

Requires the ``postgres`` extra: ``pip install "exactly-once[postgres]"``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

from .._types import ClaimRecord, ClaimResult, State
from ..errors import StoreUnavailableError
from .base import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    key         text PRIMARY KEY,
    state       text NOT NULL,
    result      bytea,
    fingerprint text,
    created_at  double precision NOT NULL,
    updated_at  double precision NOT NULL
);
"""

_MAX_RETRIES = 5

# Hoisted so the sync and async paths share one definition (and stay under the line limit).
_CLAIM_SQL = (
    "INSERT INTO effects(key, state, result, fingerprint, created_at, updated_at) "
    "VALUES (%s, 'in_flight', NULL, %s, %s, %s) "
    "ON CONFLICT (key) DO NOTHING RETURNING key;"
)
_SELECT_SQL = "SELECT state, result, fingerprint FROM effects WHERE key = %s;"
_COMMIT_SQL = (
    "INSERT INTO effects(key, state, result, fingerprint, created_at, updated_at) "
    "VALUES (%s, 'committed', %s, NULL, %s, %s) "
    "ON CONFLICT (key) DO UPDATE SET state='committed', result=EXCLUDED.result, "
    "updated_at=EXCLUDED.updated_at WHERE effects.state <> 'committed';"
)
_DELETE_SQL = "DELETE FROM effects WHERE key = %s AND state = 'in_flight';"


class PostgresStore(Store):
    def __init__(self, dsn: str, *, max_retries: int = _MAX_RETRIES) -> None:
        try:
            import psycopg
            from psycopg import IsolationLevel
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "PostgresStore requires the 'postgres' extra: pip install 'exactly-once[postgres]'"
            ) from exc
        self._psycopg = psycopg
        self._isolation = IsolationLevel.SERIALIZABLE
        self._dsn = dsn
        self._max_retries = max_retries
        self._lock = threading.Lock()
        try:
            self._conn = psycopg.connect(dsn, autocommit=True)
        except psycopg.OperationalError as exc:
            raise StoreUnavailableError(str(exc)) from exc
        self._conn.isolation_level = self._isolation
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA)
        self._aconn: Any = None

    # --- sync, with a serialization-failure retry loop ---

    def _tx(self, fn: Any) -> Any:
        errors = self._psycopg.errors
        for attempt in range(self._max_retries):
            try:
                with self._lock, self._conn.transaction(), self._conn.cursor() as cur:
                    return fn(cur)
            except errors.SerializationFailure:
                if attempt == self._max_retries - 1:
                    raise
                time.sleep(0.01 * (attempt + 1))
            except self._psycopg.OperationalError as exc:
                raise StoreUnavailableError(str(exc)) from exc
        raise RuntimeError("unreachable")  # pragma: no cover

    def claim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        now = time.time()

        def _do(cur: Any) -> ClaimResult:
            cur.execute(_CLAIM_SQL, (key, fingerprint, now, now))
            if cur.fetchone() is not None:
                return ClaimResult(State.FRESH, key, None, fingerprint)
            cur.execute(_SELECT_SQL, (key,))
            state, result, stored_fp = cur.fetchone()
            return ClaimResult(State(state), key, _to_bytes(result), stored_fp)

        return self._tx(_do)  # type: ignore[no-any-return]

    def commit(self, key: str, result: bytes) -> None:
        now = time.time()

        def _do(cur: Any) -> None:
            cur.execute(_COMMIT_SQL, (key, result, now, now))

        self._tx(_do)

    def release(self, key: str) -> None:
        def _do(cur: Any) -> None:
            cur.execute(_DELETE_SQL, (key,))

        self._tx(_do)

    def get(self, key: str) -> ClaimRecord | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT key, state, result, fingerprint, created_at, updated_at "
                "FROM effects WHERE key = %s;",
                (key,),
            )
            row = cur.fetchone()
        return _row_to_record(row) if row is not None else None

    def list(self, state: State | None = None) -> Iterator[ClaimRecord]:
        with self._lock, self._conn.cursor() as cur:
            if state is None:
                cur.execute(
                    "SELECT key, state, result, fingerprint, created_at, updated_at FROM effects;"
                )
            else:
                cur.execute(
                    "SELECT key, state, result, fingerprint, created_at, updated_at "
                    "FROM effects WHERE state = %s;",
                    (state.value,),
                )
            rows = cur.fetchall()
        for row in rows:
            yield _row_to_record(row)

    def close(self) -> None:
        self._conn.close()

    # --- async (native) ---

    async def _ac(self) -> Any:
        if self._aconn is None:
            self._aconn = await self._psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            await self._aconn.set_isolation_level(self._isolation)
        return self._aconn

    async def aclaim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        conn = await self._ac()
        now = time.time()
        errors = self._psycopg.errors
        for attempt in range(self._max_retries):
            try:
                async with conn.transaction(), conn.cursor() as cur:
                    await cur.execute(_CLAIM_SQL, (key, fingerprint, now, now))
                    if await cur.fetchone() is not None:
                        return ClaimResult(State.FRESH, key, None, fingerprint)
                    await cur.execute(_SELECT_SQL, (key,))
                    state, result, stored_fp = await cur.fetchone()
                    return ClaimResult(State(state), key, _to_bytes(result), stored_fp)
            except errors.SerializationFailure:
                if attempt == self._max_retries - 1:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    async def acommit(self, key: str, result: bytes) -> None:
        conn = await self._ac()
        now = time.time()
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(_COMMIT_SQL, (key, result, now, now))

    async def arelease(self, key: str) -> None:
        conn = await self._ac()
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(_DELETE_SQL, (key,))

    async def aclose(self) -> None:
        if self._aconn is not None:
            await self._aconn.close()


def _to_bytes(v: Any) -> bytes | None:
    if v is None:
        return None
    return bytes(v)


def _row_to_record(row: tuple[Any, ...]) -> ClaimRecord:
    key, state, result, fingerprint, created_at, updated_at = row
    return ClaimRecord(
        key=key,
        state=State(state),
        result=_to_bytes(result),
        fingerprint=fingerprint,
        created_at=float(created_at),
        updated_at=float(updated_at),
    )
