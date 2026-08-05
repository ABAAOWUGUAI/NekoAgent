#!/usr/bin/env python3
"""Automation Run terminal-state transitions extracted from the legacy module."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_automation_schema import ensure_automation_tables
from bridge_automation_execution import (
    FAILURE_STAGE_ACK,
    FAILURE_STAGE_TASK,
    classify_automation_failure,
)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def list_automation_seen_items(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    limit: int = 500,
) -> list[str]:
    """Return item keys already reserved or confirmed for one durable job."""

    ensure_automation_tables(conn)
    rows = conn.execute(
        """SELECT item_key FROM automation_item_history
           WHERE job_id=? ORDER BY recorded_at DESC LIMIT ?""",
        (str(job_id), max(1, min(int(limit), 500))),
    ).fetchall()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


def reserve_automation_items(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    run_id: str,
    item_keys: list[str],
    now: datetime | None = None,
) -> None:
    """Reserve selected result items before Delivery so retries cannot duplicate them."""

    ensure_automation_tables(conn)
    recorded_at = _timestamp(now or datetime.now(timezone.utc))
    normalized = []
    for value in item_keys:
        key = str(value or "").strip().lower()
        if key and key not in normalized:
            normalized.append(key[:300])
    conn.executemany(
        """INSERT INTO automation_item_history(job_id,item_key,run_id,state,recorded_at)
           VALUES(?,?,?,'pending',?)""",
        [(str(job_id), key, str(run_id), recorded_at) for key in normalized],
    )


def finish_automation_run(
    conn: sqlite3.Connection, job: dict, *, status: str, dispatch: str = "",
    task_id: str = "", delivery_id: str = "", error: str = "",
    now: datetime | None = None,
    retryable: bool = True,
    failure_stage: str = "",
    capability_id: str = "",
    execution_contract_hash: str = "",
) -> dict:
    ensure_automation_tables(conn)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    normalized = status if status in {"completed", "dispatched"} else "failed"
    classified = classify_automation_failure(error, stage=failure_stage) if normalized == "failed" else None
    if classified:
        failure_stage = str(classified["stage"])
        # Keep the durable record useful for operators without copying raw
        # exception text, chat content, credentials, or media metadata.
        error = str(classified["error_code"])
        retryable = bool(retryable and classified["retryable"])
    else:
        # A dispatched/completed run is not a failure record.  Never carry an
        # arbitrary caller-provided string (which may contain private text or
        # credentials) into the durable run audit row.
        error = ""
    existing = conn.execute(
        "SELECT capability_id,execution_contract_hash,failure_stage FROM automation_runs WHERE id=?",
        (job["run_id"],),
    ).fetchone()
    existing = dict(existing) if existing else {}
    saved_capability = str(capability_id or existing.get("capability_id") or "")[:120]
    saved_contract_hash = str(
        execution_contract_hash or existing.get("execution_contract_hash") or ""
    )[:128]
    saved_failure_stage = str(failure_stage or "")[:40] if normalized == "failed" else ""
    conn.execute(
        """UPDATE automation_runs SET status=?,dispatch=?,task_id=?,delivery_id=?,error=?,
                  capability_id=?,execution_contract_hash=?,failure_stage=?,
                  finished_at=?,lease_owner='',lease_until='',terminal_source=? WHERE id=?""",
        (
            normalized, str(dispatch)[:40], str(task_id)[:80], str(delivery_id)[:80],
            str(error)[:1000], saved_capability, saved_contract_hash, saved_failure_stage,
            _timestamp(current) if normalized != "dispatched" else "",
            str(dispatch)[:40] if normalized != "dispatched" else "", job["run_id"],
        ),
    )
    if normalized == "dispatched":
        conn.execute(
            "UPDATE automation_jobs SET state='dispatched',lease_until='',updated_at=? WHERE id=?",
            (_timestamp(current), job["id"]),
        )
    elif normalized == "completed":
        conn.execute(
            "UPDATE automation_item_history SET state='confirmed' WHERE run_id=?",
            (job["run_id"],),
        )
        if str(job.get("schedule_type") or "once") == "once":
            enabled, state, next_due = 0, "completed", ""
        else:
            from bridge_automation import calculate_next_due
            anchor = max(current, _parse(job.get("scheduled_for")) or current)
            enabled, state, next_due = 1, "scheduled", _timestamp(calculate_next_due(job, after=anchor))
        conn.execute(
            """UPDATE automation_jobs SET enabled=?,state=?,next_due_at=?,lease_until='',
                      last_run_at=?,last_error='',run_count=run_count+1,updated_at=? WHERE id=?""",
            (enabled, state, next_due, _timestamp(current), _timestamp(current), job["id"]),
        )
    else:
        conn.execute(
            "DELETE FROM automation_item_history WHERE run_id=? AND state='pending'",
            (job["run_id"],),
        )
        if not retryable:
            conn.execute(
                """UPDATE automation_jobs SET enabled=0,state='blocked',next_due_at='',
                          lease_until='',last_run_at=?,last_error=?,
                          failed_count=failed_count+1,updated_at=? WHERE id=?""",
                (_timestamp(current), str(error)[:1000], _timestamp(current), job["id"]),
            )
            return dict(conn.execute("SELECT * FROM automation_runs WHERE id=?", (job["run_id"],)).fetchone())
        # A failed repeating run must not become an unbounded 15-minute retry
        # storm.  One bounded retry is useful for transient network errors;
        # after that, leave the job scheduled for its normal next occurrence.
        recent = conn.execute(
            """SELECT status FROM automation_runs WHERE job_id=?
               ORDER BY started_at DESC LIMIT 2""",
            (job["id"],),
        ).fetchall()
        attempts = sum(1 for row in recent if str(row[0]) == "failed")
        if attempts >= 2:
            if str(job.get("schedule_type") or "once") != "once":
                from bridge_automation import calculate_next_due
                enabled, state = 1, "scheduled"
                next_due = _timestamp(calculate_next_due(job, after=current))
            else:
                enabled, state, next_due = 0, "blocked", ""
            conn.execute(
                """UPDATE automation_jobs SET enabled=?,state=?,next_due_at=?,lease_until='',
                          last_run_at=?,last_error=?,failed_count=failed_count+1,updated_at=? WHERE id=?""",
                (
                    enabled, state, next_due, _timestamp(current), str(error)[:1000],
                    _timestamp(current), job["id"],
                ),
            )
            return dict(conn.execute("SELECT * FROM automation_runs WHERE id=?", (job["run_id"],)).fetchone())
        retry_at = _timestamp(current + timedelta(minutes=30))
        conn.execute(
            """UPDATE automation_jobs SET state='retry_wait',next_due_at=?,lease_until='',
                      last_run_at=?,last_error=?,failed_count=failed_count+1,updated_at=? WHERE id=?""",
            (retry_at, _timestamp(current), str(error)[:1000], _timestamp(current), job["id"]),
        )
    return dict(conn.execute("SELECT * FROM automation_runs WHERE id=?", (job["run_id"],)).fetchone())


def settle_automation_dispatch(
    conn: sqlite3.Connection, *, delivery_id: str = "", task_id: str = "",
    status: str, error: str = "",
) -> dict | None:
    field, value = ("delivery_id", delivery_id) if delivery_id else ("task_id", task_id)
    if not value:
        return None
    row = conn.execute(
        f"SELECT r.id AS run_id,r.scheduled_for,j.* FROM automation_runs r "
        f"JOIN automation_jobs j ON j.id=r.job_id WHERE r.{field}=? AND r.status='dispatched'",
        (value,),
    ).fetchone()
    return finish_automation_run(
        conn, dict(row), status="completed" if status == "completed" else "failed",
        dispatch="delivery_ack" if delivery_id else "task_terminal",
        delivery_id=delivery_id,
        task_id=task_id,
        error=error,
        failure_stage=(FAILURE_STAGE_ACK if delivery_id else FAILURE_STAGE_TASK)
        if status != "completed" else "",
    ) if row else None


__all__ = [
    "finish_automation_run",
    "list_automation_seen_items",
    "reserve_automation_items",
    "settle_automation_dispatch",
]
