"""Property-based state-machine tests — TEST-PLAN §5, the crown jewel.

Model ``once`` + a real store as a state machine and let Hypothesis search for a
sequence of operations that violates the invariant. The load-bearing check is
INV-1: no reachable sequence produces more than one execution of the effect for a
key. We also assert the metamorphic property — a call either runs the effect once
or replays the *identical* committed result — and INV-2 (committed never reverts).
"""

from __future__ import annotations

from collections import defaultdict

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from exactly_once import QuarantinedError, State, Store, once


class OnceStateMachine(RuleBasedStateMachine):
    """One in-memory store, many keys, arbitrary interleavings of the operations
    a real deployment produces: fresh calls, replays, and crash-orphaned keys."""

    keys = Bundle("keys")

    def __init__(self) -> None:
        super().__init__()
        self.store = Store.memory()
        self.exec_count: dict[str, int] = defaultdict(int)
        self.committed: dict[str, object] = {}

    @rule(target=keys, name=st.text(min_size=1, max_size=4))
    def new_key(self, name: str) -> str:
        return f"k:{name}"

    @rule(key=keys)
    def call(self, key: str) -> None:
        @once(self.store, key=key)
        def effect() -> dict[str, object]:
            self.exec_count[key] += 1
            return {"key": key, "run": self.exec_count[key]}

        try:
            result = effect()
        except QuarantinedError:
            return  # orphaned key, correctly refused — no execution
        if key in self.committed:
            # metamorphic: a replay returns the SAME result as the first run
            assert result == self.committed[key], (result, self.committed[key])
        else:
            self.committed[key] = result

    @rule(key=keys)
    def crash_mid_effect(self, key: str) -> None:
        """Simulate a process dying after claim, before commit: an orphaned
        IN_FLIGHT record. The default policy must never re-run it (INV-6)."""
        self.store.claim(key)  # if this was FRESH, it is now orphaned IN_FLIGHT

    @rule(key=keys)
    def replay(self, key: str) -> None:
        self.call(key)  # re-invoking is just another call

    @invariant()
    def at_most_once(self) -> None:
        assert all(c <= 1 for c in self.exec_count.values()), dict(self.exec_count)

    @invariant()
    def committed_never_reverts(self) -> None:
        # INV-2: once we've observed a committed result, the store never shows the
        # key as FRESH again (no record) — it stays IN_FLIGHT/COMMITTED.
        for key in self.committed:
            rec = self.store.get(key)
            assert rec is not None and rec.state in (State.IN_FLIGHT, State.COMMITTED)


TestOnceStateMachine = OnceStateMachine.TestCase
TestOnceStateMachine.settings = settings(max_examples=300, stateful_step_count=40)
