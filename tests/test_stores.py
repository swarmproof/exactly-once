"""Store-contract conformance — the same assertions every adapter must satisfy.

Runs against memory + SQLite (the ``store`` fixture). Redis/Postgres run the same
shape in their testcontainer-gated modules.
"""

from __future__ import annotations

from exactly_once import State, Store


def test_first_claim_is_fresh(store: Store) -> None:
    r = store.claim("k1")
    assert r.state is State.FRESH
    assert r.key == "k1"


def test_second_claim_is_in_flight(store: Store) -> None:
    store.claim("k1")
    r = store.claim("k1")
    assert r.state is State.IN_FLIGHT


def test_commit_then_claim_replays_result(store: Store) -> None:
    store.claim("k1")
    store.commit("k1", b"result-bytes")
    r = store.claim("k1")
    assert r.state is State.COMMITTED
    assert r.result == b"result-bytes"


def test_commit_is_idempotent(store: Store) -> None:
    store.claim("k1")
    store.commit("k1", b"first")
    store.commit("k1", b"second")  # idempotent no-op once committed
    assert store.claim("k1").result == b"first"


def test_release_returns_key_to_fresh(store: Store) -> None:
    store.claim("k1")
    store.release("k1")
    assert store.claim("k1").state is State.FRESH  # claimable again


def test_release_never_undoes_a_commit(store: Store) -> None:
    store.claim("k1")
    store.commit("k1", b"done")
    store.release("k1")  # must be ignored — release is IN_FLIGHT-only
    assert store.claim("k1").state is State.COMMITTED


def test_fingerprint_is_stored_and_returned(store: Store) -> None:
    store.claim("k1", fingerprint="fp-abc")
    assert store.claim("k1").fingerprint == "fp-abc"


def test_get_returns_record_with_timestamps(store: Store) -> None:
    store.claim("k1")
    rec = store.get("k1")
    assert rec is not None
    assert rec.state is State.IN_FLIGHT
    assert rec.created_at is not None and rec.updated_at is not None


def test_get_missing_is_none(store: Store) -> None:
    assert store.get("nope") is None


def test_ledger_list_filters_by_state(store: Store) -> None:
    store.claim("a")
    store.claim("b")
    store.commit("b", b"x")
    store.claim("c")

    in_flight = {r.key for r in store.list(State.IN_FLIGHT)}
    committed = {r.key for r in store.list(State.COMMITTED)}
    assert in_flight == {"a", "c"}
    assert committed == {"b"}
    assert {r.key for r in store.list()} == {"a", "b", "c"}  # unfiltered = all
