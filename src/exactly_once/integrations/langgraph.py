"""LangGraph integration — guard a node's side-effect with `once` (v0.2, G5).

A LangGraph node is just a callable ``(state)`` or ``(state, config)``. ``once_node``
wraps one so its effect fires at most once per key, defaulting the key to the run's
``thread_id`` plus the node name — so re-invoking the graph on the same thread (a
retry, a resume) doesn't re-fire the node's effect.

    from exactly_once import Store
    from exactly_once.integrations.langgraph import once_node

    store = Store.sqlite("effects.db")

    @once_node(store)
    def charge(state, config):
        stripe.charge(state["customer"], state["amount"])
        return {"charged": True}

    graph.add_node("charge", charge)

Override the key for business identity: ``once_node(store, key=lambda state, config:
f"charge:{state['order_id']}")``. Semantics match the raw ``@once`` — this is only
ergonomics.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any

from ..core import OnStoreDown, once
from ..policies import Policy, quarantine

# A LangGraph node: (state) or (state, config) -> state update.
Node = Callable[..., Any]
# A key extractor over (state, config).
NodeKey = Callable[[Any, Any], str]


def _thread_id(config: Any) -> str | None:
    if isinstance(config, dict):
        cfg = config.get("configurable")
        if isinstance(cfg, dict):
            tid = cfg.get("thread_id")
            return str(tid) if tid is not None else None
    return None


def once_node(
    store: Any,
    *,
    key: str | NodeKey | None = None,
    policy: Policy = quarantine,
    lease_ttl: float | None = None,
    on_store_down: OnStoreDown = "fail",
) -> Callable[[Node], Node]:
    """Wrap a LangGraph node so its side-effect is guarded by :func:`once`.

    Default key is ``f"{thread_id}:{node_name}"`` (from the run's ``config``); pass
    ``key=`` (a static string or a ``(state, config) -> str`` callable) to key on
    business identity, which is recommended for money-movement.
    """

    def decorator(node_fn: Node) -> Node:
        node_name = getattr(node_fn, "__name__", "node")
        wants_config = len(inspect.signature(node_fn).parameters) >= 2

        @wraps(node_fn)
        def wrapped(state: Any, config: Any = None) -> Any:
            k = _resolve_key(key, state, config, node_name)
            guard = once(
                store, key=k, policy=policy, lease_ttl=lease_ttl, on_store_down=on_store_down
            )

            @guard
            def effect() -> Any:
                return node_fn(state, config) if wants_config else node_fn(state)

            return effect()

        return wrapped

    return decorator


def _resolve_key(key: str | NodeKey | None, state: Any, config: Any, node_name: str) -> str:
    if callable(key):
        return key(state, config)
    if key is not None:
        return key
    tid = _thread_id(config)
    if tid is None:
        raise ValueError(
            f"once_node on {node_name!r} could not derive a key: no thread_id in the run "
            "config. Invoke the graph with config={'configurable': {'thread_id': ...}}, or "
            "pass key=... (a string or a (state, config) -> str callable)."
        )
    return f"{tid}:{node_name}"
