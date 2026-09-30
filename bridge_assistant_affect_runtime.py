#!/usr/bin/env python3
"""Expression-only projection and restart-safe decay for Assistant Affect."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from bridge_assistant_affect_contract import (
    make_assistant_affect_shadow,
    render_assistant_affect_influence,
)
from bridge_assistant_affect_runtime_schema import assistant_affect_expression_enabled
from bridge_assistant_affect_shadow_schema import (
    ASSISTANT_AFFECT_SHADOW_TABLE,
    assistant_affect_shadow_enabled,
)


ASSISTANT_AFFECT_MAX_TURNS = 4
_MIN_EFFECTIVE_INTENSITY = 0.08
_TONE_GUIDANCE = {
    "light": "语气可以轻一点，但不要夸张表演",
    "warm": "语气自然温暖，不把感谢扩写成讨好",
    "engaged": "保持好奇和投入，只围绕当前明确内容",
    "calm": "更谨慎、平静，承认具体纠正而不自我辩解",
    "firm": "简短而有边界，不反击、不卖惨",
    "sharp": "直接收紧措辞，但不攻击、不升级冲突",
    "direct": "直说当前要点，不绕弯也不贴标签",
    "concise": "更简洁，只保留当前必要回应",
}


def _utc(value: object | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("assistant_affect_runtime_time_invalid")
    return parsed.astimezone(timezone.utc)


def _opaque_ref(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return f"{prefix}:sha256-{digest}"


def _inactive(**metadata: object) -> dict:
    return {
        "style_only": True,
        "active": False,
        "allowed_influences": [],
        "effective_intensity": 0.0,
        "turns_elapsed": int(metadata.pop("turns_elapsed", 0) or 0),
        "time_decay_applied": bool(metadata.pop("time_decay_applied", False)),
        "turn_decay_applied": bool(metadata.pop("turn_decay_applied", False)),
        **metadata,
    }


def _row_dict(cursor: sqlite3.Cursor, row: object) -> dict:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    names = [str(item[0]) for item in cursor.description or ()]
    return dict(zip(names, tuple(row)))


def _turns_elapsed(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    origin_revision: int,
    current_revision: int,
) -> int:
    if current_revision <= origin_revision:
        return 0
    if scope_type == "private":
        # Private inbound_sequence advances only for durable user inputs.
        return current_revision - origin_revision
    if scope_type == "group":
        row = conn.execute(
            "SELECT count(*) FROM group_messages WHERE group_id=? AND id>? AND id<=?",
            (scope_id, origin_revision, current_revision),
        ).fetchone()
        return int(row[0] if row else 0)
    raise ValueError("assistant_affect_runtime_scope_invalid")


def load_assistant_affect_influence(
    conn: sqlite3.Connection,
    *,
    assistant_id: object,
    scope_type: object,
    scope_id: object,
    target_id: object,
    turn_revision: object,
    now: object | None = None,
) -> dict:
    """Load one grounded state and decay it by both time and durable turns.

    The lookup may advance beyond the originating revision only inside the same
    Assistant, scope and target.  It never changes routing, facts or delivery.
    """

    if not assistant_affect_expression_enabled(conn) or not assistant_affect_shadow_enabled(conn):
        return _inactive(feature_enabled=False)
    owner = str(assistant_id or "").strip()
    normalized_scope = str(scope_type or "").strip().lower()
    raw_scope = str(scope_id or "").strip()
    raw_target = str(target_id or "").strip()
    try:
        revision = int(turn_revision)
    except (TypeError, ValueError):
        return _inactive(reason="turn_revision_invalid")
    if not owner or normalized_scope not in {"private", "group"} or not raw_scope or not raw_target or revision < 0:
        return _inactive(reason="binding_invalid")
    current_time = _utc(now)
    scope_ref = _opaque_ref("scope", raw_scope)
    target_ref = _opaque_ref("member", raw_target)
    cursor = conn.execute(
        f"""
        SELECT * FROM {ASSISTANT_AFFECT_SHADOW_TABLE}
        WHERE assistant_id=? AND scope_type=? AND scope_ref=?
          AND target_type='member' AND target_ref=? AND topic_revision<=?
        ORDER BY topic_revision DESC,last_updated_at DESC,id DESC LIMIT 1
        """,
        (owner, normalized_scope, scope_ref, target_ref, revision),
    )
    row = cursor.fetchone()
    if row is None:
        return _inactive(feature_enabled=True)
    stored = _row_dict(cursor, row)
    try:
        triggers = json.loads(str(stored.pop("trigger_evidence_refs_json")))
        state_id = str(stored.pop("id"))
        normalized = make_assistant_affect_shadow({
            **stored,
            "schema_version": 1,
            "trigger_evidence_refs": triggers,
        })
        updated_at = _utc(normalized["last_updated_at"])
        expires_at = _utc(normalized["expires_at"])
        turns = _turns_elapsed(
            conn,
            scope_type=normalized_scope,
            scope_id=raw_scope,
            origin_revision=int(normalized["topic_revision"]),
            current_revision=revision,
        )
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return _inactive(reason="stored_state_invalid")
    if current_time >= expires_at or turns >= ASSISTANT_AFFECT_MAX_TURNS:
        return _inactive(
            feature_enabled=True,
            state_id=state_id,
            turns_elapsed=turns,
            time_decay_applied=current_time > updated_at,
            turn_decay_applied=turns > 0,
        )
    lifetime = max((expires_at - updated_at).total_seconds(), 1.0)
    elapsed = max((current_time - updated_at).total_seconds(), 0.0)
    time_factor = max(0.0, min(1.0, 1.0 - elapsed / lifetime))
    turn_factor = max(0.0, min(1.0, 1.0 - turns / ASSISTANT_AFFECT_MAX_TURNS))
    effective = round(float(normalized["intensity"]) * time_factor * turn_factor, 4)
    if effective < _MIN_EFFECTIVE_INTENSITY:
        return _inactive(
            feature_enabled=True,
            state_id=state_id,
            turns_elapsed=turns,
            time_decay_applied=elapsed > 0,
            turn_decay_applied=turns > 0,
        )
    exact_origin = {
        key: normalized[key]
        for key in ("scope_type", "scope_ref", "topic_ref", "topic_revision", "target_type", "target_ref")
    }
    style = render_assistant_affect_influence(
        normalized,
        exact_origin,
        {"target": "expression_plan"},
        now=current_time,
    )
    if not style.get("active"):
        return _inactive(feature_enabled=True, state_id=state_id, turns_elapsed=turns)
    return {
        **style,
        "feature_enabled": True,
        "state_id": state_id,
        "primary_affect": str(normalized["primary_affect"]),
        "effective_intensity": effective,
        "turns_elapsed": turns,
        "time_decay_applied": elapsed > 0,
        "turn_decay_applied": turns > 0,
        "recovered": elapsed > 0 or turns > 0,
    }


def resolve_private_affect_binding(
    conn: sqlite3.Connection,
    *,
    user_id: object,
    interaction_context: Mapping[str, object] | None,
) -> dict | None:
    """Resolve the exact frozen private source set without creating state."""

    context = interaction_context or {}
    raw_ids = context.get("_response_cycle_source_message_ids")
    if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 64:
        return None
    message_ids = [str(item or "").strip() for item in raw_ids]
    if any(not item for item in message_ids) or len(set(message_ids)) != len(message_ids):
        return None
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"""
        SELECT m.thread_id,m.inbound_sequence,t.assistant_id
        FROM conversation_messages m
        JOIN conversation_threads t ON t.id=m.thread_id
        WHERE m.id IN ({placeholders}) AND m.role='user' AND m.inbound_sequence IS NOT NULL
        """,
        tuple(message_ids),
    ).fetchall()
    if len(rows) != len(message_ids):
        return None
    thread_ids = {str(row[0]) for row in rows}
    assistant_ids = {str(row[2]) for row in rows}
    target = str(user_id or "").strip()
    if len(thread_ids) != 1 or len(assistant_ids) != 1 or not target:
        return None
    return {
        "assistant_id": next(iter(assistant_ids)),
        "scope_type": "private",
        "scope_id": next(iter(thread_ids)),
        "target_id": target,
        "turn_revision": max(int(row[1]) for row in rows),
    }


def apply_assistant_affect_to_expression_plan(
    plan: Mapping[str, object],
    influence: Mapping[str, object] | None,
) -> dict:
    """Apply only allowlisted style changes to an existing expression plan."""

    result = dict(plan or {})
    item = dict(influence or {})
    if not item.get("active") or not item.get("style_only"):
        return result
    effective = float(item.get("effective_intensity") or 0.0)
    if effective < _MIN_EFFECTIVE_INTENSITY:
        return result
    tone_key = str(item.get("tone") or "").strip()
    guidance = _TONE_GUIDANCE.get(tone_key, "只做轻微表达调整，不改变事实和行动")
    current_tone = str(result.get("tone") or "自然").strip()
    result["tone"] = f"{current_tone}；{guidance}"
    try:
        sentence_limit = max(1, int(result.get("sentence_limit") or 1))
    except (TypeError, ValueError):
        sentence_limit = 1
    if str(item.get("sentence_length") or "") == "short":
        result["sentence_limit"] = min(sentence_limit, 2)
    structure = list(result.get("structure") or [])
    structure.append("Assistant 自身短时状态只微调表达；不得据此推断用户心理或改变事实、权限与行动")
    result["structure"] = structure
    result["assistant_affect"] = {
        "active": True,
        "style_only": True,
        "primary_affect": str(item.get("primary_affect") or ""),
        "effective_intensity": effective,
        "turns_elapsed": int(item.get("turns_elapsed") or 0),
        "expression_guidance": guidance,
    }
    return result


__all__ = [
    "ASSISTANT_AFFECT_MAX_TURNS",
    "apply_assistant_affect_to_expression_plan",
    "load_assistant_affect_influence",
    "resolve_private_affect_binding",
]
