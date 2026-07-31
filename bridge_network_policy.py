#!/usr/bin/env python3
"""Authoritative network policy and natural-language control commands."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

from bridge_migrations import utc_now
from bridge_network_policy_schema import (
    NETWORK_BASE_MODES,
    require_network_policy_schema,
)


MAX_WEB_SEARCH_TTL_MINUTES = 7 * 24 * 60
DEFAULT_WEB_SEARCH_TTL_MINUTES = 4 * 60
NETWORK_CAPABILITY_IDS = frozenset(
    {
        "weather.forecast.read",
        "github.trending.read",
        "meme.discovery.search",
    },
)
_NETWORK_TOPIC = re.compile(
    r"(?:网络访问|联网权限|开放网络|允许联网|互联网|网页搜索|web\s*search)",
    re.IGNORECASE,
)
_NETWORK_ENABLE = re.compile(
    r"(?:允许|同意|授权|开启|打开|开放|启用)",
    re.IGNORECASE,
)
_NETWORK_DISABLE = re.compile(
    r"(?:关闭|禁用|停止|撤销|收回|不允许|禁止)",
    re.IGNORECASE,
)
_NETWORK_STATUS = re.compile(
    r"(?:状态|配置|是否|现在|还有多久|到期)",
    re.IGNORECASE,
)
_NETWORK_COMPOUND = re.compile(
    r"(?:然后|并且|最后|接着|同时|顺便|检查|排查|修改|开发|部署|上线)",
    re.IGNORECASE,
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT id FROM assistant_instances
        WHERE status='active'
        ORDER BY updated_at DESC,id
        LIMIT 1
        """,
    ).fetchone()
    if not row:
        raise ValueError("active_assistant_required")
    return str(row[0])


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strict_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid_network_search_enabled")


def _public_policy(row: Mapping[str, object]) -> dict:
    now = datetime.now(timezone.utc)
    expires_at = _parse_time(row.get("owner_web_search_expires_at"))
    configured = bool(int(row.get("owner_web_search_enabled") or 0))
    active = bool(configured and expires_at and expires_at > now)
    remaining = max(
        0,
        int((expires_at - now).total_seconds()),
    ) if active and expires_at else 0
    return {
        "id": str(row.get("id") or ""),
        "assistant_id": str(row.get("assistant_id") or ""),
        "base_mode": str(row.get("base_mode") or "capability_only"),
        "owner_web_search_configured": configured,
        "owner_web_search_active": active,
        "owner_web_search_expires_at": (
            expires_at.isoformat() if expires_at else ""
        ),
        "owner_web_search_remaining_seconds": remaining,
        "version": int(row.get("version") or 1),
        "updated_by": str(row.get("updated_by") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "raw_shell_network": False,
        "task_search_runtime": "codex_login",
    }


def get_network_policy(conn: sqlite3.Connection) -> dict:
    require_network_policy_schema(conn)
    assistant_id = _active_assistant_id(conn)
    row = conn.execute(
        "SELECT * FROM assistant_network_policies WHERE assistant_id=?",
        (assistant_id,),
    ).fetchone()
    if not row:
        raise ValueError("network_policy_missing")
    return _public_policy(dict(row))


def list_network_policy_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
) -> list[dict]:
    require_network_policy_schema(conn)
    assistant_id = _active_assistant_id(conn)
    rows = conn.execute(
        """
        SELECT id,action,actor_ref,channel,created_at
        FROM assistant_network_policy_events
        WHERE assistant_id=?
        ORDER BY created_at DESC,id DESC
        LIMIT ?
        """,
        (assistant_id, max(1, min(int(limit or 20), 100))),
    ).fetchall()
    return [dict(row) for row in rows]


def set_network_policy(
    conn: sqlite3.Connection,
    *,
    base_mode: object | None = None,
    owner_web_search_enabled: object | None = None,
    ttl_minutes: object | None = None,
    expected_version: object | None = None,
    actor_ref: str = "owner",
    channel: str = "web",
) -> dict:
    """Update policy atomically and keep a redacted audit event."""

    current = get_network_policy(conn)
    if expected_version not in (None, ""):
        try:
            version = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_network_policy_version") from exc
        if version != current["version"]:
            raise ValueError("network_policy_version_conflict")

    next_mode = str(base_mode or current["base_mode"]).strip()
    if next_mode not in NETWORK_BASE_MODES:
        raise ValueError("invalid_network_base_mode")
    enable_search = (
        current["owner_web_search_active"]
        if owner_web_search_enabled is None
        else _strict_bool(owner_web_search_enabled)
    )
    if next_mode == "off":
        enable_search = False

    expires_at = ""
    if enable_search:
        try:
            ttl = int(
                ttl_minutes
                if ttl_minutes not in (None, "")
                else DEFAULT_WEB_SEARCH_TTL_MINUTES
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_network_search_ttl") from exc
        if not 1 <= ttl <= MAX_WEB_SEARCH_TTL_MINUTES:
            raise ValueError("invalid_network_search_ttl")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl)
        ).isoformat()

    now = utc_now()
    assistant_id = current["assistant_id"]
    next_version = current["version"] + 1
    conn.execute(
        """
        UPDATE assistant_network_policies
        SET base_mode=?,owner_web_search_enabled=?,
            owner_web_search_expires_at=?,version=?,updated_by=?,updated_at=?
        WHERE assistant_id=? AND version=?
        """,
        (
            next_mode,
            1 if enable_search else 0,
            expires_at,
            next_version,
            str(actor_ref or "owner")[:160],
            now,
            assistant_id,
            current["version"],
        ),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise ValueError("network_policy_version_conflict")
    updated = get_network_policy(conn)
    action = (
        "owner_web_search_enabled"
        if updated["owner_web_search_active"]
        else "network_policy_updated"
    )
    conn.execute(
        """
        INSERT INTO assistant_network_policy_events(
            id,assistant_id,action,actor_ref,channel,
            previous_json,current_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            "network-event-" + uuid.uuid4().hex,
            assistant_id,
            action,
            str(actor_ref or "owner")[:160],
            str(channel or "web")[:40],
            _canonical_json(current),
            _canonical_json(updated),
            now,
        ),
    )
    return updated


def network_capability_allowed(
    conn: sqlite3.Connection,
    capability_id: str,
) -> bool:
    if capability_id not in NETWORK_CAPABILITY_IDS:
        return True
    return get_network_policy(conn)["base_mode"] == "capability_only"


def task_web_search_allowed(conn: sqlite3.Connection) -> bool:
    policy = get_network_policy(conn)
    return (
        policy["base_mode"] == "capability_only"
        and policy["owner_web_search_active"]
    )


def parse_network_policy_command(message: str) -> dict | None:
    """Recognize only short, explicit policy commands.

    Restricting this parser prevents a larger research request that merely
    mentions networking from being swallowed as a settings change.
    """

    text = " ".join(str(message or "").split()).strip()
    if (
        not text
        or len(text) > 100
        or _NETWORK_COMPOUND.search(text)
        or not _NETWORK_TOPIC.search(text)
    ):
        return None
    action = ""
    if _NETWORK_DISABLE.search(text):
        action = "disable"
    elif _NETWORK_ENABLE.search(text):
        action = "enable"
    elif _NETWORK_STATUS.search(text):
        action = "status"
    if not action:
        return None

    ttl = DEFAULT_WEB_SEARCH_TTL_MINUTES
    duration = re.search(
        r"(?P<value>\d{1,3})\s*(?P<unit>分钟|小时|天)",
        text,
    )
    if duration:
        value = int(duration.group("value"))
        multiplier = {
            "分钟": 1,
            "小时": 60,
            "天": 24 * 60,
        }[duration.group("unit")]
        ttl = value * multiplier
    ttl = max(1, min(ttl, MAX_WEB_SEARCH_TTL_MINUTES))
    return {"action": action, "ttl_minutes": ttl}


def apply_network_policy_command(
    conn: sqlite3.Connection,
    *,
    message: str,
    is_owner: bool,
    actor_ref: str,
    channel: str,
) -> dict | None:
    command = parse_network_policy_command(message)
    if command is None:
        return None
    if not is_owner:
        return {
            "status": "denied",
            "reply": "网络策略只能由 Owner 修改；本次没有改变任何权限。",
            "policy": get_network_policy(conn),
        }
    if command["action"] == "status":
        policy = get_network_policy(conn)
    else:
        policy = set_network_policy(
            conn,
            owner_web_search_enabled=command["action"] == "enable",
            ttl_minutes=command["ttl_minutes"],
            actor_ref=actor_ref,
            channel=channel,
        )
    if command["action"] == "enable":
        hours = command["ttl_minutes"] / 60
        duration_text = (
            f"{int(hours)} 小时"
            if hours.is_integer()
            else f"{command['ttl_minutes']} 分钟"
        )
        reply = (
            f"已临时允许 Owner 后台任务使用 Codex Web Search，{duration_text}后自动失效。"
            "这不会开放任意 shell 网络、CAP_NET_ADMIN 或 danger-full-access；"
            "固定 Capability 仍按受控来源运行。"
        )
    elif command["action"] == "disable":
        reply = (
            "已关闭 Owner 后台任务的 Codex Web Search。"
            "任意 shell 网络仍保持关闭；固定 Capability 是否联网由基础策略决定。"
        )
    else:
        reply = (
            "当前网络策略："
            f"固定 Capability={'允许' if policy['base_mode'] == 'capability_only' else '关闭'}；"
            f"Owner Web Search={'已开启' if policy['owner_web_search_active'] else '未开启'}。"
        )
    return {"status": "applied", "reply": reply, "policy": policy}


__all__ = [
    "DEFAULT_WEB_SEARCH_TTL_MINUTES",
    "MAX_WEB_SEARCH_TTL_MINUTES",
    "NETWORK_CAPABILITY_IDS",
    "apply_network_policy_command",
    "get_network_policy",
    "list_network_policy_events",
    "network_capability_allowed",
    "parse_network_policy_command",
    "set_network_policy",
    "task_web_search_allowed",
]
