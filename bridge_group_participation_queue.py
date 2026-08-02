#!/usr/bin/env python3
"""Durable quiet-gap candidates for natural group participation.

The queue stores only message identifiers and delivery metadata.  Message text
remains in the governed group message store and is read only when a candidate
is claimed after the quiet gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from bridge_group_participation_schema import GROUP_PARTICIPATION_QUEUE_TABLE
from bridge_migrations import utc_now


def _parse(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        result = datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def enqueue_group_candidate(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    current: dict,
    session: str,
    sender_id: str,
    sender_name: str,
    external_message_id: str,
    quiet_gap_seconds: int,
    active_topic_window_seconds: int,
) -> dict:
    now = _parse(current.get("created_at"))
    gap_seconds = 8 if quiet_gap_seconds is None else int(quiet_gap_seconds)
    quiet_due = now + timedelta(seconds=max(0, gap_seconds))
    group = str(group_id or "").strip()
    if not group:
        raise ValueError("group_participation_group_required")
    existing = conn.execute(
        f"SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?", (group,)
    ).fetchone()
    first = (
        _parse(existing["first_message_at"])
        if existing and str(existing["state"]) not in {"completed", "failed", "cancelled"}
        else now
    )
    topic_window = max(max(0, gap_seconds), min(int(active_topic_window_seconds or 45), 600))
    due = min(quiet_due, first + timedelta(seconds=topic_window))
    replaced_message_id = (
        int(existing["latest_message_id"] or 0)
        if existing
        and str(existing["state"]) in {"pending", "claimed"}
        and int(existing["latest_message_id"] or 0) != int(current.get("id") or 0)
        else 0
    )
    conn.execute(
        f"""
        INSERT INTO {GROUP_PARTICIPATION_QUEUE_TABLE}(
            group_id,state,first_message_at,last_message_at,due_at,latest_message_id,
            latest_sender_id,latest_sender_name,latest_session,latest_external_message_id,
            attempt,lease_expires_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,0,'',?)
        ON CONFLICT(group_id) DO UPDATE SET
            state='pending',
            first_message_at=CASE
                WHEN {GROUP_PARTICIPATION_QUEUE_TABLE}.state IN ('completed','failed','cancelled')
                THEN excluded.first_message_at
                ELSE {GROUP_PARTICIPATION_QUEUE_TABLE}.first_message_at
            END,
            last_message_at=excluded.last_message_at,
            due_at=excluded.due_at,
            latest_message_id=excluded.latest_message_id,
            latest_sender_id=excluded.latest_sender_id,
            latest_sender_name=excluded.latest_sender_name,
            latest_session=excluded.latest_session,
            latest_external_message_id=excluded.latest_external_message_id,
            attempt=0,
            lease_expires_at='',
            updated_at=excluded.updated_at
        """,
        (
            group, "pending", now.isoformat(), now.isoformat(), due.isoformat(),
            int(current.get("id") or 0), str(sender_id or ""), str(sender_name or ""),
            str(session or ""), str(external_message_id or ""), utc_now(),
        ),
    )
    row = conn.execute(
        f"SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?", (group,)
    ).fetchone()
    result = dict(row)
    result["replaced_message_id"] = replaced_message_id
    result["candidate_due_reason"] = "quiet_gap" if due == quiet_due else "active_topic_window"
    return result


def claim_due_group_candidates(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    lease_seconds: int = 120,
    limit: int = 3,
) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    rows = conn.execute(
        f"""
        SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE}
        WHERE (state='pending' AND due_at<=?)
           OR (state='claimed' AND lease_expires_at<=?)
        ORDER BY due_at, group_id LIMIT ?
        """,
        (current.isoformat(), current.isoformat(), max(1, min(int(limit), 20))),
    ).fetchall()
    claimed: list[dict] = []
    lease = (current + timedelta(seconds=max(30, int(lease_seconds)))).isoformat()
    for row in rows:
        group = str(row["group_id"])
        conn.execute(
            f"""UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE}
                SET state='claimed', attempt=attempt+1, lease_expires_at=?, updated_at=?
                WHERE group_id=? AND (state='pending' OR (state='claimed' AND lease_expires_at<=?))""",
            (lease, utc_now(), group, current.isoformat()),
        )
        refreshed = conn.execute(
            f"SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?", (group,)
        ).fetchone()
        if refreshed and refreshed["state"] == "claimed":
            claimed.append(dict(refreshed))
    return claimed


def finish_group_candidate(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    state: str = "completed",
    latest_message_id: int | None = None,
) -> bool:
    if state not in {"completed", "failed", "cancelled"}:
        raise ValueError("group_participation_queue_state_invalid")
    sql = f"UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE} SET state=?, lease_expires_at='', updated_at=? WHERE group_id=?"
    values: list[object] = [state, utc_now(), str(group_id or "").strip()]
    if latest_message_id is not None:
        sql += " AND latest_message_id=?"
        values.append(int(latest_message_id))
    cursor = conn.execute(sql, values)
    return bool(cursor.rowcount)


def group_candidate_is_current(conn: sqlite3.Connection, group_id: str, latest_message_id: int) -> bool:
    row = conn.execute(
        f"SELECT latest_message_id,state FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?",
        (str(group_id or "").strip(),),
    ).fetchone()
    return bool(
        row
        and int(row["latest_message_id"] or 0) == int(latest_message_id)
        and str(row["state"]) == "claimed"
    )


def reschedule_group_candidate(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    seconds: int = 15,
    latest_message_id: int | None = None,
) -> bool:
    due = datetime.now(timezone.utc) + timedelta(seconds=max(5, int(seconds)))
    sql = (
        f"UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE} SET state='pending', due_at=?, "
        "lease_expires_at='', updated_at=? WHERE group_id=?"
    )
    values: list[object] = [due.isoformat(), utc_now(), str(group_id or "").strip()]
    if latest_message_id is not None:
        sql += " AND latest_message_id=?"
        values.append(int(latest_message_id))
    cursor = conn.execute(sql, values)
    return bool(cursor.rowcount)


__all__ = [
    "claim_due_group_candidates",
    "enqueue_group_candidate",
    "finish_group_candidate",
    "group_candidate_is_current",
    "reschedule_group_candidate",
]
