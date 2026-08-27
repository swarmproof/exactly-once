"""Framework integration smoke tests — issue #10 (E2E-5).

Gated on the framework being installed (skipped otherwise; a dedicated CI job runs
them). No LLM is involved — a LangGraph node and a CrewAI tool are just callables, so
we drive them directly and assert the guarded effect runs once across re-invocation.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from exactly_once import QuarantinedError, Store

# --- LangGraph -------------------------------------------------------------

pytest.importorskip("langgraph.graph")


class _State(TypedDict):
    order_id: str
    charged: bool


def test_langgraph_node_runs_once_across_reinvokes() -> None:
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph import END, START, StateGraph

    from exactly_once.integrations.langgraph import once_node

    store = Store.memory()
    n = {"c": 0}

    # config annotated RunnableConfig so LangGraph injects the run config (thread_id)
    @once_node(store)
    def charge(state: _State, config: RunnableConfig) -> dict:
        n["c"] += 1
        return {"charged": True}

    g = StateGraph(_State)
    g.add_node("charge", charge)
    g.add_edge(START, "charge")
    g.add_edge("charge", END)
    app = g.compile()

    cfg = {"configurable": {"thread_id": "run-1"}}
    app.invoke({"order_id": "o1", "charged": False}, cfg)
    app.invoke({"order_id": "o1", "charged": False}, cfg)  # re-invoke same thread (a retry)
    assert n["c"] == 1  # the node's effect fired once


def test_langgraph_key_override_dedupes_on_business_identity() -> None:
    from exactly_once.integrations.langgraph import once_node

    store = Store.memory()
    n = {"c": 0}

    @once_node(store, key=lambda state, config: f"charge:{state['order_id']}")
    def charge(state: _State, config: dict) -> dict:
        n["c"] += 1
        return {"charged": True}

    # Different threads, same order -> the business key dedupes across runs.
    charge({"order_id": "o1", "charged": False}, {"configurable": {"thread_id": "a"}})
    charge({"order_id": "o1", "charged": False}, {"configurable": {"thread_id": "b"}})
    assert n["c"] == 1


def test_langgraph_missing_thread_id_and_key_errors() -> None:
    from exactly_once.integrations.langgraph import once_node

    store = Store.memory()

    @once_node(store)
    def charge(state: _State, config: dict) -> dict:
        return {"charged": True}

    with pytest.raises(ValueError, match="thread_id"):
        charge({"order_id": "o1", "charged": False}, {})  # no thread_id, no key


# --- CrewAI ----------------------------------------------------------------


def test_crewai_tool_runs_once() -> None:
    crewai_tools = pytest.importorskip("crewai.tools")
    BaseTool = crewai_tools.BaseTool

    from exactly_once.integrations.crewai import once_tool_run

    store = Store.memory()
    n = {"c": 0}

    class ChargeTool(BaseTool):  # type: ignore[misc, valid-type]
        name: str = "charge"
        description: str = "Charge a customer exactly once."

        @once_tool_run(store, key=lambda self, order_id, **_: f"charge:{order_id}")
        def _run(self, order_id: str) -> str:
            n["c"] += 1
            return f"charged:{order_id}"

    tool = ChargeTool()
    r1 = tool._run(order_id="o1")
    r2 = tool._run(order_id="o1")  # retry -> replay
    assert n["c"] == 1
    assert r1 == r2 == "charged:o1"


def test_crewai_default_key_dedupes_on_args() -> None:
    crewai_tools = pytest.importorskip("crewai.tools")
    BaseTool = crewai_tools.BaseTool

    from exactly_once.integrations.crewai import once_tool_run

    store = Store.memory()
    n = {"c": 0}

    class NotifyTool(BaseTool):  # type: ignore[misc, valid-type]
        name: str = "notify"
        description: str = "Notify once per (user, message)."

        @once_tool_run(store)  # default key = name + hash(args)
        def _run(self, user: str) -> str:
            n["c"] += 1
            return "sent"

    tool = NotifyTool()
    tool._run(user="u1")
    tool._run(user="u1")  # same args -> deduped
    tool._run(user="u2")  # different args -> runs
    assert n["c"] == 2


def test_crewai_orphaned_key_quarantines() -> None:
    crewai_tools = pytest.importorskip("crewai.tools")
    BaseTool = crewai_tools.BaseTool

    from exactly_once.integrations.crewai import once_tool_run

    store = Store.memory()
    store.claim("charge:o1")  # a crashed prior run left this in-flight

    class ChargeTool(BaseTool):  # type: ignore[misc, valid-type]
        name: str = "charge"
        description: str = "Charge once."

        @once_tool_run(store, key=lambda self, order_id, **_: f"charge:{order_id}")
        def _run(self, order_id: str) -> str:
            return "charged"

    with pytest.raises(QuarantinedError):
        ChargeTool()._run(order_id="o1")
