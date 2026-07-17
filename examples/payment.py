"""Payment example — the decorator form with provider-key passthrough (ADR-005).

The recommended pattern for money movement: pass exactly-once's key through as the
provider's *own* idempotency key. That composes two independent dedupe layers — our
store keeps it at-most-once at the call site, and Stripe keeps "the world changed
once" end-to-end, even if a human later force-refires it.

    python examples/payment.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from exactly_once import Store, current_key, once

# In a real app: import stripe. Here, a tiny fake with the same idempotency behavior.
_charges: dict[str, dict] = {}


def stripe_charge(customer: str, amount: int, idempotency_key: str) -> dict:
    if idempotency_key in _charges:
        return _charges[idempotency_key]  # Stripe replays on a repeated key
    charge = {"id": f"ch_{len(_charges) + 1}", "customer": customer, "amount": amount}
    _charges[idempotency_key] = charge
    return charge


def main() -> None:
    store = Store.sqlite(str(Path(tempfile.mkdtemp()) / "effects.db"))

    # Key on business identity (order id), NEVER on a mutable value like amount.
    @once(store, key=lambda order, **_: f"charge:{order['id']}")
    def charge_card(order: dict) -> dict:
        return stripe_charge(order["customer"], order["amount"], idempotency_key=current_key())

    order = {"id": "order-42", "customer": "cus_9", "amount": 1999}

    first = charge_card(order)
    print(f"first call  → {first['id']} (ran the charge)")

    # A retry, a resume, a debug replay — all return the stored result, no re-charge.
    again = charge_card(order)
    print(f"retry       → {again['id']} (replayed; Stripe was NOT called again)")

    assert first == again
    assert len(_charges) == 1, "exactly one charge ever reached Stripe"
    print(f"\nStripe saw {len(_charges)} charge — exactly once. ✅")
    store.close()


if __name__ == "__main__":
    main()
