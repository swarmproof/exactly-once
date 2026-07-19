"""The exception taxonomy.

Each public exception is raised by exactly one documented condition (see
TEST-PLAN "error taxonomy"). They all derive from :class:`ExactlyOnceError` so a
caller can ``except ExactlyOnceError`` to catch anything this library raises.
"""

from __future__ import annotations


class ExactlyOnceError(Exception):
    """Base class for every error raised by exactly-once."""


class UnstableKeyError(ExactlyOnceError):
    """A derived key could not be computed from stable inputs (REQ-K3).

    Raised instead of silently minting a per-call key, because silent
    mis-keying is the worst failure mode: it looks like it works and dedupes
    nothing. Supply an explicit ``key=`` when arguments are unhashable or
    non-deterministic (open sockets, live objects, ``datetime.now()``).
    """


class KeyReuseError(ExactlyOnceError):
    """The same key was presented with a different payload fingerprint (REQ-K4).

    Mirrors Stripe's parameter-mismatch check and AWS Powertools' payload
    validation: replaying a *different* request under a committed/in-flight key
    would return a result computed for different inputs, so we refuse.
    """


class QuarantinedError(ExactlyOnceError):
    """A key is IN_FLIGHT and the active policy refused to run the effect.

    This is the default (safe) outcome for both the concurrent-duplicate case
    (another caller holds the claim) and the crash-mid-effect case (an orphaned
    IN_FLIGHT observed on resume). Re-running an effect of unknown outcome is
    strictly worse than pausing it — resolve via the ledger (ADR-003).
    """

    def __init__(self, key: str, message: str | None = None) -> None:
        self.key = key
        super().__init__(
            message
            or (
                f"key {key!r} is IN_FLIGHT and was quarantined (not re-run). "
                "Resolve it via the ledger: another worker may hold it, or a "
                "prior run may have crashed mid-effect."
            )
        )


class ResultTooLargeError(ExactlyOnceError):
    """A committed result exceeded the codec's size ceiling (REQ-S6).

    Idempotency ledgers are for small results (an id, a status) — the documented
    pattern is to *store a reference, not the payload*. The ceiling guards the store
    against unbounded growth from an accidental large result; raise it deliberately
    via ``JSONCodec(max_bytes=...)`` if you really mean to store something big.
    """


class StoreUnavailableError(ExactlyOnceError):
    """The store could not be reached (ADR-006, REQ-S7).

    By default exactly-once fails *closed*: if it cannot claim, it does not run
    the effect. Pass ``on_store_down="open"`` to opt into running the effect
    unguarded when the store is down (availability over safety).
    """
