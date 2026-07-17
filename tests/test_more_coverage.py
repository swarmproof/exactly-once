"""Remaining branch coverage — context-manager resolution paths and the full async
policy matrix. Correctness is the product, so these branches are exercised too."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from exactly_once import (
    ProbeResult,
    QuarantinedError,
    State,
    Store,
    StoreUnavailableError,
    Verdict,
    auto_retry,
    check_then_decide,
    fail,
    once,
    wait,
)
from exactly_once._types import ClaimRecord, ClaimResult
from exactly_once.codec import DEFAULT_CODEC
from exactly_once.policies import Action, Directive, Policy


class PeerCommits(Policy):
    """Deterministically simulate a peer committing during the RUN release window,
    so the core's re-claim observes COMMITTED and replays instead of running."""

    def resolve(self, key: str, store: Store, claim: ClaimResult, codec: object) -> Directive:
        store.release(key)
        store.claim(key)
        store.commit(key, DEFAULT_CODEC.encode("peer"))
        return Directive(Action.RUN)

    async def aresolve(
        self, key: str, store: Store, claim: ClaimResult, codec: object
    ) -> Directive:
        await store.arelease(key)
        await store.aclaim(key)
        await store.acommit(key, DEFAULT_CODEC.encode("peer"))
        return Directive(Action.RUN)


class RunButStuck(Policy):
    """RUN without releasing — the re-claim still sees IN_FLIGHT, so the core must
    fall back to quarantine (never double-run)."""

    def resolve(self, key: str, store: Store, claim: ClaimResult, codec: object) -> Directive:
        return Directive(Action.RUN)

    async def aresolve(
        self, key: str, store: Store, claim: ClaimResult, codec: object
    ) -> Directive:
        return Directive(Action.RUN)


class DownStore(Store):
    def claim(self, key: str, *, fingerprint: str | None = None) -> ClaimResult:
        raise StoreUnavailableError("down")

    def commit(self, key: str, result: bytes) -> None:
        raise StoreUnavailableError("down")

    def release(self, key: str) -> None:
        raise StoreUnavailableError("down")

    def get(self, key: str) -> ClaimRecord | None:
        return None

    def list(self, state: State | None = None) -> Iterator[ClaimRecord]:
        return iter(())


# --- sync context-manager resolution paths ---------------------------------


def test_cm_committed_replay(store: Store) -> None:
    with once(store, key="k") as g:
        g.result = "v1"
    with once(store, key="k") as g:
        assert g.fresh is False
        assert g.result == "v1"


def test_cm_block_exception_leaves_in_flight(store: Store) -> None:
    with pytest.raises(RuntimeError), once(store, key="k") as g:
        assert g.fresh
        raise RuntimeError("boom")
    assert store.get("k").state is State.IN_FLIGHT


def test_cm_check_then_decide_replay(store: Store) -> None:
    store.claim("k")  # orphaned

    def prober(key: str) -> ProbeResult:
        return ProbeResult(Verdict.COMMITTED, "recovered")

    with once(store, key="k", policy=check_then_decide(prober)) as g:
        assert g.fresh is False
        assert g.result == "recovered"


def test_cm_check_then_decide_not_committed_runs(store: Store) -> None:
    store.claim("k")

    def prober(key: str) -> ProbeResult:
        return ProbeResult(Verdict.NOT_COMMITTED)

    n = {"c": 0}
    with once(store, key="k", policy=check_then_decide(prober)) as g:
        assert g.fresh  # released + fresh reclaim
        n["c"] += 1
    assert n["c"] == 1


def test_check_then_decide_unknown_quarantines(store: Store) -> None:
    store.claim("k")

    def prober(key: str) -> ProbeResult:
        return ProbeResult(Verdict.UNKNOWN)

    @once(store, key="k", policy=check_then_decide(prober))
    def effect() -> str:
        return "x"

    with pytest.raises(QuarantinedError):
        effect()


def test_decorator_committed_after_reclaim(store: Store) -> None:
    """auto_retry releases; if a peer commits before our reclaim, we replay it."""
    store.claim("k")
    # Pre-commit a result so the reclaim inside auto_retry observes COMMITTED.
    store.commit("k", __import__("json").dumps("peer-committed").encode())

    @once(store, key="k", policy=auto_retry)
    def effect() -> str:
        return "fresh-run"

    assert effect() == "peer-committed"  # committed path wins, effect not run


# --- async policy matrix ----------------------------------------------------


async def test_async_fail_policy(store: Store) -> None:
    await store.aclaim("stuck")

    @once(store, key="stuck", policy=fail)
    async def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        await effect()


async def test_async_auto_retry_decorator(store: Store) -> None:
    n = {"c": 0}
    await store.aclaim("email")

    @once(store, key="email", policy=auto_retry)
    async def send() -> str:
        n["c"] += 1
        return "sent"

    assert await send() == "sent"
    assert n["c"] == 1


async def test_async_auto_retry_context_manager(store: Store) -> None:
    n = {"c": 0}
    await store.aclaim("email2")
    async with once(store, key="email2", policy=auto_retry) as g:
        assert g.fresh
        n["c"] += 1
    assert n["c"] == 1


async def test_async_check_then_decide_not_committed(store: Store) -> None:
    n = {"c": 0}
    await store.aclaim("k")

    async def prober(key: str) -> ProbeResult:
        return ProbeResult(Verdict.NOT_COMMITTED)

    @once(store, key="k", policy=check_then_decide(prober))
    async def effect() -> str:
        n["c"] += 1
        return "ran"

    assert await effect() == "ran"
    assert n["c"] == 1


async def test_async_wait_times_out(store: Store) -> None:
    await store.aclaim("stuck")

    @once(store, key="stuck", policy=wait(timeout=0.15, interval=0.02))
    async def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        await effect()


async def test_async_cm_committed_replay(store: Store) -> None:
    async with once(store, key="k") as g:
        g.result = "v1"
    async with once(store, key="k") as g:
        assert g.fresh is False
        assert g.result == "v1"


async def test_async_cm_block_exception_leaves_in_flight(store: Store) -> None:
    with pytest.raises(RuntimeError):
        async with once(store, key="k") as g:
            assert g.fresh
            raise RuntimeError("boom")
    rec = await store.aget("k")
    assert rec is not None and rec.state is State.IN_FLIGHT


async def test_async_cm_check_then_decide_replay(store: Store) -> None:
    await store.aclaim("k")

    async def prober(key: str) -> ProbeResult:
        await asyncio.sleep(0)
        return ProbeResult(Verdict.COMMITTED, "recovered")

    async with once(store, key="k", policy=check_then_decide(prober)) as g:
        assert g.fresh is False
        assert g.result == "recovered"


async def test_async_fail_open_decorator() -> None:
    n = {"c": 0}

    @once(DownStore(), key="k", on_store_down="open")
    async def effect() -> str:
        n["c"] += 1
        return "ran"

    assert await effect() == "ran"
    assert n["c"] == 1


async def test_async_fail_open_context_manager() -> None:
    n = {"c": 0}
    async with once(DownStore(), key="k", on_store_down="open") as g:
        assert g.fresh
        n["c"] += 1
    assert n["c"] == 1


# --- RUN -> re-claim resolution branches (peer commits / still stuck) -------


def test_decorator_run_then_peer_committed(store: Store) -> None:
    store.claim("k")

    @once(store, key="k", policy=PeerCommits())
    def effect() -> str:
        return "fresh"

    assert effect() == "peer"  # re-claim saw COMMITTED -> replayed


async def test_async_decorator_run_then_peer_committed(store: Store) -> None:
    await store.aclaim("k")

    @once(store, key="k", policy=PeerCommits())
    async def effect() -> str:
        return "fresh"

    assert await effect() == "peer"


def test_cm_run_then_peer_committed(store: Store) -> None:
    store.claim("k")
    with once(store, key="k", policy=PeerCommits()) as g:
        assert g.fresh is False
        assert g.result == "peer"


async def test_async_cm_run_then_peer_committed(store: Store) -> None:
    await store.aclaim("k")
    async with once(store, key="k", policy=PeerCommits()) as g:
        assert g.fresh is False
        assert g.result == "peer"


def test_cm_run_but_stuck_quarantines(store: Store) -> None:
    store.claim("k")
    with pytest.raises(QuarantinedError), once(store, key="k", policy=RunButStuck()):
        pass


async def test_async_cm_run_but_stuck_quarantines(store: Store) -> None:
    await store.aclaim("k")
    with pytest.raises(QuarantinedError):
        async with once(store, key="k", policy=RunButStuck()):
            pass


async def test_async_cm_sentinel_quarantine(store: Store) -> None:
    from exactly_once import quarantine

    await store.aclaim("k")
    async with once(store, key="k", policy=quarantine(sentinel="SK")) as g:
        assert g.fresh is False
        assert g.result == "SK"


async def test_async_wait_race_every_caller_gets_result(store: Store) -> None:
    """Concurrent asyncio tasks with the `wait` policy: losers block on the ledger
    until the winner commits, then replay — exercises Wait.aresolve's REPLAY path."""
    n = {"c": 0}

    @once(store, key="race:aw", policy=wait(timeout=10.0, interval=0.01))
    async def effect() -> str:
        n["c"] += 1
        await asyncio.sleep(0.03)  # hold the claim so peers observe IN_FLIGHT
        return "ok"

    results = await asyncio.gather(*(effect() for _ in range(12)))
    assert n["c"] == 1
    assert all(r == "ok" for r in results)
