"""Onchain adapter — a resumed agent never double-submits a transaction (v0.2).

The account **nonce is the chain's own idempotency token**: at most one transaction
per (account, nonce) can ever be mined. So the guard keys on
``(chain_id, from, nonce, calldata_hash)`` (REQ-O1) and reconciles a crash by
*observing the chain* rather than guessing:

* the nonce advanced past ours → our tx mined → replay its hash (COMMITTED);
* the nonce is still free and our tx is unknown → nothing landed → safe to re-sign
  **at the same nonce** (NOT_COMMITTED);
* our tx (or something) is pending / occupies the nonce → indeterminate → quarantine.

Because signing is deterministic (same key + fields ⇒ same signed bytes ⇒ same tx
hash), a resumed run can recover the tx hash *without* re-broadcasting, and even a
forced resend is deduplicated by the chain — belt and suspenders (ADR-005).

The logic here talks to a small :class:`ChainClient` protocol so it is testable with
a fake and pluggable with web3; :class:`Web3ChainClient` (requires the ``onchain``
extra) is the production implementation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, Literal, Protocol, runtime_checkable

from .core import once
from .policies import Policy, ProbeResult, Verdict, check_then_decide
from .stores.base import Store

TxStatus = Literal["mined", "pending", "unknown"]


@dataclass(frozen=True, slots=True)
class TxIntent:
    """A fully-specified transaction to send. Fields must be **stable for a given
    key** so signing is deterministic (that is what lets a resume recover the hash)."""

    to: str
    value: int = 0
    data: bytes = b""
    gas: int = 21_000
    max_fee_per_gas: int = 0
    max_priority_fee_per_gas: int = 0


@runtime_checkable
class ChainClient(Protocol):
    """The minimal chain surface the adapter needs. Implement it for any signer/RPC."""

    @property
    def chain_id(self) -> int: ...

    @property
    def address(self) -> str:
        """The sender (``from``) address."""

    def latest_nonce(self) -> int:
        """Confirmed transaction count for ``address`` (mined only)."""

    def pending_nonce(self) -> int:
        """Transaction count including mempool — the next nonce to use."""

    def sign(self, intent: TxIntent, nonce: int) -> tuple[str, bytes]:
        """Deterministically sign ``intent`` at ``nonce``; return ``(tx_hash, raw)``."""

    def broadcast(self, raw: bytes) -> str:
        """Broadcast a raw signed tx; return its hash. Re-broadcasting is idempotent."""

    def status(self, tx_hash: str) -> TxStatus:
        """``mined`` / ``pending`` (in mempool) / ``unknown`` (never seen)."""


def onchain_key(chain_id: int, address: str, nonce: int, data: bytes) -> str:
    """Derive the guard key (REQ-O1). Keyed on the nonce (the chain's idempotency
    token) plus a hash of the calldata, so 'same intent, same slot' collapses."""
    calldata_hash = hashlib.sha256(data).hexdigest()[:16]
    return f"tx:{chain_id}:{address.lower()}:{nonce}:{calldata_hash}"


def _chain_prober(chain: ChainClient, intent: TxIntent, nonce: int) -> Callable[[str], ProbeResult]:
    def prober(_key: str) -> ProbeResult:
        # Re-sign deterministically to recover the hash we'd have broadcast.
        tx_hash, _raw = chain.sign(intent, nonce)
        status = chain.status(tx_hash)
        if status == "mined":
            return ProbeResult(Verdict.COMMITTED, tx_hash)  # it landed — replay the hash
        if status == "pending":
            return ProbeResult(Verdict.UNKNOWN)  # in mempool — refuse to guess
        if chain.latest_nonce() > nonce:
            return ProbeResult(Verdict.UNKNOWN)  # our nonce was consumed by another tx
        return ProbeResult(Verdict.NOT_COMMITTED)  # nonce free, tx unseen — safe to (re)send
    return prober


def onchain_once(
    store: Store,
    chain: ChainClient,
    *,
    nonce: int | None = None,
    policy: Policy | None = None,
    lease_ttl: float | None = None,
) -> Callable[[Callable[..., TxIntent]], Callable[..., str]]:
    """Wrap a function that returns a :class:`TxIntent` so the transaction is signed
    and broadcast **at most once** per ``(chain_id, from, nonce, calldata)``.

        @onchain_once(store, chain)
        def payout(to, amount) -> TxIntent:
            return TxIntent(to=to, value=amount)

        tx_hash = payout("0xabc...", 10**18)   # signed + broadcast once; replays the hash after

    The default policy is ``check_then_decide`` with a chain prober (chain state is
    observable, so reconciliation can narrow the crash window). Pass ``lease_ttl`` to
    make concurrent resumes safe (L-8); compose ``nonce`` with your own nonce manager.
    """

    def decorator(fn: Callable[..., TxIntent]) -> Callable[..., str]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            intent = fn(*args, **kwargs)
            n = chain.pending_nonce() if nonce is None else nonce
            key = onchain_key(chain.chain_id, chain.address, n, intent.data)
            pol = policy or check_then_decide(_chain_prober(chain, intent, n))
            guard = once(store, key=key, policy=pol, lease_ttl=lease_ttl)

            @guard
            def effect() -> str:
                tx_hash, raw = chain.sign(intent, n)
                chain.broadcast(raw)
                return tx_hash

            result: str = effect()
            return result

        return wrapper

    return decorator


class Web3ChainClient:
    """Production :class:`ChainClient` over web3.py + a local private key.

    Requires the ``onchain`` extra: ``pip install "exactly-once[onchain]"``.
    Fields on the :class:`TxIntent` (incl. gas/fees) must be stable per key so signing
    stays deterministic across a resume.
    """

    def __init__(self, w3: Any, private_key: str) -> None:
        try:
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "Web3ChainClient requires the 'onchain' extra: "
                "pip install 'exactly-once[onchain]'"
            ) from exc
        self._w3 = w3
        self._account = Account.from_key(private_key)
        self._pk = private_key

    @property
    def chain_id(self) -> int:
        return int(self._w3.eth.chain_id)

    @property
    def address(self) -> str:
        return str(self._account.address)

    def latest_nonce(self) -> int:
        return int(self._w3.eth.get_transaction_count(self._account.address, "latest"))

    def pending_nonce(self) -> int:
        return int(self._w3.eth.get_transaction_count(self._account.address, "pending"))

    def sign(self, intent: TxIntent, nonce: int) -> tuple[str, bytes]:
        tx = {
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": intent.to,
            "value": intent.value,
            "data": intent.data,
            "gas": intent.gas,
            "maxFeePerGas": intent.max_fee_per_gas,
            "maxPriorityFeePerGas": intent.max_priority_fee_per_gas,
        }
        signed = self._w3.eth.account.sign_transaction(tx, self._pk)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self._w3.to_hex(getattr(signed, "hash", None) or self._w3.keccak(raw))
        return tx_hash, bytes(raw)

    def broadcast(self, raw: bytes) -> str:
        return str(self._w3.to_hex(self._w3.eth.send_raw_transaction(raw)))

    def status(self, tx_hash: str) -> TxStatus:
        from web3.exceptions import TransactionNotFound

        try:
            tx = self._w3.eth.get_transaction(tx_hash)
        except TransactionNotFound:
            return "unknown"
        return "mined" if tx.get("blockNumber") is not None else "pending"
