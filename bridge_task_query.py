#!/usr/bin/env python3
"""SQLite-authoritative Task list and cold-history lookup."""

from __future__ import annotations


def list_tasks(db_connect, row_to_task, public_task, *, limit=10, status=None, offset=0):
    where, params = (" WHERE status=?", [status]) if status else ("", [])
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks" + where
            + " ORDER BY COALESCE(created_at,updated_at) DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(int(limit), 100)), max(0, int(offset))),
        ).fetchall()
    return [public_task(row_to_task(row), include_output=False) for row in rows]


def load_active_and_recent(db_connect, row_to_task, *, recent_limit):
    """Load every recoverable Task plus a bounded final-state history."""
    with db_connect() as conn:
        active = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('queued','running') "
            "ORDER BY COALESCE(created_at,updated_at)",
        ).fetchall()
        recent = conn.execute(
            "SELECT * FROM tasks WHERE status NOT IN ('queued','running') "
            "ORDER BY COALESCE(created_at,updated_at) DESC LIMIT ?",
            (max(1, int(recent_limit)),),
        ).fetchall()
    rows = list(active) + list(reversed(recent))
    rows.sort(key=lambda row: str(row["created_at"] or row["updated_at"] or ""))
    return [row_to_task(row) for row in rows]


def get_task(task_id, *, lock, hot_tasks, db_connect, row_to_task, public_task):
    with lock:
        task = hot_tasks.get(task_id)
        if task:
            return public_task(task, include_output=True)
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return public_task(row_to_task(row), include_output=True) if row else None


__all__ = ["get_task", "list_tasks", "load_active_and_recent"]
