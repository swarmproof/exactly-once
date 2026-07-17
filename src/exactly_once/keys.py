"""Key derivation, normalization, and payload fingerprinting.

The key is the identity of an effect. Getting it right is the difference between
dedup that works and dedup that silently doesn't. Priority order (ARCH §4):

1. Explicit **static** key — ``key="welcome:user-4471"``. Highest trust.
2. Explicit **callable** key — ``key=lambda order, **_: f"charge:{order.id}"``.
   Derives from *business identity*.
3. **Derived** key (fallback) — a stable hash of the callable's qualified name plus
   its normalized arguments.

The cardinal rule (REQ-K6): key on **business identity** (``order_id``), never on a
**mutable value** (``amount``). ``key=f"charge:{customer}:{amount}"`` is a bug: two
legitimate distinct $50 charges collapse into one, and a repriced retry forks the
key. Docstrings and examples use ``key=f"charge:{order_id}"`` for this reason.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from .codec import DEFAULT_CODEC, Codec
from .errors import UnstableKeyError

# A key spec is a static string, or a callable over the wrapped call's args/kwargs.
KeySpec = str | Callable[..., str]
# A fingerprint spec selects the fields whose change should be rejected (REQ-K4).
FingerprintSpec = Callable[..., Any]


def resolve_key(
    key_spec: KeySpec | None,
    *,
    func: Callable[..., Any] | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    namespace: str | None = None,
    codec: Codec = DEFAULT_CODEC,
) -> str:
    """Resolve the final key for one guarded call, applying the priority order.

    Raises :class:`UnstableKeyError` if a derived key must be computed from inputs
    that are not stably serializable — never silently mint a per-call key, because
    that looks like it works and dedupes nothing (REQ-K3).
    """
    if key_spec is None:
        key = _derive_key(func=func, args=args, kwargs=kwargs, codec=codec)
    elif callable(key_spec):
        key = key_spec(*args, **kwargs)
        if not isinstance(key, str):
            raise TypeError(
                f"key callable must return str, got {type(key).__name__!r}"
            )
    else:
        key = key_spec

    if namespace:
        return f"{namespace}:{key}"
    return key


def _derive_key(
    *,
    func: Callable[..., Any] | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    codec: Codec,
) -> str:
    qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", "<anon>")
    module = getattr(func, "__module__", "")
    normalized = _normalize_args(args, kwargs, codec)
    digest = hashlib.sha256(f"{module}.{qualname}\0{normalized}".encode()).hexdigest()
    return f"auto:{qualname}:{digest[:32]}"


def _normalize_args(args: tuple[Any, ...], kwargs: dict[str, Any], codec: Codec) -> str:
    """Order-insensitive, stable serialization of a call's arguments (REQ-K3).

    kwargs are sorted by name so call-order can't change the key. Unstable or
    unserializable inputs raise rather than mis-key.
    """
    try:
        payload = {
            "args": list(args),
            "kwargs": {k: kwargs[k] for k in sorted(kwargs)},
        }
        return codec.encode(payload).decode("utf-8", errors="surrogatepass")
    except TypeError as exc:
        raise UnstableKeyError(
            "could not derive a stable key from the call's arguments "
            f"({exc}). Pass an explicit key=..., e.g. key=lambda order, **_: "
            "f'charge:{order.id}', keyed on business identity."
        ) from exc


def compute_fingerprint(
    fingerprint_spec: FingerprintSpec | None,
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    codec: Codec = DEFAULT_CODEC,
) -> str | None:
    """Compute an optional payload fingerprint for REQ-K4.

    Independent of the key: it captures the fields that must *not* differ between
    two calls that share a key. If a later claim presents the same key with a
    different fingerprint, the core raises :class:`~exactly_once.errors.KeyReuseError`.
    Returns ``None`` when no fingerprint is configured.
    """
    if fingerprint_spec is None:
        return None
    selected = fingerprint_spec(*args, **kwargs)
    return hashlib.sha256(codec.encode(selected)).hexdigest()
