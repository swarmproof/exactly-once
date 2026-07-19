"""The result codec (REQ-S6).

A committed result is stored as ``bytes`` in the store. The codec turns the
effect's return value into those bytes and back. JSON is the default because it is
transparent, cross-language (a Python producer and a TypeScript consumer can share
one ledger — SPEC §8.2), and refuses silently-lossy encodings.

Non-serializable results raise a clear error rather than corrupting the ledger.
When a result genuinely cannot be serialized, the documented pattern is to *store a
reference, not the payload* (e.g. commit the Stripe charge id, not the whole
object) and re-fetch it on replay.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from .errors import ResultTooLargeError

# Generous default ceiling (1 MiB) — results should be small (ids, statuses). Guards
# the ledger against an accidental large payload; raise via JSONCodec(max_bytes=...).
DEFAULT_MAX_BYTES = 1_048_576


@runtime_checkable
class Codec(Protocol):
    """Serialize a result to bytes and back. Must round-trip."""

    def encode(self, value: Any) -> bytes: ...

    def decode(self, raw: bytes) -> Any: ...


class JSONCodec:
    """The default codec. UTF-8 JSON, deterministic key order.

    ``sort_keys`` makes the encoding stable, which also makes it safe to reuse for
    key derivation and fingerprinting (see :mod:`exactly_once.keys`). ``max_bytes``
    caps the encoded size (REQ-S6); pass ``None`` to disable the ceiling.
    """

    def __init__(self, max_bytes: int | None = DEFAULT_MAX_BYTES) -> None:
        self._max_bytes = max_bytes

    def encode(self, value: Any) -> bytes:
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"result of type {type(value).__name__!r} is not JSON-serializable: {exc}. "
                "Provide a custom codec, or store a reference (e.g. an id) rather than the "
                "whole object and re-fetch it on replay."
            ) from exc
        if self._max_bytes is not None and len(encoded) > self._max_bytes:
            raise ResultTooLargeError(
                f"encoded result is {len(encoded)} bytes, over the {self._max_bytes}-byte "
                "ceiling. Store a reference (an id) instead of the payload, or raise the "
                "limit with JSONCodec(max_bytes=...)."
            )
        return encoded

    def decode(self, raw: bytes) -> Any:
        return json.loads(raw.decode("utf-8"))


DEFAULT_CODEC: Codec = JSONCodec()
