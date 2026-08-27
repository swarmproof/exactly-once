"""Onchain adapter — issue #8. Logic validated against a deterministic FakeChain.

The property: across a crash mid-broadcast, a resumed agent never submits a second
transaction at a new nonce. Reconciliation observes the chain — mined → replay the
hash; dropped → re-sign at the SAME nonce; pending → quarantine. The real-chain
version of this runs against Anvil in ``test_onchain_anvil.py``.
"""

from __future__ import annotations

import hashlib

import pytest

from exactly_once import QuarantinedError, State, Store
from exactly_once.onchain import TxIntent, onchain_key, onchain_once


class FakeChain:
    """Deterministic in-memory chain: sequential nonces, a mempool, and a mined set.

    ``submitted`` is the set of DISTINCT tx hashes ever broadcast — the count that
    must stay at 1 across a crash + resume (re-signing is deterministic, so a resend
    is the same hash, not a new transaction)."""

    def __init__(self, address: str = "0xSender", chain_id: int = 31337) -> None:
        self._chain_id = chain_id
        self._address = address
        self._latest = 0  # confirmed (mined) nonce count
        self._mempool: dict[str, int] = {}  # hash -> nonce
        self._mined: dict[str, int] = {}  # hash -> nonce
        self._signed: dict[str, int] = {}  # hash -> nonce (from sign)
        self.submitted: set[str] = set()

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def address(self) -> str:
        return self._address

    def latest_nonce(self) -> int:
        return self._latest

    def pending_nonce(self) -> int:
        return self._latest + len(self._mempool)

    def sign(self, intent: TxIntent, nonce: int) -> tuple[str, bytes]:
        raw = f"{self._chain_id}|{self._address}|{nonce}|{intent.data.hex()}|{intent.value}"
        h = "0x" + hashlib.sha256(raw.encode()).hexdigest()
        self._signed[h] = nonce
        return h, h.encode()

    def broadcast(self, raw: bytes) -> str:
        h = raw.decode()
        self.submitted.add(h)
        if h not in self._mined:
            self._mempool[h] = self._signed[h]  # idempotent: same hash, same slot
        return h

    def status(self, tx_hash: str) -> str:
        if tx_hash in self._mined:
            return "mined"
        if tx_hash in self._mempool:
            return "pending"
        return "unknown"

    # --- test controls ---
    def mine_all(self) -> None:
        for h, nonce in self._mempool.items():
            self._mined[h] = nonce
            self._latest = max(self._latest, nonce + 1)
        self._mempool.clear()

    def drop_all(self) -> None:
        self._mempool.clear()  # dropped without mining; nonce stays free


def test_onchain_key_is_stable_and_slot_sensitive() -> None:
    k1 = onchain_key(1, "0xABC", 5, b"\x01\x02")
    assert k1 == onchain_key(1, "0xabc", 5, b"\x01\x02")  # address case-insensitive
    assert k1 != onchain_key(1, "0xABC", 6, b"\x01\x02")  # different nonce
    assert k1 != onchain_key(1, "0xABC", 5, b"\x09")  # different calldata


def test_fresh_send_broadcasts_once() -> None:
    chain = FakeChain()
    store = Store.memory()

    @onchain_once(store, chain, nonce=0)
    def payout() -> TxIntent:
        return TxIntent(to="0xDest", value=100)

    h = payout()
    chain.mine_all()
    assert h.startswith("0x")
    assert len(chain.submitted) == 1


def test_replay_does_not_rebroadcast() -> None:
    chain = FakeChain()
    store = Store.memory()

    @onchain_once(store, chain, nonce=0)
    def payout() -> TxIntent:
        return TxIntent(to="0xDest", value=100)

    first = payout()
    chain.mine_all()
    again = payout()  # committed -> replay
    assert again == first
    assert len(chain.submitted) == 1


def _crash_before_commit(store: Store) -> None:
    """Make the next commit raise, simulating a kill between broadcast and commit."""
    orig = store.commit

    def boom(k: str, r: bytes) -> None:
        store.commit = orig  # only the first commit "crashes"
        raise RuntimeError("killed after broadcast, before commit")

    store.commit = boom  # type: ignore[method-assign]


def test_crash_then_mined_replays_hash_no_second_tx() -> None:
    chain = FakeChain()
    store = Store.memory()

    def payout_fn() -> TxIntent:
        return TxIntent(to="0xDest", value=100)

    charge = onchain_once(store, chain, nonce=0)(payout_fn)

    _crash_before_commit(store)
    with pytest.raises(RuntimeError):
        charge()  # broadcast happened, commit "crashed"
    assert len(chain.submitted) == 1
    assert store.get(onchain_key(chain.chain_id, chain.address, 0, b"")).state is State.IN_FLIGHT

    chain.mine_all()  # the broadcast tx gets mined while we were "down"
    h = charge()  # resume: prober sees it mined -> COMMITTED -> replay the hash
    assert h in chain._mined
    assert len(chain.submitted) == 1  # NO second transaction
    assert chain.latest_nonce() == 1  # exactly one nonce consumed


def test_crash_then_dropped_resigns_same_nonce() -> None:
    chain = FakeChain()
    store = Store.memory()
    charge = onchain_once(store, chain, nonce=0)(lambda: TxIntent(to="0xDest", value=100))

    _crash_before_commit(store)
    with pytest.raises(RuntimeError):
        charge()
    first_hashes = set(chain.submitted)

    chain.drop_all()  # the tx never mined and fell out of the mempool
    h = charge()  # resume: nonce free, tx unknown -> NOT_COMMITTED -> re-sign at SAME nonce
    chain.mine_all()

    assert chain._mined[h] == 0  # re-signed at nonce 0, NOT nonce+1
    assert chain.latest_nonce() == 1  # still exactly one tx mined
    assert chain.submitted == first_hashes  # same deterministic hash — not a new tx


def test_crash_then_pending_quarantines() -> None:
    chain = FakeChain()
    store = Store.memory()
    charge = onchain_once(store, chain, nonce=0)(lambda: TxIntent(to="0xDest", value=100))

    _crash_before_commit(store)
    with pytest.raises(RuntimeError):
        charge()

    # tx is still pending in the mempool (neither mined nor dropped)
    with pytest.raises(QuarantinedError):
        charge()  # indeterminate -> refuse to guess
    assert len(chain.submitted) == 1  # no second submit
