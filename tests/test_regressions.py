"""Regression tests for the pre-release audit findings.

Each test reproduces a specific bug found in review and asserts the fix holds. They
are written to FAIL on the pre-fix code and pass on the current code.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import threading

import pytest

from exactly_once import JSONCodec, ResultTooLargeError, State, Store, once
from exactly_once.keys import resolve_key

_WORKERS = 8


# --- Finding #1 (POL-1): `release` is owner-aware (a fencing token) --------
#
# The token guarantee: a reconciler that observed one claim cannot delete a *newer*
# claim of the same key. (The release-based policies — check_then_decide /
# auto_retry — remain single-reconciler crash-recovery tools per their docstrings;
# the concurrency-safe reconcilers are quarantine and wait.)


def test_stale_reconciler_cannot_delete_a_fresh_reclaim(store: Store) -> None:
    orphan_token = store.claim("k").token  # a crashed run left this orphan (token T0)

    # The orphan is retired and a live worker re-claims the key (token T1).
    store.release("k", orphan_token)
    fresh = store.claim("k")
    assert fresh.state is State.FRESH
    assert fresh.token != orphan_token

    # A stale reconciler still holding T0 tries to release — must be a no-op.
    store.release("k", orphan_token)
    rec = store.get("k")
    assert rec is not None and rec.state is State.IN_FLIGHT  # T1 survived
    assert rec.token == fresh.token


# --- Finding #2 (CM-2): one `once` instance used as a CM concurrently ------


def test_cm_instance_reused_across_threads_commits_correct_keys() -> None:
    """A single `once(...)` object with a per-call key, entered concurrently from
    many threads, must commit each thread's OWN result — not a racing thread's."""
    store = Store.memory()
    guard = once(store, key=lambda: f"job:{threading.current_thread().name}")
    barrier = threading.Barrier(_WORKERS)

    def work(i: int) -> None:
        barrier.wait()  # force all blocks to overlap
        with guard as g:
            assert g.fresh
            g.result = f"r{i}"

    threads = [threading.Thread(target=work, args=(i,), name=f"T{i}") for i in range(_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(_WORKERS):
        rec = store.get(f"job:T{i}")
        assert rec is not None and rec.state is State.COMMITTED, f"T{i} did not commit"
        assert json.loads(rec.result) == f"r{i}", f"T{i} committed the wrong result"


async def test_cm_instance_reused_across_tasks_commits_correct_keys() -> None:
    store = Store.memory()
    task_id: contextvars.ContextVar[str] = contextvars.ContextVar("task_id")
    guard = once(store, key=lambda: f"job:{task_id.get()}")

    async def work(i: int) -> None:
        task_id.set(f"A{i}")
        async with guard as g:
            assert g.fresh
            await asyncio.sleep(0)  # force interleaving across the await
            g.result = f"r{i}"

    await asyncio.gather(*(work(i) for i in range(_WORKERS)))

    for i in range(_WORKERS):
        rec = await store.aget(f"job:A{i}")
        assert rec is not None and rec.state is State.COMMITTED
        assert json.loads(rec.result) == f"r{i}"


# --- Finding #3 (SQL-3): SQLite claim churn never deadlocks ----------------


def test_sqlite_claim_release_churn_no_deadlock(tmp_path: object) -> None:
    """Many threads hammer claim+release on one key (the INSERT-conflict / vanished-
    record path). Must complete (no lock re-entry deadlock) and stay consistent.
    If this regresses to the recursive-claim-inside-lock bug, it hangs."""
    store = Store.sqlite(os.path.join(str(tmp_path), "churn.db"))
    stop = threading.Event()
    errors: list[Exception] = []

    def churn() -> None:
        try:
            while not stop.is_set():
                r = store.claim("hot")
                if r.state is State.FRESH:
                    store.release("hot", r.token)
        except Exception as exc:  # capture to fail the test, not hang the run
            errors.append(exc)

    threads = [threading.Thread(target=churn) for _ in range(6)]
    for t in threads:
        t.start()
    threading.Event().wait(0.5)  # let them contend
    stop.set()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "claim churn deadlocked (recursive claim under lock?)"
    assert not errors, f"churn raised: {errors[:3]}"
    store.close()


# --- Security: result size ceiling (REQ-S6) --------------------------------


def test_result_size_ceiling_enforced() -> None:
    small = JSONCodec(max_bytes=32)
    small.encode("ok")  # under the ceiling
    with pytest.raises(ResultTooLargeError):
        small.encode("x" * 1000)


def test_result_size_ceiling_can_be_disabled() -> None:
    assert JSONCodec(max_bytes=None).encode("x" * 10_000)  # no ceiling


def test_default_codec_has_a_ceiling() -> None:
    with pytest.raises(ResultTooLargeError):
        JSONCodec().encode("x" * 2_000_000)  # over the 1 MiB default


# --- Minor: distinct lambdas derive distinct keys --------------------------


def test_distinct_lambdas_derive_distinct_keys() -> None:
    f1 = lambda: "a"  # noqa: E731
    f2 = lambda: "b"  # noqa: E731 - different source line -> different derived key
    k1 = resolve_key(None, func=f1, args=(), kwargs={})
    k2 = resolve_key(None, func=f2, args=(), kwargs={})
    assert k1 != k2, "two distinct lambdas collided onto one derived key"
