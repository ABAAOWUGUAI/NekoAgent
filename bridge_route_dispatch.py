"""Execute one resolved deterministic request route without model fallback."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bridge_action_registry import action_definition
from bridge_automation_actions import dispatch_automation_action, execute_automation_action
from bridge_qq_admin_actions import (
    build_qq_control_model_readiness,
    dispatch_qq_admin_action,
    execute_qq_admin_action,
)
from bridge_interaction_contract import PLAN_SCHEMA_VERSION
from bridge_request_router import initial_route_disposition, route_execution_missing_result, route_metadata


_AUTOMATION_ACTION_TYPES = {
    "automation_create": "automation.schedule.create",
    "automation_update": "automation.schedule.update",
    "automation_disable": "automation.schedule.disable",
    "automation_run_now": "automation.schedule.run_now",
}


def _composite_mode_decision(decision: Mapping[str, object]) -> dict:
    """Build one validated Interaction Plan for explicit multi-domain work."""

    intents = []
    actions = []
    for index, candidate in enumerate(decision.get("candidates") or [], start=1):
        if not isinstance(candidate, Mapping):
            continue
        domain = str(candidate.get("domain") or "")
        intent_type = "automation" if domain == "automation" else "ops"
        raw_action = str(candidate.get("action_type") or "respond")
        action_type = _AUTOMATION_ACTION_TYPES.get(raw_action, raw_action)
        definition = action_definition(action_type)
        intent_id = f"intent-{index}"
        action_id = f"action-{index}"
        intents.append({
            "id": intent_id,
            "type": intent_type,
            "confidence": 1.0,
            "objective": f"执行 {raw_action} 的服务端动作",
            "requires_tools": bool(definition.requires_tools),
            "risk_level": definition.risk_level,
        })
        actions.append({
            "id": action_id,
            "type": action_type,
            "intent_id": intent_id,
            "objective": f"执行 {raw_action} 并取得 ActionReceipt",
            "requires_tools": bool(definition.requires_tools),
            "risk_level": definition.risk_level,
            "depends_on": [],
        })
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "summary_mode": "mixed",
        "primary_intent": str(intents[0]["type"] if intents else "chat"),
        "confidence": 1.0,
        "reason": "多个当前消息明确提出的领域动作由一个 Interaction Plan 组合。",
        "affect": {"expression_present": False, "kind": "neutral", "confidence": 0.0, "intensity": "low"},
        "intents": intents,
        "reply_parts": [],
        "actions": actions,
        "approval_requests": [],
        "memory_candidates": [],
    }


def _dispatch_composite_route(
    *, decision: Mapping[str, object], assistant_connect: Callable[[], Any], store: object,
    actor_id: str, message: str, history: list[dict], trace_id: str, source: str,
    inbound_context: dict, automation_preflight: Callable[[dict], dict],
    get_fallback: Callable[[], dict], get_role_settings: Callable[[str, dict], dict],
    readiness_check: Callable[[dict], tuple[bool, str]],
) -> dict:
    """Execute explicit independent candidates through one plan/receipt lane."""

    mode_decision = {"interaction_plan": _composite_mode_decision(decision)}
    plan_record = store.persist(actor_id, mode_decision, source=source)
    component_results = []
    for candidate in decision.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        domain = str(candidate.get("domain") or "")
        action = dict(candidate.get("parameters") or {})
        if domain == "automation":
            result = execute_automation_action(
                assistant_connect, actor_id=actor_id, action=action,
                trace_id=trace_id, preflight=automation_preflight,
            )
        elif domain == "qq":
            result = execute_qq_admin_action(
                assistant_connect, actor_id=actor_id, action=action,
                trace_id=trace_id,
                model_readiness=lambda: build_qq_control_model_readiness(
                    get_fallback, get_role_settings, readiness_check,
                ),
            )
        else:
            result = {"ok": False, "dispatch": "composite_unsupported_domain", "reply": "本轮动作域不受支持。"}
        component_results.append(result or {"ok": False, "dispatch": "composite_no_result"})
    replies = [str(item.get("reply") or "").strip() for item in component_results if isinstance(item, dict)]
    receipts = [
        receipt for item in component_results if isinstance(item, dict)
        for receipt in (item.get("action_receipts") or []) if isinstance(receipt, dict)
    ]
    reply = "\n\n".join(item for item in replies if item)
    store.record_exchange(actor_id, message, reply, mode_decision, source=source, inbound_context=inbound_context)
    return {
        "ok": all(bool(item.get("ok", False)) for item in component_results if isinstance(item, dict)),
        "dispatch": "composite_route",
        "reply": reply,
        "action_receipts": receipts,
        "component_dispatches": [str(item.get("dispatch") or "") for item in component_results if isinstance(item, dict)],
        "mode": "work",
        "intent": "automation" if any(
            str(item.get("domain") or "") == "automation"
            for item in (decision.get("candidates") or []) if isinstance(item, Mapping)
        ) else "ops",
        "mode_decision": mode_decision,
        "interaction_plan": mode_decision["interaction_plan"],
        "interaction_plan_record": plan_record,
    }


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
    if decision.get("status") == "mixed":
        result = _dispatch_composite_route(
            decision=decision, assistant_connect=assistant_connect, store=store,
            actor_id=actor_id, message=message, history=history, trace_id=trace_id,
            source=source, inbound_context=inbound_context,
            automation_preflight=automation_preflight,
            get_fallback=get_fallback, get_role_settings=get_role_settings,
            readiness_check=readiness_check,
        )
        result["route_decision"] = decision
        result["route_metadata"] = route_metadata(decision)
        return result, decision
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
