"""exactly-once — idempotency middleware for agent side-effects.

A guarded effect fires **at most once per key**, and replays its stored result on
every subsequent call — across retries, concurrent workers, crashes, and replays.

    from exactly_once import once, Store

    store = Store.sqlite("effects.db")

    @once(store, key=lambda order, **_: f"charge:{order.id}")
    def charge_card(order):
        return stripe.charge(order.customer, order.amount)   # at most once, ever

This is exactly-once *effect* (at-most-once execution + replay-on-success), not
exactly-once *delivery* (which is impossible). See the "Guarantees & Limits"
section of the README before trusting anything here.
"""

from __future__ import annotations

from ._types import ClaimRecord, ClaimResult, State
from .codec import Codec, JSONCodec
from .core import current_key, once
from .errors import (
    ExactlyOnceError,
    KeyReuseError,
    QuarantinedError,
    ResultTooLargeError,
    StoreUnavailableError,
    UnstableKeyError,
)
from .policies import (
    Policy,
    ProbeResult,
    Verdict,
    auto_retry,
    check_then_decide,
    fail,
    quarantine,
    wait,
)
from .stores.base import Store

__version__ = "0.1.0"

__all__ = [
    # core API
    "once",
    "Store",
    "current_key",
    # types
    "State",
    "ClaimResult",
    "ClaimRecord",
    "Codec",
    "JSONCodec",
    # policies
    "Policy",
    "quarantine",
    "fail",
    "auto_retry",
    "check_then_decide",
    "wait",
    "Verdict",
    "ProbeResult",
    # errors
    "ExactlyOnceError",
    "UnstableKeyError",
    "KeyReuseError",
    "QuarantinedError",
    "ResultTooLargeError",
    "StoreUnavailableError",
    "__version__",
]
