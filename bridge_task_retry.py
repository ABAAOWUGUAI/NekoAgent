#!/usr/bin/env python3
"""Cold-history aware Task retry construction."""

from __future__ import annotations


def retry_task(
    task_id: str, *, lock, hot_tasks, db_connect, row_to_task, retryable_statuses,
    default_cwd, safe_cwd, create_task,
) -> tuple[dict | None, str]:
    with lock:
        original = hot_tasks.get(task_id)
        if not original:
            with db_connect() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            original = row_to_task(row) if row else None
        if not original:
            return None, "task_not_found"
        source_status = str(original.get("status") or "")
        if source_status not in retryable_statuses:
            return None, f"task_not_retryable:{source_status or 'unknown'}"
        prompt = str(original.get("prompt") or "").strip()
        sandbox = str(original.get("sandbox") or "read-only")
        values = {
            "timeout": int(original.get("timeout") or (600 if sandbox == "workspace-write" else 180)),
            "source": str(original.get("source") or "admin"),
            "user_id": str(original.get("user_id") or ""),
            "trace_id": str(original.get("trace_id") or ""),
            "origin_message": str(original.get("origin_message") or ""),
            "intent": str(original.get("intent") or ""),
            "mode": str(original.get("mode") or ""),
            # A retry is a new execution attempt. Never inherit a time-bounded
            # Web Search grant from the original task.
            "network_mode": "controlled",
            "delivery_recipient_id": str(original.get("delivery_recipient_id") or ""),
            "delivery_session": str(original.get("delivery_session") or ""),
        }
        cwd_raw = str(original.get("cwd") or str(default_cwd))
    if not prompt:
        return None, "task_prompt_unavailable"
    if sandbox not in {"read-only", "workspace-write"}:
        return None, "invalid_sandbox"
    try:
        cwd = safe_cwd(cwd_raw)
    except ValueError as exc:
        return None, str(exc)
    return create_task(
        prompt=prompt, sandbox=sandbox, cwd=cwd, source_task_id=task_id, **values,
    ), ""


__all__ = ["retry_task"]
