#!/usr/bin/env python3
"""Delivery and user-feedback projections for proactive social events."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_social_opportunity import record_feedback


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _interaction_plan_id(event: dict) -> str:
    try:
        evidence = json.loads(str(event.get("evidence_snapshot_json") or "{}"))
    except json.JSONDecodeError:
        return ""
    return _clip(evidence.get("interaction_plan_id"), 80) if isinstance(evidence, dict) else ""


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
    policy_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_policies)")
    }
    # The event settles even if its generation is stale, but only the exact
    # policy generation that produced it may receive ACK/error counters.  An
    # Assistant A -> B -> A switch can reuse assistant_id while advancing the
    # version; assistant_id alone therefore is not a sufficient ownership key.
    policy_clauses = ["user_id=?"]
    policy_params: list[object] = [event["user_id"]]
    for column, value in (
        ("assistant_id", _clip(event.get("assistant_id"), 80)),
        ("policy_kind", _clip(event.get("policy_kind"), 40)),
        ("policy_version", int(event.get("policy_version") or 0)),
    ):
        if column in policy_columns and value:
            policy_clauses.append(f"{column}=?")
            policy_params.append(value)
    policy_where = " AND ".join(policy_clauses)
    if error:
        conn.execute("UPDATE proactive_events SET error=? WHERE id=?", (_clip(error, 1000), event["id"]))
        conn.execute(
            f"""UPDATE proactive_policies SET state='retry_wait', state_reason='delivery_failed',
                      failed_count=failed_count+1, updated_at=? WHERE {policy_where}""",
            (_timestamp(current), *policy_params),
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
        policy_row = conn.execute(
            f"SELECT last_user_at FROM proactive_policies WHERE {policy_where}",
            tuple(policy_params),
        ).fetchone()
        last_user_at = _parse_timestamp(policy_row["last_user_at"] if policy_row else "")
        decision_at = _parse_timestamp(event.get("decision_at"))
        user_returned = bool(
            str(event.get("policy_kind") or "social") == "social"
            and last_user_at
            and decision_at
            and last_user_at > decision_at
        )
        feedback_column = "feedback_state='replied'," if user_returned and "feedback_state" in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_events)")
        } else ""
        conn.execute(
            f"UPDATE proactive_events SET delivered_at=?,{feedback_column}responded_at=? WHERE id=?",
            (
                _timestamp(current),
                _timestamp(last_user_at) if user_returned else "",
                event["id"],
            ),
        )
        conn.execute(
            f"""UPDATE proactive_policies SET state='scheduled', state_reason='', last_sent_at=?,
                      consecutive_unanswered=CASE WHEN ? THEN 0 ELSE consecutive_unanswered+1 END,
                      updated_at=? WHERE {policy_where}""",
            (_timestamp(current), int(user_returned), _timestamp(current), *policy_params),
        )
        if user_returned and event.get("opportunity_id"):
            record_feedback(conn, {
                "assistant_id": event.get("assistant_id"), "opportunity_id": event.get("opportunity_id"),
                "decision_ref": event["id"], "subject_type": "private_user", "subject_id": event["user_id"],
                "topic_candidate_id": event.get("topic_candidate_id"), "approach": event.get("approach"),
                "signal": "replied", "source": "qq_inbound_before_delivery_ack",
            })
    plan_id = _interaction_plan_id(event)
    if plan_id and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_plans'",
    ).fetchone():
        conn.execute(
            """UPDATE interaction_plans SET status=?,updated_at=?
               WHERE id=? AND status IN ('planned','dispatched')""",
            ("failed" if error else "completed", _timestamp(current), plan_id),
        )
    row = conn.execute("SELECT * FROM proactive_events WHERE id=?", (event["id"],)).fetchone()
    return dict(row) if row else None


def note_activity(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    now: datetime | None = None,
    expected_policy: dict | None = None,
) -> dict | None:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    user_id = _clip(user_id, 80)
    if not user_id:
        return None
    policy = conn.execute("SELECT * FROM proactive_policies WHERE user_id=?", (user_id,)).fetchone()
    if not policy:
        return None
    policy_item = dict(policy)
    if expected_policy is not None and any(
        str(policy_item.get(key) or "") != str(expected_policy.get(key) or "")
        for key in ("assistant_id", "policy_kind", "policy_version")
    ):
        return None
    saved_activity = _parse_timestamp(policy_item.get("last_user_at"))
    if saved_activity and current < saved_activity:
        return policy_item
    policy_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_policies)")
    }
    event_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_events)")
    }
    assistant_id = _clip(policy_item.get("assistant_id"), 80)
    policy_clauses = ["user_id=?"]
    policy_params: list[object] = [user_id]
    event_clauses = ["user_id=?"]
    event_params: list[object] = [user_id]
    for column, value in (
        ("assistant_id", assistant_id),
        ("policy_kind", _clip(policy_item.get("policy_kind"), 40)),
        ("policy_version", int(policy_item.get("policy_version") or 0)),
    ):
        if column in policy_columns and value:
            policy_clauses.append(f"{column}=?")
            policy_params.append(value)
        if column in event_columns and column in policy_columns and value:
            event_clauses.append(f"{column}=?")
            event_params.append(value)
    policy_where = " AND ".join(policy_clauses)
    wake_at = _timestamp(current + timedelta(
        minutes=max(15, int(policy_item.get("min_silence_minutes") or 15))
    ))
    updated_policy = conn.execute(
        f"""UPDATE proactive_policies SET last_user_at=?, consecutive_unanswered=0,
                  state=CASE WHEN enabled=1 AND authorized=1 THEN 'scheduled' ELSE 'disabled' END,
                  state_reason='',
                  next_check_at=CASE WHEN enabled=1 AND authorized=1
                    AND (next_check_at='' OR next_check_at>?) THEN ? ELSE next_check_at END,
                  updated_at=? WHERE {policy_where}""",
        (_timestamp(current), wake_at, wake_at, _timestamp(current), *policy_params),
    )
    # The generation snapshot may change after the read but before this write.
    # Only a successful policy CAS grants attribution of this user activity to
    # an event from that same generation.  On a miss, leave all old events
    # untouched; the current policy can be observed by the caller on retry.
    if updated_policy.rowcount != 1:
        row = conn.execute(
            "SELECT * FROM proactive_policies WHERE user_id=?", (user_id,),
        ).fetchone()
        return dict(row) if row else None
    event_where = " AND ".join(event_clauses)
    event_row = conn.execute(
        f"""SELECT * FROM proactive_events WHERE {event_where} AND action='send'
           AND delivered_at<>'' AND delivered_at<=? AND responded_at='' AND error=''
           ORDER BY delivered_at DESC LIMIT 1""",
        (*event_params, _timestamp(current)),
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


def reconcile_recorded_private_activity(
    conn: sqlite3.Connection, policy: dict, *, observed_at: datetime,
) -> dict | None:
    """Repair only feedback backed by same-instance durable QQ private input.

    A counter or last_user_at timestamp alone is deliberately insufficient.
    This also covers an interrupted dispatch whose activity timestamp was
    already copied by an older scheduler without settling its event.
    """
    if str(policy.get("policy_kind") or "") != "social" or not policy.get("assistant_id"):
        return None
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"conversation_threads", "conversation_messages"}.issubset(tables):
        return None
    stamp = _timestamp(observed_at)
    pending = conn.execute(
        """SELECT 1 FROM proactive_events WHERE user_id=? AND assistant_id=?
           AND policy_kind='social' AND policy_version=? AND action='send'
           AND delivered_at<>'' AND delivered_at<=? AND responded_at='' AND error='' LIMIT 1""",
        (policy["user_id"], policy["assistant_id"], policy.get("policy_version", 1), stamp),
    ).fetchone()
    if not pending:
        return None
    evidence = conn.execute(
        """SELECT 1 FROM conversation_messages m JOIN conversation_threads t ON t.id=m.thread_id
           WHERE t.legacy_user_id=? AND t.assistant_id=? AND t.channel_type='qq_private'
             AND m.role='user' AND m.source_type='qq_private' AND m.created_at=? LIMIT 1""",
        (policy["user_id"], policy["assistant_id"], stamp),
    ).fetchone()
    if not evidence:
        return None
    return note_activity(conn, policy["user_id"], now=observed_at, expected_policy=policy)


__all__ = ["mark_delivery", "note_activity", "reconcile_recorded_private_activity"]
