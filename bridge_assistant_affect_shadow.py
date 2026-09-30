#!/usr/bin/env python3
"""BE-3 persistence for Assistant Affect Shadow with no expression integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from bridge_assistant_affect_contract import make_assistant_affect_shadow
from bridge_assistant_affect_shadow_schema import (
    ASSISTANT_AFFECT_SHADOW_TABLE,
    assistant_affect_shadow_enabled,
    require_assistant_affect_shadow_schema,
    set_assistant_affect_shadow_feature,
)


def _utc(value: object | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("assistant_affect_shadow_time_invalid")
    return parsed.astimezone(timezone.utc)


def _shadow_id(value: Mapping[str, object]) -> str:
    key = "|".join(
        str(value[item])
        for item in ("assistant_id", "scope_type", "scope_ref", "topic_ref", "topic_revision", "target_type", "target_ref")
    )
    return "assistant-affect-shadow:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _stored(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        triggers = json.loads(str(item.pop("trigger_evidence_refs_json")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("assistant_affect_shadow_stored_json_invalid") from exc
    return {**item, "trigger_evidence_refs": triggers}


def record_assistant_affect_shadow(conn: sqlite3.Connection, value: Mapping[str, object]) -> dict | None:
    """Store a validated Shadow state; no caller receives an expression directive."""

    if not assistant_affect_shadow_enabled(conn):
        return None
    normalized = make_assistant_affect_shadow(value)
    identifier = _shadow_id(normalized)
    triggers_json = json.dumps(normalized["trigger_evidence_refs"], ensure_ascii=True, separators=(",", ":"))
    conn.execute(
        f"""
        INSERT INTO {ASSISTANT_AFFECT_SHADOW_TABLE}(
            id,assistant_id,scope_type,scope_ref,topic_ref,topic_revision,target_type,target_ref,primary_affect,valence,
            arousal,intensity,confidence,trigger_evidence_refs_json,reason_code,created_at,last_updated_at,expires_at,
            decay_policy,policy_version,state
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(assistant_id,scope_type,scope_ref,topic_ref,topic_revision,target_type,target_ref) DO UPDATE SET
            primary_affect=excluded.primary_affect,valence=excluded.valence,arousal=excluded.arousal,
            intensity=excluded.intensity,confidence=excluded.confidence,trigger_evidence_refs_json=excluded.trigger_evidence_refs_json,
            reason_code=excluded.reason_code,last_updated_at=excluded.last_updated_at,expires_at=excluded.expires_at,
            decay_policy=excluded.decay_policy,policy_version=excluded.policy_version,state='shadow'
        """,
        (
            identifier, normalized["assistant_id"], normalized["scope_type"], normalized["scope_ref"], normalized["topic_ref"],
            normalized["topic_revision"], normalized["target_type"], normalized["target_ref"], normalized["primary_affect"],
            normalized["valence"], normalized["arousal"], normalized["intensity"], normalized["confidence"], triggers_json,
            normalized["reason_code"], normalized["created_at"], normalized["last_updated_at"], normalized["expires_at"],
            normalized["decay_policy"], normalized["policy_version"], "shadow",
        ),
    )
    row = conn.execute(f"SELECT * FROM {ASSISTANT_AFFECT_SHADOW_TABLE} WHERE id=?", (identifier,)).fetchone()
    return _stored(row)


def list_assistant_affect_shadows(conn: sqlite3.Connection, *, assistant_id: object, limit: int = 100) -> list[dict]:
    require_assistant_affect_shadow_schema(conn)
    owner = str(assistant_id or "").strip()
    if not owner:
        raise ValueError("assistant_affect_shadow_assistant_id_required")
    safe_limit = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"SELECT * FROM {ASSISTANT_AFFECT_SHADOW_TABLE} WHERE assistant_id=? ORDER BY last_updated_at DESC,id DESC LIMIT ?",
        (owner, safe_limit),
    ).fetchall()
    return [_stored(row) for row in rows if row is not None]


def purge_expired_assistant_affect_shadows(conn: sqlite3.Connection, *, now: object | None = None) -> dict:
    require_assistant_affect_shadow_schema(conn)
    deleted = conn.execute(
        f"DELETE FROM {ASSISTANT_AFFECT_SHADOW_TABLE} WHERE expires_at<=?",
        (_utc(now).isoformat(),),
    ).rowcount
    return {"deleted_affect_shadows": int(deleted)}


def shadow_expression_projection(value: Mapping[str, object]) -> dict:
    """The one permitted BE-3 projection: proof that no expression is applied."""

    candidate = dict(value)
    candidate.pop("id", None)
    candidate["schema_version"] = 1
    make_assistant_affect_shadow(candidate)
    return {"state": "shadow_only", "active": False, "allowed_influences": []}


__all__ = [
    "list_assistant_affect_shadows",
    "purge_expired_assistant_affect_shadows",
    "record_assistant_affect_shadow",
    "set_assistant_affect_shadow_feature",
    "shadow_expression_projection",
]
