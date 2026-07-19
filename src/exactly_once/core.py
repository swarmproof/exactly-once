"""``once`` — the two-line API and the state machine that enforces the guarantee.

One object, three shapes:

* ``@once(store, key=...)`` — decorator; the function runs at most once per key and
  replays the committed result thereafter.
* ``with once(store, key=...) as guard:`` — sync context manager; ``guard.fresh``
  tells you whether to run the block; the block's result commits on clean exit.
* ``async with once(store, key=...) as guard:`` — the async mirror.

The load-bearing rule (ADR-004): an exception raised by the effect **does not**
commit and **does not** release — it leaves the key ``IN_FLIGHT`` so reconciliation,
not a silent re-fire, decides what happens next.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Literal

from ._types import ClaimResult, State
from .codec import DEFAULT_CODEC, Codec
from .errors import KeyReuseError, QuarantinedError, StoreUnavailableError
from .keys import FingerprintSpec, KeySpec, compute_fingerprint, resolve_key
from .policies import _UNSET, Action, Policy, quarantine
from .stores.base import Store

logger = logging.getLogger("exactly_once")

OnStoreDown = Literal["fail", "open"]

# The key of the guarded call currently executing on this thread / async task.
# Lets a wrapped effect pass exactly-once's key through as the provider's own
# idempotency key (ADR-005) without threading it through arguments.
_current_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exactly_once_current_key", default=None
)


def current_key() -> str | None:
    """The key of the guarded effect currently running, or ``None`` outside one.

    Use it to pass exactly-once's key into the provider as *its* idempotency key::

        @once(store, key=lambda o, **_: f"charge:{o.id}")
        def charge_card(order):
            return stripe.charge(order.customer, order.amount,
                                 idempotency_key=current_key())
    """
    return _current_key.get()


class _Guard:
    """Handed to the ``with once(...) as guard`` block."""

    __slots__ = ("_unguarded", "fresh", "key", "result")

    def __init__(
        self, key: str, fresh: bool, result: Any = None, *, unguarded: bool = False
    ) -> None:
        self.key = key
        self.fresh = fresh
        #: On replay, the committed value. On a fresh run, set this to the value you
        #: want stored (defaults to ``None``); it is committed on clean block exit.
        self.result = result
        self._unguarded = unguarded


class once:
    """See module docstring. Construct with a store and a key spec."""

    def __init__(
        self,
        store: Store,
        *,
        key: KeySpec | None = None,
        fingerprint: FingerprintSpec | None = None,
        policy: Policy = quarantine,
        namespace: str | None = None,
        codec: Codec = DEFAULT_CODEC,
        on_store_down: OnStoreDown = "fail",
    ) -> None:
        self._store = store
        self._key = key
        self._fingerprint = fingerprint
        self._policy = policy
        self._namespace = namespace
        self._codec = codec
        self._on_store_down = on_store_down
        # Per-block guard state lives in a ContextVar stack, NOT a shared instance
        # attribute — so one `once(...)` object entered from multiple threads or
        # asyncio tasks never commits the wrong block's result. The stack handles
        # nesting the same object within a single context.
        self._cm_stack: contextvars.ContextVar[tuple[_Guard, ...]] = contextvars.ContextVar(
            f"exactly_once_cm_{id(self)}", default=()
        )

    def _push_guard(self, guard: _Guard) -> _Guard:
        self._cm_stack.set((*self._cm_stack.get(), guard))
        return guard

    def _pop_guard(self) -> _Guard | None:
        stack = self._cm_stack.get()
        if not stack:
            return None
        self._cm_stack.set(stack[:-1])
        return stack[-1]

    # ------------------------------------------------------------------ #
    # decorator form
    # ------------------------------------------------------------------ #

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        import inspect

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                key = self._resolve_key(func, args, kwargs)
                return await self._arun(func, key, self._fp(args, kwargs), args, kwargs)

            return awrapper

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = self._resolve_key(func, args, kwargs)
            return self._run(func, key, self._fp(args, kwargs), args, kwargs)

        return wrapper

    # ------------------------------------------------------------------ #
    # sync context-manager form
    # ------------------------------------------------------------------ #

    def __enter__(self) -> _Guard:
        key = self._require_cm_key()
        fp = self._fp((), {})
        claim = self._claim_or_open(key, fp, is_async=False)
        if claim is None:  # store down, fail-open
            return self._push_guard(_Guard(key, fresh=True, unguarded=True))
        self._check_fingerprint(claim, fp)

        if claim.state is State.FRESH:
            guard = _Guard(key, fresh=True)
        elif claim.state is State.COMMITTED:
            guard = _Guard(key, fresh=False, result=self._decode(claim.result))
        else:
            guard = self._resolve_in_flight_cm(key, fp, claim, is_async=False)
        return self._push_guard(guard)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        guard = self._pop_guard()
        if guard is None or guard._unguarded or not guard.fresh:
            return False
        if exc_type is None:
            self._store.commit(guard.key, self._codec.encode(guard.result))
        # else: exception -> leave IN_FLIGHT (ADR-004), never release/commit.
        return False

    # ------------------------------------------------------------------ #
    # async context-manager form
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> _Guard:
        key = self._require_cm_key()
        fp = self._fp((), {})
        claim = await self._aclaim_or_open(key, fp)
        if claim is None:
            return self._push_guard(_Guard(key, fresh=True, unguarded=True))
        self._check_fingerprint(claim, fp)

        if claim.state is State.FRESH:
            guard = _Guard(key, fresh=True)
        elif claim.state is State.COMMITTED:
            guard = _Guard(key, fresh=False, result=self._decode(claim.result))
        else:
            guard = await self._aresolve_in_flight_cm(key, fp, claim)
        return self._push_guard(guard)

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        guard = self._pop_guard()
        if guard is None or guard._unguarded or not guard.fresh:
            return False
        if exc_type is None:
            await self._store.acommit(guard.key, self._codec.encode(guard.result))
        return False

    # ------------------------------------------------------------------ #
    # shared internals
    # ------------------------------------------------------------------ #

    def _resolve_key(
        self, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> str:
        return resolve_key(
            self._key,
            func=func,
            args=args,
            kwargs=kwargs,
            namespace=self._namespace,
            codec=self._codec,
        )

    def _require_cm_key(self) -> str:
        if self._key is None:
            raise ValueError(
                "the context-manager form requires an explicit key=..., e.g. "
                "with once(store, key='send-welcome:user-4471') as guard: ..."
            )
        return resolve_key(
            self._key, func=None, args=(), kwargs={}, namespace=self._namespace, codec=self._codec
        )

    def _fp(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
        return compute_fingerprint(self._fingerprint, args=args, kwargs=kwargs, codec=self._codec)

    def _decode(self, raw: bytes | None) -> Any:
        return None if raw is None else self._codec.decode(raw)

    def _check_fingerprint(self, claim: ClaimResult, fp: str | None) -> None:
        if (
            fp is not None
            and claim.state is not State.FRESH
            and claim.fingerprint is not None
            and claim.fingerprint != fp
        ):
            raise KeyReuseError(
                f"key {claim.key!r} was first used with a different payload; refusing to "
                "replay a result computed for other inputs. Use a distinct key if this is a "
                "genuinely different request."
            )

    def _claim_or_open(self, key: str, fp: str | None, *, is_async: bool) -> ClaimResult | None:
        try:
            return self._store.claim(key, fingerprint=fp)
        except StoreUnavailableError:
            self._handle_store_down(key)  # raises if fail-closed
            return None  # fail-open signal: run unguarded

    async def _aclaim_or_open(self, key: str, fp: str | None) -> ClaimResult | None:
        try:
            return await self._store.aclaim(key, fingerprint=fp)
        except StoreUnavailableError:
            self._handle_store_down(key)
            return None

    def _handle_store_down(self, key: str) -> None:
        if self._on_store_down == "open":
            logger.warning(
                "exactly_once: store unavailable for key %r; on_store_down='open', "
                "running the effect UNGUARDED (no dedupe this call).",
                key,
            )
            return None  # signal: run unguarded
        raise StoreUnavailableError(
            f"store unavailable while claiming key {key!r}; failing closed (effect not run). "
            "Pass on_store_down='open' to run it unguarded instead."
        )

    # ----- decorator execution -----

    def _run(
        self,
        func: Callable[..., Any],
        key: str,
        fp: str | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        claim = self._claim_or_open(key, fp, is_async=False)
        if claim is None:  # fail-open
            return func(*args, **kwargs)
        self._check_fingerprint(claim, fp)

        if claim.state is State.FRESH:
            return self._execute_and_commit(func, key, args, kwargs)
        if claim.state is State.COMMITTED:
            return self._decode(claim.result)

        # IN_FLIGHT -> reconciliation / concurrency policy
        directive = self._policy.resolve(key, self._store, claim, self._codec)
        if directive.action is Action.REPLAY:
            return self._decode(directive.result)
        if directive.action is Action.QUARANTINE:
            if directive.sentinel is _UNSET:
                raise QuarantinedError(key)
            return directive.sentinel
        # RUN: policy has released the key; re-claim exactly once.
        reclaim = self._store.claim(key, fingerprint=fp)
        if reclaim.state is State.FRESH:
            return self._execute_and_commit(func, key, args, kwargs)
        if reclaim.state is State.COMMITTED:
            return self._decode(reclaim.result)
        raise QuarantinedError(key)  # someone else re-claimed; stay safe

    def _execute_and_commit(
        self, func: Callable[..., Any], key: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        token = _current_key.set(key)
        try:
            result = func(*args, **kwargs)
        finally:
            _current_key.reset(token)
        # Only reached on success; an exception propagates and leaves the key IN_FLIGHT.
        self._store.commit(key, self._codec.encode(result))
        return result

    async def _arun(
        self,
        func: Callable[..., Awaitable[Any]],
        key: str,
        fp: str | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        claim = await self._aclaim_or_open(key, fp)
        if claim is None:
            return await func(*args, **kwargs)
        self._check_fingerprint(claim, fp)

        if claim.state is State.FRESH:
            return await self._aexecute_and_commit(func, key, args, kwargs)
        if claim.state is State.COMMITTED:
            return self._decode(claim.result)

        directive = await self._policy.aresolve(key, self._store, claim, self._codec)
        if directive.action is Action.REPLAY:
            return self._decode(directive.result)
        if directive.action is Action.QUARANTINE:
            if directive.sentinel is _UNSET:
                raise QuarantinedError(key)
            return directive.sentinel
        reclaim = await self._store.aclaim(key, fingerprint=fp)
        if reclaim.state is State.FRESH:
            return await self._aexecute_and_commit(func, key, args, kwargs)
        if reclaim.state is State.COMMITTED:
            return self._decode(reclaim.result)
        raise QuarantinedError(key)

    async def _aexecute_and_commit(
        self,
        func: Callable[..., Awaitable[Any]],
        key: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        token = _current_key.set(key)
        try:
            result = await func(*args, **kwargs)
        finally:
            _current_key.reset(token)
        await self._store.acommit(key, self._codec.encode(result))
        return result

    # ----- context-manager IN_FLIGHT resolution -----

    def _resolve_in_flight_cm(
        self, key: str, fp: str | None, claim: ClaimResult, *, is_async: bool
    ) -> _Guard:
        directive = self._policy.resolve(key, self._store, claim, self._codec)
        if directive.action is Action.REPLAY:
            return _Guard(key, fresh=False, result=self._decode(directive.result))
        if directive.action is Action.QUARANTINE:
            if directive.sentinel is _UNSET:
                raise QuarantinedError(key)
            return _Guard(key, fresh=False, result=directive.sentinel)
        reclaim = self._store.claim(key, fingerprint=fp)
        if reclaim.state is State.FRESH:
            return _Guard(key, fresh=True)
        if reclaim.state is State.COMMITTED:
            return _Guard(key, fresh=False, result=self._decode(reclaim.result))
        raise QuarantinedError(key)

    async def _aresolve_in_flight_cm(self, key: str, fp: str | None, claim: ClaimResult) -> _Guard:
        directive = await self._policy.aresolve(key, self._store, claim, self._codec)
        if directive.action is Action.REPLAY:
            return _Guard(key, fresh=False, result=self._decode(directive.result))
        if directive.action is Action.QUARANTINE:
            if directive.sentinel is _UNSET:
                raise QuarantinedError(key)
            return _Guard(key, fresh=False, result=directive.sentinel)
        reclaim = await self._store.aclaim(key, fingerprint=fp)
        if reclaim.state is State.FRESH:
            return _Guard(key, fresh=True)
        if reclaim.state is State.COMMITTED:
            return _Guard(key, fresh=False, result=self._decode(reclaim.result))
        raise QuarantinedError(key)
