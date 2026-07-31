#!/usr/bin/env python3
"""Project final QQ task results into the unified Delivery Outbox."""

from __future__ import annotations


FINAL_STATUSES = {"done", "failed", "timeout", "cancelled"}
TERMINAL_DELIVERY_STATES = {"none", "sent", "skipped", "failed"}


def enqueue_task_result(outbox, task: dict, projection: dict | None, *, sessions, public_task, trim_output):
    if task.get("status") not in FINAL_STATUSES:
        return None
    if task.get("source") != "qq" or not str(task.get("user_id") or "").strip():
        return None
    if str(task.get("delivery_status") or "pending") in TERMINAL_DELIVERY_STATES:
        return None
    projection = projection or {}
    projected = projection.get("projection") if isinstance(projection.get("projection"), dict) else {}
    goal_id = str(task.get("goal_id") or projected.get("goal_id") or "")
    run_id = str(task.get("run_id") or projected.get("run_id") or "")
    actor_id = str(task.get("user_id") or "").strip()
    user_id = str(task.get("delivery_recipient_id") or actor_id).strip()
    send_session = str(task.get("delivery_session") or "").strip() or sessions.get(user_id, "")
    visible_task = public_task(task, include_output=True)
    visible_task.update({"goal_id": goal_id, "run_id": run_id})
    raw = str(task.get("stdout") or task.get("output") or task.get("error") or "").strip()
    content = trim_output(raw) if raw else f"任务 #{task.get('id', '?')} 已结束，状态：{task.get('status', 'unknown')}。"
    notification_category = "task_completed" if task.get("status") == "done" else "task_failed"
    if user_id.startswith("group:"):
        thread_ref = f"qq:task-result:{user_id}"
    else:
        thread_ref = f"qq:task-result:{user_id}"
    return outbox.enqueue(
        dedupe_key=f"qq:task:{task.get('id')}:final:v1",
        channel="qq",
        destination=send_session or user_id,
        payload={
            "kind": "run_result", "task_id": str(task.get("id") or ""),
            "goal_id": goal_id, "run_id": run_id, "user_id": user_id,
            "actor_id": actor_id, "send_session": send_session,
            "content": content, "task": visible_task,
            "notification_category": notification_category,
        },
        max_attempts=100,
        thread_ref=thread_ref,
        delivery_class="operational",
    )


__all__ = ["enqueue_task_result"]
