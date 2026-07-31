#!/usr/bin/env python3
"""Gate 8 relationship state and proactive-policy service.

The service keeps relationship context, social proactive chat, and operational
notifications as separate domains. All mutations are feature-gated,
version-checked, and idempotent so QQ and Web can safely retry the same action.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import uuid
from typing import Iterator

from bridge_assistant_identity import current_assistant
from bridge_automation import upsert_proactive_policy
from bridge_migrations import utc_now
from bridge_relationship_proactive_schema import (
    RELATIONSHIP_PROACTIVE_FEATURE_FLAG,
    require_relationship_proactive_schema,
)
from bridge_relationship_cutover import (
    relationship_proactive_cutover_plan,
    set_relationship_proactive_feature,
)


RELATIONSHIP_SCOPE_TYPES = {
    "private_user",
    "channel_thread",
    "qq_group",
    "project",
    "global_preference",
    "sensitive_private",
}
FAMILIARITY_CONTEXTS = {"new", "familiar", "long_term"}
INTERACTION_STYLES = {"natural", "quiet", "supportive", "playful", "direct"}
NOTIFICATION_CATEGORIES = {
    "approval",
    "task_completed",
    "task_failed",
    "delivery_failed",
    "security",
    "resource",
}
DEFAULT_NOTIFICATION_CATEGORIES = (
    "approval",
    "task_completed",
    "task_failed",
    "delivery_failed",
    "security",
    "resource",
)
def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _json_list(value: object, *, allowed: set[str] | None = None) -> list[str]:
    if value is None:
        items: list[object] = []
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in value.split(",")]
        items = parsed if isinstance(parsed, list) else []
    else:
        items = []
    result = sorted({_clip(item, 120) for item in items if _clip(item, 120)})
    if allowed is not None and any(item not in allowed for item in result):
        raise ValueError("unsupported_list_value")
    return result


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _clock(value: object, default: str) -> str:
    text = _clip(value, 5) or default
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("invalid_clock")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError("invalid_clock") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid_clock")
    return f"{hour:02d}:{minute:02d}"


def _active_assistant(conn: sqlite3.Connection) -> dict:
    assistant = current_assistant(conn)
    if not assistant:
        raise ValueError("active_assistant_missing")
    return assistant


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Use an immediate transaction, or a savepoint for an existing caller tx."""

    if conn.in_transaction:
        name = f"gate8_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def relationship_proactive_feature_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (RELATIONSHIP_PROACTIVE_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def _require_feature(conn: sqlite3.Connection) -> None:
    require_relationship_proactive_schema(conn)
    if not relationship_proactive_feature_enabled(conn):
        raise ValueError("relationship_proactive_feature_disabled")


def _idempotent_replay(
    conn: sqlite3.Connection,
    *,
    action: str,
    idempotency_key: str,
    request_hash: str,
) -> dict | None:
    key = _clip(idempotency_key, 160)
    if not key:
        raise ValueError("idempotency_key_required")
    row = conn.execute(
        """
        SELECT request_hash,response_json
        FROM assistant_idempotency_records
        WHERE action=? AND idempotency_key=?
        """,
        (action, key),
    ).fetchone()
    if not row:
        return None
    if str(row[0]) != request_hash:
        raise ValueError("idempotency_key_payload_conflict")
    try:
        result = json.loads(str(row[1] or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("idempotency_record_corrupt") from exc
    if not isinstance(result, dict):
        raise ValueError("idempotency_record_corrupt")
    result["idempotent_replay"] = True
    return result


def _save_idempotency(
    conn: sqlite3.Connection,
    *,
    action: str,
    idempotency_key: str,
    request_hash: str,
    response: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO assistant_idempotency_records(
            action,idempotency_key,request_hash,response_json,created_at
        ) VALUES(?,?,?,?,?)
        """,
        (
            action,
            _clip(idempotency_key, 160),
            request_hash,
            _canonical_json(response),
            utc_now(),
        ),
    )


def _relationship_public(row: sqlite3.Row | tuple | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["allowed_topics"] = _json_list(item.pop("allowed_topics_json", "[]"))
    item["blocked_topics"] = _json_list(item.pop("blocked_topics_json", "[]"))
    item["social_proactive_enabled"] = bool(item["social_proactive_enabled"])
    item["version"] = int(item["version"])
    return item


def get_relationship_state(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    scope_type: str = "private_user",
    scope_id: str = "",
) -> dict:
    require_relationship_proactive_schema(conn)
    assistant = _active_assistant(conn)
    user = _clip(user_id, 80)
    scope = _clip(scope_type, 40)
    if not user:
        raise ValueError("user_id_required")
    if scope not in RELATIONSHIP_SCOPE_TYPES:
        raise ValueError("invalid_relationship_scope")
    normalized_scope_id = _clip(scope_id, 160)
    row = conn.execute(
        """
        SELECT * FROM relationship_states
        WHERE assistant_id=? AND user_id=? AND scope_type=? AND scope_id=?
        """,
        (assistant["id"], user, scope, normalized_scope_id),
    ).fetchone()
    existing = _relationship_public(row)
    if existing:
        return existing
    return {
        "id": "",
        "assistant_id": assistant["id"],
        "user_id": user,
        "scope_type": scope,
        "scope_id": normalized_scope_id,
        "preferred_address": "",
        "interaction_style": "natural",
        "familiarity_context": "new",
        "allowed_topics": [],
        "blocked_topics": [],
        "social_proactive_enabled": False,
        "version": 0,
        "created_at": "",
        "updated_at": "",
    }


def update_relationship_state(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    idempotency_key: str,
) -> dict:
    _require_feature(conn)
    assistant = _active_assistant(conn)
    user_id = _clip(payload.get("user_id"), 80)
    scope_type = _clip(payload.get("scope_type") or "private_user", 40)
    scope_id = _clip(payload.get("scope_id"), 160)
    if not user_id:
        raise ValueError("user_id_required")
    if scope_type not in RELATIONSHIP_SCOPE_TYPES:
        raise ValueError("invalid_relationship_scope")
    interaction_style = _clip(payload.get("interaction_style") or "natural", 30)
    familiarity_context = _clip(payload.get("familiarity_context") or "new", 30)
    if interaction_style not in INTERACTION_STYLES:
        raise ValueError("invalid_interaction_style")
    if familiarity_context not in FAMILIARITY_CONTEXTS:
        raise ValueError("invalid_familiarity_context")
    allowed_topics = _json_list(payload.get("allowed_topics"))
    blocked_topics = _json_list(payload.get("blocked_topics"))
    if set(allowed_topics) & set(blocked_topics):
        raise ValueError("relationship_topic_conflict")
    try:
        expected_version = int(payload.get("expected_version", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_version_required") from exc
    normalized = {
        "assistant_id": assistant["id"],
        "user_id": user_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "preferred_address": _clip(payload.get("preferred_address"), 80),
        "interaction_style": interaction_style,
        "familiarity_context": familiarity_context,
        "allowed_topics": allowed_topics,
        "blocked_topics": blocked_topics,
        "social_proactive_enabled": _truthy(payload.get("social_proactive_enabled")),
        "expected_version": expected_version,
    }
    action = f"relationship:{assistant['id']}:{user_id}:{scope_type}:{scope_id}"
    request_hash = _request_hash(normalized)
    with _write_transaction(conn):
        replay = _idempotent_replay(
            conn,
            action=action,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        existing = get_relationship_state(
            conn,
            user_id=user_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        if expected_version != int(existing["version"]):
            raise ValueError("stale_relationship_version")
        now = utc_now()
        next_version = expected_version + 1
        row_id = existing["id"] or f"rel_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO relationship_states(
                id,assistant_id,user_id,scope_type,scope_id,preferred_address,
                interaction_style,familiarity_context,allowed_topics_json,
                blocked_topics_json,social_proactive_enabled,version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(assistant_id,user_id,scope_type,scope_id) DO UPDATE SET
                preferred_address=excluded.preferred_address,
                interaction_style=excluded.interaction_style,
                familiarity_context=excluded.familiarity_context,
                allowed_topics_json=excluded.allowed_topics_json,
                blocked_topics_json=excluded.blocked_topics_json,
                social_proactive_enabled=excluded.social_proactive_enabled,
                version=excluded.version,updated_at=excluded.updated_at
            """,
            (
                row_id,
                assistant["id"],
                user_id,
                scope_type,
                scope_id,
                normalized["preferred_address"],
                interaction_style,
                familiarity_context,
                _canonical_json(allowed_topics),
                _canonical_json(blocked_topics),
                1 if normalized["social_proactive_enabled"] else 0,
                next_version,
                now,
                now,
            ),
        )
        response = get_relationship_state(
            conn,
            user_id=user_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        response["idempotent_replay"] = False
        _save_idempotency(
            conn,
            action=action,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=response,
        )
        return response


def _notification_public(row: sqlite3.Row | tuple | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["enabled_categories"] = _json_list(
        item.pop("enabled_categories_json", "[]"),
        allowed=NOTIFICATION_CATEGORIES,
    )
    item["critical_bypass_quiet"] = bool(item["critical_bypass_quiet"])
    item["group_window_minutes"] = int(item["group_window_minutes"])
    item["version"] = int(item["version"])
    return item


def get_notification_policy(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    channel_scope: str = "owner",
) -> dict:
    require_relationship_proactive_schema(conn)
    assistant = _active_assistant(conn)
    user = _clip(user_id, 80)
    channel = _clip(channel_scope or "owner", 80)
    if not user:
        raise ValueError("user_id_required")
    row = conn.execute(
        """
        SELECT * FROM operational_notification_policies
        WHERE assistant_id=? AND user_id=? AND channel_scope=?
        """,
        (assistant["id"], user, channel),
    ).fetchone()
    existing = _notification_public(row)
    if existing:
        return existing
    return {
        "id": "",
        "assistant_id": assistant["id"],
        "user_id": user,
        "channel_scope": channel,
        "enabled_categories": list(DEFAULT_NOTIFICATION_CATEGORIES),
        "quiet_start": "23:30",
        "quiet_end": "09:00",
        "critical_bypass_quiet": True,
        "group_window_minutes": 10,
        "version": 0,
        "created_at": "",
        "updated_at": "",
    }


def update_notification_policy(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    idempotency_key: str,
) -> dict:
    _require_feature(conn)
    assistant = _active_assistant(conn)
    user_id = _clip(payload.get("user_id"), 80)
    channel_scope = _clip(payload.get("channel_scope") or "owner", 80)
    if not user_id:
        raise ValueError("user_id_required")
    categories = _json_list(
        payload.get("enabled_categories", DEFAULT_NOTIFICATION_CATEGORIES),
        allowed=NOTIFICATION_CATEGORIES,
    )
    if not categories:
        raise ValueError("notification_category_required")
    try:
        expected_version = int(payload.get("expected_version", -1))
        group_window_minutes = int(payload.get("group_window_minutes", 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_notification_policy_number") from exc
    if not 0 <= group_window_minutes <= 1440:
        raise ValueError("invalid_group_window_minutes")
    normalized = {
        "assistant_id": assistant["id"],
        "user_id": user_id,
        "channel_scope": channel_scope,
        "enabled_categories": categories,
        "quiet_start": _clock(payload.get("quiet_start"), "23:30"),
        "quiet_end": _clock(payload.get("quiet_end"), "09:00"),
        "critical_bypass_quiet": _truthy(
            payload.get("critical_bypass_quiet", True),
        ),
        "group_window_minutes": group_window_minutes,
        "expected_version": expected_version,
    }
    action = f"notification:{assistant['id']}:{user_id}:{channel_scope}"
    request_hash = _request_hash(normalized)
    with _write_transaction(conn):
        replay = _idempotent_replay(
            conn,
            action=action,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        existing = get_notification_policy(
            conn,
            user_id=user_id,
            channel_scope=channel_scope,
        )
        if expected_version != int(existing["version"]):
            raise ValueError("stale_notification_policy_version")
        now = utc_now()
        next_version = expected_version + 1
        row_id = existing["id"] or f"notify_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO operational_notification_policies(
                id,assistant_id,user_id,channel_scope,enabled_categories_json,
                quiet_start,quiet_end,critical_bypass_quiet,group_window_minutes,
                version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(assistant_id,user_id,channel_scope) DO UPDATE SET
                enabled_categories_json=excluded.enabled_categories_json,
                quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,
                critical_bypass_quiet=excluded.critical_bypass_quiet,
                group_window_minutes=excluded.group_window_minutes,
                version=excluded.version,updated_at=excluded.updated_at
            """,
            (
                row_id,
                assistant["id"],
                user_id,
                channel_scope,
                _canonical_json(categories),
                normalized["quiet_start"],
                normalized["quiet_end"],
                1 if normalized["critical_bypass_quiet"] else 0,
                group_window_minutes,
                next_version,
                now,
                now,
            ),
        )
        response = get_notification_policy(
            conn,
            user_id=user_id,
            channel_scope=channel_scope,
        )
        response["idempotent_replay"] = False
        _save_idempotency(
            conn,
            action=action,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=response,
        )
        return response


def _social_public(row: sqlite3.Row | tuple | None, *, assistant_id: str, user_id: str) -> dict:
    if row is None:
        return {
            "user_id": user_id,
            "assistant_id": assistant_id,
            "policy_kind": "social",
            "policy_version": 0,
            "enabled": False,
            "authorized": False,
            "timezone": "Asia/Shanghai",
            "quiet_start": "23:30",
            "quiet_end": "09:00",
            "min_silence_minutes": 180,
            "min_gap_minutes": 360,
            "daily_limit": 2,
            "weekly_limit": 5,
            "unanswered_limit": 2,
            "evaluation_interval_minutes": 60,
            "topic_notes": "",
            "include_meme": False,
            "initiative_mode": "balanced",
            "allowed_intents": ["check_in", "follow_up"],
            "schedule_jitter_minutes": 20,
            "topic_cooldown_minutes": 1440,
            "trigger_reason_required": True,
            "condition_contract": {},
            "state": "disabled",
            "state_reason": "",
            "next_check_at": "",
            "last_evaluated_at": "",
            "last_sent_at": "",
            "last_user_at": "",
            "consecutive_unanswered": 0,
            "decision_count": 0,
            "skip_count": 0,
            "failed_count": 0,
            "created_at": "",
            "updated_at": "",
        }
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["authorized"] = bool(item["authorized"])
    item["include_meme"] = bool(item["include_meme"])
    item["trigger_reason_required"] = bool(item["trigger_reason_required"])
    item["allowed_intents"] = _json_list(item.get("allowed_intents"))
    item["condition_contract"] = _json_object(
        item.pop("condition_contract_json", "{}"),
    )
    item.pop("lease_until", None)
    return item


def get_social_proactive_policy(
    conn: sqlite3.Connection,
    *,
    user_id: str,
) -> dict:
    require_relationship_proactive_schema(conn)
    assistant = _active_assistant(conn)
    user = _clip(user_id, 80)
    if not user:
        raise ValueError("user_id_required")
    row = conn.execute(
        "SELECT * FROM proactive_policies WHERE user_id=?",
        (user,),
    ).fetchone()
    return _social_public(row, assistant_id=assistant["id"], user_id=user)


def update_social_proactive_policy(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    idempotency_key: str,
) -> dict:
    """Version the existing social proactive policy without mixing notifications."""

    _require_feature(conn)
    assistant = _active_assistant(conn)
    user_id = _clip(payload.get("user_id"), 80)
    if not user_id:
        raise ValueError("user_id_required")
    try:
        expected_version = int(payload.get("expected_version", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_version_required") from exc
    normalized = {
        key: value
        for key, value in payload.items()
        if key not in {"idempotency_key"}
    }
    normalized["assistant_id"] = assistant["id"]
    normalized["policy_kind"] = "social"
    action = f"social_proactive:{assistant['id']}:{user_id}"
    request_hash = _request_hash(normalized)
    with _write_transaction(conn):
        replay = _idempotent_replay(
            conn,
            action=action,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        existing = conn.execute(
            "SELECT * FROM proactive_policies WHERE user_id=?",
            (user_id,),
        ).fetchone()
        current_version = int(dict(existing).get("policy_version") or 1) if existing else 0
        if expected_version != current_version:
            raise ValueError("stale_social_proactive_policy_version")
        response = upsert_proactive_policy(conn, payload)
        contract = _json_object(payload.get("condition_contract"))
        conn.execute(
            """
            UPDATE proactive_policies
            SET assistant_id=?,policy_kind='social',policy_version=?,
                trigger_reason_required=1,condition_contract_json=?,updated_at=?
            WHERE user_id=?
            """,
            (
                assistant["id"],
                current_version + 1,
                _canonical_json(contract),
                utc_now(),
                user_id,
            ),
        )
        response = _social_public(
            conn.execute(
                "SELECT * FROM proactive_policies WHERE user_id=?",
                (user_id,),
            ).fetchone(),
            assistant_id=assistant["id"],
            user_id=user_id,
        )
        response["idempotent_replay"] = False
        _save_idempotency(
            conn,
            action=action,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=response,
        )
        return response


__all__ = [
    "DEFAULT_NOTIFICATION_CATEGORIES",
    "FAMILIARITY_CONTEXTS",
    "INTERACTION_STYLES",
    "NOTIFICATION_CATEGORIES",
    "RELATIONSHIP_SCOPE_TYPES",
    "get_notification_policy",
    "get_relationship_state",
    "get_social_proactive_policy",
    "relationship_proactive_cutover_plan",
    "relationship_proactive_feature_enabled",
    "set_relationship_proactive_feature",
    "update_notification_policy",
    "update_relationship_state",
    "update_social_proactive_policy",
]
