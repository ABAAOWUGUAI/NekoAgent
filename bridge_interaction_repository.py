#!/usr/bin/env python3
"""Persistence and cutover controls for validated Interaction Plans."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Mapping

from bridge_conversation_memory import resolve_thread
from bridge_interaction_contract import interaction_plan_hash, normalize_interaction_plan
from bridge_interaction_plan_schema import (
    INTERACTION_PLAN_FEATURE_FLAG,
    require_interaction_plan_schema,
)
from bridge_migrations import utc_after, utc_now


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def interaction_plan_feature_enabled(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_plans'",
    ).fetchone()
    if not table:
        return False
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (INTERACTION_PLAN_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def _public(row: Mapping[str, object]) -> dict:
    try:
        plan = json.loads(str(row.get("plan_json") or "{}"))
    except json.JSONDecodeError:
        plan = {}
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "request_message_id": row.get("request_message_id"),
        "schema_version": int(row.get("schema_version") or 0),
        "status": row["status"],
        "summary_mode": row["summary_mode"],
        "primary_intent": row["primary_intent"],
        "intent_count": int(row.get("intent_count") or 0),
        "action_count": int(row.get("action_count") or 0),
        "plan_hash": row["plan_hash"],
        "classifier_source": row["classifier_source"],
        "origin_channel": row["origin_channel"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "plan": plan,
    }


def create_interaction_plan(
    conn: sqlite3.Connection,
    legacy_user_id: str,
    plan: Mapping[str, object],
    *,
    request_source: str = "",
    classifier_source: str = "fallback",
) -> dict:
    """Persist a validated plan without duplicating the raw inbound message."""

    normalized = normalize_interaction_plan(plan)
    thread = resolve_thread(conn, legacy_user_id, source=request_source)
    plan_id = "plan-" + uuid.uuid4().hex
    now = utc_now()
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = interaction_plan_hash(normalized)
    conn.execute(
        """
        INSERT INTO interaction_plans(
            id,owner_actor_id,assistant_id,thread_id,request_message_id,
            schema_version,status,summary_mode,primary_intent,intent_count,
            action_count,plan_json,plan_hash,classifier_source,origin_channel,
            created_at,updated_at
        ) VALUES(?,?,?,?,NULL,?,'planned',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            plan_id,
            thread["owner_actor_id"],
            thread["assistant_id"],
            thread["id"],
            int(normalized["schema_version"]),
            normalized["summary_mode"],
            normalized["primary_intent"],
            len(normalized["intents"]),
            len(normalized["actions"]),
            payload,
            digest,
            str(classifier_source or "fallback")[:40],
            str(thread["channel_type"] or "legacy_unknown")[:40],
            now,
            now,
        ),
    )
    row = _rows(conn.execute("SELECT * FROM interaction_plans WHERE id=?", (plan_id,)))[0]
    return _public(row)


def bind_plan_to_message(
    conn: sqlite3.Connection,
    plan_id: str,
    message_id: str,
    *,
    status: str = "dispatched",
) -> dict:
    if status not in {"planned", "dispatched", "completed", "failed", "cancelled"}:
        raise ValueError("interaction_plan_status_invalid")
    plan_rows = _rows(
        conn.execute("SELECT * FROM interaction_plans WHERE id=?", (str(plan_id or ""),)),
    )
    if not plan_rows:
        raise ValueError("interaction_plan_not_found")
    plan = plan_rows[0]
    message = conn.execute(
        "SELECT thread_id FROM conversation_messages WHERE id=?",
        (str(message_id or ""),),
    ).fetchone()
    if not message:
        raise ValueError("interaction_plan_message_not_found")
    if str(message[0]) != str(plan["thread_id"]):
        raise ValueError("interaction_plan_message_thread_mismatch")
    if plan.get("request_message_id") and str(plan["request_message_id"]) != str(message_id):
        raise ValueError("interaction_plan_message_already_bound")
    now = utc_after(str(plan["updated_at"]))
    conn.execute(
        """
        UPDATE interaction_plans
        SET request_message_id=?,status=?,updated_at=?
        WHERE id=?
        """,
        (str(message_id), status, now, str(plan_id)),
    )
    row = _rows(conn.execute("SELECT * FROM interaction_plans WHERE id=?", (str(plan_id),)))[0]
    return _public(row)


def list_interaction_plans(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    thread_id: str = "",
) -> list[dict]:
    limit = max(1, min(int(limit or 50), 100))
    active = conn.execute(
        """
        SELECT owner_actor_id FROM assistant_instances
        WHERE status='active' ORDER BY created_at LIMIT 1
        """,
    ).fetchone()
    if not active:
        return []
    params: list[object] = [str(active[0])]
    where = "owner_actor_id=?"
    if thread_id:
        where += " AND thread_id=?"
        params.append(str(thread_id))
    params.append(limit)
    rows = _rows(
        conn.execute(
            f"""
            SELECT * FROM interaction_plans
            WHERE {where}
            ORDER BY created_at DESC,id DESC LIMIT ?
            """,
            tuple(params),
        ),
    )
    return [_public(row) for row in rows]


def interaction_plan_cutover_plan(conn: sqlite3.Connection) -> dict:
    schema = require_interaction_plan_schema(conn)
    flag = interaction_plan_feature_enabled(conn)
    identity_flag = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name='assistant_identity_v2'",
    ).fetchone()
    memory_flag = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name='memory_scope_v2'",
    ).fetchone()
    counts = conn.execute(
        """
        SELECT count(*),
               coalesce(sum(CASE WHEN summary_mode='mixed' THEN 1 ELSE 0 END),0),
               coalesce(sum(CASE WHEN intent_count>1 THEN 1 ELSE 0 END),0)
        FROM interaction_plans
        """,
    ).fetchone()
    prerequisites = {
        "identity_enabled": bool(identity_flag and int(identity_flag[0])),
        "memory_scope_enabled": bool(memory_flag and int(memory_flag[0])),
    }
    ok = bool(schema["ok"] and all(prerequisites.values()))
    result = {
        "ok": ok,
        "feature_enabled": flag,
        "schema": schema,
        "prerequisites": prerequisites,
        "plan_count": int(counts[0]),
        "mixed_plan_count": int(counts[1]),
        "multi_intent_plan_count": int(counts[2]),
        "rollback": "disable_interaction_plan_v2_keep_additive_rows",
    }
    checksum_payload = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["plan_checksum"] = hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest()
    return result


def set_interaction_plan_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    plan = interaction_plan_cutover_plan(conn)
    if str(expect_plan_checksum or "") != plan["plan_checksum"]:
        raise ValueError("stale_interaction_plan_cutover_plan")
    if enabled and not plan["ok"]:
        raise ValueError("interaction_plan_cutover_prerequisite_failed")
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET
            enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (INTERACTION_PLAN_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return interaction_plan_cutover_plan(conn)


__all__ = [
    "bind_plan_to_message",
    "create_interaction_plan",
    "interaction_plan_cutover_plan",
    "interaction_plan_feature_enabled",
    "list_interaction_plans",
    "set_interaction_plan_feature",
]
