from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_automation_schema import ensure_automation_tables


def record_proactive_failure(
    conn: sqlite3.Connection,
    policy: dict,
    error: str,
    *,
    now: datetime | None = None,
) -> None:
    from bridge_automation import _clip, _defer_policy, timestamp, utc_now

    current = (now or utc_now()).astimezone(timezone.utc)
    _defer_policy(conn, policy["user_id"], "retry_wait", _clip(error, 300), current + timedelta(minutes=15), current)
    conn.execute(
        """UPDATE proactive_events SET error=? WHERE id=(
               SELECT id FROM proactive_events WHERE user_id=? AND action='send'
                 AND delivery_id='' AND error='' ORDER BY decision_at DESC LIMIT 1
           )""",
        (_clip(error, 1000), policy["user_id"]),
    )
    conn.execute(
        "UPDATE proactive_policies SET failed_count=failed_count+1 WHERE user_id=?",
        (policy["user_id"],),
    )


def attach_proactive_delivery(conn: sqlite3.Connection, event_id: str, delivery_id: str) -> dict | None:
    from bridge_automation import _clip, _row

    ensure_automation_tables(conn)
    conn.execute(
        "UPDATE proactive_events SET delivery_id=? WHERE id=?",
        (_clip(delivery_id, 80), _clip(event_id, 80)),
    )
    return _row(conn.execute("SELECT * FROM proactive_events WHERE id=?", (_clip(event_id, 80),)).fetchone())


def mark_proactive_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    error: str = "",
    now: datetime | None = None,
) -> dict | None:
    ensure_automation_tables(conn)
    from bridge_proactive_feedback import mark_delivery

    return mark_delivery(conn, delivery_id, error=error, now=now)


def note_user_activity(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> dict | None:
    ensure_automation_tables(conn)
    from bridge_proactive_feedback import note_activity

    return note_activity(conn, user_id, now=now)


def seconds_until_next_event(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    maximum: float = 60.0,
) -> float:
    from bridge_automation import parse_datetime, utc_now

    ensure_automation_tables(conn)
    current = (now or utc_now()).astimezone(timezone.utc)
    rows = conn.execute(
        """SELECT next_due_at AS due FROM automation_jobs WHERE enabled=1 AND next_due_at<>''
           UNION ALL
           SELECT next_check_at AS due FROM proactive_policies
           WHERE enabled=1 AND authorized=1 AND next_check_at<>''"""
    ).fetchall()
    delays = []
    for row in rows:
        due = parse_datetime(row["due"])
        if due:
            delays.append(max(0.0, (due - current).total_seconds()))
    return min([max(0.25, float(maximum)), *delays]) if delays else max(0.25, float(maximum))
