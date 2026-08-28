#!/usr/bin/env python3
"""Micro-benchmark of per-call overhead (NFR-8). Not a CI gate — run manually.

The library adds one store round-trip per guarded call plus key/codec work. Against
the in-memory store (which isolates the library's own overhead from any I/O) this
prints the marginal cost of the *fresh* path (claim + run + commit) and the *replay*
path (claim + decode). Real deployments are dominated by the store's latency, not this.

    python scripts/benchmark.py
"""

from __future__ import annotations

import time

from exactly_once import Store, once


def bench(n: int = 50_000) -> None:
    store = Store.memory()

    @once(store, key=lambda i: f"bench:{i}")
    def effect(i: int) -> int:
        return i

    t0 = time.perf_counter()
    for i in range(n):
        effect(i)  # fresh: claim -> run -> commit
    fresh = (time.perf_counter() - t0) / n

    t0 = time.perf_counter()
    for i in range(n):
        effect(i)  # replay: claim -> decode stored result
    replay = (time.perf_counter() - t0) / n

    print(f"n = {n:,} per path (in-memory store)")
    print(f"  fresh  (claim + run + commit): {fresh * 1e6:6.2f} µs/call")
    print(f"  replay (claim + decode):       {replay * 1e6:6.2f} µs/call")


if __name__ == "__main__":
    bench()
