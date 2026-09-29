from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_automation_schema import ensure_automation_tables


def _interaction_plan_id(event: dict) -> str:
    try:
        evidence = json.loads(str(event.get("evidence_snapshot_json") or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(evidence.get("interaction_plan_id") or "").strip()[:80] if isinstance(evidence, dict) else ""


def record_proactive_failure(
    conn: sqlite3.Connection,
    policy: dict,
    error: str,
    *,
    event_id: str = "",
    now: datetime | None = None,
) -> None:
    from bridge_automation import (
        _clip,
        _proactive_claim_where,
        timestamp,
        utc_now,
    )

    current = (now or utc_now()).astimezone(timezone.utc)
    if _clip(event_id, 80):
        event = conn.execute(
            "SELECT * FROM proactive_events WHERE id=? AND user_id=?",
            (_clip(event_id, 80), policy["user_id"]),
        ).fetchone()
    else:
        # Failure happened before this iteration produced an event.  Guessing
        # the latest pending row can corrupt an older delivery and its Plan.
        event = None
    event_item = dict(event) if event else {}
    policy_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_policies)")
    }
    update_where = ""
    update_params: list[object] = []
    if event_item:
        clauses = ["user_id=?"]
        update_params = [_clip(event_item.get("user_id"), 80)]
        for field, expected in (
            ("assistant_id", _clip(event_item.get("assistant_id"), 80)),
            ("policy_kind", _clip(event_item.get("policy_kind"), 40)),
            ("policy_version", int(event_item.get("policy_version") or 0)),
        ):
            if field in policy_columns and expected:
                clauses.append(f"{field}=?")
                update_params.append(expected)
        update_where = " AND ".join(clauses)
    elif _clip(policy.get("claim_token"), 80):
        update_where, update_params = _proactive_claim_where(policy, policy_columns)
    if update_where:
        # One compare-and-swap performs the defer and counter update.  A prior
        # SELECT followed by a user-only UPDATE allowed a concurrent Assistant
        # or policy-version switch to receive an old generation's failure.
        conn.execute(
            f"""UPDATE proactive_policies
                   SET state='retry_wait',state_reason=?,next_check_at=?,lease_until='',
                       last_evaluated_at=?,updated_at=?,failed_count=failed_count+1
                 WHERE {update_where}""",
            (
                _clip(error, 300),
                timestamp(current + timedelta(minutes=15)),
                timestamp(current),
                timestamp(current),
                *update_params,
            ),
        )
    if event_item:
        conn.execute(
            "UPDATE proactive_events SET error=? WHERE id=?",
            (_clip(error, 1000), event_item["id"]),
        )
        plan_id = _interaction_plan_id(event_item)
        if plan_id and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_plans'",
        ).fetchone():
            conn.execute(
                """UPDATE interaction_plans SET status='failed',updated_at=?
                   WHERE id=? AND status IN ('planned','dispatched')""",
                (current.isoformat(), plan_id),
            )


def attach_proactive_delivery(conn: sqlite3.Connection, event_id: str, delivery_id: str) -> dict | None:
    from bridge_automation import _clip, _row

    ensure_automation_tables(conn)
    conn.execute(
        "UPDATE proactive_events SET delivery_id=? WHERE id=?",
        (_clip(delivery_id, 80), _clip(event_id, 80)),
    )
    event = _row(conn.execute("SELECT * FROM proactive_events WHERE id=?", (_clip(event_id, 80),)).fetchone())
    plan_id = _interaction_plan_id(event or {})
    plan_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_plans'",
    ).fetchone()
    if plan_id and plan_table:
        conn.execute(
            """UPDATE interaction_plans SET status='dispatched',updated_at=?
               WHERE id=? AND status='planned'""",
            (datetime.now(timezone.utc).isoformat(), plan_id),
        )
    return event


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
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "group_participation_queue" in tables:
        rows.extend(conn.execute(
            """SELECT due_at AS due FROM group_participation_queue
               WHERE state='pending' AND due_at<>''
               UNION ALL
               SELECT lease_expires_at AS due FROM group_participation_queue
               WHERE state='claimed' AND lease_expires_at<>''"""
        ).fetchall())
    delays = []
    for row in rows:
        due = parse_datetime(row["due"])
        if due:
            delays.append(max(0.0, (due - current).total_seconds()))
    return min([max(0.25, float(maximum)), *delays]) if delays else max(0.25, float(maximum))
