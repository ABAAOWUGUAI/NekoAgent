#!/usr/bin/env python3
"""Registry-lane adapter for existing deterministic action executors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import sqlite3

from bridge_action_registry import action_definition
from bridge_automation_actions import execute_automation_action
from bridge_interaction_contract import PLAN_SCHEMA_VERSION
from bridge_qq_admin_actions import execute_qq_admin_action


_AUTOMATION_ACTION_TYPES = {
    "automation.schedule.create": "automation_create",
    "automation.schedule.update": "automation_update",
    "automation.schedule.disable": "automation_disable",
    "automation.schedule.run_now": "automation_run_now",
}
_QQ_LANES = frozenset({
    "qq.group.policy.clone",
    "qq.group.allowlist.enable",
    "qq.group.allowlist.disable",
    "qq.group.allowlist.disable_all",
    "qq.group.allowlist.list",
    "qq.group.status.read",
    "qq.group.diagnose",
})
_INTENT_BY_DEFAULT = {"coding": "code"}


def _failed(action_type: str, reason: str) -> dict:
    return {
        "ok": False,
        "dispatch": "registered_action_unsupported",
        "reply": "这项操作没有进入可执行通道，因此没有把它说成已开始或已完成。",
        "action_receipts": [{
            "action_type": str(action_type or "invoke_capability"),
            "status": "failed",
            "facts": {"reason": str(reason or "execution_lane_unavailable")},
        }],
    }


def execute_registered_action(
    connect: Callable[[], sqlite3.Connection],
    *,
    actor_id: str,
    action: Mapping[str, object],
    trace_id: str = "",
    automation_preflight: Callable[[dict], dict] | None = None,
    qq_model_readiness: Callable[[], dict] | None = None,
) -> dict:
    """Delegate one registered action through its declared execution lane."""

    payload = dict(action)
    action_type = str(payload.get("action_type") or "")
    try:
        definition = action_definition(action_type)
    except KeyError:
        return _failed(action_type, "action_type_unregistered")
    lane = definition.execution_lane
    if lane in _AUTOMATION_ACTION_TYPES:
        delegated = dict(payload)
        delegated["action_type"] = _AUTOMATION_ACTION_TYPES[lane]
        return execute_automation_action(
            connect,
            actor_id=actor_id,
            action=delegated,
            trace_id=trace_id,
            preflight=automation_preflight,
        )
    if lane in _QQ_LANES:
        return execute_qq_admin_action(
            connect,
            actor_id=actor_id,
            action=payload,
            trace_id=trace_id,
            model_readiness=qq_model_readiness,
        )
    return _failed(action_type, "execution_lane_unmapped")


def build_registered_action_mode_decision(action: Mapping[str, object]) -> dict:
    """Build a factual Interaction Plan without granting execution authority."""

    action_type = str(action.get("action_type") or "")
    definition = action_definition(action_type)
    intent = _INTENT_BY_DEFAULT.get(definition.default_intent, definition.default_intent)
    if intent not in {"chat", "ops", "code", "research", "analysis", "memory", "automation", "meta"}:
        intent = "ops"
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "summary_mode": "work",
        "primary_intent": intent,
        "confidence": 1.0,
        "reason": "已确认的用户承诺由注册动作通道执行，并返回服务端回执。",
        "affect": {
            "expression_present": False,
            "kind": "neutral",
            "confidence": 0.0,
            "intensity": "low",
        },
        "intents": [{
            "id": "intent-1",
            "type": intent,
            "confidence": 1.0,
            "objective": definition.description,
            "requires_tools": definition.requires_tools,
            "risk_level": definition.risk_level,
        }],
        "reply_parts": [],
        "actions": [{
            "id": "action-1",
            "type": action_type,
            "intent_id": "intent-1",
            "objective": definition.description,
            "requires_tools": definition.requires_tools,
            "risk_level": definition.risk_level,
            "depends_on": [],
        }],
        "approval_requests": [],
        "memory_candidates": [],
    }
    return {
        "mode": "work",
        "intent": intent,
        "confidence": 1.0,
        "reason": plan["reason"],
        "source": "action_commitment_router",
        "interaction_plan": plan,
    }


__all__ = ["build_registered_action_mode_decision", "execute_registered_action"]
