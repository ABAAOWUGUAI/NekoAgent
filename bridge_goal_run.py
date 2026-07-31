#!/usr/bin/env python3
"""Pure Goal/Run projection rules for the compatibility migration."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Mapping


GOAL_STATUSES = {
    "draft",
    "active",
    "waiting_user",
    "completed",
    "failed",
    "cancelled",
    "superseded",
}
RUN_STATUSES = {
    "queued",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "interrupted",
}
STRATEGIES = {"direct", "grounded", "action", "workflow", "sandbox"}
COMPLETION_POLICIES = {"auto", "user_confirm", "manual"}

LEGACY_RUN_STATUS_MAP = {
    "queued": "queued",
    "running": "running",
    "done": "succeeded",
    "succeeded": "succeeded",
    "failed": "failed",
    "timeout": "timed_out",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "waiting_approval": "waiting_approval",
    "interrupted": "interrupted",
}

GOAL_TRANSITIONS = {
    "draft": {"active", "cancelled", "superseded"},
    "active": {"waiting_user", "completed", "failed", "cancelled", "superseded"},
    "waiting_user": {"active", "completed", "failed", "cancelled", "superseded"},
    "completed": {"active", "superseded"},
    "failed": {"active", "cancelled", "superseded"},
    "cancelled": {"active", "superseded"},
    "superseded": set(),
}
RUN_TRANSITIONS = {
    "queued": {"running", "waiting_approval", "failed", "cancelled", "interrupted"},
    "waiting_approval": {"queued", "running", "cancelled", "failed"},
    "running": {"succeeded", "failed", "timed_out", "cancelled", "interrupted"},
    "succeeded": set(),
    "failed": set(),
    "timed_out": set(),
    "cancelled": set(),
    "interrupted": set(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def legacy_goal_id(root_task_id: str) -> str:
    return _stable_id("goal", f"legacy-task-root:{str(root_task_id).strip()}")


def legacy_run_id(task_id: str) -> str:
    return _stable_id("run", f"legacy-task:{str(task_id).strip()}")


def normalize_legacy_task_status(status: object) -> str:
    return LEGACY_RUN_STATUS_MAP.get(str(status or "").strip().lower(), "failed")


def infer_strategy(task: Mapping[str, object]) -> str:
    explicit = str(task.get("strategy") or "").strip().lower()
    if explicit in STRATEGIES:
        return explicit
    sandbox = str(task.get("sandbox") or "read-only").strip().lower()
    intent = str(task.get("intent") or "").strip().lower()
    mode = str(task.get("mode") or "").strip().lower()
    if sandbox == "workspace-write":
        return "sandbox"
    if intent in {"research", "fresh_info", "lookup", "weather"}:
        return "grounded"
    if intent in {"action", "automation"}:
        return "action"
    if intent in {"code", "ops", "project", "workflow"} or mode == "work":
        return "workflow"
    return "direct"


def infer_completion_policy(task: Mapping[str, object], strategy: str | None = None) -> str:
    explicit = str(task.get("completion_policy") or "").strip().lower()
    if explicit in COMPLETION_POLICIES:
        return explicit
    strategy = strategy or infer_strategy(task)
    return "user_confirm" if strategy in {"action", "workflow", "sandbox"} else "auto"


def goal_status_for_run(run_status: str, completion_policy: str) -> str:
    if run_status in {"queued", "running"}:
        return "active"
    if run_status == "waiting_approval":
        return "waiting_user"
    if run_status == "succeeded":
        return "completed" if completion_policy == "auto" else "waiting_user"
    if run_status == "cancelled":
        return "cancelled"
    return "failed"


def can_transition_goal(current: str, target: str) -> bool:
    return current == target or target in GOAL_TRANSITIONS.get(current, set())


def can_transition_run(current: str, target: str) -> bool:
    return current == target or target in RUN_TRANSITIONS.get(current, set())


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


@dataclass(frozen=True)
class TaskProjection:
    goal_id: str
    run_id: str
    root_task_id: str
    legacy_task_id: str
    source_run_id: str
    actor_id: str
    channel: str
    conversation_ref: str
    title: str
    goal_status: str
    completion_policy: str
    run_status: str
    strategy: str
    capability_id: str
    summary: str
    created_at: str
    updated_at: str
    started_at: str
    finished_at: str
    input_data: dict = field(default_factory=dict)
    output_data: dict = field(default_factory=dict)
    error_data: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def project_legacy_task(
    task: Mapping[str, object],
    *,
    root_task_id: str | None = None,
    goal_id: str | None = None,
    now: str | None = None,
) -> TaskProjection:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise ValueError("task_id_required")
    root_task_id = str(root_task_id or task_id).strip()
    now = now or utc_now()
    strategy = infer_strategy(task)
    completion_policy = infer_completion_policy(task, strategy)
    run_status = normalize_legacy_task_status(task.get("status"))
    source_task_id = str(task.get("source_task_id") or "").strip()
    created_at = str(task.get("created_at") or now)
    updated_at = str(task.get("updated_at") or task.get("finished_at") or task.get("started_at") or created_at)
    title = _clip(task.get("origin_message") or task.get("summary") or "Legacy task", 240)
    summary = _clip(task.get("summary") or task.get("origin_message") or title, 500)
    source_channel = _clip(task.get("source") or "legacy", 80)
    delivery_recipient = _clip(task.get("delivery_recipient_id"), 300)
    is_qq_group = source_channel == "qq" and delivery_recipient.startswith("group:")
    goal_channel = "qq_group" if is_qq_group else source_channel
    conversation_ref = _clip(
        task.get("conversation_ref") or delivery_recipient or task.get("user_id"),
        300,
    )

    input_data = {
        "origin_message": _clip(task.get("origin_message"), 12000),
        "intent": _clip(task.get("intent"), 80),
        "mode": _clip(task.get("mode"), 40),
        "sandbox": _clip(task.get("sandbox"), 40),
        "cwd": _clip(task.get("cwd"), 1000),
        "timeout": task.get("timeout"),
    }
    output_text = task.get("stdout") or task.get("output")
    output_data = {
        "ok": task.get("ok"),
        "returncode": task.get("returncode"),
        "duration": task.get("duration"),
        "result": str(output_text or "")[:200000],
    }
    error_data = {
        "kind": _clip(task.get("error_kind"), 120),
        "message": str(task.get("error") or task.get("stderr") or "")[:50000],
    }
    metadata = {
        "projection": "legacy_task_v1",
        "legacy_status": _clip(task.get("status"), 40),
        "source": _clip(task.get("source"), 80),
        "trace_id": _clip(task.get("trace_id"), 160),
        "delivery_status": _clip(task.get("delivery_status"), 40),
    }

    return TaskProjection(
        goal_id=goal_id or legacy_goal_id(root_task_id),
        run_id=legacy_run_id(task_id),
        root_task_id=root_task_id,
        legacy_task_id=task_id,
        source_run_id=legacy_run_id(source_task_id) if source_task_id else "",
        actor_id=_clip(task.get("user_id"), 200),
        channel=goal_channel,
        conversation_ref=conversation_ref,
        title=title,
        goal_status=goal_status_for_run(run_status, completion_policy),
        completion_policy=completion_policy,
        run_status=run_status,
        strategy=strategy,
        capability_id=_clip(task.get("capability_id") or ("codex.sandbox" if strategy == "sandbox" else "codex.task"), 160),
        summary=summary,
        created_at=created_at,
        updated_at=updated_at,
        started_at=str(task.get("started_at") or ""),
        finished_at=str(task.get("finished_at") or ""),
        input_data=input_data,
        output_data=output_data,
        error_data=error_data,
        metadata=metadata,
    )
