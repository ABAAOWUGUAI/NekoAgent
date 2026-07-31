#!/usr/bin/env python3
"""Review and approval of proactive message drafts.

The existing ``proactive_events`` table remains the audit source.  A
``review`` action is a pending owner decision; approval changes that same
event to ``send`` and stages the existing Delivery Outbox action.  No second
message store is introduced.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from bridge_automation_schema import ensure_automation_tables
from bridge_proactive_messaging_policy import (
    proactive_message_gate,
    proactive_target_for_user,
)
from bridge_reliability_service import stage_proactive_delivery


REVIEW_ACTIONS = ("draft", "review")


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idempotency_hash(payload: dict) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def list_proactive_reviews(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[dict]:
    ensure_automation_tables(conn)
    rows = conn.execute(
        """
        SELECT * FROM proactive_events
        WHERE action IN ('draft','review')
        ORDER BY decision_at DESC
        LIMIT ?
        """,
        (max(1, min(int(limit or 50), 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def decide_proactive_review(
    conn: sqlite3.Connection,
    event_id: str,
    decision: str,
    *,
    idempotency_key: str,
) -> dict:
    """Approve or reject one pending confirmation event idempotently."""

    ensure_automation_tables(conn)
    event_id = _clip(event_id, 100)
    decision = _clip(decision, 20).lower()
    if not event_id:
        raise ValueError("proactive_review_event_required")
    if decision not in {"approve", "reject"}:
        raise ValueError("proactive_review_decision_invalid")
    key = _clip(idempotency_key, 160)
    if not key:
        raise ValueError("proactive_review_idempotency_key_required")
    payload = {"event_id": event_id, "decision": decision}
    request_hash = _idempotency_hash(payload)
    replay = conn.execute(
        """
        SELECT request_hash,response_json
        FROM assistant_idempotency_records
        WHERE action=? AND idempotency_key=?
        """,
        ("proactive_review:" + event_id, key),
    ).fetchone()
    if replay:
        if str(replay[0]) != request_hash:
            raise ValueError("proactive_review_idempotency_conflict")
        result = json.loads(str(replay[1] or "{}"))
        result["idempotent_replay"] = True
        return result

    row = conn.execute(
        "SELECT * FROM proactive_events WHERE id=? AND action IN ('draft','review')",
        (event_id,),
    ).fetchone()
    if not row:
        raise ValueError("proactive_review_not_found")
    event = dict(row)
    now = _now()
    if decision == "reject":
        conn.execute(
            """
            UPDATE proactive_events
            SET action='skip',reason='review_rejected',blocked_reason='review_rejected'
            WHERE id=? AND action IN ('draft','review')
            """,
            (event_id,),
        )
        result = {
            "ok": True,
            "event_id": event_id,
            "decision": "reject",
            "action": "skip",
            "idempotent_replay": False,
        }
    else:
        if str(event.get("action") or "") != "review":
            raise ValueError("proactive_draft_requires_confirm_policy")
        policy_row = conn.execute(
            "SELECT * FROM proactive_policies WHERE user_id=?",
            (str(event.get("user_id") or ""),),
        ).fetchone()
        if not policy_row:
            raise ValueError("proactive_policy_not_found")
        policy = dict(policy_row)
        target_type, target_id = proactive_target_for_user(
            conn,
            str(event.get("user_id") or ""),
        )
        gate = proactive_message_gate(
            conn,
            target_type=target_type,
            target_id=target_id,
            intent=str(event.get("intent") or ""),
        )
        if not gate["allowed"]:
            raise ValueError(str(gate.get("reason") or "proactive_policy_disabled"))
        message = _clip(event.get("message"), 1200)
        if not message:
            raise ValueError("proactive_review_message_missing")
        staged = stage_proactive_delivery(conn, policy, event_id, message)
        if not staged:
            raise ValueError("proactive_review_delivery_unavailable")
        conn.execute(
            """
            UPDATE proactive_events
            SET action='send',reason='review_approved',blocked_reason=''
            WHERE id=? AND action='review'
            """,
            (event_id,),
        )
        conn.execute(
            """
            UPDATE proactive_policies
            SET state='delivery_pending',state_reason='review_approved',updated_at=?
            WHERE user_id=?
            """,
            (now, str(event.get("user_id") or "")),
        )
        result = {
            "ok": True,
            "event_id": event_id,
            "decision": "approve",
            "action": "send",
            "action_outbox_id": staged.get("id", ""),
            "idempotent_replay": False,
        }
    conn.execute(
        """
        INSERT INTO assistant_idempotency_records(
            action,idempotency_key,request_hash,response_json,created_at
        ) VALUES(?,?,?,?,?)
        """,
        (
            "proactive_review:" + event_id,
            key,
            request_hash,
            _json(result),
            now,
        ),
    )
    return result


__all__ = ["decide_proactive_review", "list_proactive_reviews"]
