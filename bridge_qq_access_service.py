#!/usr/bin/env python3
"""Gate C1 QQ identity, role, and access-control service."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import uuid
from typing import Iterator

from bridge_assistant_migrations import record_security_audit
from bridge_migrations import utc_now
from bridge_qq_access_schema import (
    QQ_ACCESS_FEATURE_FLAG,
    QQ_CHANNEL_ID,
    require_qq_access_schema,
)
from bridge_qq_runtime_service import (
    get_runtime_settings,
    get_runtime_summary,
    normalize_runtime_settings,
)


QQ_ID_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")
ADMIN_ROLES = {"super_admin", "admin", "operator"}
ROLE_PRIORITY = ("super_admin", "admin", "operator", "user")
SAFE_USER_ACTIONS = {
    "chat",
    "ask",
    "code",
    "help",
    "status",
    "github_trending",
}
OBJECT_ACTIONS = {"tasks", "task_stats", "result", "cancel", "retry", "project", "projects", "memory"}
OPERATOR_ACTIONS = SAFE_USER_ACTIONS | {
    "health",
    "tasks",
    "task_stats",
    "result",
    "cancel",
    "retry",
}
ADMIN_ACTIONS = OPERATOR_ACTIONS | {
    "project",
    "projects",
    "memory",
    "persona",
    "relationship",
    "settings",
    "approval",
    "models",
}
GROUP_PARTICIPATION_MODES = {
    "disabled",
    "mentions_only",
    "directed_context",
    "natural_participation",
}


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _normalize_group_participation(value: object) -> dict | None:
    """Validate an explicit atomic sync request for enabled QQ groups.

    QQ access and participation are different policies.  A caller must opt in
    to synchronising them; omitted data deliberately preserves the existing
    fail-closed behavior for legacy/API callers.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("group_participation_invalid")
    if not _truthy(value.get("apply_to_enabled_groups")):
        raise ValueError("group_participation_sync_required")
    mode = _clip(value.get("participation_mode"), 40)
    if mode not in GROUP_PARTICIPATION_MODES:
        raise ValueError("group_participation_mode_invalid")
    try:
        probability = float(value.get("reply_probability"))
    except (TypeError, ValueError) as exc:
        raise ValueError("group_participation_probability_invalid") from exc
    if not 0.0 <= probability <= 1.0:
        raise ValueError("group_participation_probability_invalid")
    return {"participation_mode": mode, "reply_probability": probability}


def _sync_enabled_group_participation(
    conn: sqlite3.Connection,
    groups: list[dict],
    participation: dict,
) -> int:
    """Apply one requested behavior to the currently enabled QQ group scope."""

    from bridge_group_policy_store import get_group_policy, upsert_group_policy
    from bridge_social_experience import ensure_social_experience_tables

    ensure_social_experience_tables(conn)
    changed = 0
    for entry in groups:
        if not entry["enabled"]:
            continue
        group_id = entry["subject_id"]
        existing = get_group_policy(conn, group_id) or {}
        payload = dict(existing)
        payload.update({
            "group_id": group_id,
            "group_name": str(existing.get("group_name") or entry.get("remark") or ""),
            "participation_mode": participation["participation_mode"],
            "reply_probability": participation["reply_probability"],
        })
        upsert_group_policy(conn, payload)
        changed += 1
    return changed


def _group_participation_summary(conn: sqlite3.Connection, groups: list[dict]) -> dict:
    """Project the effective policy beside the QQ allowlist without duplicating it."""

    try:
        from bridge_group_policy_store import get_group_policy
    except ImportError:
        return {"state": "unavailable", "groups": []}
    items = []
    for entry in groups:
        try:
            policy = get_group_policy(conn, entry["group_id"])
        except sqlite3.OperationalError:
            return {"state": "unavailable", "groups": []}
        items.append({
            "group_id": entry["group_id"],
            "enabled": bool(entry["enabled"]),
            "participation_mode": str((policy or {}).get("participation_mode") or "unconfigured"),
            "reply_probability": (policy or {}).get("reply_probability"),
        })
    enabled = [item for item in items if item["enabled"]]
    modes = {item["participation_mode"] for item in enabled}
    probabilities = {item["reply_probability"] for item in enabled}
    state = "unconfigured" if not enabled or "unconfigured" in modes else "mixed"
    if len(modes) == 1 and "unconfigured" not in modes:
        state = "uniform"
    return {
        "state": state,
        "participation_mode": next(iter(modes)) if len(modes) == 1 else "",
        "reply_probability": next(iter(probabilities)) if len(probabilities) == 1 else None,
        "groups": items,
    }


def _qq_id(value: object, error: str = "qq_id_invalid") -> str:
    text = _clip(value, 20)
    if not QQ_ID_PATTERN.fullmatch(text):
        raise ValueError(error)
    return text


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        name = f"qq_access_{uuid.uuid4().hex}"
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


def qq_access_feature_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (QQ_ACCESS_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def _settings_row(conn: sqlite3.Connection):
    row = conn.execute(
        """
        SELECT channel_id,channel_enabled,access_mode,private_chat_enabled,
               group_chat_enabled,config_version,updated_by,updated_at
        FROM qq_channel_settings WHERE channel_id=?
        """,
        (QQ_CHANNEL_ID,),
    ).fetchone()
    if not row:
        raise ValueError("qq_channel_settings_missing")
    return row


def _role_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.qq_id,i.display_name,r.role,r.enabled,r.created_by,r.created_at,r.updated_at
        FROM qq_role_assignments r
        JOIN qq_identities i ON i.id=r.identity_id
        WHERE i.status='active' AND r.enabled=1
          AND r.role IN ('super_admin','admin','operator')
        ORDER BY CASE r.role WHEN 'super_admin' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END,
                 i.qq_id
        """,
    ).fetchall()
    return [
        {
            "qq_id": str(row[0]),
            "display_name": str(row[1] or ""),
            "role": str(row[2]),
            "enabled": bool(row[3]),
            "created_by": str(row[4] or ""),
            "created_at": str(row[5] or ""),
            "updated_at": str(row[6] or ""),
        }
        for row in rows
    ]


def _access_rows(conn: sqlite3.Connection, subject_type: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT subject_id,enabled,remark,created_by,created_at,updated_at
        FROM qq_access_entries
        WHERE subject_type=? AND enabled=1
        ORDER BY subject_id
        """,
        (subject_type,),
    ).fetchall()
    return [
        {
            "qq_id" if subject_type == "private_user" else "group_id": str(row[0]),
            "enabled": bool(row[1]),
            "remark": str(row[2] or ""),
            "created_by": str(row[3] or ""),
            "created_at": str(row[4] or ""),
            "updated_at": str(row[5] or ""),
        }
        for row in rows
    ]


def get_qq_access_settings(conn: sqlite3.Connection) -> dict:
    require_qq_access_schema(conn)
    row = _settings_row(conn)
    runtime_settings = get_runtime_settings(conn)
    group_allowlist = _access_rows(conn, "qq_group")
    return {
        "feature_enabled": qq_access_feature_enabled(conn),
        "settings": {
            "channel_id": str(row[0]),
            "channel_enabled": bool(row[1]),
            "access_mode": str(row[2]),
            "private_chat_enabled": bool(row[3]),
            "group_chat_enabled": bool(row[4]),
            "config_version": int(row[5]),
            "updated_by": str(row[6] or ""),
            "updated_at": str(row[7] or ""),
            "expected_bot_id": runtime_settings["expected_bot_id"],
            "command_prefixes": runtime_settings["command_prefixes"],
            "auto_private_chat": runtime_settings["auto_private_chat"],
            "reply_max_chars": runtime_settings["reply_max_chars"],
            "delivery_poll_seconds": runtime_settings["delivery_poll_seconds"],
            "notification_interval_seconds": runtime_settings[
                "notification_interval_seconds"
            ],
        },
        "administrators": _role_rows(conn),
        "private_allowlist": _access_rows(conn, "private_user"),
        "group_allowlist": group_allowlist,
        "group_participation": _group_participation_summary(conn, group_allowlist),
        "runtime": get_runtime_summary(conn),
    }


def _normalize_administrators(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("administrators_must_be_list")
    result: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("administrator_invalid")
        qq_id = _qq_id(item.get("qq_id"), "administrator_qq_id_invalid")
        if qq_id in seen:
            raise ValueError("administrator_qq_id_duplicate")
        seen.add(qq_id)
        role = _clip(item.get("role"), 20)
        if role not in ADMIN_ROLES:
            raise ValueError("administrator_role_invalid")
        result.append(
            {
                "qq_id": qq_id,
                "display_name": _clip(item.get("display_name"), 80),
                "role": role,
                "enabled": _truthy(item.get("enabled", True)),
            },
        )
    return result


def _normalize_access(value: object, *, subject_type: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError(f"{subject_type}_allowlist_must_be_list")
    key = "qq_id" if subject_type == "private_user" else "group_id"
    result: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{subject_type}_allowlist_invalid")
        subject_id = _qq_id(item.get(key), f"{subject_type}_id_invalid")
        if subject_id in seen:
            raise ValueError(f"{subject_type}_id_duplicate")
        seen.add(subject_id)
        result.append(
            {
                "subject_id": subject_id,
                "enabled": _truthy(item.get("enabled", True)),
                "remark": _clip(item.get("remark"), 160),
            },
        )
    return result


def _identity_id(conn: sqlite3.Connection, qq_id: str, display_name: str, now: str) -> str:
    row = conn.execute("SELECT id FROM qq_identities WHERE qq_id=?", (qq_id,)).fetchone()
    if row:
        identity_id = str(row[0])
        conn.execute(
            """
            UPDATE qq_identities SET display_name=?,status='active',updated_at=? WHERE id=?
            """,
            (display_name, now, identity_id),
        )
        return identity_id
    identity_id = "qq-identity-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO qq_identities(
            id,qq_id,display_name,status,last_seen_at,created_at,updated_at
        ) VALUES(?,?,?,'active','',?,?)
        """,
        (identity_id, qq_id, display_name, now, now),
    )
    return identity_id


def _upsert_role(
    conn: sqlite3.Connection,
    identity_id: str,
    role: str,
    enabled: bool,
    changed_by: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO qq_role_assignments(
            id,identity_id,role,enabled,created_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(identity_id,role) DO UPDATE SET
            enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (
            "qq-role-" + uuid.uuid4().hex,
            identity_id,
            role,
            int(enabled),
            changed_by,
            now,
            now,
        ),
    )


def _upsert_access_entries(
    conn: sqlite3.Connection,
    entries: list[dict],
    subject_type: str,
    changed_by: str,
    now: str,
) -> None:
    conn.execute(
        "UPDATE qq_access_entries SET enabled=0,updated_at=? WHERE subject_type=?",
        (now, subject_type),
    )
    for item in entries:
        conn.execute(
            """
            INSERT INTO qq_access_entries(
                id,subject_type,subject_id,enabled,remark,created_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(subject_type,subject_id) DO UPDATE SET
                enabled=excluded.enabled,remark=excluded.remark,updated_at=excluded.updated_at
            """,
            (
                "qq-access-" + uuid.uuid4().hex,
                subject_type,
                item["subject_id"],
                int(item["enabled"]),
                item["remark"],
                changed_by,
                now,
                now,
            ),
        )


def _idempotent_replay(
    conn: sqlite3.Connection,
    idempotency_key: str,
    request_hash: str,
) -> dict | None:
    key = _clip(idempotency_key, 160)
    if not key:
        raise ValueError("idempotency_key_required")
    row = conn.execute(
        """
        SELECT request_hash,response_json FROM assistant_idempotency_records
        WHERE action='qq_settings_update' AND idempotency_key=?
        """,
        (key,),
    ).fetchone()
    if not row:
        return None
    if str(row[0]) != request_hash:
        raise ValueError("idempotency_key_payload_conflict")
    response = json.loads(str(row[1] or "{}"))
    if not isinstance(response, dict):
        raise ValueError("idempotency_record_corrupt")
    response["idempotent_replay"] = True
    return response


def update_qq_access_settings(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    idempotency_key: str,
    changed_by: str,
) -> dict:
    require_qq_access_schema(conn)
    if not isinstance(payload, dict):
        raise ValueError("qq_settings_payload_invalid")
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("qq_settings_required")
    access_mode = _clip(settings.get("access_mode"), 20)
    if access_mode not in {"disabled", "admin_only", "allowlist"}:
        raise ValueError("qq_access_mode_invalid")
    current_runtime = get_runtime_settings(conn)
    normalized_runtime = normalize_runtime_settings(settings, current_runtime)
    normalized = {
        "expected_version": int(payload.get("expected_version") or 0),
        "channel_enabled": _truthy(settings.get("channel_enabled")),
        "access_mode": access_mode,
        "private_chat_enabled": _truthy(settings.get("private_chat_enabled")),
        "group_chat_enabled": _truthy(settings.get("group_chat_enabled")),
        "runtime": normalized_runtime,
        "administrators": _normalize_administrators(payload.get("administrators", [])),
        "private_allowlist": _normalize_access(
            payload.get("private_allowlist", []), subject_type="private_user",
        ),
        "group_allowlist": _normalize_access(
            payload.get("group_allowlist", []), subject_type="qq_group",
        ),
        "group_participation": _normalize_group_participation(
            payload.get("group_participation"),
        ),
    }
    if normalized["channel_enabled"] and access_mode == "disabled":
        raise ValueError("enabled_channel_requires_access_mode")
    enabled_super_admins = [
        item for item in normalized["administrators"]
        if item["enabled"] and item["role"] == "super_admin"
    ]
    existing_super_admins = int(
        conn.execute(
            """
            SELECT count(*) FROM qq_role_assignments r
            JOIN qq_identities i ON i.id=r.identity_id
            WHERE r.role='super_admin' AND r.enabled=1 AND i.status='active'
            """,
        ).fetchone()[0],
    )
    if normalized["channel_enabled"] and not enabled_super_admins:
        raise ValueError("super_admin_required_before_enable")
    if existing_super_admins and not enabled_super_admins:
        raise ValueError("last_super_admin_required")
    request_hash = _request_hash(normalized)
    replay = _idempotent_replay(conn, idempotency_key, request_hash)
    if replay is not None:
        return replay

    with _write_transaction(conn):
        current = _settings_row(conn)
        current_version = int(current[5])
        if normalized["expected_version"] != current_version:
            raise ValueError("stale_qq_settings_version")
        now = utc_now()
        conn.execute(
            """
            UPDATE qq_role_assignments SET enabled=0,updated_at=?
            WHERE role IN ('super_admin','admin','operator')
            """,
            (now,),
        )
        for item in normalized["administrators"]:
            identity_id = _identity_id(
                conn, item["qq_id"], item["display_name"], now,
            )
            _upsert_role(
                conn, identity_id, item["role"], item["enabled"], changed_by, now,
            )

        _upsert_access_entries(
            conn, normalized["private_allowlist"], "private_user", changed_by, now,
        )
        _upsert_access_entries(
            conn, normalized["group_allowlist"], "qq_group", changed_by, now,
        )
        synchronized_group_count = 0
        if normalized["group_participation"] is not None:
            synchronized_group_count = _sync_enabled_group_participation(
                conn,
                normalized["group_allowlist"],
                normalized["group_participation"],
            )
        conn.execute(
            "UPDATE qq_role_assignments SET enabled=0,updated_at=? WHERE role='user'",
            (now,),
        )
        for item in normalized["private_allowlist"]:
            identity_id = _identity_id(conn, item["subject_id"], "", now)
            _upsert_role(conn, identity_id, "user", item["enabled"], changed_by, now)

        next_version = current_version + 1
        conn.execute(
            """
            UPDATE qq_channel_settings SET
                channel_enabled=?,access_mode=?,private_chat_enabled=?,
                group_chat_enabled=?,expected_bot_id=?,command_prefixes_json=?,
                auto_private_chat=?,reply_max_chars=?,delivery_poll_seconds=?,
                notification_interval_seconds=?,config_version=?,updated_by=?,updated_at=?
            WHERE channel_id=?
            """,
            (
                int(normalized["channel_enabled"]),
                normalized["access_mode"],
                int(normalized["private_chat_enabled"]),
                int(normalized["group_chat_enabled"]),
                normalized_runtime["expected_bot_id"],
                _canonical_json(normalized_runtime["command_prefixes"]),
                int(normalized_runtime["auto_private_chat"]),
                normalized_runtime["reply_max_chars"],
                normalized_runtime["delivery_poll_seconds"],
                normalized_runtime["notification_interval_seconds"],
                next_version,
                _clip(changed_by, 80) or "admin",
                now,
                QQ_CHANNEL_ID,
            ),
        )
        response = get_qq_access_settings(conn)
        response["idempotent_replay"] = False
        conn.execute(
            """
            INSERT INTO assistant_idempotency_records(
                action,idempotency_key,request_hash,response_json,created_at
            ) VALUES('qq_settings_update',?,?,?,?)
            """,
            (
                _clip(idempotency_key, 160),
                request_hash,
                _canonical_json(response),
                now,
            ),
        )
        record_security_audit(
            conn,
            "qq_access_settings_updated",
            "success",
            actor_type=_clip(changed_by, 40) or "admin",
            detail={
                "config_version": next_version,
                "channel_enabled": normalized["channel_enabled"],
                "access_mode": normalized["access_mode"],
                "administrator_count": len(normalized["administrators"]),
                "private_allowlist_count": len(normalized["private_allowlist"]),
                "group_allowlist_count": len(normalized["group_allowlist"]),
                "group_participation_mode": (
                    normalized["group_participation"] or {}
                ).get("participation_mode", "unchanged"),
                "group_participation_probability": (
                    normalized["group_participation"] or {}
                ).get("reply_probability"),
                "synchronized_group_count": synchronized_group_count,
            },
        )
    return response


def qq_access_cutover_plan(conn: sqlite3.Connection) -> dict:
    current = get_qq_access_settings(conn)
    enabled_super_admins = sum(
        1 for item in current["administrators"]
        if item["enabled"] and item["role"] == "super_admin"
    )
    settings = current["settings"]
    prerequisites = {
        "channel_enabled": bool(settings["channel_enabled"]),
        "access_mode_configured": settings["access_mode"] in {"admin_only", "allowlist"},
        "super_admin_present": enabled_super_admins > 0,
    }
    ready = all(prerequisites.values())
    checksum_payload = {
        "config_version": settings["config_version"],
        "feature_enabled": current["feature_enabled"],
        "prerequisites": prerequisites,
    }
    return {
        "feature_enabled": current["feature_enabled"],
        "config_version": settings["config_version"],
        "prerequisites": prerequisites,
        "ready": ready,
        "plan_checksum": hashlib.sha256(
            _canonical_json(checksum_payload).encode("utf-8"),
        ).hexdigest(),
    }


def set_qq_access_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
    changed_by: str,
) -> dict:
    require_qq_access_schema(conn)
    plan = qq_access_cutover_plan(conn)
    if not expect_plan_checksum or expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_qq_access_cutover_plan")
    if enabled and not plan["ready"]:
        raise ValueError("qq_access_cutover_prerequisite_missing")
    with _write_transaction(conn):
        conn.execute(
            "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
            (int(enabled), utc_now(), QQ_ACCESS_FEATURE_FLAG),
        )
        record_security_audit(
            conn,
            "qq_access_cutover_changed",
            "success",
            actor_type=_clip(changed_by, 40) or "admin",
            detail={"enabled": bool(enabled), "config_version": plan["config_version"]},
        )
    return qq_access_cutover_plan(conn)


def _active_role(conn: sqlite3.Connection, sender_id: str) -> str:
    rows = conn.execute(
        """
        SELECT r.role FROM qq_role_assignments r
        JOIN qq_identities i ON i.id=r.identity_id
        WHERE i.qq_id=? AND i.status='active' AND r.enabled=1
        """,
        (sender_id,),
    ).fetchall()
    present = {str(row[0]) for row in rows}
    return next((role for role in ROLE_PRIORITY if role in present), "")


def _entry_enabled(conn: sqlite3.Connection, subject_type: str, subject_id: str) -> bool:
    row = conn.execute(
        """
        SELECT enabled FROM qq_access_entries WHERE subject_type=? AND subject_id=?
        """,
        (subject_type, subject_id),
    ).fetchone()
    return bool(row and int(row[0]))


def _action_allowed(role: str, action: str) -> bool:
    if role in {"super_admin", "admin"}:
        return action in ADMIN_ACTIONS
    if role == "operator":
        return action in OPERATOR_ACTIONS
    if role == "user":
        return action in SAFE_USER_ACTIONS
    return False


def _object_feature_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name='qq_object_authorization_v2'",
    ).fetchone()
    return bool(row and int(row[0]))


def check_qq_access(conn: sqlite3.Connection, payload: dict) -> dict:
    require_qq_access_schema(conn)
    if not isinstance(payload, dict):
        raise ValueError("qq_access_payload_invalid")
    if any(key in payload for key in ("message", "content", "raw_message")):
        raise ValueError("qq_access_body_not_allowed")
    sender_id = _qq_id(payload.get("sender_id"), "sender_qq_id_invalid")
    event_type = _clip(payload.get("event_type"), 20)
    action = _clip(payload.get("requested_action"), 40)
    if event_type not in {"private", "group"}:
        raise ValueError("qq_event_type_invalid")
    if not action:
        raise ValueError("qq_requested_action_required")
    current = get_qq_access_settings(conn)
    settings = current["settings"]
    base = {
        "allowed": False,
        "reason": "access_denied",
        "role": "",
        "config_version": settings["config_version"],
        "feature_enabled": current["feature_enabled"],
    }
    if not current["feature_enabled"]:
        return {**base, "reason": "access_feature_disabled"}
    if not settings["channel_enabled"] or settings["access_mode"] == "disabled":
        return {**base, "reason": "channel_disabled"}

    role = _active_role(conn, sender_id)
    base["role"] = role
    if event_type == "group":
        if not settings["group_chat_enabled"]:
            return {**base, "reason": "group_chat_disabled"}
        group_id = _qq_id(payload.get("group_id"), "group_qq_id_invalid")
        if not _entry_enabled(conn, "qq_group", group_id):
            return {**base, "reason": "group_not_allowlisted"}
        return {**base, "allowed": True, "reason": "group_allowlisted"}

    if not settings["private_chat_enabled"]:
        return {**base, "reason": "private_chat_disabled"}
    is_admin = role in {"super_admin", "admin"}
    is_allowlisted = _entry_enabled(conn, "private_user", sender_id)
    if settings["access_mode"] == "admin_only" and not is_admin:
        return {**base, "reason": "administrator_required"}
    if settings["access_mode"] == "allowlist" and not (is_admin or is_allowlisted):
        return {**base, "reason": "sender_not_allowlisted"}
    effective_role = role or ("user" if is_allowlisted else "")
    base["role"] = effective_role
    object_action = action in OBJECT_ACTIONS and _object_feature_enabled(conn)
    if not (_action_allowed(effective_role, action) or object_action):
        return {**base, "reason": "action_not_allowed"}
    conn.execute(
        "UPDATE qq_identities SET last_seen_at=?,updated_at=? WHERE qq_id=?",
        (utc_now(), utc_now(), sender_id),
    )
    return {**base, "allowed": True, "reason": "authorized", "role": effective_role}


__all__ = [
    "check_qq_access",
    "get_qq_access_settings",
    "qq_access_cutover_plan",
    "qq_access_feature_enabled",
    "set_qq_access_feature",
    "update_qq_access_settings",
]
