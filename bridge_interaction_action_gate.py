#!/usr/bin/env python3
"""Fail-closed bridge from model-proposed actions to server-owned executors."""

from __future__ import annotations


def planned_automation_action_types(mode_decision: dict) -> list[str]:
    """Return Automation actions proposed by an Interaction Plan."""

    plan = mode_decision.get("interaction_plan")
    if not isinstance(plan, dict):
        return []
    result: list[str] = []
    for item in plan.get("actions") or []:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type") or "").strip()
        if action_type.startswith("automation.schedule.") and action_type not in result:
            result.append(action_type)
    return result


def gate_unvalidated_automation_actions(
    store: object,
    *,
    user_id: str,
    message: str,
    mode_decision: dict,
    interaction_plan_record: dict | None,
    source: str,
    inbound_context: dict | None,
) -> dict | None:
    """Block model-only Automation proposals that missed server validation."""

    action_types = planned_automation_action_types(mode_decision)
    if not action_types:
        return None
    reply = (
        "我识别到你可能要操作定时任务，但本轮还没有通过服务端的对象绑定与动作校验，"
        "所以没有创建、修改、停用或触发任何任务。请引用对应的任务回执，或明确说明任务名称和动作。"
    )
    receipts = [
        {"action_type": item, "status": "blocked", "facts": {"reason": "server_validation_required"}}
        for item in action_types
    ]
    store.record_exchange(
        user_id, message, reply, mode_decision, source=source, inbound_context=inbound_context,
        exchange_metadata={
            "automation_action_types": action_types,
            "automation_validation": "blocked",
        },
    )
    return {
        "ok": True,
        "dispatch": "automation_validation_required",
        "reply": reply,
        "output": reply,
        "action_receipts": receipts,
        "intent": "automation",
        "mode": mode_decision.get("mode"),
        "mode_decision": mode_decision,
        "interaction_plan": mode_decision.get("interaction_plan"),
        "interaction_plan_record": interaction_plan_record,
    }


def gate_actions(
    store: object,
    lock: object,
    user_id: str,
    message: str,
    mode_decision: dict,
    source: str,
    inbound_context: dict | None,
) -> tuple[dict | None, dict | None]:
    """Persist one plan, then apply server-owned action gates before dispatch."""

    record = store.persist(user_id, mode_decision, source=source)
    with lock:
        result = gate_unvalidated_automation_actions(
            store, user_id=user_id, message=message, mode_decision=mode_decision,
            interaction_plan_record=record, source=source, inbound_context=inbound_context,
        )
    return record, result


__all__ = [
    "gate_unvalidated_automation_actions",
    "gate_actions",
    "planned_automation_action_types",
]
