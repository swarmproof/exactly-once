"""Redis store — distributed workers sharing one Redis (the common prod case).

Atomicity mechanism: a Lua script runs the check-and-set in a single atomic step
server-side (Redis executes a script without interleaving other commands). Each key
is a hash holding ``state`` / ``result`` / ``fingerprint`` / timestamps.

Honest guarantee (ARCH §3.1): **strong against a single Redis instance.** Under
Sentinel/Cluster *failover* Redis is not strictly linearizable — a failover window
can, in principle, lose a very-recent claim. This adapter is documented as
"strong single-instance, best-effort under failover"; use Postgres when you need
true linearizable multi-writer.

Requires the ``redis`` extra: ``pip install "exactly-once[redis]"``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from .._types import ClaimRecord, ClaimResult, State
from ..errors import StoreUnavailableError
from .base import Store

# --- atomic Lua scripts (server-side, non-interleaved) ---------------------

_CLAIM_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    redis.call('HSET', KEYS[1], 'state', 'in_flight',
               'fingerprint', ARGV[1], 'created_at', ARGV[2], 'updated_at', ARGV[2])
    return {'fresh', '', ARGV[1]}
end
local result = redis.call('HGET', KEYS[1], 'result')
return {redis.call('HGET', KEYS[1], 'state'), result or '',
        redis.call('HGET', KEYS[1], 'fingerprint') or ''}
"""

_COMMIT_LUA = """
if redis.call('HGET', KEYS[1], 'state') == 'committed' then return 0 end
redis.call('HSET', KEYS[1], 'state', 'committed', 'result', ARGV[1], 'updated_at', ARGV[2])
if tonumber(ARGV[3]) > 0 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3])) end
return 1
"""

_RELEASE_LUA = """
if redis.call('HGET', KEYS[1], 'state') == 'in_flight' then
    redis.call('DEL', KEYS[1]); return 1
end
return 0
"""


def _b(v: Any) -> bytes | None:
    if v in (None, b"", ""):
        return None
    return v if isinstance(v, bytes) else str(v).encode()


def _s(v: Any) -> str | None:
    if v in (None, b"", ""):
        return None
    return v.decode() if isinstance(v, bytes) else str(v)


def _state(v: Any) -> State:
    """Decode a required state field (always present on any existing record)."""
    s = _s(v)
    if s is None:  # pragma: no cover - a record always has a state
        raise ValueError("record is missing its state field")
    return State(s)


class RedisStore(Store):
    def __init__(
        self, url: str = "redis://localhost:6379/0", *, prefix: str = "eo:", committed_ttl: int = 0
    ) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "RedisStore requires the 'redis' extra: pip install 'exactly-once[redis]'"
            ) from exc
        self._redis = redis
        self._url = url
        self._prefix = prefix
        self._committed_ttl = committed_ttl
        self._client = redis.Redis.from_url(url)
        self._claim_s = self._client.register_script(_CLAIM_LUA)
        self._commit_s = self._client.register_script(_COMMIT_LUA)
        self._release_s = self._client.register_script(_RELEASE_LUA)
        self._aclient: Any = None

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _now(self) -> str:
        import time

        return repr(time.time())

    # --- sync ---

    def claim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        try:
            state, result, fp = self._claim_s(
                keys=[self._k(key)], args=[fingerprint or "", self._now()]
            )
        except (self._redis.ConnectionError, self._redis.TimeoutError) as exc:
            raise StoreUnavailableError(str(exc)) from exc
        return ClaimResult(_state(state), key, _b(result), _s(fp))

    def commit(self, key: str, result: bytes) -> None:
        try:
            self._commit_s(keys=[self._k(key)], args=[result, self._now(), self._committed_ttl])
        except (self._redis.ConnectionError, self._redis.TimeoutError) as exc:
            raise StoreUnavailableError(str(exc)) from exc

    def release(self, key: str) -> None:
        try:
            self._release_s(keys=[self._k(key)], args=[])
        except (self._redis.ConnectionError, self._redis.TimeoutError) as exc:
            raise StoreUnavailableError(str(exc)) from exc

    def get(self, key: str) -> ClaimRecord | None:
        h = self._client.hgetall(self._k(key))
        return _hash_to_record(key, h)

    def list(self, state: State | None = None) -> Iterator[ClaimRecord]:
        for raw in self._client.scan_iter(match=f"{self._prefix}*"):
            full = raw.decode() if isinstance(raw, bytes) else raw
            key = full[len(self._prefix) :]
            rec = _hash_to_record(key, self._client.hgetall(full))
            if rec is not None and (state is None or rec.state is state):
                yield rec

    def close(self) -> None:
        self._client.close()

    # --- async (native, non-blocking) ---

    def _ac(self) -> Any:
        if self._aclient is None:
            import redis.asyncio as aioredis

            self._aclient = aioredis.from_url(self._url)
            self._aclaim_s = self._aclient.register_script(_CLAIM_LUA)
            self._acommit_s = self._aclient.register_script(_COMMIT_LUA)
            self._arelease_s = self._aclient.register_script(_RELEASE_LUA)
        return self._aclient

    async def aclaim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        self._ac()
        try:
            state, result, fp = await self._aclaim_s(
                keys=[self._k(key)], args=[fingerprint or "", self._now()]
            )
        except (self._redis.ConnectionError, self._redis.TimeoutError) as exc:
            raise StoreUnavailableError(str(exc)) from exc
        return ClaimResult(_state(state), key, _b(result), _s(fp))

    async def acommit(self, key: str, result: bytes) -> None:
        self._ac()
        await self._acommit_s(keys=[self._k(key)], args=[result, self._now(), self._committed_ttl])

    async def arelease(self, key: str) -> None:
        self._ac()
        await self._arelease_s(keys=[self._k(key)], args=[])

    async def aget(self, key: str) -> ClaimRecord | None:
        h = await self._ac().hgetall(self._k(key))
        return _hash_to_record(key, h)

    async def alist(self, state: State | None = None) -> Sequence[ClaimRecord]:
        out: list[ClaimRecord] = []
        async for raw in self._ac().scan_iter(match=f"{self._prefix}*"):
            full = raw.decode() if isinstance(raw, bytes) else raw
            key = full[len(self._prefix) :]
            rec = _hash_to_record(key, await self._ac().hgetall(full))
            if rec is not None and (state is None or rec.state is state):
                out.append(rec)
        return out

    async def aclose(self) -> None:
        if self._aclient is not None:
            await self._aclient.aclose()


def _hash_to_record(key: str, h: dict[Any, Any]) -> ClaimRecord | None:
    if not h:
        return None
    g = {(k.decode() if isinstance(k, bytes) else k): v for k, v in h.items()}
    created = _s(g.get("created_at"))
    updated = _s(g.get("updated_at"))
    return ClaimRecord(
        key=key,
        state=_state(g.get("state")),
        result=_b(g.get("result")),
        fingerprint=_s(g.get("fingerprint")),
        created_at=float(created) if created else None,
        updated_at=float(updated) if updated else None,
    )
