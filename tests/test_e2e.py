"""End-to-end scenarios — TEST-PLAN §2. The flagship + the control + replay safety.

E2E-1 and E2E-2 are the side-by-side demo: the SAME crash, WITH vs WITHOUT
exactly-once. With it: one charge. Without it: two. That contrast is the whole pitch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exactly_once import (
    ProbeResult,
    State,
    Store,
    Verdict,
    check_then_decide,
    current_key,
    once,
)


class FakeStripe:
    """A minimal Stripe stand-in with real idempotency-key semantics: a repeat with
    the same key replays the cached charge instead of creating a new one."""

    def __init__(self) -> None:
        self.charges: list[dict] = []
        self._by_key: dict[str, dict] = {}

    def charge(self, customer: str, amount: int, idempotency_key: str | None = None) -> dict:
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]  # replay — no new charge
        ch = {"id": f"ch_{len(self.charges) + 1}", "customer": customer, "amount": amount}
        self.charges.append(ch)
        if idempotency_key is not None:
            self._by_key[idempotency_key] = ch
        return ch

    def get_by_key(self, idempotency_key: str) -> dict | None:
        return self._by_key.get(idempotency_key)


def _dies(_k: str, _r: bytes) -> None:
    raise RuntimeError("process killed after charge returned 200, before commit landed")


# --- E2E-1: the flagship -- crash mid-payment, resume, do NOT double-charge -


def test_e2e1_crash_mid_payment_does_not_double_charge(tmp_path: Path) -> None:
    stripe = FakeStripe()
    store = Store.sqlite(str(tmp_path / "effects.db"))
    order = {"id": "order-1", "customer": "cus_1", "amount": 4999}

    def prober(key: str) -> ProbeResult:
        charge = stripe.get_by_key(key)  # observe the world: did Stripe record it?
        if charge is not None:
            return ProbeResult(Verdict.COMMITTED, charge)
        return ProbeResult(Verdict.NOT_COMMITTED)

    def make_charger():
        @once(store, key=lambda o: f"charge:{o['id']}", policy=check_then_decide(prober))
        def charge_card(o: dict) -> dict:
            # provider-key passthrough (ADR-005): exactly-once's key IS Stripe's key
            return stripe.charge(o["customer"], o["amount"], idempotency_key=current_key())

        return charge_card

    # WHEN the process is killed after stripe.charge returns but before commit lands
    original_commit = store.commit
    store.commit = _dies  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        make_charger()(order)

    # THEN Stripe shows exactly ONE charge, and the key is quarantined IN_FLIGHT
    assert len(stripe.charges) == 1
    assert store.get("charge:order-1").state is State.IN_FLIGHT

    # AND after resume, the prober finds the charge and replays it — no second charge
    store.commit = original_commit  # type: ignore[method-assign]
    result = make_charger()(order)
    assert len(stripe.charges) == 1  # STILL one charge
    assert result["id"] == "ch_1"
    assert store.get("charge:order-1").state is State.COMMITTED
    store.close()


# --- E2E-2: the control -- same crash WITHOUT exactly-once -> double-charge -


def test_e2e2_control_without_exactly_once_double_charges() -> None:
    stripe = FakeStripe()
    order = {"id": "order-1", "customer": "cus_1", "amount": 4999}

    def naive_charge() -> dict:
        # a naive agent: no guard, no idempotency key
        return stripe.charge(order["customer"], order["amount"])

    naive_charge()  # first attempt succeeds
    naive_charge()  # crash-then-resume = a blind retry

    assert len(stripe.charges) == 2  # ← the bug exactly-once exists to prevent


# --- E2E-3: replay safety (debug re-run) -----------------------------------


def test_e2e3_replay_safety_on_rerun(tmp_path: Path) -> None:
    path = str(tmp_path / "effects.db")
    stripe = FakeStripe()
    order = {"id": "order-7", "customer": "cus_7", "amount": 100}

    def run_script(db: Store) -> dict:
        @once(db, key=f"charge:{order['id']}")
        def charge_card() -> dict:
            return stripe.charge(order["customer"], order["amount"], idempotency_key=current_key())

        return charge_card()

    store1 = Store.sqlite(path)
    r1 = run_script(store1)
    store1.close()

    # Re-execute the entire script from scratch against the durable store (replay).
    store2 = Store.sqlite(path)
    r2 = run_script(store2)
    store2.close()

    assert r1 == r2
    assert len(stripe.charges) == 1  # zero additional charges on replay
