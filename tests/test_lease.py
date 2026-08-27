"""Lease / heartbeat reconciliation — issue #11, closing ARCH §9 L-8.

Proves the property the audit left open: with a lease, the release-based policies
(``auto_retry`` / ``check_then_decide``) become concurrency-safe. A key whose lease
is still valid belongs to a live owner and is never adopted; only an expired-lease
orphan (a dead worker) is adopted, and then by exactly one reconciler.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from exactly_once import (
    ProbeResult,
    QuarantinedError,
    State,
    Store,
    Verdict,
    auto_retry,
    check_then_decide,
    once,
)

_R = b'"x"'  # a valid JSON-encoded result for direct store.commit calls


# --- store-contract: lease + heartbeat -------------------------------------


def test_claim_stamps_a_lease_when_ttl_given(store: Store) -> None:
    r = store.claim("k", lease_ttl=30.0)
    assert r.lease_expires_at is not None
    assert r.lease_expires_at > store.now()


def test_claim_without_ttl_has_no_lease(store: Store) -> None:
    assert store.claim("k").lease_expires_at is None


def test_heartbeat_renews_the_lease(store: Store) -> None:
    r = store.claim("k", lease_ttl=1.0)
    before = store.get("k").lease_expires_at
    time.sleep(0.02)
    assert store.heartbeat("k", r.token, lease_ttl=30.0) is True
    after = store.get("k").lease_expires_at
    assert after is not None and before is not None and after > before


def test_heartbeat_fails_when_not_owner(store: Store) -> None:
    store.claim("k", lease_ttl=30.0)
    assert store.heartbeat("k", "wrong-token", 30.0) is False


def test_heartbeat_fails_after_commit(store: Store) -> None:
    r = store.claim("k", lease_ttl=30.0)
    store.commit("k", _R)
    assert store.heartbeat("k", r.token, 30.0) is False


# --- the adoption gate -----------------------------------------------------


def test_valid_lease_is_not_adopted(store: Store) -> None:
    store.claim("k", lease_ttl=30.0)  # a live owner holding a valid lease

    @once(store, key="k", policy=auto_retry, lease_ttl=30.0)
    def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        effect()  # a concurrent reconciler must NOT adopt a live owner


def test_check_then_decide_does_not_probe_a_live_owner(store: Store) -> None:
    store.claim("k", lease_ttl=30.0)
    probed = {"c": 0}

    def prober(_k: str) -> ProbeResult:
        probed["c"] += 1
        return ProbeResult(Verdict.NOT_COMMITTED)

    @once(store, key="k", policy=check_then_decide(prober), lease_ttl=30.0)
    def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        effect()
    assert probed["c"] == 0  # never even probed — the lease says the owner is alive


def test_expired_lease_is_adopted_and_runs_once(store: Store) -> None:
    store.claim("k", lease_ttl=0.03)  # a dead worker; its lease will lapse
    time.sleep(0.1)
    n = {"c": 0}

    @once(store, key="k", policy=auto_retry, lease_ttl=30.0)
    def effect() -> str:
        n["c"] += 1
        return "ran"

    assert effect() == "ran"
    assert n["c"] == 1
    assert store.get("k").state is State.COMMITTED


# --- the property the lease exists for: live vs dead ------------------------


def test_heartbeat_protects_a_long_effect_from_adoption(store: Store) -> None:
    """A live worker whose effect outlives the ttl keeps its lease via the heartbeat,
    so concurrent reconcilers never adopt it. WITHOUT the lease this double-fires
    (the POL-1 case); WITH it, exactly one execution."""
    n = {"c": 0}
    lock = threading.Lock()

    @once(store, key="job", policy=auto_retry, lease_ttl=0.2, heartbeat_interval=0.04)
    def slow() -> str:
        with lock:
            n["c"] += 1
        time.sleep(0.6)  # runs well past the 0.2s ttl — only the heartbeat keeps it alive
        return "done"

    def reconciler(_: int) -> str | None:
        time.sleep(0.3)  # after the ttl, but the heartbeat should have renewed it
        recon = once(store, key="job", policy=auto_retry, lease_ttl=0.2)

        @recon
        def r() -> str:
            with lock:
                n["c"] += 1
            return "reran"

        try:
            return r()
        except QuarantinedError:
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        fut = ex.submit(slow)
        list(ex.map(reconciler, range(3)))
        fut.result()

    assert n["c"] == 1, f"effect ran {n['c']} times — a live owner was adopted"


def _expired_orphan_round(store: Store, key: str, workers: int = 8) -> int:
    """One round: `workers` reconcilers race an expired-lease orphan. Returns how many
    actually ran. A helper (not an inline loop body) so closures bind these locals."""
    n = {"c": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    @once(store, key=key, policy=auto_retry, lease_ttl=30.0)
    def effect() -> str:
        with lock:
            n["c"] += 1
        return "ran"

    def worker(_: int) -> None:
        barrier.wait()
        with contextlib.suppress(QuarantinedError):
            effect()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(worker, range(workers)))
    return n["c"]


def test_concurrent_reconcilers_of_an_expired_orphan_adopt_once(store: Store) -> None:
    """Many reconcilers hit a dead worker's expired-lease orphan at once — exactly
    one adopts and runs (single-winner via the ownership token), the rest quarantine."""
    for rnd in range(15):
        key = f"job:{rnd}"
        store.claim(key, lease_ttl=0.02)  # dead owner
        time.sleep(0.05)  # lease has expired
        ran = _expired_orphan_round(store, key)
        assert ran == 1, f"round {rnd}: {ran} reconcilers adopted the orphan"


# --- async parity ----------------------------------------------------------


async def test_async_valid_lease_is_not_adopted(store: Store) -> None:
    await store.aclaim("k", lease_ttl=30.0)

    @once(store, key="k", policy=auto_retry, lease_ttl=30.0)
    async def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        await effect()


async def test_async_expired_lease_is_adopted_once(store: Store) -> None:
    await store.aclaim("k", lease_ttl=0.03)
    await asyncio.sleep(0.1)
    n = {"c": 0}

    @once(store, key="k", policy=auto_retry, lease_ttl=30.0)
    async def effect() -> str:
        n["c"] += 1
        return "ran"

    assert await effect() == "ran"
    assert n["c"] == 1


async def test_async_heartbeat_protects_a_long_effect(store: Store) -> None:
    """The async heartbeat renews a long effect's lease across many intervals, so
    concurrent reconciler tasks never adopt the live owner."""
    n = {"c": 0}

    @once(store, key="ajob", policy=auto_retry, lease_ttl=0.2, heartbeat_interval=0.04)
    async def slow() -> str:
        n["c"] += 1
        await asyncio.sleep(0.6)  # far past the ttl — only the heartbeat keeps it alive
        return "done"

    async def reconciler() -> str | None:
        await asyncio.sleep(0.3)
        recon = once(store, key="ajob", policy=auto_retry, lease_ttl=0.2)

        @recon
        async def r() -> str:
            n["c"] += 1
            return "reran"

        try:
            return await r()
        except QuarantinedError:
            return None

    await asyncio.gather(slow(), reconciler(), reconciler())
    assert n["c"] == 1, f"async effect ran {n['c']} times — a live owner was adopted"
