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


DELIVERY_PROJECTION_KEY = "d1_delivery_projections_v1"
DELIVERY_PROJECTION_LIMIT = 16


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


def _delivery_projection(value: Mapping[str, object]) -> dict:
    mode = str(value.get("mode") or "INLINE").strip().upper()
    if mode not in {"INLINE", "ARTIFACT", "BOTH"}:
        raise ValueError("delivery_projection_mode_invalid")
    points: list[str] = []
    if mode == "BOTH":
        raw_points = value.get("summary_points")
        if not isinstance(raw_points, list):
            raw_points = []
        for raw in raw_points[:3]:
            point = " ".join(str(raw or "").split()).strip()[:200]
            if point and point not in points:
                points.append(point)
    d1_report = bool(value.get("d1_report_profile"))
    presentation = str(value.get("presentation") or "").strip()
    if d1_report:
        if mode not in {"ARTIFACT", "BOTH"} or presentation != "result_page_v1":
            raise ValueError("delivery_projection_profile_invalid")
    elif presentation:
        raise ValueError("delivery_projection_presentation_invalid")
    return {
        "schema_version": 1,
        "mode": mode,
        "summary_points": points,
        "d1_report_profile": d1_report,
        "presentation": presentation,
    }


def store_task_delivery_projection(
    conn: sqlite3.Connection,
    goal_id: str,
    task_id: str,
    projection: Mapping[str, object],
) -> dict:
    """Persist bounded runtime delivery state without mutating the Plan.

    Goal metadata is already an extensible runtime field and, unlike projected
    Run metadata, is not overwritten by later legacy Task synchronization.
    """

    goal_id = str(goal_id or "").strip()
    task_id = str(task_id or "").strip()
    if not goal_id or not task_id:
        raise ValueError("delivery_projection_binding_required")
    row = conn.execute("SELECT metadata_json FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not row:
        raise ValueError("delivery_projection_goal_not_found")
    try:
        metadata = json.loads(str(row[0] or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    bucket = metadata.get(DELIVERY_PROJECTION_KEY)
    if not isinstance(bucket, dict):
        bucket = {}
    normalized = {**_delivery_projection(projection), "stored_at": utc_now()}
    bucket[task_id] = normalized
    if len(bucket) > DELIVERY_PROJECTION_LIMIT:
        ordered = sorted(
            bucket.items(),
            key=lambda item: str((item[1] if isinstance(item[1], dict) else {}).get("stored_at") or ""),
            reverse=True,
        )[:DELIVERY_PROJECTION_LIMIT]
        bucket = dict(ordered)
    metadata[DELIVERY_PROJECTION_KEY] = bucket
    conn.execute(
        "UPDATE goals SET metadata_json=?,version=version+1 WHERE id=?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")), goal_id),
    )
    return dict(normalized)


def load_task_delivery_projection(
    conn: sqlite3.Connection,
    goal_id: str,
    task_id: str,
) -> dict:
    goal_id = str(goal_id or "").strip()
    task_id = str(task_id or "").strip()
    if not goal_id or not task_id:
        return {}
    row = conn.execute("SELECT metadata_json FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not row:
        return {}
    try:
        metadata = json.loads(str(row[0] or "{}"))
    except json.JSONDecodeError:
        return {}
    bucket = metadata.get(DELIVERY_PROJECTION_KEY) if isinstance(metadata, dict) else None
    value = bucket.get(task_id) if isinstance(bucket, dict) else None
    if not isinstance(value, dict):
        return {}
    try:
        normalized = _delivery_projection(value)
    except ValueError:
        return {}
    stored_at = str(value.get("stored_at") or "")
    return {**normalized, "stored_at": stored_at}


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
    "DELIVERY_PROJECTION_KEY",
    "bind_plan_to_message",
    "create_interaction_plan",
    "interaction_plan_cutover_plan",
    "interaction_plan_feature_enabled",
    "list_interaction_plans",
    "load_task_delivery_projection",
    "set_interaction_plan_feature",
    "store_task_delivery_projection",
]
