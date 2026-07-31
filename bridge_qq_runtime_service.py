#!/usr/bin/env python3
"""Gate C5 runtime-config projection, heartbeat validation, and admin status."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from bridge_migrations import utc_now
from bridge_qq_access_schema import QQ_CHANNEL_ID
from bridge_qq_runtime_schema import require_qq_runtime_schema


QQ_ID_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")
INSTANCE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,120}$")
PREFIX_PATTERN = re.compile(r"^/[A-Za-z0-9_-]{1,31}$")
CAPABILITY_KEYS = frozenset(
    {
        "runtime_config",
        "heartbeat",
        "delivery_claim",
        "actual_bot_discovery",
    },
)


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def normalize_command_prefixes(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("qq_command_prefixes_must_be_list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        prefix = _clip(item, 32).lower()
        if not PREFIX_PATTERN.fullmatch(prefix):
            raise ValueError("qq_command_prefix_invalid")
        if prefix not in seen:
            seen.add(prefix)
            result.append(prefix)
    if "/codex" not in seen:
        raise ValueError("qq_canonical_command_required")
    if len(result) > 8:
        raise ValueError("qq_command_prefix_limit")
    return result


def normalize_runtime_settings(settings: dict, current: dict | None = None) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("qq_runtime_settings_invalid")
    current = current or {}
    expected_bot_id = _clip(settings.get("expected_bot_id", current.get("expected_bot_id")), 20)
    if expected_bot_id and not QQ_ID_PATTERN.fullmatch(expected_bot_id):
        raise ValueError("qq_expected_bot_id_invalid")
    prefixes = settings.get("command_prefixes", current.get("command_prefixes"))
    if prefixes is None:
        prefixes = ["/codex", "/c", "/agent", "/a"]
    prefixes = normalize_command_prefixes(prefixes)

    def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = settings.get(name, current.get(name, default))
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"qq_{name}_invalid") from exc
        if value < minimum or value > maximum:
            raise ValueError(f"qq_{name}_invalid")
        return value

    auto_value = settings.get("auto_private_chat", current.get("auto_private_chat", True))
    if not isinstance(auto_value, bool):
        raise ValueError("qq_auto_private_chat_boolean_required")
    return {
        "expected_bot_id": expected_bot_id,
        "command_prefixes": prefixes,
        "auto_private_chat": auto_value,
        "reply_max_chars": bounded("reply_max_chars", 3600, 500, 10000),
        "delivery_poll_seconds": bounded("delivery_poll_seconds", 12, 5, 300),
        "notification_interval_seconds": bounded(
            "notification_interval_seconds", 90, 10, 3600,
        ),
    }


def runtime_settings_from_row(row) -> dict:
    try:
        prefixes = json.loads(str(row[1] or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError("qq_command_prefixes_corrupt") from exc
    return normalize_runtime_settings(
        {
            "expected_bot_id": row[0],
            "command_prefixes": prefixes,
            "auto_private_chat": bool(row[2]),
            "reply_max_chars": row[3],
            "delivery_poll_seconds": row[4],
            "notification_interval_seconds": row[5],
        },
    )


def get_runtime_settings(conn: sqlite3.Connection) -> dict:
    require_qq_runtime_schema(conn)
    row = conn.execute(
        """
        SELECT expected_bot_id,command_prefixes_json,auto_private_chat,
               reply_max_chars,delivery_poll_seconds,notification_interval_seconds,
               config_version,updated_at
        FROM qq_channel_settings WHERE channel_id=?
        """,
        (QQ_CHANNEL_ID,),
    ).fetchone()
    if not row:
        raise ValueError("qq_channel_settings_missing")
    settings = runtime_settings_from_row(row)
    settings.update({"version": int(row[6]), "updated_at": str(row[7] or "")})
    etag_payload = {key: value for key, value in settings.items() if key != "updated_at"}
    settings["etag"] = hashlib.sha256(
        _canonical_json(etag_payload).encode("utf-8"),
    ).hexdigest()
    return settings


def channel_runtime_config(conn: sqlite3.Connection) -> dict:
    settings = get_runtime_settings(conn)
    # The Channel surface intentionally excludes expected_bot_id and every
    # identity/role/allowlist record.
    return {
        "channel_id": QQ_CHANNEL_ID,
        "version": settings["version"],
        "etag": settings["etag"],
        "command_prefixes": settings["command_prefixes"],
        "auto_private_chat": settings["auto_private_chat"],
        "reply_max_chars": settings["reply_max_chars"],
        "delivery_poll_seconds": settings["delivery_poll_seconds"],
        "notification_interval_seconds": settings["notification_interval_seconds"],
    }


def record_channel_heartbeat(conn: sqlite3.Connection, payload: dict) -> dict:
    require_qq_runtime_schema(conn)
    if not isinstance(payload, dict):
        raise ValueError("qq_channel_heartbeat_invalid")
    forbidden = {
        "token", "secret", "password", "cookie", "authorization",
        "message", "content", "raw_message",
    }
    if forbidden.intersection(str(key).lower() for key in payload):
        raise ValueError("qq_channel_heartbeat_sensitive_field")
    instance_id = _clip(payload.get("channel_instance_id"), 120)
    if not INSTANCE_PATTERN.fullmatch(instance_id):
        raise ValueError("qq_channel_instance_id_invalid")
    actual_bot_id = _clip(payload.get("actual_bot_id"), 20)
    if actual_bot_id and not QQ_ID_PATTERN.fullmatch(actual_bot_id):
        raise ValueError("qq_actual_bot_id_invalid")
    adapter_id = _clip(payload.get("adapter_id"), 80)
    if not adapter_id or any(char.isspace() for char in adapter_id):
        raise ValueError("qq_adapter_id_invalid")
    try:
        applied_version = int(payload.get("applied_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("qq_applied_version_invalid") from exc
    if applied_version < 0:
        raise ValueError("qq_applied_version_invalid")
    raw_capabilities = payload.get("capabilities") or {}
    if not isinstance(raw_capabilities, dict):
        raise ValueError("qq_channel_capabilities_invalid")
    unknown = set(raw_capabilities) - CAPABILITY_KEYS
    if unknown:
        raise ValueError("qq_channel_capability_unknown")
    capabilities = {key: bool(raw_capabilities.get(key)) for key in sorted(raw_capabilities)}
    sync_error = _clip(payload.get("last_sync_error"), 240)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO qq_channel_runtime_receipts(
            channel_instance_id,channel_id,actual_bot_id,adapter_id,
            applied_version,capabilities_json,last_heartbeat_at,last_sync_error,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(channel_instance_id) DO UPDATE SET
            actual_bot_id=excluded.actual_bot_id,
            adapter_id=excluded.adapter_id,
            applied_version=excluded.applied_version,
            capabilities_json=excluded.capabilities_json,
            last_heartbeat_at=excluded.last_heartbeat_at,
            last_sync_error=excluded.last_sync_error,
            updated_at=excluded.updated_at
        """,
        (
            instance_id, QQ_CHANNEL_ID, actual_bot_id, adapter_id,
            applied_version, _canonical_json(capabilities), now, sync_error, now, now,
        ),
    )
    return {
        "accepted": True,
        "server_version": get_runtime_settings(conn)["version"],
        "heartbeat_at": now,
    }


def get_runtime_summary(conn: sqlite3.Connection) -> dict:
    settings = get_runtime_settings(conn)
    row = conn.execute(
        """
        SELECT channel_instance_id,actual_bot_id,adapter_id,applied_version,
               capabilities_json,last_heartbeat_at,last_sync_error
        FROM qq_channel_runtime_receipts
        WHERE channel_id=?
        ORDER BY last_heartbeat_at DESC,updated_at DESC
        LIMIT 1
        """,
        (QQ_CHANNEL_ID,),
    ).fetchone()
    timeout_seconds = max(180, int(settings["notification_interval_seconds"]) * 3)
    expected = settings["expected_bot_id"]
    if not row:
        return {
            "state": "offline",
            "expected_bot_id": expected,
            "actual_bot_id": "",
            "adapter_id": "",
            "config_version": settings["version"],
            "applied_version": 0,
            "last_heartbeat_at": "",
            "last_sync_error": "",
            "heartbeat_timeout_seconds": timeout_seconds,
            "capabilities": {},
        }
    heartbeat = _parse_time(row[5])
    age = None
    if heartbeat:
        age = max(0, int((datetime.now(timezone.utc) - heartbeat).total_seconds()))
    try:
        capabilities = json.loads(str(row[4] or "{}"))
    except json.JSONDecodeError:
        capabilities = {}
    actual = str(row[1] or "")
    applied_version = int(row[3] or 0)
    sync_error = str(row[6] or "")
    if age is None or age > timeout_seconds:
        state = "offline"
    elif sync_error:
        state = "degraded"
    elif expected and actual and expected != actual:
        state = "mismatch"
    elif not expected or not actual or applied_version != settings["version"]:
        state = "pending"
    else:
        state = "applied"
    return {
        "state": state,
        "expected_bot_id": expected,
        "actual_bot_id": actual,
        "adapter_id": str(row[2] or ""),
        "config_version": settings["version"],
        "applied_version": applied_version,
        "last_heartbeat_at": str(row[5] or ""),
        "last_sync_error": sync_error,
        "heartbeat_age_seconds": age,
        "heartbeat_timeout_seconds": timeout_seconds,
        "channel_instance_id": str(row[0] or ""),
        "capabilities": capabilities if isinstance(capabilities, dict) else {},
    }


__all__ = [
    "channel_runtime_config",
    "get_runtime_settings",
    "get_runtime_summary",
    "normalize_command_prefixes",
    "normalize_runtime_settings",
    "record_channel_heartbeat",
]
