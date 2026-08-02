"""Execute one resolved deterministic request route without model fallback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bridge_automation_actions import dispatch_automation_action
from bridge_qq_admin_actions import dispatch_qq_admin_action
from bridge_request_router import initial_route_disposition, route_execution_missing_result, route_metadata


def dispatch_deterministic_route(
    *, assistant_connect: Callable[[], Any], store: object, actor_id: str,
    message: str, history: list[dict], trace_id: str, source: str,
    inbound_context: dict, automation_preflight: Callable[[dict], dict],
    resolve_automation_target: Callable[[str, dict], dict],
    get_fallback: Callable[[], dict], get_role_settings: Callable[[str, dict], dict],
    readiness_check: Callable[[dict], tuple[bool, str]],
) -> tuple[dict | None, dict]:
    """Return an executor result or a decision for generic planning.

    Authorization, approval and durable writes remain in each domain executor.
    """

    group_id = str(inbound_context.get("group_id") or "")
    decision, blocked = initial_route_disposition(message, history, current_group_id=group_id)
    if blocked is not None:
        return blocked, decision
    result = dispatch_automation_action(
        assistant_connect, store, actor_id, message, history, trace_id, source, group_id,
        preflight=automation_preflight, inbound_context=inbound_context,
        resolve_target=resolve_automation_target,
    )
    if result is None:
        result = dispatch_qq_admin_action(
            assistant_connect, store, actor_id, message, history, trace_id, source,
            get_fallback, get_role_settings, readiness_check, current_group_id=group_id,
        )
    if result is None:
        result = route_execution_missing_result(decision)
    if result is not None:
        result["route_decision"] = decision
        result["route_metadata"] = route_metadata(decision)
    return result, decision


__all__ = ["dispatch_deterministic_route"]
