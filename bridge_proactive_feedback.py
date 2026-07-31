#!/usr/bin/env python3
"""Delivery and user-feedback projections for proactive social events."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from bridge_social_opportunity import record_feedback


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def mark_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    error: str = "",
    now: datetime | None = None,
) -> dict | None:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    event_row = conn.execute(
        "SELECT * FROM proactive_events WHERE delivery_id=?", (_clip(delivery_id, 80),),
    ).fetchone()
    event = dict(event_row) if event_row else None
    if not event:
        return None
    if error:
        conn.execute("UPDATE proactive_events SET error=? WHERE id=?", (_clip(error, 1000), event["id"]))
        conn.execute(
            """UPDATE proactive_policies SET state='retry_wait', state_reason='delivery_failed',
                      failed_count=failed_count+1, updated_at=? WHERE user_id=?""",
            (_timestamp(current), event["user_id"]),
        )
        if event.get("opportunity_id"):
            is_group = str(event.get("policy_kind") or "") == "group_social"
            subject_id = str(event["user_id"])[6:] if is_group and str(event["user_id"]).startswith("group:") else event["user_id"]
            record_feedback(conn, {
                "assistant_id": event.get("assistant_id"), "opportunity_id": event.get("opportunity_id"),
                "decision_ref": event["id"], "subject_type": "qq_group" if is_group else "private_user",
                "subject_id": subject_id, "topic_candidate_id": event.get("topic_candidate_id"),
                "approach": event.get("approach"), "signal": "delivery_failed",
                "source": "delivery_outbox", "detail": {"error_kind": "delivery_failed"},
            })
    elif not event.get("delivered_at"):
        conn.execute("UPDATE proactive_events SET delivered_at=? WHERE id=?", (_timestamp(current), event["id"]))
        conn.execute(
            """UPDATE proactive_policies SET state='scheduled', state_reason='', last_sent_at=?,
                      consecutive_unanswered=consecutive_unanswered+1, updated_at=? WHERE user_id=?""",
            (_timestamp(current), _timestamp(current), event["user_id"]),
        )
    row = conn.execute("SELECT * FROM proactive_events WHERE id=?", (event["id"],)).fetchone()
    return dict(row) if row else None


def note_activity(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    now: datetime | None = None,
) -> dict | None:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    user_id = _clip(user_id, 80)
    if not user_id:
        return None
    policy = conn.execute("SELECT * FROM proactive_policies WHERE user_id=?", (user_id,)).fetchone()
    if not policy:
        return None
    conn.execute(
        """UPDATE proactive_policies SET last_user_at=?, consecutive_unanswered=0,
                  state=CASE WHEN enabled=1 AND authorized=1 THEN 'scheduled' ELSE 'disabled' END,
                  state_reason='', updated_at=? WHERE user_id=?""",
        (_timestamp(current), _timestamp(current), user_id),
    )
    event_row = conn.execute(
        """SELECT * FROM proactive_events WHERE user_id=? AND action='send'
           AND delivered_at<>'' AND responded_at='' ORDER BY delivered_at DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    if event_row:
        event = dict(event_row)
        feedback_column = "feedback_state='replied'," if "feedback_state" in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_events)")
        } else ""
        conn.execute(
            f"UPDATE proactive_events SET {feedback_column}responded_at=? WHERE id=?",
            (_timestamp(current), event["id"]),
        )
        if event.get("opportunity_id"):
            record_feedback(conn, {
                "assistant_id": event.get("assistant_id"), "opportunity_id": event.get("opportunity_id"),
                "decision_ref": event["id"], "subject_type": "private_user", "subject_id": user_id,
                "topic_candidate_id": event.get("topic_candidate_id"), "approach": event.get("approach"),
                "signal": "replied", "source": "qq_inbound",
            })
    row = conn.execute("SELECT * FROM proactive_policies WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


__all__ = ["mark_delivery", "note_activity"]
