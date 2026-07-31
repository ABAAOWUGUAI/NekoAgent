#!/usr/bin/env python3
"""Pure routing policy for deciding whether a turn needs a work executor."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping


_WRITE_HINTS = (
    "改", "修改", "修复", "优化", "上线", "部署", "安装", "配置", "写入", "创建", "新增", "重构", "删除",
    "update", "fix", "deploy", "install", "write", "create", "refactor",
)
_LONG_HINTS = ("深度", "详细", "全面", "全部", "整体", "调研", "排查", "优化", "部署", "上线")
_NEW_TASK_HINTS = ("新任务", "另一个任务", "单独开", "另外开", "new task", "separate task")


def dispatch_sandbox(message: str, intent: str) -> str:
    text = (message or "").lower()
    if intent == "code" and any(hint in text for hint in _WRITE_HINTS):
        return "workspace-write"
    return "workspace-write" if any(hint in text for hint in _WRITE_HINTS) else "read-only"


def dispatch_timeout(
    message: str,
    sandbox: str,
    raw_timeout: int | None = None,
    *,
    work_task_timeout: int,
) -> int:
    if raw_timeout:
        return max(60, min(int(raw_timeout), 900))
    default = work_task_timeout if sandbox == "workspace-write" or any(
        hint in (message or "") for hint in _LONG_HINTS
    ) else 300
    return max(60, min(default, 900))


def should_dispatch_as_task(
    message: str,
    mode_decision: Mapping[str, object],
    force: str = "auto",
    *,
    detect_intent: Callable[[str], str] | None = None,
) -> bool:
    force = (force or "auto").strip().lower()
    if force == "task":
        return True
    if force == "chat":
        return False
    if str(mode_decision.get("execution_lane") or "") in {
        "respond", "invoke_capability", "automation.schedule.create", "broker_operation",
    } or mode_decision.get("end_work"):
        return False
    plan = mode_decision.get("interaction_plan") or {}
    if isinstance(plan, Mapping) and any(
        isinstance(item, Mapping)
        and item.get("type") in {"start_task", "continue_task"}
        and bool(item.get("requires_tools"))
        for item in plan.get("actions") or []
    ):
        return True
    mode = str(mode_decision.get("mode") or "daily")
    intent = str(mode_decision.get("intent") or (detect_intent or (lambda _message: "chat"))(message))
    return mode in {"work", "mixed"} and bool(mode_decision.get("need_tools")) and bool(intent)


def new_task_requested(message: str) -> bool:
    text = message or ""
    lowered = text.lower()
    return any(hint in lowered or hint in text for hint in _NEW_TASK_HINTS)


def pending_messages(raw: str | None) -> list[dict]:
    try:
        items = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def active_qq_task(tasks: Mapping[str, dict], lock, source: str, user_id: str) -> dict | None:
    with lock:
        candidates = [
            task for task in tasks.values()
            if task.get("source") == source
            and str(task.get("user_id") or "") == user_id
            and task.get("status") in {"queued", "running"}
        ]
        candidates.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return candidates[0] if candidates else None


__all__ = [
    "dispatch_sandbox",
    "dispatch_timeout",
    "active_qq_task",
    "new_task_requested",
    "pending_messages",
    "should_dispatch_as_task",
]
