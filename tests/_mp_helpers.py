"""Top-level worker functions for the multi-process race and crash tests.

These live in their own module (not a test file) so ``multiprocessing`` with the
``spawn`` start method — the default on macOS — can import and pickle them.
"""

from __future__ import annotations

import os

from exactly_once import QuarantinedError, Store


def _append(counter_path: str) -> None:
    with open(counter_path, "ab") as fh:
        fh.write(b"x\n")
        fh.flush()
        os.fsync(fh.fileno())


def sqlite_race_worker(db_path: str, counter_path: str, key: str) -> None:
    """Claim ``key`` and run the effect once. Losers quarantine and do nothing."""
    store = Store.sqlite(db_path)
    from exactly_once import once

    @once(store, key=key)
    def effect() -> str:
        _append(counter_path)
        return "ok"

    try:
        effect()
    except QuarantinedError:
        pass
    finally:
        store.close()


def sqlite_crash_worker(db_path: str, counter_path: str, key: str) -> None:
    """Run the effect, then die HARD (``os._exit``) in the crash window — after the
    effect has happened but before ``commit`` lands. Mimics a SIGKILL."""
    store = Store.sqlite(db_path)
    from exactly_once import once

    @once(store, key=key)
    def effect() -> str:
        _append(counter_path)
        os._exit(137)  # killed before commit() is reached — key left IN_FLIGHT

    effect()
    store.close()  # unreachable
