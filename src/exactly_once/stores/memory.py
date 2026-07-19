"""In-process store — tests, dev, and single-process demos.

Atomicity mechanism: a ``dict`` guarded by a ``threading.Lock``. Strong **within
one process**; nothing survives a crash (it's RAM). The async methods acquire the
same lock — memory ops are instant, so there is no event-loop blocking worth
avoiding, and one lock keeps a sync thread and the event loop from racing the dict.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace

from .._types import ClaimRecord, ClaimResult, State
from .base import Store


class MemoryStore(Store):
    def __init__(self) -> None:
        self._data: dict[str, ClaimRecord] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        with self._lock:
            row = self._data.get(key)
            if row is None:
                now = time.time()
                token = uuid.uuid4().hex
                self._data[key] = ClaimRecord(
                    key=key,
                    state=State.IN_FLIGHT,
                    result=None,
                    fingerprint=fingerprint,
                    created_at=now,
                    updated_at=now,
                    token=token,
                )
                return ClaimResult(State.FRESH, key, None, fingerprint, token)
            return ClaimResult(row.state, key, row.result, row.fingerprint, row.token)

    def commit(self, key: str, result: bytes) -> None:
        with self._lock:
            row = self._data.get(key)
            now = time.time()
            if row is None:
                # Defensive: a prober-driven back-fill of a key with no local record.
                self._data[key] = ClaimRecord(key, State.COMMITTED, result, None, now, now)
                return
            if row.state is State.COMMITTED:
                return  # idempotent
            self._data[key] = replace(row, state=State.COMMITTED, result=result, updated_at=now)

    def release(self, key: str, token: str | None = None) -> None:
        with self._lock:
            row = self._data.get(key)
            # release is IN_FLIGHT -> FRESH only; never undo a COMMITTED effect.
            # With a token, only the observer of that exact claim may retire it.
            if row is not None and row.state is State.IN_FLIGHT:
                if token is not None and row.token != token:
                    return  # a peer has re-claimed since we observed it — no-op
                del self._data[key]

    def get(self, key: str) -> ClaimRecord | None:
        with self._lock:
            return self._data.get(key)

    def list(self, state: State | None = None) -> Iterator[ClaimRecord]:
        with self._lock:
            rows = list(self._data.values())
        for row in rows:
            if state is None or row.state is state:
                yield row
