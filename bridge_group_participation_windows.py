#!/usr/bin/env python3
"""Canonical, body-free time policy for ambient group participation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


AMBIENT_PARTICIPATION_TIMEZONE = "Asia/Shanghai"
DEFAULT_AMBIENT_PARTICIPATION_POLICY = {
    "timezone": AMBIENT_PARTICIPATION_TIMEZONE,
    "effective_at": "2026-08-17T00:00:00+08:00",
    "allowed_windows": [
        {"start": "18:00", "end": "09:00"},
        {"start": "12:00", "end": "14:00"},
    ],
}


def _as_datetime(value: object | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ambient_allowed_windows_invalid")
    return parsed


def _clock_minute(value: object) -> int:
    text = str(value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("ambient_allowed_windows_invalid") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or text != f"{hour:02d}:{minute:02d}":
        raise ValueError("ambient_allowed_windows_invalid")
    return hour * 60 + minute


def _window_minutes(start: int, end: int) -> set[int]:
    if start == end:
        raise ValueError("ambient_allowed_windows_invalid")
    if start < end:
        return set(range(start, end))
    return set(range(start, 24 * 60)) | set(range(0, end))


def normalize_ambient_windows(value: object) -> dict:
    """Return the only accepted two-window ambient participation policy."""

    if not isinstance(value, Mapping):
        raise ValueError("ambient_allowed_windows_invalid")
    timezone_name = str(value.get("timezone") or "").strip()
    if timezone_name != AMBIENT_PARTICIPATION_TIMEZONE:
        raise ValueError("ambient_allowed_windows_invalid")
    try:
        effective_at = _as_datetime(value.get("effective_at"))
    except (TypeError, ValueError) as exc:
        raise ValueError("ambient_allowed_windows_invalid") from exc
    raw_windows = value.get("allowed_windows")
    if not isinstance(raw_windows, list) or len(raw_windows) != 2:
        raise ValueError("ambient_allowed_windows_invalid")
    windows: list[dict] = []
    occupied: set[int] = set()
    for raw in raw_windows:
        if not isinstance(raw, Mapping):
            raise ValueError("ambient_allowed_windows_invalid")
        start, end = _clock_minute(raw.get("start")), _clock_minute(raw.get("end"))
        minutes = _window_minutes(start, end)
        if occupied & minutes:
            raise ValueError("ambient_allowed_windows_invalid")
        occupied.update(minutes)
        windows.append({"start": f"{start // 60:02d}:{start % 60:02d}", "end": f"{end // 60:02d}:{end % 60:02d}"})
    return {
        "timezone": AMBIENT_PARTICIPATION_TIMEZONE,
        "effective_at": effective_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "allowed_windows": windows,
    }


def _inside_window(local_minute: int, window: Mapping[str, object]) -> bool:
    start, end = _clock_minute(window.get("start")), _clock_minute(window.get("end"))
    return start <= local_minute < end if start < end else local_minute >= start or local_minute < end


def _shanghai_zone():
    try:
        return ZoneInfo(AMBIENT_PARTICIPATION_TIMEZONE)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8), AMBIENT_PARTICIPATION_TIMEZONE)


def ambient_window_decision(policy: Mapping[str, object], *, now: object | None = None) -> dict:
    """Allow ambient participation only after effective time and inside a window."""

    normalized = normalize_ambient_windows(policy)
    current = _as_datetime(now).astimezone(timezone.utc)
    effective_at = _as_datetime(normalized["effective_at"]).astimezone(timezone.utc)
    if current < effective_at:
        return {"action": "allow", "reason": "ambient_window_not_effective"}
    local = current.astimezone(_shanghai_zone())
    minute = local.hour * 60 + local.minute
    if any(_inside_window(minute, window) for window in normalized["allowed_windows"]):
        return {"action": "allow", "reason": "ambient_window_allowed"}
    return {"action": "cancel", "reason": "ambient_outside_participation_window"}


def ambient_participation_window_decision(conn, group_id: object, *, now: object | None = None) -> dict:
    """Resolve the live inherited policy before evaluating an ambient action."""

    from bridge_group_participation_window_schema import effective_ambient_window_policy

    policy = effective_ambient_window_policy(conn, group_id)
    return {
        **ambient_window_decision(policy, now=now),
        "policy_version": int(policy["version"]),
        "policy_origin": str(policy["origin"]),
        "policy_timezone": str(policy["timezone"]),
    }


__all__ = [
    "AMBIENT_PARTICIPATION_TIMEZONE",
    "DEFAULT_AMBIENT_PARTICIPATION_POLICY",
    "ambient_participation_window_decision",
    "ambient_window_decision",
    "normalize_ambient_windows",
]
