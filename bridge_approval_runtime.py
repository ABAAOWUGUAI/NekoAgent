#!/usr/bin/env python3
"""Runtime adapters for legacy and formal approval messages.

This module keeps channel parsing and compatibility writes out of the main
Bridge.  Formal approval state itself remains authoritative in tasks.sqlite3.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from bridge_assistant_identity import current_assistant
from bridge_formal_approval import (
    ApprovalError,
    FormalApprovalRepository,
    formal_approval_feature_enabled,
)
from bridge_platform_repository import PlatformRepository


RISKY_HINTS = (
    "删除", "清空", "格式化", "卸载", "重启", "开放端口", "关闭防火墙",
    "防火墙", "改权限", "chmod", "密钥", "token", "api key", "生产部署",
    "生产环境", "上线", " rm ", "restart", "reboot",
)
EXPLICIT_AUTHORIZATION_HINTS = (
    "确认执行", "我确认", "已确认", "直接执行", "立即执行", "我授权", "风险我知道",
)
FORMAL_APPROVE_RE = re.compile(
    r"^(?:确认执行|确认操作|我确认)\s*#?([a-f0-9]{8}|approval-[a-f0-9]{32})\s*$",
    flags=re.I,
)
FORMAL_REJECT_RE = re.compile(
    r"^(?:拒绝执行|拒绝操作)\s*#?([a-f0-9]{8}|approval-[a-f0-9]{32})(?:\s+(.+))?$",
    flags=re.I,
)


def requires_risky_confirmation(message: str) -> bool:
    text = str(message or "").lower()
    return any(hint in text for hint in RISKY_HINTS)


def has_explicit_authorization(message: str) -> bool:
    text = str(message or "").lower()
    return any(hint in text for hint in EXPLICIT_AUTHORIZATION_HINTS)


def create_legacy_pending(
    connect: Callable[[], sqlite3.Connection],
    user_id: str,
    message: str,
    *,
    trace_id: str = "",
) -> dict:
    approval_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=30)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_approvals(id,user_id,message,trace_id,status,created_at,expires_at)
            VALUES(?,?,?,?,'pending',?,?)
            """,
            (approval_id, user_id, message, trace_id, now.isoformat(), expires.isoformat()),
        )
    return {"id": approval_id, "expires_at": expires.isoformat(), "message": message}


def consume_legacy_pending(
    connect: Callable[[], sqlite3.Connection],
    user_id: str,
    message: str,
    *,
    now: str,
) -> dict | None:
    match = re.match(
        r"^(?:确认执行|确认操作|我确认)\s*#?([a-f0-9]{8})?\s*$",
        str(message or "").strip(),
        flags=re.I,
    )
    if not match:
        return None
    approval_id = str(match.group(1) or "").strip()
    with connect() as conn:
        if approval_id:
            row = conn.execute(
                """
                SELECT * FROM pending_approvals
                WHERE id=? AND user_id=? AND status='pending' AND expires_at>?
                """,
                (approval_id, user_id, now),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM pending_approvals
                WHERE user_id=? AND status='pending' AND expires_at>?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, now),
            ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE pending_approvals SET status='approved',decided_at=? WHERE id=?",
            (now, row["id"]),
        )
        return dict(row)


def formal_feature_enabled(connect: Callable[[], sqlite3.Connection]) -> bool:
    with connect() as conn:
        return formal_approval_feature_enabled(conn)


def create_paused_task_approval(
    assistant_connect: Callable[[], sqlite3.Connection],
    task_connect: Callable[[], sqlite3.Connection],
    task: dict,
    *,
    upsert_task: Callable[[sqlite3.Connection, dict], None],
    task_lookup: Callable[[], dict[str, dict]],
    requested_channel: str,
    requested_by: str,
    target_environment: str,
    action_summary: str,
    ttl_seconds: int = 1800,
) -> dict:
    """Persist Task, Run and Approval atomically before exposing the task."""

    if str(task.get("status") or "") != "waiting_approval":
        raise ApprovalError("approval_task_not_paused")
    with assistant_connect() as assistant_conn:
        assistant = current_assistant(assistant_conn)
        assistant_id = str((assistant or {}).get("id") or "")
    with task_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        upsert_task(conn, task)
        lookup = task_lookup()
        lookup[str(task["id"])] = dict(task)
        projection_result = PlatformRepository(conn).sync_task(task, task_lookup=lookup)
        projection = projection_result.get("projection") or {}
        task["goal_id"] = str(projection.get("goal_id") or "")
        task["run_id"] = str(projection.get("run_id") or "")
        approval = FormalApprovalRepository(conn).create_for_task(
            task,
            goal_id=task["goal_id"],
            run_id=task["run_id"],
            assistant_id=assistant_id,
            requested_channel=requested_channel,
            requested_by=requested_by,
            target_environment=target_environment,
            request_idempotency_key=f"task:{task['id']}:approval:v1",
            action_summary=action_summary,
            ttl_seconds=ttl_seconds,
        )
    return {"task": task, "approval": approval}


def _formal_command(message: str) -> dict | None:
    text = str(message or "").strip()
    match = FORMAL_APPROVE_RE.match(text)
    if match:
        return {"identifier": match.group(1), "decision": "approve", "reason": ""}
    match = FORMAL_REJECT_RE.match(text)
    if match:
        return {
            "identifier": match.group(1),
            "decision": "reject",
            "reason": str(match.group(2) or "").strip()[:1000],
        }
    return None


def decide_formal_message(
    assistant_connect: Callable[[], sqlite3.Connection],
    task_connect: Callable[[], sqlite3.Connection],
    *,
    user_id: str,
    message: str,
    trace_id: str,
    decision_applied: Callable[[dict], None],
) -> dict | None:
    """Apply a strict QQ decision command; ordinary messages return ``None``."""

    command = _formal_command(message)
    if command is None or not formal_feature_enabled(assistant_connect):
        return None
    identifier = str(command["identifier"])
    try:
        with task_connect() as conn:
            repository = FormalApprovalRepository(conn)
            approval = repository.get(identifier, actor_id=user_id)
            if not approval:
                raise ApprovalError("approval_not_found")
            stable_trace = str(trace_id or "").strip()
            if not stable_trace:
                stable_trace = hashlib.sha256(
                    f"{user_id}\n{message}".encode("utf-8"),
                ).hexdigest()[:32]
            result = repository.decide(
                identifier,
                decision=str(command["decision"]),
                expected_version=int(approval["version"]),
                actor_id=user_id,
                channel="qq",
                idempotency_key=f"qq:{stable_trace}",
                reason=str(command["reason"]),
            )
        decision_applied(result)
    except ApprovalError as exc:
        code = str(exc)
        replies = {
            "approval_not_found": "没有找到属于你的待确认操作，可能编号有误或已经失效。",
            "approval_not_pending": "这项操作已经处理过了，不会重复执行。",
            "approval_expired": "这项确认已经过期，任务没有执行；请重新发起需求。",
            "approval_version_conflict": "确认状态刚刚发生变化，请刷新后再处理。",
            "approval_action_changed": "待执行内容已经变化，旧确认已失效；请重新发起确认。",
            "approval_task_state_changed": "任务状态已经变化，旧确认不再有效。",
        }
        return {
            "ok": False,
            "dispatch": "approval_decision_error",
            "error": code,
            "reply": replies.get(code, "这项确认没有生效，任务不会继续执行。"),
        }
    approved = str(command["decision"]) == "approve"
    return {
        "ok": True,
        "dispatch": "approval_decided",
        "reply": "已确认，任务现在进入执行队列。" if approved else "已拒绝，这项任务不会执行。",
        **result,
    }


def sync_runtime_task(
    result: Mapping[str, object],
    task_connect: Callable[[], sqlite3.Connection],
    row_to_task: Callable[[sqlite3.Row], dict],
    tasks: dict[str, dict],
    task_queue,
    task_event,
    task_lock,
) -> None:
    task_id = str(result.get("task_id") or "")
    if not task_id:
        return
    with task_connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return
    task = row_to_task(row)
    with task_lock:
        previous_status = str(tasks.get(task_id, {}).get("status") or "")
        if task.get("status") == "queued":
            if task_id not in tasks or previous_status == "waiting_approval":
                tasks[task_id] = task
                if task_id not in task_queue:
                    task_queue.append(task_id)
                task_event.set()
        else:
            tasks[task_id] = task
            while task_id in task_queue:
                task_queue.remove(task_id)


def formal_expiry_worker(
    assistant_connect: Callable[[], sqlite3.Connection],
    task_connect: Callable[[], sqlite3.Connection],
    decision_applied: Callable[[dict], None],
    interval_seconds: int = 30,
    *,
    health=None,
    stop_event=None,
    log_event: Callable[[dict], None] | None = None,
) -> None:
    worker_id = "approval_expiry"
    failures = 0
    while not (stop_event and stop_event.is_set()):
        if health is not None:
            health.begin(worker_id)
        try:
            if formal_feature_enabled(assistant_connect):
                with task_connect() as conn:
                    expired = FormalApprovalRepository(conn).expire_due()
                for task_id in expired:
                    decision_applied({"task_id": task_id})
            failures = 0
            if health is not None:
                health.success(worker_id)
        except Exception as exc:
            failures += 1
            if health is not None:
                health.failure(worker_id, exc)
            if log_event is not None:
                log_event(
                    {
                        "worker": worker_id,
                        "event": "cycle_failed",
                        "error_type": type(exc).__name__,
                        "consecutive_failures": failures,
                    },
                )
        base_wait = max(5, int(interval_seconds))
        wait_seconds = min(300, base_wait * (2 ** min(failures, 4)))
        if stop_event is not None:
            stop_event.wait(wait_seconds)
        else:
            time.sleep(wait_seconds)


__all__ = [
    "consume_legacy_pending",
    "create_legacy_pending",
    "create_paused_task_approval",
    "decide_formal_message",
    "formal_feature_enabled",
    "formal_expiry_worker",
    "has_explicit_authorization",
    "requires_risky_confirmation",
    "sync_runtime_task",
]
