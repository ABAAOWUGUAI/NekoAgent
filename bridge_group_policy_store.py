#!/usr/bin/env python3
"""Persistence and normalization for the single group-policy fact source."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bridge_conversation_participation_contract import (
    GroupParticipationMode,
    group_mode_from_legacy,
    legacy_group_flags_for_mode,
)
from bridge_group_context_frame import DEFAULT_GROUP_CONTEXT_LIMIT, normalize_group_context_limit


DEFAULT_TIMEZONE = "Asia/Shanghai"


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "启用"}


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        if name in {"UTC", "Etc/UTC"}:
            return timezone.utc
        if name == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=8), name)
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_time(value: object, default: str) -> str:
    text = str(value or default).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("invalid_quiet_time")
    return text


def upsert_group_policy(conn: sqlite3.Connection, payload: dict) -> dict:
    """Validate and persist one group policy without introducing a second store."""

    group_id = _clip(payload.get("group_id") or payload.get("id"), 80)
    if not group_id:
        raise ValueError("group_id_required")
    timezone_name = _clip(payload.get("timezone") or DEFAULT_TIMEZONE, 80) or DEFAULT_TIMEZONE
    try:
        _timezone(timezone_name)
    except Exception as exc:
        raise ValueError("invalid_timezone") from exc
    try:
        probability = max(0.0, min(float(payload.get("reply_probability") or 0.2), 1.0))
    except (TypeError, ValueError):
        probability = 0.2
    try:
        cooldown = max(15, min(int(payload.get("cooldown_seconds") or 180), 86400))
        max_context = normalize_group_context_limit(payload.get("max_context"))
        quiet_gap_raw = payload.get("quiet_gap_seconds")
        quiet_gap = max(0, min(int(8 if quiet_gap_raw in {None, ""} else quiet_gap_raw), 120))
        burst_window = max(5, min(int(payload.get("burst_window_seconds") or 12), 300))
        burst_max = max(2, min(int(payload.get("burst_max_messages") or 6), 30))
        daily_budget_raw = payload.get("daily_reply_budget")
        daily_budget = max(0, min(int(20 if daily_budget_raw in {None, ""} else daily_budget_raw), 200))
        continuation_window_raw = payload.get("continuation_window_seconds")
        continuation_window = max(15, min(int(120 if continuation_window_raw in {None, ""} else continuation_window_raw), 600))
        max_auto_raw = payload.get("max_auto_continuations")
        max_auto_continuations = max(1, min(int(2 if max_auto_raw in {None, ""} else max_auto_raw), 3))
    except (TypeError, ValueError):
        cooldown, max_context = 180, DEFAULT_GROUP_CONTEXT_LIMIT
        quiet_gap, burst_window, burst_max, daily_budget = 8, 12, 6, 20
        continuation_window, max_auto_continuations = 120, 2
    requested_mode = str(payload.get("participation_mode") or "").strip()
    if requested_mode:
        mode = GroupParticipationMode(requested_mode)
        legacy_flags = legacy_group_flags_for_mode(mode)
    else:
        legacy_flags = {
            "enabled": 1 if _truthy(payload.get("enabled")) else 0,
            "mention_only": 1 if _truthy(payload.get("mention_only", "1")) else 0,
            "active_reply": 1 if _truthy(payload.get("active_reply")) else 0,
        }
        mode = group_mode_from_legacy(legacy_flags)
    values = {
        "group_name": _clip(payload.get("group_name") or payload.get("name"), 120),
        "session": _clip(payload.get("session"), 300),
        "participation_mode": mode.value,
        **legacy_flags,
        "reply_probability": probability,
        "cooldown_seconds": cooldown,
        "quiet_start": _normalize_time(payload.get("quiet_start"), "23:30"),
        "quiet_end": _normalize_time(payload.get("quiet_end"), "08:30"),
        "timezone": timezone_name,
        "max_context": max_context,
        "allow_work": 1 if _truthy(payload.get("allow_work")) else 0,
        "allowed_work_senders": _clip(payload.get("allowed_work_senders"), 500),
        "meme_enabled": 1 if _truthy(payload.get("meme_enabled")) else 0,
        "quiet_gap_seconds": quiet_gap,
        "burst_window_seconds": burst_window,
        "burst_max_messages": burst_max,
        "daily_reply_budget": daily_budget,
        "continuation_window_seconds": continuation_window,
        "max_auto_continuations": max_auto_continuations,
    }
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO group_policies(
            group_id, group_name, session, participation_mode, enabled, mention_only, active_reply,
            reply_probability, cooldown_seconds, quiet_start, quiet_end, timezone,
            max_context, allow_work, allowed_work_senders, meme_enabled,
            quiet_gap_seconds, burst_window_seconds, burst_max_messages, daily_reply_budget,
            continuation_window_seconds, max_auto_continuations,
            last_reply_at, message_count, reply_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            group_name = excluded.group_name,
            session = CASE WHEN excluded.session <> '' THEN excluded.session ELSE group_policies.session END,
            participation_mode = excluded.participation_mode,
            enabled = excluded.enabled,
            mention_only = excluded.mention_only,
            active_reply = excluded.active_reply,
            reply_probability = excluded.reply_probability,
            cooldown_seconds = excluded.cooldown_seconds,
            quiet_start = excluded.quiet_start,
            quiet_end = excluded.quiet_end,
            timezone = excluded.timezone,
            max_context = excluded.max_context,
            allow_work = excluded.allow_work,
            allowed_work_senders = excluded.allowed_work_senders,
            meme_enabled = excluded.meme_enabled,
            quiet_gap_seconds = excluded.quiet_gap_seconds,
            burst_window_seconds = excluded.burst_window_seconds,
            burst_max_messages = excluded.burst_max_messages,
            daily_reply_budget = excluded.daily_reply_budget,
            continuation_window_seconds = excluded.continuation_window_seconds,
            max_auto_continuations = excluded.max_auto_continuations,
            updated_at = excluded.updated_at
        """,
        (
            group_id, values["group_name"], values["session"], values["participation_mode"],
            values["enabled"], values["mention_only"], values["active_reply"],
            values["reply_probability"], values["cooldown_seconds"], values["quiet_start"],
            values["quiet_end"], values["timezone"], values["max_context"], values["allow_work"],
            values["allowed_work_senders"], values["meme_enabled"], values["quiet_gap_seconds"],
            values["burst_window_seconds"], values["burst_max_messages"], values["daily_reply_budget"],
            values["continuation_window_seconds"], values["max_auto_continuations"], now, now,
        ),
    )
    return _present_group_policy(
        conn.execute("SELECT * FROM group_policies WHERE group_id = ?", (group_id,)).fetchone(),
    )


def _present_group_policy(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    policy = dict(row)
    mode = group_mode_from_legacy(policy)
    policy["participation_mode"] = mode.value
    policy.update(legacy_group_flags_for_mode(mode))
    return policy


def list_group_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM group_policies ORDER BY enabled DESC, updated_at DESC, group_id",
    ).fetchall()
    return [_present_group_policy(row) for row in rows]


def get_group_policy(conn: sqlite3.Connection, group_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM group_policies WHERE group_id = ?",
        (str(group_id or "").strip(),),
    ).fetchone()
    return _present_group_policy(row)
