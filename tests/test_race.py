"""Concurrency race tests — TEST-PLAN §3. Two-plus workers, one key, one execution.

The single most important integration suite: barrier-synchronized workers pile onto
one key and we assert the effect ran exactly once. Thread variants run against the
``store`` fixture (memory + SQLite); a multi-process variant proves SQLite serializes
real OS processes on the same file (RACE-5), not just threads.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _mp_helpers import sqlite_race_worker
from _spy import SpyEffect
from exactly_once import QuarantinedError, Store, once, wait

_N = 32
_ROUNDS = 40  # bump via a longer sweep for the ≥10k-trial DoD gate


def _race_round(store: Store, key: str, policy: object | None) -> tuple[int, list[str | None]]:
    """Fire _N barrier-synchronized threads at one key. A helper (not an inline loop
    body) so the worker closure binds these locals, not a loop variable (B023)."""
    spy = SpyEffect()
    barrier = threading.Barrier(_N)
    guard = once(store, key=key) if policy is None else once(store, key=key, policy=policy)

    @guard
    def effect() -> str:
        spy.run()
        return "ok"

    def worker(_: int) -> str | None:
        barrier.wait()
        try:
            return effect()
        except QuarantinedError:
            return None

    with ThreadPoolExecutor(max_workers=_N) as ex:
        results = list(ex.map(worker, range(_N)))
    return spy.count, results


def test_thread_race_default_policy_one_execution(store: Store) -> None:
    """N threads race one key. Exactly one runs; losers see IN_FLIGHT/COMMITTED."""
    for rnd in range(_ROUNDS):
        count, results = _race_round(store, f"race:{rnd}", policy=None)
        assert count == 1, f"round {rnd}: effect ran {count} times"
        assert results.count("ok") >= 1  # the winner's result is observable


def test_thread_race_wait_policy_every_caller_gets_the_result(store: Store) -> None:
    """With the `wait` policy, concurrent losers block until the winner commits and
    then replay its result — still exactly one execution (RACE-3 block path)."""
    for rnd in range(_ROUNDS):
        count, results = _race_round(store, f"racewait:{rnd}", policy=wait(timeout=10.0))
        assert count == 1, f"round {rnd}: effect ran {count} times"
        assert all(r == "ok" for r in results)  # everyone got the one result


def test_multiprocess_sqlite_race_one_execution(tmp_path: Path) -> None:
    """RACE-5: real OS processes on one SQLite file — SQLite's write lock serializes
    the claim, so exactly one process runs the effect."""
    db = str(tmp_path / "race.db")
    counter = tmp_path / "counter"
    counter.write_bytes(b"")
    Store.sqlite(db).close()  # pre-create the schema so children don't race on it

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=sqlite_race_worker, args=(db, str(counter), "charge:order-1"))
        for _ in range(16)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    executions = len([b for b in counter.read_bytes().split(b"\n") if b])
    assert executions == 1, f"effect ran {executions} times across 16 processes"
