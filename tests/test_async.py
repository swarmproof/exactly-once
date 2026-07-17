"""Async-parity tests — NFR-3. Every semantic mirrored through the async API.

Runs against memory + SQLite; their async methods delegate to the sync path in a
worker thread, so this also exercises that delegation.
"""

from __future__ import annotations

import asyncio

import pytest

from exactly_once import (
    ProbeResult,
    QuarantinedError,
    State,
    Store,
    Verdict,
    check_then_decide,
    once,
)


async def test_async_decorator_runs_once_then_replays(store: Store) -> None:
    n = {"c": 0}

    @once(store, key=lambda o, **_: f"charge:{o}")
    async def charge(o: str) -> dict:
        n["c"] += 1
        await asyncio.sleep(0)
        return {"order": o}

    a = await charge("o1")
    b = await charge("o1")
    assert a == b == {"order": "o1"}
    assert n["c"] == 1


async def test_async_context_manager_fresh_then_replay(store: Store) -> None:
    sent = {"c": 0}

    async def send() -> int:
        async with once(store, key="welcome:u1") as guard:
            if guard.fresh:
                sent["c"] += 1
                guard.result = 42
            return guard.result

    assert await send() == 42
    assert await send() == 42  # replay
    assert sent["c"] == 1


async def test_async_quarantines_orphaned_key(store: Store) -> None:
    await store.aclaim("stuck")  # orphaned IN_FLIGHT

    @once(store, key="stuck")
    async def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        await effect()


async def test_async_exception_leaves_in_flight(store: Store) -> None:
    n = {"c": 0}

    @once(store, key="risky")
    async def risky() -> str:
        n["c"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await risky()
    rec = await store.aget("risky")
    assert rec is not None and rec.state is State.IN_FLIGHT
    with pytest.raises(QuarantinedError):
        await risky()
    assert n["c"] == 1


async def test_async_check_then_decide_with_async_prober(store: Store) -> None:
    n = {"c": 0}
    await store.aclaim("charge:o2")  # orphaned

    async def prober(key: str) -> ProbeResult:
        await asyncio.sleep(0)
        return ProbeResult(Verdict.COMMITTED, {"id": "recovered"})

    @once(store, key="charge:o2", policy=check_then_decide(prober))
    async def charge() -> dict:
        n["c"] += 1
        return {"id": "new"}

    assert await charge() == {"id": "recovered"}
    assert n["c"] == 0  # never re-ran
    assert (await store.aget("charge:o2")).state is State.COMMITTED


async def test_async_concurrent_tasks_one_execution(store: Store) -> None:
    """Many concurrent asyncio tasks on one key: exactly one runs the effect."""
    n = {"c": 0}
    started = asyncio.Event()

    @once(store, key="race:async")
    async def effect() -> str:
        n["c"] += 1
        started.set()
        await asyncio.sleep(0.02)  # hold the claim so peers observe IN_FLIGHT
        return "ok"

    async def worker() -> str | None:
        try:
            return await effect()
        except QuarantinedError:
            return None

    results = await asyncio.gather(*(worker() for _ in range(16)))
    assert n["c"] == 1
    assert results.count("ok") >= 1
