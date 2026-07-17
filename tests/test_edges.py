"""Edge cases & policy coverage — the branches the happy path doesn't reach.

Store-unavailable behavior (ADR-006), the non-default policies through the real
``once`` control flow, sentinel quarantine, context-manager in-flight resolution,
and the KeyReuseError paths.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from exactly_once import (
    KeyReuseError,
    QuarantinedError,
    State,
    Store,
    StoreUnavailableError,
    auto_retry,
    current_key,
    fail,
    once,
    quarantine,
    wait,
)
from exactly_once._types import ClaimRecord, ClaimResult
from exactly_once.policies import Action, Directive, Policy


class DownStore(Store):
    """A store that is always unreachable — exercises the fail-closed/open paths."""

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


# --- store-unavailable (ADR-006, REQ-S7) -----------------------------------


def test_fail_closed_default_does_not_run_effect() -> None:
    ran = {"c": 0}

    @once(DownStore(), key="k")
    def effect() -> str:
        ran["c"] += 1
        return "ran"

    with pytest.raises(StoreUnavailableError):
        effect()
    assert ran["c"] == 0  # effect NOT run when the store is down (safety)


def test_fail_open_runs_effect_unguarded() -> None:
    ran = {"c": 0}

    @once(DownStore(), key="k", on_store_down="open")
    def effect() -> str:
        ran["c"] += 1
        return "ran"

    assert effect() == "ran"
    assert ran["c"] == 1  # ran unguarded (availability over dedupe, opt-in)


def test_fail_open_context_manager_runs_block() -> None:
    ran = {"c": 0}
    with once(DownStore(), key="k", on_store_down="open") as guard:
        assert guard.fresh
        ran["c"] += 1
    assert ran["c"] == 1


async def test_fail_closed_async() -> None:
    @once(DownStore(), key="k")
    async def effect() -> str:
        return "ran"

    with pytest.raises(StoreUnavailableError):
        await effect()


# --- sentinel quarantine (return instead of raise) -------------------------


def test_quarantine_with_sentinel_returns_value(store: Store) -> None:
    store.claim("stuck")

    @once(store, key="stuck", policy=quarantine(sentinel="SKIPPED"))
    def effect() -> str:
        return "ran"

    assert effect() == "SKIPPED"  # branched instead of raising


def test_quarantine_sentinel_in_context_manager(store: Store) -> None:
    store.claim("stuck")
    with once(store, key="stuck", policy=quarantine(sentinel=None)) as guard:
        assert guard.fresh is False
        assert guard.result is None


# --- fail policy -----------------------------------------------------------


def test_fail_policy_raises(store: Store) -> None:
    store.claim("stuck")

    @once(store, key="stuck", policy=fail)
    def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        effect()


# --- auto_retry (opt-in, unsafe-for-irreversible) --------------------------


def test_auto_retry_reruns_orphaned_key(store: Store) -> None:
    n = {"c": 0}
    store.claim("email:u1")  # orphaned

    @once(store, key="email:u1", policy=auto_retry)
    def send() -> str:
        n["c"] += 1
        return "sent"

    assert send() == "sent"  # released + re-ran (acceptable for idempotent email)
    assert n["c"] == 1
    assert store.get("email:u1").state is State.COMMITTED


def test_auto_retry_in_context_manager(store: Store) -> None:
    n = {"c": 0}
    store.claim("email:u2")
    with once(store, key="email:u2", policy=auto_retry) as guard:
        assert guard.fresh  # released and re-claimed fresh
        n["c"] += 1
    assert n["c"] == 1


# --- wait policy: orphaned that never commits -> quarantine ----------------


def test_wait_times_out_to_quarantine(store: Store) -> None:
    store.claim("stuck")  # will never commit

    @once(store, key="stuck", policy=wait(timeout=0.15, interval=0.02))
    def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError):
        effect()


# --- the "someone re-claimed after RUN" safety fallback --------------------


class RunButDontRelease(Policy):
    """A pathological policy that says RUN without releasing — the core must then
    observe the key still IN_FLIGHT on re-claim and quarantine (never double-run)."""

    def resolve(self, key: str, store: object, claim: ClaimResult, codec: object) -> Directive:
        return Directive(Action.RUN)

    async def aresolve(
        self, key: str, store: object, claim: ClaimResult, codec: object
    ) -> Directive:
        return Directive(Action.RUN)


def test_run_directive_without_release_falls_back_to_quarantine(store: Store) -> None:
    n = {"c": 0}
    store.claim("stuck")

    @once(store, key="stuck", policy=RunButDontRelease())
    def effect() -> str:
        n["c"] += 1
        return "ran"

    with pytest.raises(QuarantinedError):
        effect()
    assert n["c"] == 0  # never ran — the fallback protected us


# --- KeyReuseError via context manager -------------------------------------


def test_key_reuse_error_in_context_manager(store: Store) -> None:
    with once(store, key="k", fingerprint=lambda: {"v": 1}) as guard:
        guard.result = "first"
    # A second use with a different fingerprint under the same key is refused.
    with pytest.raises(KeyReuseError), once(store, key="k", fingerprint=lambda: {"v": 2}):
        pass


# --- misc surface ----------------------------------------------------------


def test_current_key_is_none_outside_effect() -> None:
    assert current_key() is None


def test_context_manager_requires_explicit_key(store: Store) -> None:
    with pytest.raises(ValueError, match="explicit key"), once(store):
        pass


def test_namespace_isolates_keys(store: Store) -> None:
    n = {"a": 0, "b": 0}

    @once(store, key="charge:1", namespace="tenant-a")
    def a() -> str:
        n["a"] += 1
        return "a"

    @once(store, key="charge:1", namespace="tenant-b")
    def b() -> str:
        n["b"] += 1
        return "b"

    a()
    b()  # same logical key, different namespace -> both run
    assert n == {"a": 1, "b": 1}


def test_none_result_roundtrips(store: Store) -> None:
    @once(store, key="k")
    def effect() -> None:
        return None

    assert effect() is None
    assert effect() is None  # replay of a committed None
