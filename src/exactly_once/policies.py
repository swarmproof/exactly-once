"""Reconciliation policies — what to do when a claim observes ``IN_FLIGHT``.

A single-store library cannot tell a *live concurrent worker* apart from a
*crashed prior run*: both leave the key ``IN_FLIGHT``. So the policy answers one
question — "someone (or something) already claimed this key; what now?" — and the
**safe default is to not run the effect** (ADR-003).

Policies return a :class:`Directive` telling the core one of three things:

* ``QUARANTINE`` — do not run; raise :class:`~exactly_once.errors.QuarantinedError`
  (or hand back a sentinel). The key stays ``IN_FLIGHT`` for an explicit decision.
* ``RUN`` — the policy has ``release``-d the key (it is now safe to re-run); the
  core should re-claim and execute the effect.
* ``REPLAY`` — the effect already happened; return the given result, do not run.

The default, ``quarantine``, only ever emits ``QUARANTINE`` — it never guesses.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from ._types import ClaimResult, State
from .codec import Codec

_UNSET = object()


class Action(Enum):
    QUARANTINE = auto()
    RUN = auto()
    REPLAY = auto()


@dataclass(frozen=True, slots=True)
class Directive:
    action: Action
    result: bytes | None = None  # populated for REPLAY (already-serialized)
    sentinel: Any = _UNSET  # for QUARANTINE: return this instead of raising


class Verdict(Enum):
    """A prober's judgement about whether an orphaned effect really happened."""

    COMMITTED = auto()
    NOT_COMMITTED = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What a ``check_then_decide`` prober returns.

    ``result`` is the observed effect result and is required when the verdict is
    :attr:`Verdict.COMMITTED`, so the ledger can be back-filled and replayed.
    """

    verdict: Verdict
    result: Any = None


# A store passed to a policy exposes at least claim/commit/release/get. We type it
# loosely here (Any) to avoid a circular import with stores.base.
_Store = Any


class Policy:
    """Base class. Override :meth:`resolve` (sync) and :meth:`aresolve` (async)."""

    def resolve(
        self, key: str, store: _Store, claim: ClaimResult, codec: Codec
    ) -> Directive:  # pragma: no cover - abstract
        raise NotImplementedError

    async def aresolve(
        self, key: str, store: _Store, claim: ClaimResult, codec: Codec
    ) -> Directive:  # pragma: no cover - abstract
        raise NotImplementedError


class Quarantine(Policy):
    """Default policy. Never run an effect of unknown outcome (ADR-003, REQ-R2).

    Leaves the key ``IN_FLIGHT`` and, by default, raises
    :class:`~exactly_once.errors.QuarantinedError`. Configure a sentinel return with
    ``quarantine(sentinel=value)`` when the caller would rather branch than catch.
    """

    def __init__(self, sentinel: Any = _UNSET) -> None:
        self._sentinel = sentinel

    def __call__(self, sentinel: Any = _UNSET) -> Quarantine:
        return Quarantine(sentinel=sentinel)

    def resolve(self, key: str, store: _Store, claim: ClaimResult, codec: Codec) -> Directive:
        return Directive(Action.QUARANTINE, sentinel=self._sentinel)

    async def aresolve(
        self, key: str, store: _Store, claim: ClaimResult, codec: Codec
    ) -> Directive:
        return Directive(Action.QUARANTINE, sentinel=self._sentinel)


class Fail(Policy):
    """Strict variant of quarantine: always raise, never a sentinel (REQ-R3)."""

    def resolve(self, key: str, store: _Store, claim: ClaimResult, codec: Codec) -> Directive:
        return Directive(Action.QUARANTINE, sentinel=_UNSET)

    async def aresolve(
        self, key: str, store: _Store, claim: ClaimResult, codec: Codec
    ) -> Directive:
        return Directive(Action.QUARANTINE, sentinel=_UNSET)


class AutoRetry(Policy):
    """Release an orphaned key and re-run the effect (REQ-R3).

    ⚠️ This re-opens the double-fire window and is the AWS-Powertools-style
    delete-and-rerun behavior this library rejects *by default*. Use it **only** for
    genuinely idempotent, reversible effects (e.g. an email you'd tolerate twice),
    never for payments or onchain transactions.
    """

    def resolve(self, key: str, store: _Store, claim: ClaimResult, codec: Codec) -> Directive:
        store.release(key)
        return Directive(Action.RUN)

    async def aresolve(
        self, key: str, store: _Store, claim: ClaimResult, codec: Codec
    ) -> Directive:
        await store.arelease(key)
        return Directive(Action.RUN)


class CheckThenDecide(Policy):
    """Narrow the crash window with external truth (REQ-R3; the E2E-1 policy).

    Runs a user-supplied prober that *observes the world* — queries Stripe by
    idempotency key, asks the chain whether the tx was mined — and returns a
    :class:`ProbeResult`:

    * ``COMMITTED`` → back-fill the ledger with the observed result and replay it.
    * ``NOT_COMMITTED`` → ``release`` and re-run (the effect provably did not happen).
    * ``UNKNOWN`` → quarantine (we still cannot tell; refuse to guess).

    The prober may be sync or async.
    """

    def __init__(self, prober: Callable[[str], ProbeResult | Any]) -> None:
        self._prober = prober

    def _apply(
        self, key: str, store: _Store, codec: Codec, probe: ProbeResult, *, is_async: bool
    ) -> Directive:
        if probe.verdict is Verdict.COMMITTED:
            encoded = codec.encode(probe.result)
            return Directive(Action.REPLAY, result=encoded)
        if probe.verdict is Verdict.NOT_COMMITTED:
            return Directive(Action.RUN)
        return Directive(Action.QUARANTINE, sentinel=_UNSET)

    def resolve(self, key: str, store: _Store, claim: ClaimResult, codec: Codec) -> Directive:
        probe = self._prober(key)
        if inspect.isawaitable(probe):  # pragma: no cover - defensive
            raise TypeError("async prober used with a sync call; use an async effect")
        directive = self._apply(key, store, codec, probe, is_async=False)
        if directive.action is Action.REPLAY and directive.result is not None:
            store.commit(key, directive.result)
        elif directive.action is Action.RUN:
            store.release(key)
        return directive

    async def aresolve(
        self, key: str, store: _Store, claim: ClaimResult, codec: Codec
    ) -> Directive:
        probe = self._prober(key)
        if inspect.isawaitable(probe):
            probe = await probe
        directive = self._apply(key, store, codec, probe, is_async=True)
        if directive.action is Action.REPLAY and directive.result is not None:
            await store.acommit(key, directive.result)
        elif directive.action is Action.RUN:
            await store.arelease(key)
        return directive


class Wait(Policy):
    """Block a concurrent loser until the winner commits, then replay (RACE-3).

    Polls the ledger: ``COMMITTED`` → replay the stored result; the record
    disappearing (``release``) → re-run; ``timeout`` → quarantine. A genuinely
    orphaned crash key never commits, so ``wait`` degrades safely to quarantine.
    """

    def __init__(self, timeout: float = 5.0, interval: float = 0.05) -> None:
        self._timeout = timeout
        self._interval = interval

    def resolve(self, key: str, store: _Store, claim: ClaimResult, codec: Codec) -> Directive:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            rec = store.get(key)
            if rec is None:
                return Directive(Action.RUN)
            if rec.state is State.COMMITTED:
                return Directive(Action.REPLAY, result=rec.result)
            time.sleep(self._interval)
        return Directive(Action.QUARANTINE, sentinel=_UNSET)

    async def aresolve(
        self, key: str, store: _Store, claim: ClaimResult, codec: Codec
    ) -> Directive:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            rec = await store.aget(key)
            if rec is None:
                return Directive(Action.RUN)
            if rec.state is State.COMMITTED:
                return Directive(Action.REPLAY, result=rec.result)
            await asyncio.sleep(self._interval)
        return Directive(Action.QUARANTINE, sentinel=_UNSET)


# Public policy handles. `quarantine` is the default; it's also callable to
# configure a sentinel: `policy=quarantine(sentinel=None)`.
quarantine = Quarantine()
fail = Fail()
auto_retry = AutoRetry()


def check_then_decide(prober: Callable[[str], ProbeResult | Any]) -> CheckThenDecide:
    """Build a :class:`CheckThenDecide` policy from a world-observing prober."""
    return CheckThenDecide(prober)


def wait(timeout: float = 5.0, interval: float = 0.05) -> Wait:
    """Build a :class:`Wait` policy (block-until-committed for concurrent losers)."""
    return Wait(timeout=timeout, interval=interval)
