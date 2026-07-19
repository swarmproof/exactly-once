"""Postgres store — the strongest offered: true multi-writer, linearizable claim.

Atomicity mechanism: ``INSERT ... ON CONFLICT (key) DO NOTHING RETURNING`` under
``SERIALIZABLE`` isolation. The insert returns a row only for the winning writer;
everyone else sees the conflict and reads the existing record. The unique PRIMARY
KEY plus serializable isolation gives a linearizable claim across many
writers/hosts (ARCH §3.1).

Serialization failures surface as claim *contention*, not effect duplication: the
effect has not run yet, so retrying the whole claim transaction is safe.

Concurrency within one process is serialized: the sync path holds a
``threading.Lock`` and the async path an ``asyncio.Lock`` around the single shared
connection (psycopg forbids concurrent operations on one connection). True
multi-writer concurrency comes from *separate* processes/connections, which
Postgres itself serializes.

Requires the ``postgres`` extra: ``pip install "exactly-once[postgres]"``.
"""

from __future__ import annotations

import asyncio
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
    key         text PRIMARY KEY,
    state       text NOT NULL,
    result      bytea,
    fingerprint text,
    created_at  double precision NOT NULL,
    updated_at  double precision NOT NULL,
    token       text
);
"""

_MAX_RETRIES = 5

_CLAIM_SQL = (
    "INSERT INTO effects(key, state, result, fingerprint, created_at, updated_at, token) "
    "VALUES (%s, 'in_flight', NULL, %s, %s, %s, %s) "
    "ON CONFLICT (key) DO NOTHING RETURNING key;"
)
_SELECT_SQL = "SELECT state, result, fingerprint, token FROM effects WHERE key = %s;"
_COMMIT_SQL = (
    "INSERT INTO effects(key, state, result, fingerprint, created_at, updated_at, token) "
    "VALUES (%s, 'committed', %s, NULL, %s, %s, NULL) "
    "ON CONFLICT (key) DO UPDATE SET state='committed', result=EXCLUDED.result, "
    "updated_at=EXCLUDED.updated_at WHERE effects.state <> 'committed';"
)
_DELETE_SQL = "DELETE FROM effects WHERE key = %s AND state = 'in_flight';"
_DELETE_TOKEN_SQL = "DELETE FROM effects WHERE key = %s AND state = 'in_flight' AND token = %s;"
_LEDGER_COLS = "key, state, result, fingerprint, created_at, updated_at, token"


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
        self._alock: asyncio.Lock | None = None

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
        token = uuid.uuid4().hex

        def _do(cur: Any) -> ClaimResult:
            cur.execute(_CLAIM_SQL, (key, fingerprint, now, now, token))
            if cur.fetchone() is not None:
                return ClaimResult(State.FRESH, key, None, fingerprint, token)
            cur.execute(_SELECT_SQL, (key,))
            st, res, fp, tok = cur.fetchone()
            return ClaimResult(State(st), key, _to_bytes(res), fp, tok)

        return self._tx(_do)  # type: ignore[no-any-return]

    def commit(self, key: str, result: bytes) -> None:
        now = time.time()

        def _do(cur: Any) -> None:
            cur.execute(_COMMIT_SQL, (key, result, now, now))

        self._tx(_do)

    def release(self, key: str, token: str | None = None) -> None:
        def _do(cur: Any) -> None:
            if token is None:
                cur.execute(_DELETE_SQL, (key,))
            else:
                cur.execute(_DELETE_TOKEN_SQL, (key, token))

        self._tx(_do)

    def get(self, key: str) -> ClaimRecord | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_LEDGER_COLS} FROM effects WHERE key = %s;", (key,))
            row = cur.fetchone()
        return _row_to_record(row) if row is not None else None

    def list(self, state: State | None = None) -> Iterator[ClaimRecord]:
        with self._lock, self._conn.cursor() as cur:
            if state is None:
                cur.execute(f"SELECT {_LEDGER_COLS} FROM effects;")
            else:
                cur.execute(f"SELECT {_LEDGER_COLS} FROM effects WHERE state = %s;", (state.value,))
            rows = cur.fetchall()
        for row in rows:
            yield _row_to_record(row)

    def close(self) -> None:
        self._conn.close()

    # --- async (native, serialized on one connection via an asyncio.Lock) ---

    async def _ac(self) -> Any:
        if self._aconn is None:
            self._aconn = await self._psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            await self._aconn.set_isolation_level(self._isolation)
            self._alock = asyncio.Lock()
        return self._aconn

    async def aclaim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        conn = await self._ac()
        assert self._alock is not None
        now = time.time()
        token = uuid.uuid4().hex
        errors = self._psycopg.errors
        async with self._alock:
            for attempt in range(self._max_retries):
                try:
                    async with conn.transaction(), conn.cursor() as cur:
                        await cur.execute(_CLAIM_SQL, (key, fingerprint, now, now, token))
                        if await cur.fetchone() is not None:
                            return ClaimResult(State.FRESH, key, None, fingerprint, token)
                        await cur.execute(_SELECT_SQL, (key,))
                        st, res, fp, tok = await cur.fetchone()
                        return ClaimResult(State(st), key, _to_bytes(res), fp, tok)
                except errors.SerializationFailure:
                    if attempt == self._max_retries - 1:
                        raise
        raise RuntimeError("unreachable")  # pragma: no cover

    async def acommit(self, key: str, result: bytes) -> None:
        conn = await self._ac()
        assert self._alock is not None
        now = time.time()
        async with self._alock, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_COMMIT_SQL, (key, result, now, now))

    async def arelease(self, key: str, token: str | None = None) -> None:
        conn = await self._ac()
        assert self._alock is not None
        async with self._alock, conn.transaction(), conn.cursor() as cur:
            if token is None:
                await cur.execute(_DELETE_SQL, (key,))
            else:
                await cur.execute(_DELETE_TOKEN_SQL, (key, token))

    async def aclose(self) -> None:
        if self._aconn is not None:
            await self._aconn.close()


def _to_bytes(v: Any) -> bytes | None:
    if v is None:
        return None
    return bytes(v)


def _row_to_record(row: tuple[Any, ...]) -> ClaimRecord:
    key, state, result, fingerprint, created_at, updated_at, token = row
    return ClaimRecord(
        key=key,
        state=State(state),
        result=_to_bytes(result),
        fingerprint=fingerprint,
        created_at=float(created_at),
        updated_at=float(updated_at),
        token=token,
    )
