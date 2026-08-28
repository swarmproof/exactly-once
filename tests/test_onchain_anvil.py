"""Onchain E2E-4 against a real Anvil (Foundry) node.

Gated on the ``anvil`` binary being present; skipped otherwise (CI installs Foundry).
Proves against a real chain + real signing that a crash between broadcast and commit,
followed by a resume, produces exactly ONE transaction — the resume replays the mined
hash instead of re-signing a new one.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from exactly_once import QuarantinedError, State, Store
from exactly_once.onchain import TxIntent, Web3ChainClient, onchain_key, onchain_once


def _crash_next_commit(store: Store) -> None:
    """Make the next commit raise once, simulating a kill after broadcast."""
    orig = store.commit

    def boom(k: str, r: bytes) -> None:
        store.commit = orig  # type: ignore[method-assign]
        raise RuntimeError("killed after broadcast, before commit")

    store.commit = boom  # type: ignore[method-assign]

pytestmark = pytest.mark.skipif(
    shutil.which("anvil") is None, reason="anvil (Foundry) not installed"
)

# Anvil's first deterministic dev account.
_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_DEST = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # anvil account #1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def w3():
    from web3 import Web3

    port = _free_port()
    proc = subprocess.Popen(
        ["anvil", "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        client = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
        for _ in range(50):
            if client.is_connected():
                break
            time.sleep(0.1)
        else:  # pragma: no cover
            raise RuntimeError("anvil did not come up")
        yield client
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _intent() -> TxIntent:
    return TxIntent(
        to=_DEST,
        value=10**18,  # 1 ETH
        gas=21_000,
        max_fee_per_gas=5_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
    )


def test_e2e4_crash_mid_broadcast_resumes_without_double_submit(w3) -> None:
    chain = Web3ChainClient(w3, _PK)
    store = Store.memory()
    start_balance = w3.eth.get_balance(_DEST)

    charge = onchain_once(store, chain, nonce=0)(_intent)

    # Kill between broadcast (anvil mines instantly) and our commit.
    orig = store.commit

    def boom(k: str, r: bytes) -> None:
        store.commit = orig
        raise RuntimeError("killed after broadcast, before commit")

    store.commit = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        charge()

    key = onchain_key(chain.chain_id, chain.address, 0, _intent().data)
    assert store.get(key).state is State.IN_FLIGHT

    # The broadcast tx confirms while the agent is "down" — wait for it explicitly
    # (broadcast != mined; don't assume the node instant-mined by the time we look).
    broadcast_hash, _ = chain.sign(_intent(), 0)
    w3.eth.wait_for_transaction_receipt(broadcast_hash, timeout=15)
    assert chain.latest_nonce() == 1  # the tx really did mine on-chain

    # Resume: the prober sees the tx mined and replays its hash — no new tx.
    tx_hash = charge()
    assert chain.status(tx_hash) == "mined"
    assert chain.latest_nonce() == 1  # STILL one tx from the sender
    assert w3.eth.get_balance(_DEST) - start_balance == 10**18  # recipient credited once


def test_e2e4_pending_tx_quarantines(w3) -> None:
    """With mining paused, the broadcast tx stays pending — a resume must refuse to
    guess and quarantine, never re-send."""
    w3.provider.make_request("anvil_setAutomine", [False])
    chain = Web3ChainClient(w3, _PK)
    store = Store.memory()
    charge = onchain_once(store, chain, nonce=0)(_intent)

    _crash_next_commit(store)
    with pytest.raises(RuntimeError):
        charge()  # broadcast to mempool, then "crash"

    key = onchain_key(chain.chain_id, chain.address, 0, _intent().data)
    assert store.get(key).state is State.IN_FLIGHT
    with pytest.raises(QuarantinedError):
        charge()  # tx still pending on-chain -> indeterminate -> quarantine


def test_e2e4_dropped_tx_resigns_same_nonce(w3) -> None:
    """A broadcast that never mines and is dropped from the mempool must be re-signed
    at the SAME nonce on resume — not abandoned, not sent at a new nonce."""
    w3.provider.make_request("anvil_setAutomine", [False])
    chain = Web3ChainClient(w3, _PK)
    store = Store.memory()
    charge = onchain_once(store, chain, nonce=0)(_intent)

    _crash_next_commit(store)
    with pytest.raises(RuntimeError):
        charge()

    dropped_hash, _ = chain.sign(_intent(), 0)
    w3.provider.make_request("anvil_dropTransaction", [dropped_hash])
    w3.provider.make_request("anvil_setAutomine", [True])  # the resend can now mine

    tx_hash = charge()  # nonce free + tx unknown -> NOT_COMMITTED -> re-sign same nonce
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=15)
    assert tx_hash == dropped_hash  # deterministic re-sign at nonce 0, not nonce+1
    assert chain.latest_nonce() == 1  # exactly one tx mined
