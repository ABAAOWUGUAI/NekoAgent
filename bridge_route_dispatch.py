"""Execute one resolved deterministic request route without model fallback."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bridge_action_registry import action_definition
from bridge_action_execution import (
    build_registered_action_mode_decision,
    execute_registered_action,
)
from bridge_action_commitment import resolve_action_commitment
from bridge_automation_actions import dispatch_automation_action
from bridge_qq_admin_actions import (
    build_qq_control_model_readiness,
    dispatch_qq_admin_action,
)
from bridge_interaction_contract import PLAN_SCHEMA_VERSION, assemble_response, fallback_interaction_plan
from bridge_request_router import initial_route_disposition, route_execution_missing_result, route_metadata


_AUTOMATION_ACTION_TYPES = {
    "automation_create": "automation.schedule.create",
    "automation_update": "automation.schedule.update",
    "automation_disable": "automation.schedule.disable",
    "automation_run_now": "automation.schedule.run_now",
}


def dispatch_committed_action(
    *,
    assistant_connect: Callable[[], Any],
    store: object,
    actor_id: str,
    message: str,
    trace_id: str,
    source: str,
    inbound_context: Mapping[str, object],
    commitment: Mapping[str, object] | None,
    action_commitments: object,
    automation_preflight: Callable[[dict], dict],
    get_fallback: Callable[[], dict],
    get_role_settings: Callable[[str, dict], dict],
    readiness_check: Callable[[dict], tuple[bool, str]],
) -> dict | None:
    """Execute one accepted private Owner Commitment through the registry."""

    item = dict(commitment or {})
    action = item.get("action")
    if (
        not isinstance(action, Mapping)
        or not str(item.get("id") or "")
        or str(source or "") not in {"qq", "qq_private", "private"}
        or str((inbound_context or {}).get("group_id") or "")
    ):
        return None
    if str(item.get("state") or "") == "declined":
        mode_decision = {
            "mode": "daily",
            "intent": "chat",
            "confidence": 1.0,
            "reason": "Owner 明确撤回了尚未执行的操作承诺。",
            "need_tools": False,
            "emotion": "neutral",
        }
        mode_decision["interaction_plan"] = fallback_interaction_plan(message, mode_decision)
        plan_record = store.persist(actor_id, mode_decision, source=source)
        reply = "好的，这项操作已取消，本次没有执行任何配置变更。"
        store.record_exchange(
            actor_id,
            message,
            reply,
            mode_decision,
            source=source,
            inbound_context=dict(inbound_context or {}),
        )
        return {
            "ok": True,
            "dispatch": "action_commitment_declined",
            "reply": reply,
            "mode": "daily",
            "intent": "chat",
            "mode_decision": mode_decision,
            "interaction_plan": mode_decision["interaction_plan"],
            "interaction_plan_record": plan_record,
            "action_commitment": item,
        }
    execution_action = dict(action)
    # Batch admission removal is executable only after the repository has
    # recorded the exact accepted Owner commitment.  The executor rechecks it
    # against this marker before touching settings.
    execution_action["_action_commitment_id"] = str(item["id"])
    try:
        result = execute_registered_action(
            assistant_connect,
            actor_id=actor_id,
            action=execution_action,
            trace_id=trace_id,
            automation_preflight=automation_preflight,
            qq_model_readiness=lambda: build_qq_control_model_readiness(
                get_fallback, get_role_settings, readiness_check,
            ),
        )
        mode_decision = build_registered_action_mode_decision(action)
    except (KeyError, TypeError, ValueError):
        return None
    plan_record = store.persist(actor_id, mode_decision, source=source)
    store.record_exchange(
        actor_id,
        message,
        str(result.get("reply") or ""),
        mode_decision,
        source=source,
        inbound_context=dict(inbound_context or {}),
    )
    receipts = result.get("action_receipts")
    receipt = next((dict(value) for value in receipts or [] if isinstance(value, Mapping)), None)
    marked = action_commitments.mark_execution(
        str(item["id"]),
        actor_id=actor_id,
        thread_ref=f"qq:private:{actor_id}",
        receipt=receipt,
    )
    result.update({
        "mode": "work",
        "intent": mode_decision["intent"],
        "mode_decision": mode_decision,
        "interaction_plan": mode_decision["interaction_plan"],
        "interaction_plan_record": plan_record,
        "action_commitment": marked or {"id": str(item["id"]), "state": "failed"},
    })
    return result


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
        try:
            definition = action_definition(action_type)
        except KeyError:
            # Keep the plan serializable so the execution lane can emit a
            # typed fail-closed receipt instead of crashing on model input.
            definition = None
        plan_action_type = action_type if definition is not None else "respond"
        intent_id = f"intent-{index}"
        action_id = f"action-{index}"
        intents.append(
            {
                "id": intent_id,
                "type": intent_type,
                "confidence": 1.0,
                "objective": f"执行 {raw_action} 的服务端动作",
                "requires_tools": True if definition is None else bool(definition.requires_tools),
                "risk_level": "high" if definition is None else definition.risk_level,
            },
        )
        actions.append(
            {
                "id": action_id,
                # Interaction Plan normalization only accepts registered
                # action types.  Preserve the raw type in the execution
                # receipt, but persist a registered no-op plan action when
                # the candidate is unsupported so the route fails closed.
                "type": plan_action_type,
                "intent_id": intent_id,
                "objective": f"执行 {raw_action} 并取得 ActionReceipt",
                "requires_tools": True if definition is None else bool(definition.requires_tools),
                "risk_level": "high" if definition is None else definition.risk_level,
                "depends_on": [],
            },
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "summary_mode": "mixed",
        "primary_intent": str(intents[0]["type"] if intents else "chat"),
        "confidence": 1.0,
        "reason": "多个当前消息明确提出的领域动作由一个 Interaction Plan 组合。",
        "affect": {
            "expression_present": False,
            "kind": "neutral",
            "confidence": 0.0,
            "intensity": "low",
        },
        "intents": intents,
        # Composite work must keep a user-visible conversational handoff.  The
        # action receipts remain independent, but the social/chat portion of a
        # mixed turn cannot disappear merely because every candidate is work.
        "reply_parts": [
            {
                "id": "reply-social-ack",
                "type": "social_ack",
                "text": "收到，我先把这几件事分别处理。",
                "styleable": True,
            },
        ],
        "actions": actions,
        "approval_requests": [],
        "memory_candidates": [],
    }


def _composite_action_failure(
    candidate: Mapping[str, object],
    *,
    reason: str = "action_execution_failed",
) -> dict:
    """Return a stable, redacted receipt when one candidate raises."""

    raw_action = str(candidate.get("action_type") or "respond")
    action_type = _AUTOMATION_ACTION_TYPES.get(raw_action, raw_action)
    return {
        "ok": False,
        "dispatch": "composite_action_failed",
        "reply": "这一项动作执行失败了，但我会继续处理同一条消息里的其他事项。",
        "action_receipts": [
            {
                "action_type": action_type,
                "status": "failed",
                "facts": {
                    "stage": "action_registry",
                    "reason": reason,
                },
            },
        ],
    }


def _dispatch_composite_route(
    *,
    decision: Mapping[str, object],
    assistant_connect: Callable[[], Any],
    store: object,
    actor_id: str,
    message: str,
    history: list[dict],
    trace_id: str,
    source: str,
    inbound_context: dict,
    automation_preflight: Callable[[dict], dict],
    get_fallback: Callable[[], dict],
    get_role_settings: Callable[[str, dict], dict],
    readiness_check: Callable[[dict], tuple[bool, str]],
) -> dict:
    """Execute explicit independent candidates while keeping one plan/receipt lane."""

    mode_decision = {"interaction_plan": _composite_mode_decision(decision)}
    plan_record = store.persist(actor_id, mode_decision, source=source)
    component_results = []
    for candidate in decision.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        domain = str(candidate.get("domain") or "")
        try:
            raw_parameters = candidate.get("parameters")
            if raw_parameters is None:
                action = {}
            elif not isinstance(raw_parameters, Mapping):
                raise ValueError("action_parameters_invalid")
            else:
                action = dict(raw_parameters)
        except Exception:
            component_results.append(
                _composite_action_failure(candidate, reason="action_parameters_invalid"),
            )
            continue
        try:
            raw_action = str(candidate.get("action_type") or "respond")
            try:
                action_definition(_AUTOMATION_ACTION_TYPES.get(raw_action, raw_action))
            except KeyError:
                result = _composite_action_failure(candidate, reason="action_type_unsupported")
                component_results.append(result)
                continue
            action["action_type"] = _AUTOMATION_ACTION_TYPES.get(raw_action, raw_action)
            result = execute_registered_action(
                assistant_connect,
                actor_id=actor_id,
                action=action,
                trace_id=trace_id,
                automation_preflight=automation_preflight,
                qq_model_readiness=lambda: build_qq_control_model_readiness(
                    get_fallback, get_role_settings, readiness_check,
                ),
            )
        except Exception:
            # Never expose exception text or abort independent candidates.
            result = _composite_action_failure(candidate)
        component_results.append(result or {"ok": False, "dispatch": "composite_no_result"})

    replies = [str(item.get("reply") or "").strip() for item in component_results if isinstance(item, dict)]
    receipts = [
        receipt
        for item in component_results
        if isinstance(item, dict)
        for receipt in (item.get("action_receipts") or [])
        if isinstance(receipt, dict)
    ]
    factual_reply = "\n\n".join(item for item in replies if item)
    content_blocks, reply = assemble_response(
        mode_decision["interaction_plan"],
        factual_reply or "本轮没有可交付的动作结果。",
        factual_type="status",
    )
    store.record_exchange(
        actor_id,
        message,
        reply,
        mode_decision,
        source=source,
        inbound_context=inbound_context,
    )
    return {
        "ok": all(bool(item.get("ok", False)) for item in component_results if isinstance(item, dict)),
        "dispatch": "composite_route",
        "reply": reply,
        "content_blocks": content_blocks,
        "action_receipts": receipts,
        "component_dispatches": [
            str(item.get("dispatch") or "") for item in component_results if isinstance(item, dict)
        ],
        "mode": "work",
        "intent": "automation" if any(
            str(item.get("domain") or "") == "automation"
            for item in (decision.get("candidates") or [])
            if isinstance(item, Mapping)
        ) else "ops",
        "mode_decision": mode_decision,
        "interaction_plan": mode_decision["interaction_plan"],
        "interaction_plan_record": plan_record,
    }


def dispatch_deterministic_route(
    *,
    assistant_connect: Callable[[], Any],
    store: object,
    actor_id: str,
    message: str,
    history: list[dict],
    trace_id: str,
    source: str,
    inbound_context: dict,
    automation_preflight: Callable[[dict], dict],
    resolve_automation_target: Callable[[str, dict], dict],
    get_fallback: Callable[[], dict],
    get_role_settings: Callable[[str, dict], dict],
    readiness_check: Callable[[dict], tuple[bool, str]],
    action_commitments: object | None = None,
    before_effect: Callable[[], object] | None = None,
) -> tuple[dict | None, dict]:
    """Return an executed route result or the decision for generic planning.

    The caller owns channel delivery and event signalling.  This function owns
    only route arbitration; each domain executor still performs authorization,
    approval and its own durable write.
    """

    group_id = str(inbound_context.get("group_id") or "")
    if action_commitments is not None and not group_id and str(source or "") in {"qq", "qq_private", "private"}:
        commitment = resolve_action_commitment(
            action_commitments,
            actor_id=actor_id,
            thread_ref=f"qq:private:{actor_id}",
            message=message,
            before_transition=before_effect,
        )
        if commitment is not None:
            committed = dispatch_committed_action(
                assistant_connect=assistant_connect,
                store=store,
                actor_id=actor_id,
                message=message,
                trace_id=trace_id,
                source=source,
                inbound_context=inbound_context,
                commitment=commitment,
                action_commitments=action_commitments,
                automation_preflight=automation_preflight,
                get_fallback=get_fallback,
                get_role_settings=get_role_settings,
                readiness_check=readiness_check,
            )
            if committed is not None:
                decision = {
                    "status": "matched",
                    "domain": "action_commitment",
                    "action_type": str(commitment.get("action_type") or ""),
                    "reason": "owner_action_commitment_resolved",
                }
                committed["route_decision"] = decision
                committed["route_metadata"] = route_metadata(decision)
                return committed, decision
    decision, blocked = initial_route_disposition(message, history, current_group_id=group_id)
    if blocked is not None:
        return blocked, decision
    if decision.get("status") == "mixed":
        if callable(before_effect):
            before_effect()
        result = _dispatch_composite_route(
            decision=decision,
            assistant_connect=assistant_connect,
            store=store,
            actor_id=actor_id,
            message=message,
            history=history,
            trace_id=trace_id,
            source=source,
            inbound_context=inbound_context,
            automation_preflight=automation_preflight,
            get_fallback=get_fallback,
            get_role_settings=get_role_settings,
            readiness_check=readiness_check,
        )
        result["route_decision"] = decision
        result["route_metadata"] = route_metadata(decision)
        return result, decision
    if decision.get("status") == "resolved" and callable(before_effect):
        before_effect()
    result = dispatch_automation_action(
        assistant_connect, store, actor_id, message, history, trace_id, source, group_id,
        preflight=automation_preflight, inbound_context=inbound_context,
        resolve_target=resolve_automation_target,
    )
    if result is None:
        result = dispatch_qq_admin_action(
            assistant_connect, store, actor_id, message, history, trace_id, source,
            get_fallback, get_role_settings, readiness_check, current_group_id=group_id,
            action_commitments=action_commitments,
        )
    if result is None:
        result = route_execution_missing_result(decision)
    if result is not None:
        result["route_decision"] = decision
        result["route_metadata"] = route_metadata(decision)
    return result, decision


__all__ = ["dispatch_committed_action", "dispatch_deterministic_route"]
