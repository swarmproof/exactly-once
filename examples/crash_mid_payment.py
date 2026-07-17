"""The flagship demo: an agent crashes mid-payment, resumes, and does NOT
double-charge — shown WITH vs WITHOUT exactly-once, side by side.

Run it:  python examples/crash_mid_payment.py

This is the whole pitch. The left column is a naive agent; the right column is the
same agent wrapped in `@once`. Both are killed in the worst possible window — after
the charge succeeds but before the agent has recorded that it succeeded — and then
resumed. The naive one charges the customer twice. The guarded one charges once.

No real network, no real Stripe — a tiny in-memory fake stands in, with the same
idempotency-key behavior the real API has.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

from exactly_once import (
    ProbeResult,
    Store,
    Verdict,
    check_then_decide,
    current_key,
    once,
)


class FakeStripe:
    """A Stripe stand-in with real idempotency-key semantics."""

    def __init__(self) -> None:
        self.charges: list[dict] = []
        self._by_key: dict[str, dict] = {}

    def charge(self, customer: str, amount: int, idempotency_key: str | None = None) -> dict:
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        ch = {"id": f"ch_{len(self.charges) + 1}", "customer": customer, "amount": amount}
        self.charges.append(ch)
        if idempotency_key is not None:
            self._by_key[idempotency_key] = ch
        return ch


ORDER = {"id": "order-1", "customer": "cus_ABC", "amount": 4999}


class Killed(Exception):
    """Stands in for the process dying in the crash window."""


def without_exactly_once() -> int:
    print("─" * 34, "WITHOUT exactly-once", "─" * 2)
    stripe = FakeStripe()

    def agent_pays() -> None:
        stripe.charge(ORDER["customer"], ORDER["amount"])  # no guard, no idempotency key

    print("  attempt 1: charge $49.99 …", "→", stripe_state(stripe))
    agent_pays()
    print(f"             charged {stripe.charges[-1]['id']}")
    print("  💥 crash — after the charge, before the agent recorded 'done'")
    print("  resume:    the agent retries the whole step …")
    agent_pays()  # blind retry on resume
    print(f"             charged {stripe.charges[-1]['id']}")
    print(f"  RESULT:    Stripe shows {len(stripe.charges)} charges  ❌ customer double-charged\n")
    return len(stripe.charges)


def with_exactly_once() -> int:
    print("─" * 35, "WITH exactly-once", "─" * 3)
    stripe = FakeStripe()
    db = Path(tempfile.mkdtemp()) / "effects.db"
    store = Store.sqlite(str(db))

    def prober(key: str) -> ProbeResult:
        # On resume, observe the world: did Stripe actually record this charge?
        charge = stripe._by_key.get(key)
        if charge is not None:
            return ProbeResult(Verdict.COMMITTED, charge)
        return ProbeResult(Verdict.NOT_COMMITTED)

    def charger():
        @once(store, key=lambda o: f"charge:{o['id']}", policy=check_then_decide(prober))
        def charge_card(o: dict) -> dict:
            # provider-key passthrough (ADR-005): our key IS Stripe's idempotency key
            return stripe.charge(o["customer"], o["amount"], idempotency_key=current_key())

        return charge_card

    # Kill in the crash window: charge returns 200, then commit "dies".
    real_commit = store.commit
    store.commit = _dying_commit  # type: ignore[method-assign]
    print("  attempt 1: charge $49.99  (idempotency_key = charge:order-1) …")
    with contextlib.suppress(Killed):
        charger()(ORDER)
    print(f"             charged {stripe.charges[-1]['id']}")
    print("  💥 crash — after the charge returned 200, before commit landed")
    state = store.get("charge:order-1").state.value.upper()
    print(f"             key is left {state} (quarantined)")

    store.commit = real_commit  # type: ignore[method-assign]
    print("  resume:    prober asks Stripe 'did charge:order-1 happen?' → yes → replay")
    result = charger()(ORDER)
    print(f"             replayed {result['id']} (effect NOT re-run)")
    print(f"  RESULT:    Stripe shows {len(stripe.charges)} charge  ✅ charged exactly once\n")
    store.close()
    return len(stripe.charges)


def _dying_commit(key: str, result: bytes) -> None:
    raise Killed()


def stripe_state(stripe: FakeStripe) -> str:
    return f"Stripe has {len(stripe.charges)} charge(s)"


def main() -> None:
    print("\nagent crashes mid-payment, resumes — does it double-charge?\n")
    naive = without_exactly_once()
    guarded = with_exactly_once()
    print("=" * 60)
    print(f"  naive agent:      {naive} charges   ❌")
    print(f"  with exactly-once: {guarded} charge    ✅")
    print("=" * 60)
    assert naive == 2 and guarded == 1


if __name__ == "__main__":
    main()
