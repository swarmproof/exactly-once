"""Unit tests — TEST-PLAN §6. Key derivation, codec, policies, fingerprint, errors."""

from __future__ import annotations

import pytest

from exactly_once import (
    JSONCodec,
    KeyReuseError,
    QuarantinedError,
    Store,
    UnstableKeyError,
    once,
)
from exactly_once.keys import compute_fingerprint, resolve_key

# --- key derivation / normalization (REQ-K1..K3) ---------------------------


def test_static_key() -> None:
    assert resolve_key("welcome:u1", func=None, args=(), kwargs={}) == "welcome:u1"


def test_callable_key_from_business_identity() -> None:
    key = resolve_key(lambda order, **_: f"charge:{order['id']}", func=None,
                      args=({"id": "o-1", "amount": 50},), kwargs={})
    assert key == "charge:o-1"


def test_callable_key_must_return_str() -> None:
    with pytest.raises(TypeError):
        resolve_key(lambda **_: 123, func=None, args=(), kwargs={})  # type: ignore[arg-type,return-value]


def test_derived_key_is_stable_and_order_insensitive() -> None:
    def f(a: int, b: int) -> int:
        return a + b

    k1 = resolve_key(None, func=f, args=(), kwargs={"a": 1, "b": 2})
    k2 = resolve_key(None, func=f, args=(), kwargs={"b": 2, "a": 1})  # kwargs reordered
    assert k1 == k2  # order-insensitive (REQ-K3)


def test_derived_key_differs_for_different_args() -> None:
    def f(a: int) -> int:
        return a

    assert resolve_key(None, func=f, args=(1,), kwargs={}) != resolve_key(
        None, func=f, args=(2,), kwargs={}
    )


def test_unstable_input_raises_rather_than_mis_keys() -> None:
    def f(x: object) -> object:
        return x

    with pytest.raises(UnstableKeyError):
        resolve_key(None, func=f, args=(object(),), kwargs={})  # not serializable


def test_namespace_prefix() -> None:
    assert resolve_key("k", func=None, args=(), kwargs={}, namespace="ns") == "ns:k"


# --- the value-in-key anti-pattern (REQ-K6) --------------------------------


def test_key_on_business_identity_keeps_distinct_charges_distinct() -> None:
    """Two legitimate same-amount charges for DIFFERENT orders must NOT collapse."""
    store = Store.memory()
    runs: list[str] = []

    @once(store, key=lambda order, **_: f"charge:{order['id']}")  # business identity ✓
    def charge(order: dict) -> str:
        runs.append(order["id"])
        return order["id"]

    charge({"id": "order-1", "amount": 50})
    charge({"id": "order-2", "amount": 50})  # same amount, different order
    assert runs == ["order-1", "order-2"]  # both ran — not collapsed


def test_value_in_key_antipattern_collapses_distinct_charges() -> None:
    """Demonstrates the footgun: keying on the amount collapses two distinct charges."""
    store = Store.memory()
    runs: list[str] = []

    @once(store, key=lambda order, **_: f"charge:{order['amount']}")  # WRONG: mutable value
    def charge(order: dict) -> str:
        runs.append(order["id"])
        return order["id"]

    r1 = charge({"id": "order-1", "amount": 50})
    r2 = charge({"id": "order-2", "amount": 50})  # collapses onto order-1's result
    assert runs == ["order-1"]  # order-2 NEVER ran — the bug
    assert r1 == r2 == "order-1"  # order-2 got order-1's result back


# --- payload fingerprinting (REQ-K4) ---------------------------------------


def test_fingerprint_mismatch_raises_key_reuse() -> None:
    store = Store.memory()

    @once(store, key="charge:o1", fingerprint=lambda amount: {"amount": amount})
    def charge(amount: int) -> int:
        return amount

    assert charge(50) == 50
    with pytest.raises(KeyReuseError):
        charge(999)  # same key, different fingerprinted payload


def test_same_key_same_fingerprint_replays() -> None:
    store = Store.memory()
    n = {"c": 0}

    @once(store, key="charge:o1", fingerprint=lambda amount: {"amount": amount})
    def charge(amount: int) -> int:
        n["c"] += 1
        return amount

    assert charge(50) == 50
    assert charge(50) == 50  # identical fingerprint -> replay
    assert n["c"] == 1


def test_compute_fingerprint_none_when_unset() -> None:
    assert compute_fingerprint(None, args=(), kwargs={}) is None


# --- codec (REQ-S6) --------------------------------------------------------


def test_json_codec_roundtrip() -> None:
    c = JSONCodec()
    assert c.decode(c.encode({"a": 1, "b": [1, 2]})) == {"a": 1, "b": [1, 2]}


def test_json_codec_rejects_nonserializable_clearly() -> None:
    with pytest.raises(TypeError, match="not JSON-serializable"):
        JSONCodec().encode(object())


# --- error taxonomy --------------------------------------------------------


def test_context_manager_quarantines_orphaned_key() -> None:
    store = Store.memory()
    store.claim("stuck")  # orphan it (a prior run left it IN_FLIGHT)
    with pytest.raises(QuarantinedError), once(store, key="stuck"):
        pytest.fail("should not enter the block for an orphaned key")


def test_quarantined_error_key_attribute() -> None:
    store = Store.memory()
    store.claim("stuck")

    @once(store, key="stuck")
    def effect() -> str:
        return "ran"

    with pytest.raises(QuarantinedError) as ei:
        effect()
    assert ei.value.key == "stuck"
