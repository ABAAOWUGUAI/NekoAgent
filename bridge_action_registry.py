#!/usr/bin/env python3
"""Single source of truth for assistant action contracts.

Actions describe intent-to-execution routing.  Capability manifests remain the
authority for the concrete adapter, permissions and input/output schema.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ActionDefinition:
    action_type: str
    default_intent: str
    execution_lane: str
    capability_id: str
    risk_level: str
    side_effect: bool
    requires_tools: bool
    approval_policy: str
    goal_policy: str
    description: str


ACTION_DEFINITIONS: tuple[ActionDefinition, ...] = (
    ActionDefinition(
        "respond", "chat", "respond", "chat.reply", "none", False, False,
        "none", "none", "Reply without executing a tool.",
    ),
    ActionDefinition(
        "invoke_capability", "research", "invoke_capability", "", "low", False, True,
        "capability", "result", "Invoke one server-owned capability selected at runtime.",
    ),
    ActionDefinition(
        "automation.schedule.create", "automation", "automation.schedule.create",
        "automation.schedule.create", "medium", True, True, "owner_private",
        "result", "Create one durable Owner automation.",
    ),
    ActionDefinition(
        "automation.schedule.update", "automation", "automation.schedule.update",
        "automation.schedule.update", "medium", True, True, "owner_private",
        "result", "Update one durable Owner automation.",
    ),
    ActionDefinition(
        "automation.schedule.disable", "automation", "automation.schedule.disable",
        "automation.schedule.disable", "medium", True, True, "owner_private",
        "result", "Disable one durable Owner automation while retaining its audit history.",
    ),
    ActionDefinition(
        "automation.schedule.run_now", "automation", "automation.schedule.run_now",
        "automation.schedule.run_now", "medium", True, True, "owner_private",
        "result", "Queue one immediate run without changing its schedule.",
    ),
    ActionDefinition(
        "workspace_task", "coding", "workspace_task", "codex.sandbox", "high", True, True,
        "risk_based", "required", "Execute bounded workspace work.",
    ),
    ActionDefinition(
        "broker_operation", "ops", "broker_operation", "", "high", True, True,
        "always", "required", "Request an allowlisted privileged operation.",
    ),
    ActionDefinition(
        "start_task", "coding", "task", "codex.sandbox", "high", True, True,
        "risk_based", "required", "Create a new Goal Run backed by a task.",
    ),
    ActionDefinition(
        "continue_task", "coding", "task", "codex.sandbox", "medium", True, True,
        "risk_based", "existing", "Continue an existing Goal Run.",
    ),
    ActionDefinition(
        "finish_work", "chat", "respond", "chat.reply", "none", False, False,
        "none", "existing", "Acknowledge completion without a new execution.",
    ),
    ActionDefinition(
        "request_clarification", "chat", "respond", "chat.reply", "none", False, False,
        "none", "none", "Ask only for information required to continue safely.",
    ),
    ActionDefinition(
        "propose_memory", "memory", "memory_candidate", "", "low", True, False,
        "scope_based", "none", "Propose scoped memory without silently changing facts.",
    ),
)

_ACTIONS = {item.action_type: item for item in ACTION_DEFINITIONS}
if len(_ACTIONS) != len(ACTION_DEFINITIONS):  # pragma: no cover - import guard
    raise RuntimeError("duplicate_action_type")


def action_types() -> frozenset[str]:
    return frozenset(_ACTIONS)


def action_definition(action_type: str) -> ActionDefinition:
    item = _ACTIONS.get(str(action_type or "").strip())
    if item is None:
        raise KeyError("unknown_action_type")
    return item


def action_contract(action_type: str) -> dict:
    return asdict(action_definition(action_type))


def planner_action_types() -> str:
    return "/".join(item.action_type for item in ACTION_DEFINITIONS)


def validate_action_capabilities(capability_ids: set[str] | frozenset[str]) -> dict:
    missing = sorted(
        item.capability_id
        for item in ACTION_DEFINITIONS
        if item.capability_id and item.capability_id not in capability_ids
    )
    return {
        "ok": not missing,
        "action_count": len(ACTION_DEFINITIONS),
        "missing_capabilities": missing,
    }


__all__ = [
    "ACTION_DEFINITIONS",
    "ActionDefinition",
    "action_contract",
    "action_definition",
    "action_types",
    "planner_action_types",
    "validate_action_capabilities",
]
