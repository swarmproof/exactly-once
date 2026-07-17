"""The core value types shared by the library and every store adapter.

Three states only — ``FRESH`` / ``IN_FLIGHT`` / ``COMMITTED`` (ADR-007). There is
deliberately no fourth ``QUARANTINED`` state: a quarantined effect is an
``IN_FLIGHT`` record that has been *observed* as orphaned and routed to a policy.
Keeping it as ``IN_FLIGHT`` means the only exits are explicit decisions
(mark-committed / release / force-refire) — a timer can never quietly resurrect it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    """The lifecycle state of a single idempotency key."""

    FRESH = "fresh"
    """No record exists. Exactly one caller may observe this and run the effect."""

    IN_FLIGHT = "in_flight"
    """A record exists with no result yet — claimed, effect not known to be done."""

    COMMITTED = "committed"
    """A record exists with a stored result. Subsequent claims replay it."""


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """The outcome of a single :meth:`Store.claim` call.

    ``result`` and ``fingerprint`` are populated only when the store has them:
    ``result`` iff ``state is COMMITTED``; ``fingerprint`` whenever one was stored
    on the first claim.
    """

    state: State
    key: str
    result: bytes | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """A durable ledger row, as returned by :meth:`Store.get` / :meth:`Store.list`.

    Richer than :class:`ClaimResult`: it carries the timestamps the ledger uses to
    tell a *recently* in-flight key (a live concurrent worker) from an *orphaned*
    one (a crash), since both share ``State.IN_FLIGHT`` (ADR-007).
    """

    key: str
    state: State
    result: bytes | None = None
    fingerprint: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
