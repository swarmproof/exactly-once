"""The store contract — the source of truth, and where all correctness lives.

The library itself is stateless between calls (ADR-002); the entire guarantee is
delegated to :meth:`Store.claim` being an **atomic check-and-set**. An adapter that
cannot make ``claim`` atomic is not a valid adapter — there is no
"eventually-consistent" store, because an eventually-consistent claim is a
double-fire waiting to happen.

Sync is the primitive; async parity (NFR-3) is provided by default methods that run
the sync call in a worker thread. Adapters backed by a native async driver (Redis,
Postgres) override the ``a*`` methods for true non-blocking I/O.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any

from .._types import ClaimRecord, ClaimResult, State


class Store(ABC):
    """Abstract store. Also the factory entry point: ``Store.sqlite("effects.db")``.

    The three mutating operations map exactly onto the state machine:

    * :meth:`claim` — atomic ``FRESH → IN_FLIGHT`` (exactly one winner).
    * :meth:`commit` — ``IN_FLIGHT → COMMITTED`` (idempotent).
    * :meth:`release` — ``IN_FLIGHT → FRESH`` (**pre-effect only**; see ADR-004).

    Plus a read-only ledger (:meth:`get`, :meth:`list`) for reconciliation review
    and for the stampede/costbomb assertion surface (REQ-S9).
    """

    # --- the atomic core ---------------------------------------------------

    @abstractmethod
    def claim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        """ATOMIC check-and-set.

        * No record → create ``IN_FLIGHT`` (storing ``fingerprint`` and a
          created-at timestamp) and return ``state=FRESH``.
        * ``IN_FLIGHT`` → return ``state=IN_FLIGHT`` (+ the stored fingerprint).
        * ``COMMITTED`` → return ``state=COMMITTED`` + the stored ``result``.

        MUST be atomic: under concurrency, exactly one caller sees ``FRESH``.
        """

    @abstractmethod
    def commit(self, key: str, result: bytes) -> None:
        """``IN_FLIGHT → COMMITTED``, storing the serialized result. Idempotent."""

    @abstractmethod
    def release(self, key: str, token: str | None = None) -> None:
        """``IN_FLIGHT → FRESH`` (delete the record). Legal only pre-effect (§5).

        If ``token`` is given this is a **compare-and-delete**: the record is removed
        only if it is ``IN_FLIGHT`` *and* still carries that ownership token, so a
        concurrent reconciler cannot delete a claim a peer has already re-claimed.
        With ``token=None`` it deletes any ``IN_FLIGHT`` record (internal/pre-effect).
        """

    # --- ledger (read-only) ------------------------------------------------

    @abstractmethod
    def get(self, key: str) -> ClaimRecord | None:
        """Return the ledger record for ``key``, or ``None`` if there is none."""

    @abstractmethod
    def list(self, state: State | None = None) -> Iterator[ClaimRecord]:
        """Enumerate records, optionally filtered by ``state`` (quarantine review)."""

    def close(self) -> None:  # noqa: B027 - intentional optional-override hook, not abstract
        """Release any backend resources. No-op by default."""

    # --- async parity: default delegates to the sync path in a thread ------

    async def aclaim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        return await asyncio.to_thread(self.claim, key, fingerprint=fingerprint)

    async def acommit(self, key: str, result: bytes) -> None:
        await asyncio.to_thread(self.commit, key, result)

    async def arelease(self, key: str, token: str | None = None) -> None:
        await asyncio.to_thread(self.release, key, token)

    async def aget(self, key: str) -> ClaimRecord | None:
        return await asyncio.to_thread(self.get, key)

    async def alist(self, state: State | None = None) -> Sequence[ClaimRecord]:
        return await asyncio.to_thread(lambda: list(self.list(state)))

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    # --- factories (lazy imports keep optional backends optional) ----------

    @classmethod
    def memory(cls) -> Store:
        """In-process store (tests/dev only; nothing survives a crash)."""
        from .memory import MemoryStore

        return MemoryStore()

    @classmethod
    def sqlite(cls, path: str = ":memory:", **kwargs: Any) -> Store:
        """Single-host durable store backed by a SQLite file."""
        from .sqlite import SQLiteStore

        return SQLiteStore(path, **kwargs)

    @classmethod
    def redis(cls, url: str = "redis://localhost:6379/0", **kwargs: Any) -> Store:
        """Distributed store against a single Redis (requires ``exactly-once[redis]``)."""
        from .redis import RedisStore

        return RedisStore(url, **kwargs)

    @classmethod
    def postgres(cls, dsn: str, **kwargs: Any) -> Store:
        """True multi-writer store (requires ``exactly-once[postgres]``)."""
        from .postgres import PostgresStore

        return PostgresStore(dsn, **kwargs)
