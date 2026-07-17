"""Crash-injection tests — TEST-PLAN §4. The case that defines the brand.

The killer window is CRASH-3: the process dies *after* the effect happened but
*before* commit landed. The store then shows IN_FLIGHT and the library cannot know
whether the effect occurred. The core safety property: it must NOT re-run by
default (quarantine), and a prober that observes the world can recover it.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from _mp_helpers import sqlite_crash_worker
from _spy import SpyEffect
from exactly_once import (
    ProbeResult,
    QuarantinedError,
    State,
    Store,
    Verdict,
    check_then_decide,
    once,
)


def _lines(path: Path) -> int:
    return len([b for b in path.read_bytes().split(b"\n") if b])


# --- CRASH-3 in-process: crash between effect and commit -------------------


def test_crash_after_effect_before_commit_default_quarantines(store: Store) -> None:
    spy = SpyEffect()
    key = "charge:o1"

    # Simulate the crash window: the effect runs, then commit "dies".
    original_commit = store.commit

    def dying_commit(k: str, r: bytes) -> None:
        raise RuntimeError("process died between effect and commit")

    store.commit = dying_commit  # type: ignore[method-assign]

    @once(store, key=key)
    def charge() -> str:
        spy.run()
        return "charged"

    with pytest.raises(RuntimeError):
        charge()
    assert spy.count == 1
    assert store.get(key).state is State.IN_FLIGHT  # left orphaned, not committed

    # Resume with the default policy: refuse to re-run (CRASH-3 core safety).
    store.commit = original_commit  # type: ignore[method-assign]

    @once(store, key=key)
    def charge_resume() -> str:
        spy.run()
        return "charged-again"

    with pytest.raises(QuarantinedError):
        charge_resume()
    assert spy.count == 1  # NO auto-refire


def test_crash_then_check_then_decide_recovers(store: Store) -> None:
    spy = SpyEffect()
    key = "charge:o2"
    store.claim(key)  # orphaned IN_FLIGHT from a "crashed" run; effect already happened

    # A prober observes the world (e.g. queries Stripe by idempotency key) and finds
    # the effect committed — so we back-fill and replay, never re-run.
    def prober(k: str) -> ProbeResult:
        return ProbeResult(Verdict.COMMITTED, {"charge_id": "ch_recovered"})

    @once(store, key=key, policy=check_then_decide(prober))
    def charge() -> dict:
        spy.run()
        return {"charge_id": "ch_new"}

    assert charge() == {"charge_id": "ch_recovered"}  # replayed the observed truth
    assert spy.count == 0  # effect never re-ran
    assert store.get(key).state is State.COMMITTED


def test_check_then_decide_not_committed_reruns(store: Store) -> None:
    spy = SpyEffect()
    key = "charge:o3"
    store.claim(key)  # orphaned, but the prober will prove the effect never happened

    def prober(k: str) -> ProbeResult:
        return ProbeResult(Verdict.NOT_COMMITTED)

    @once(store, key=key, policy=check_then_decide(prober))
    def charge() -> str:
        spy.run()
        return "charged"

    assert charge() == "charged"  # provably safe to run -> ran exactly once
    assert spy.count == 1


# --- the anti-Powertools regression (ADR-004) -----------------------------


def test_exception_leaves_in_flight_and_does_not_release(store: Store) -> None:
    """An effect that RAISES must leave the key IN_FLIGHT — never delete/release it
    (the AWS-Powertools delete-on-exception behavior this library rejects)."""
    spy = SpyEffect()

    @once(store, key="risky")
    def risky() -> str:
        spy.run()
        raise ValueError("boom")

    with pytest.raises(ValueError):
        risky()
    rec = store.get("risky")
    assert rec is not None and rec.state is State.IN_FLIGHT  # NOT released to FRESH

    with pytest.raises(QuarantinedError):
        risky()  # resume is quarantined, not re-run
    assert spy.count == 1


# --- the strongest: real SIGKILL in the crash window (SQLite) --------------


def test_sigkill_in_crash_window_never_double_charges(tmp_path: Path) -> None:
    db = str(tmp_path / "crash.db")
    counter = tmp_path / "counter"
    counter.write_bytes(b"")
    Store.sqlite(db).close()
    key = "charge:order-9"

    ctx = mp.get_context("spawn")
    p = ctx.Process(target=sqlite_crash_worker, args=(db, str(counter), key))
    p.start()
    p.join(timeout=30)
    assert p.exitcode == 137  # died hard, inside the crash window
    assert _lines(counter) == 1  # the effect happened exactly once before the kill

    store = Store.sqlite(db)
    assert store.get(key).state is State.IN_FLIGHT  # commit never landed

    # Default resume: quarantine — no second charge.
    @once(store, key=key)
    def resume() -> str:
        counter.write_bytes(counter.read_bytes() + b"x\n")  # would be a 2nd execution
        return "x"

    with pytest.raises(QuarantinedError):
        resume()
    assert _lines(counter) == 1  # STILL one — the whole point

    # A prober that observes the completed charge recovers it without re-running.
    def prober(k: str) -> ProbeResult:
        return ProbeResult(Verdict.COMMITTED, {"charge_id": "ch_recovered"})

    @once(store, key=key, policy=check_then_decide(prober))
    def resume_probe() -> dict:
        counter.write_bytes(counter.read_bytes() + b"x\n")
        return {"charge_id": "ch_new"}

    assert resume_probe() == {"charge_id": "ch_recovered"}
    assert _lines(counter) == 1  # never re-charged
    store.close()
