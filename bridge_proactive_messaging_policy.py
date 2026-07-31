#!/usr/bin/env python3
"""Configurable proactive-message policy service.

This is a policy boundary, not a scheduler. The existing scheduler remains
responsible for deciding when a candidate is due; this module decides whether
the candidate may be sent, drafted, or must wait for confirmation.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import uuid
from typing import Iterator

from bridge_assistant_identity import current_assistant
from bridge_migrations import utc_now
from bridge_proactive_messaging_schema import (
    PROACTIVE_MESSAGING_MODES,
    PROACTIVE_MESSAGING_SCOPE_TYPES,
    require_proactive_messaging_schema,
)


ALLOWED_INTENTS = {
    "task_failed",
    "task_completed",
    "approval",
    "security",
    "resource",
    "follow_up",
    "share",
    "check_in",
    "celebrate",
    "reminder",
}
DEFAULT_INTENTS = ["task_failed", "task_completed", "approval", "security"]


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _json_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = value.split(",")
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({_clip(item, 80) for item in value if _clip(item, 80)})


def _clock(value: object, default: str) -> str:
    text = _clip(value, 5) or default
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("invalid_proactive_policy_clock")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError("invalid_proactive_policy_clock") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid_proactive_policy_clock")
    return f"{hour:02d}:{minute:02d}"


def _active_assistant(conn: sqlite3.Connection) -> dict:
    assistant = current_assistant(conn)
    if not assistant:
        raise ValueError("active_assistant_missing")
    return assistant


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        name = f"proactive_policy_{uuid.uuid4().hex}"
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


def _require_feature(conn: sqlite3.Connection) -> None:
    require_proactive_messaging_schema(conn)
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        ("relationship_proactive_v2",),
    ).fetchone()
    if not row or not int(row[0]):
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
    response = json.loads(str(row[1] or "{}"))
    response["idempotent_replay"] = True
    return response


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


def _public(row: sqlite3.Row | tuple | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["allowed_intents"] = _json_list(item.pop("allowed_intents_json", "[]"))
    item["daily_limit"] = int(item["daily_limit"])
    item["weekly_limit"] = int(item["weekly_limit"])
    item["unanswered_limit"] = int(item["unanswered_limit"])
    item["version"] = int(item["version"])
    item["effective"] = True
    return item


def _validate_target(target_type: object, target_id: object) -> tuple[str, str]:
    kind = _clip(target_type or "global", 20)
    target = _clip(target_id, 160)
    if kind not in PROACTIVE_MESSAGING_SCOPE_TYPES:
        raise ValueError("invalid_proactive_policy_target_type")
    if kind == "global" and target:
        raise ValueError("global_proactive_policy_target_id_forbidden")
    if kind == "owner" and target not in {"", "owner"}:
        raise ValueError("owner_proactive_policy_target_id_invalid")
    if kind in {"user", "group"} and target == "":
        # Empty IDs are the generic default for that target class.
        return kind, ""
    return kind, target


def _candidate_keys(target_type: str, target_id: str) -> list[tuple[str, str]]:
    if target_type == "global":
        return [("global", "")]
    if target_type == "owner":
        return [("owner", target_id or "owner"), ("owner", ""), ("global", "")]
    if target_type == "user":
        return [("user", target_id), ("user", ""), ("global", "")]
    return [("group", target_id), ("group", ""), ("global", "")]


def get_proactive_messaging_policy(
    conn: sqlite3.Connection,
    *,
    target_type: str = "global",
    target_id: str = "",
) -> dict:
    """Return the most specific policy and its inheritance path."""

    require_proactive_messaging_schema(conn)
    assistant = _active_assistant(conn)
    kind, target = _validate_target(target_type, target_id)
    candidates = _candidate_keys(kind, target)
    row = None
    source_type, source_id = "global", ""
    for candidate_type, candidate_id in candidates:
        row = conn.execute(
            """
            SELECT * FROM proactive_messaging_policies
            WHERE assistant_id=? AND target_type=? AND target_id=?
            """,
            (assistant["id"], candidate_type, candidate_id),
        ).fetchone()
        if row is not None:
            source_type, source_id = candidate_type, candidate_id
            break
    result = _public(row) or {
        "id": "",
        "assistant_id": assistant["id"],
        "target_type": kind,
        "target_id": target,
        "mode": "off",
        "allowed_intents": list(DEFAULT_INTENTS),
        "quiet_start": "23:00",
        "quiet_end": "08:00",
        "daily_limit": 0,
        "weekly_limit": 0,
        "unanswered_limit": 0,
        "version": 0,
        "updated_by": "default",
        "created_at": "",
        "updated_at": "",
        "effective": True,
    }
    result["requested_target_type"] = kind
    result["requested_target_id"] = target
    result["resolved_from"] = {"target_type": source_type, "target_id": source_id}
    result["inherited"] = source_type != kind or source_id != target
    return result


def list_proactive_messaging_policies(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[dict]:
    require_proactive_messaging_schema(conn)
    assistant = _active_assistant(conn)
    rows = conn.execute(
        """
        SELECT * FROM proactive_messaging_policies
        WHERE assistant_id=?
        ORDER BY CASE target_type
            WHEN 'global' THEN 0 WHEN 'owner' THEN 1
            WHEN 'user' THEN 2 ELSE 3 END, target_id
        LIMIT ?
        """,
        (assistant["id"], max(1, min(int(limit or 100), 300))),
    ).fetchall()
    return [_public(row) for row in rows]


def update_proactive_messaging_policy(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    idempotency_key: str,
) -> dict:
    """Version and persist a policy row without sending any message."""

    _require_feature(conn)
    assistant = _active_assistant(conn)
    target_type, target_id = _validate_target(
        payload.get("target_type"),
        payload.get("target_id"),
    )
    mode = _clip(payload.get("mode") or "off", 20)
    if mode not in PROACTIVE_MESSAGING_MODES:
        raise ValueError("invalid_proactive_policy_mode")
    existing = conn.execute(
        """
        SELECT * FROM proactive_messaging_policies
        WHERE assistant_id=? AND target_type=? AND target_id=?
        """,
        (assistant["id"], target_type, target_id),
    ).fetchone()
    current = dict(existing) if existing else {}
    try:
        expected_version = int(payload.get("expected_version", current.get("version") or 0))
        daily_limit = int(payload.get("daily_limit", current.get("daily_limit") or 2))
        weekly_limit = int(payload.get("weekly_limit", current.get("weekly_limit") or 7))
        unanswered_limit = int(
            payload.get("unanswered_limit", current.get("unanswered_limit") or 2),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_proactive_policy_number") from exc
    if not 0 <= daily_limit <= 50 or not 0 <= weekly_limit <= 200 or not 0 <= unanswered_limit <= 20:
        raise ValueError("invalid_proactive_policy_budget")
    raw_intents = payload.get(
        "allowed_intents",
        current.get("allowed_intents_json") or DEFAULT_INTENTS,
    )
    intents = _json_list(raw_intents)
    if not intents or any(item not in ALLOWED_INTENTS for item in intents):
        raise ValueError("invalid_proactive_policy_intents")
    normalized = {
        "assistant_id": assistant["id"],
        "target_type": target_type,
        "target_id": target_id,
        "mode": mode,
        "allowed_intents": intents,
        "quiet_start": _clock(payload.get("quiet_start", current.get("quiet_start")), "23:00"),
        "quiet_end": _clock(payload.get("quiet_end", current.get("quiet_end")), "08:00"),
        "daily_limit": daily_limit,
        "weekly_limit": weekly_limit,
        "unanswered_limit": unanswered_limit,
        "expected_version": expected_version,
    }
    action = f"proactive_messaging:{assistant['id']}:{target_type}:{target_id}"
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
        if existing and expected_version != int(existing["version"]):
            raise ValueError("stale_proactive_policy_version")
        now = utc_now()
        next_version = expected_version + 1
        row_id = str(existing["id"]) if existing else f"proactive_policy_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO proactive_messaging_policies(
                id,assistant_id,target_type,target_id,mode,allowed_intents_json,
                quiet_start,quiet_end,daily_limit,weekly_limit,unanswered_limit,
                version,updated_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(assistant_id,target_type,target_id) DO UPDATE SET
                mode=excluded.mode,
                allowed_intents_json=excluded.allowed_intents_json,
                quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,
                daily_limit=excluded.daily_limit,weekly_limit=excluded.weekly_limit,
                unanswered_limit=excluded.unanswered_limit,version=excluded.version,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at
            """,
            (
                row_id,
                assistant["id"],
                target_type,
                target_id,
                mode,
                _canonical_json(intents),
                normalized["quiet_start"],
                normalized["quiet_end"],
                daily_limit,
                weekly_limit,
                unanswered_limit,
                next_version,
                "admin",
                current.get("created_at") or now,
                now,
            ),
        )
        result = get_proactive_messaging_policy(
            conn,
            target_type=target_type,
            target_id=target_id,
        )
        result["idempotent_replay"] = False
        _save_idempotency(
            conn,
            action=action,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=result,
        )
        return result


def proactive_message_gate(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str = "",
    intent: str = "",
) -> dict:
    """Return the decision that the scheduler/delivery layer must enforce."""

    policy = get_proactive_messaging_policy(
        conn,
        target_type=target_type,
        target_id=target_id,
    )
    normalized_intent = _clip(intent, 80)
    if policy["mode"] == "off":
        return {"allowed": False, "send_allowed": False, "reason": "policy_disabled", "policy": policy}
    if normalized_intent and normalized_intent not in policy["allowed_intents"]:
        return {"allowed": False, "send_allowed": False, "reason": "intent_not_allowed", "policy": policy}
    return {
        "allowed": True,
        "send_allowed": policy["mode"] == "auto",
        "execution_mode": policy["mode"],
        "reason": "policy_allows",
        "policy": policy,
    }


def proactive_target_for_user(
    conn: sqlite3.Connection,
    user_id: str,
) -> tuple[str, str]:
    """Map a channel actor to the policy scope it actually belongs to.

    The legacy bridge uses ``owner`` as the Owner alias while the identity
    domain stores the stable owner actor id (normally ``owner-local``).
    Treat both as the owner scope; all other actors remain user-scoped.
    """

    actor_id = _clip(user_id, 160)
    if actor_id in {"owner", "owner-local"}:
        return "owner", ""
    assistant = _active_assistant(conn)
    if actor_id == _clip(assistant.get("owner_actor_id"), 160):
        return "owner", ""
    # Channel identities are not the same namespace as the platform's stable
    # owner actor id.  A QQ super-admin is the channel projection of Owner and
    # must inherit the owner policy instead of the generic user default.
    qq_access_tables = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ('qq_identities','qq_role_assignments')
            """,
        ).fetchall()
    }
    if actor_id and qq_access_tables == {"qq_identities", "qq_role_assignments"}:
        owner_projection = conn.execute(
            """
            SELECT 1
            FROM qq_identities i
            JOIN qq_role_assignments r ON r.identity_id=i.id
            WHERE i.qq_id=? AND i.status='active'
              AND r.role='super_admin' AND r.enabled=1
            LIMIT 1
            """,
            (actor_id,),
        ).fetchone()
        if owner_projection:
            return "owner", ""
    return "user", actor_id


def policy_gate_if_present(conn: sqlite3.Connection, user_id: str) -> dict | None:
    """Use the configurable gate when v25 is installed; otherwise preserve
    isolated legacy scheduler tests and pre-migration maintenance databases."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_messaging_policies'",
    ).fetchone()
    if not table:
        return None
    feature = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name='relationship_proactive_v2'",
    ).fetchone()
    if not feature or not int(feature[0]):
        return None
    try:
        actor = _clip(user_id, 160)
        if actor.startswith("group:") and actor[6:]:
            target_type, target_id = "group", actor[6:]
        else:
            target_type, target_id = proactive_target_for_user(conn, actor)
        return proactive_message_gate(conn, target_type=target_type, target_id=target_id)
    except (sqlite3.Error, ValueError):
        return None


__all__ = [
    "ALLOWED_INTENTS",
    "DEFAULT_INTENTS",
    "get_proactive_messaging_policy",
    "list_proactive_messaging_policies",
    "policy_gate_if_present",
    "proactive_message_gate",
    "proactive_target_for_user",
    "update_proactive_messaging_policy",
]
