#!/usr/bin/env python3
"""Idempotent conversion of running-task supplements into follow-up tasks."""

from __future__ import annotations

import hashlib
import json


def consume_running_supplements(
    task: dict, *, pending_messages, create_task, safe_cwd, save_task,
) -> dict | None:
    pending = pending_messages(task.get("pending_messages"))
    waiting = [
        item for item in pending
        if not item.get("applied_to_prompt") and not item.get("consumed_by_task_id")
    ]
    if not waiting or task.get("follow_up_source_task_id"):
        return None
    digest = hashlib.sha256(
        json.dumps(waiting, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()[:24]
    prompt = "原任务已经结束。请结合原任务目标和结果，处理运行期间收到的补充消息：\n\n" + "\n".join(
        f"- {str(item.get('message') or '').strip()}" for item in waiting
    )
    follow_up = create_task(
        prompt=prompt, sandbox=str(task.get("sandbox") or "read-only"),
        timeout=int(task.get("timeout") or 180), cwd=safe_cwd(str(task.get("cwd") or "")),
        source=str(task.get("source") or "admin"), user_id=str(task.get("user_id") or ""),
        trace_id=str(task.get("trace_id") or ""), origin_message=prompt,
        intent="task_follow_up", mode=str(task.get("mode") or "work"),
        delivery_recipient_id=str(task.get("delivery_recipient_id") or ""),
        delivery_session=str(task.get("delivery_session") or ""),
        source_task_id=str(task.get("id") or ""),
        follow_up_source_task_id=str(task.get("id") or ""),
        request_idempotency_key=f"task-follow-up:{task.get('id')}:{digest}",
    )
    for item in waiting:
        item["consumed_by_task_id"] = follow_up.get("id")
    task["pending_messages"] = json.dumps(pending[-20:], ensure_ascii=False)
    save_task(task)
    return follow_up


__all__ = ["consume_running_supplements"]
