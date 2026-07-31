#!/usr/bin/env python3
"""Gate C3 transactional action-outbox helpers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from bridge_reliability_schema import RELIABILITY_FEATURE_FLAG, require_reliability_schema


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reliability_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (RELIABILITY_FEATURE_FLAG,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and int(row[0]))


def stage_action(
    conn: sqlite3.Connection,
    *,
    kind: str,
    aggregate_type: str,
    aggregate_id: str,
    dedupe_key: str,
    payload: dict,
) -> dict:
    require_reliability_schema(conn)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO assistant_action_outbox(
            id,kind,aggregate_type,aggregate_id,dedupe_key,payload_json,status,
            delivery_id,attempt_count,next_attempt_at,last_error,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'pending','',0,'','',?,?)
        ON CONFLICT(dedupe_key) DO NOTHING
        """,
        (
            uuid.uuid4().hex, str(kind)[:60], str(aggregate_type)[:60],
            str(aggregate_id)[:100], str(dedupe_key)[:300],
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now, now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM assistant_action_outbox WHERE dedupe_key=?", (str(dedupe_key)[:300],),
    ).fetchone()
    return dict(row)


def stage_proactive_delivery(
    conn: sqlite3.Connection, policy: dict, event_id: str, message: str,
) -> dict | None:
    if not reliability_enabled(conn):
        return None
    session = str(policy.get("send_session") or "")
    user_id = str(policy.get("user_id") or "")
    is_group = str(policy.get("policy_kind") or "") == "group_social" and user_id.startswith("group:")
    target_id = user_id[6:] if is_group else user_id
    thread_ref = f"qq:group:{target_id}" if is_group else f"qq:private:{user_id}"
    return stage_action(
        conn, kind="proactive_delivery", aggregate_type="proactive_event",
        aggregate_id=event_id, dedupe_key=f"qq:proactive:{event_id}",
        payload={
            "channel": "qq", "destination": session or user_id, "max_attempts": 100,
            "thread_ref": thread_ref, "delivery_class": "social",
            "payload": {
                "kind": "proactive_chat", "proactive_event_id": event_id,
                "user_id": target_id, "send_session": session, "content": message,
                "scope": "group" if is_group else "private",
                "group_id": target_id if is_group else "",
            },
        },
    )


def pending_actions(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict]:
    require_reliability_schema(conn)
    now = utc_now()
    rows = conn.execute(
        """
        SELECT * FROM assistant_action_outbox
        WHERE status='pending' AND (next_attempt_at='' OR next_attempt_at<=?)
        ORDER BY created_at LIMIT ?
        """,
        (now, max(1, min(int(limit), 50))),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        result.append(item)
    return result


def mark_action_linked(conn: sqlite3.Connection, action_id: str, delivery_id: str) -> dict:
    now = utc_now()
    conn.execute(
        """
        UPDATE assistant_action_outbox
        SET status='linked',delivery_id=?,attempt_count=attempt_count+1,
            next_attempt_at='',last_error='',updated_at=?
        WHERE id=?
        """,
        (str(delivery_id)[:100], now, str(action_id)[:100]),
    )
    row = conn.execute("SELECT * FROM assistant_action_outbox WHERE id=?", (action_id,)).fetchone()
    return dict(row)


def mark_action_retry(conn: sqlite3.Connection, action_id: str, error: str) -> dict:
    now_dt = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT attempt_count FROM assistant_action_outbox WHERE id=?", (action_id,),
    ).fetchone()
    attempt = int(row[0] if row else 0) + 1
    terminal = attempt >= 100
    delay = min(900, max(5, 2 ** min(attempt, 9)))
    conn.execute(
        """
        UPDATE assistant_action_outbox SET status=?,attempt_count=?,next_attempt_at=?,
            last_error=?,updated_at=? WHERE id=?
        """,
        (
            "failed" if terminal else "pending", attempt,
            "" if terminal else (now_dt + timedelta(seconds=delay)).isoformat(),
            str(error)[:1000], now_dt.isoformat(), action_id,
        ),
    )
    saved = conn.execute("SELECT * FROM assistant_action_outbox WHERE id=?", (action_id,)).fetchone()
    return dict(saved)


__all__ = [
    "mark_action_linked", "mark_action_retry", "pending_actions",
    "reliability_enabled", "stage_action", "stage_proactive_delivery",
]
