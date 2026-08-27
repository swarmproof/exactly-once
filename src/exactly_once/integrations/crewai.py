"""CrewAI integration — guard a tool's side-effect with `once` (v0.2, G5).

A CrewAI tool is a ``BaseTool`` with a ``_run`` method. ``once_tool_run`` decorates
that method so the tool's effect fires at most once per key, defaulting the key to
the tool name plus a hash of its arguments.

    from exactly_once import Store, current_key
    from exactly_once.integrations.crewai import once_tool_run
    from crewai.tools import BaseTool

    store = Store.sqlite("effects.db")

    class ChargeTool(BaseTool):
        name: str = "charge"
        description: str = "Charge a customer once."

        @once_tool_run(store, key=lambda self, order_id, **_: f"charge:{order_id}")
        def _run(self, order_id: str, amount: int) -> str:
            return stripe.charge(order_id, amount, idempotency_key=current_key())

Key on business identity for money-movement (as above). Semantics match the raw
``@once`` — this is only ergonomics.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from functools import wraps
from typing import Any

from ..core import OnStoreDown, once
from ..policies import Policy, quarantine

ToolRun = Callable[..., Any]


def once_tool_run(
    store: Any,
    *,
    key: str | Callable[..., str] | None = None,
    policy: Policy = quarantine,
    lease_ttl: float | None = None,
    on_store_down: OnStoreDown = "fail",
) -> Callable[[ToolRun], ToolRun]:
    """Wrap a CrewAI ``BaseTool._run`` so its side-effect is guarded by :func:`once`.

    Default key is ``f"{tool.name}:{hash(args, kwargs)}"``; pass ``key=`` (a static
    string or a ``(self, *args, **kwargs) -> str`` callable) to key on business
    identity, recommended for money-movement.
    """

    def decorator(run_method: ToolRun) -> ToolRun:
        @wraps(run_method)
        def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            k = _resolve_key(key, self, args, kwargs)
            guard = once(
                store, key=k, policy=policy, lease_ttl=lease_ttl, on_store_down=on_store_down
            )

            @guard
            def effect() -> Any:
                return run_method(self, *args, **kwargs)

            return effect()

        return wrapped

    return decorator


def _resolve_key(
    key: str | Callable[..., str] | None, tool: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str:
    if callable(key):
        return key(tool, *args, **kwargs)
    if key is not None:
        return key
    name = getattr(tool, "name", type(tool).__name__)
    payload = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    return f"{name}:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"
