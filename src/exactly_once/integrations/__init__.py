"""Thin integration helpers for agent frameworks (v0.2).

These are *adapters, not forks*: `@once` already works inside any LangGraph node or
CrewAI tool. The helpers just derive a sensible key from the framework's run context
(thread id / tool call) so you don't hand-roll it, and wrap the effect. The
guarantee is identical to the raw-loop case.

Import the one you need — each lazily imports its framework, so neither is a hard
dependency of exactly-once:

    from exactly_once.integrations.langgraph import once_node
    from exactly_once.integrations.crewai import once_tool_run
"""

from __future__ import annotations
