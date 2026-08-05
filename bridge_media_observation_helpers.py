"""Small, bounded helpers for group media-observation policy facts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from bridge_media_observation import DEFAULT_MEDIA_BURST_LIMIT


def has_visual_attachment(attachments: object) -> bool:
    """Recognize typed visual components without touching payload attributes."""

    if not isinstance(attachments, (list, tuple)):
        return False
    visual = {
        "image", "photo", "picture", "video", "gif", "mface",
        "marketface", "market_face", "dynamicface", "dynamic_face",
    }
    for item in attachments:
        if not isinstance(item, dict):
            continue
        token = str(
            item.get("media_kind") or item.get("type")
            or item.get("source_component") or ""
        ).strip().lower().replace("-", "_")
        if token in visual:
            return True
    return False


def media_budget_snapshot(
    conn: sqlite3.Connection,
    group_id: str,
    policy: dict,
) -> tuple[int, int]:
    """Read existing timing/count facts for a media policy decision."""

    try:
        row = conn.execute(
            "SELECT day_key,daily_reply_count,burst_message_count "
            "FROM group_participation_budget WHERE group_id=?",
            (str(group_id or "").strip(),),
        ).fetchone()
        daily_budget_raw = policy.get("daily_reply_budget")
        daily_budget = max(
            0,
            min(int(20 if daily_budget_raw in {None, ""} else daily_budget_raw), 200),
        )
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        return DEFAULT_MEDIA_BURST_LIMIT, 0

    today = datetime.now(timezone.utc).date().isoformat()
    daily_used = (
        int(row["daily_reply_count"] or 0)
        if row and str(row["day_key"] or "") == today
        else 0
    )
    burst_count = int(row["burst_message_count"] or 0) if row else 0
    return burst_count, max(0, daily_budget - daily_used)


def media_burst_limit(policy: dict) -> int:
    """Read the existing group-policy burst cap for media observations."""

    raw = policy.get("burst_max_messages")
    try:
        return max(2, min(int(6 if raw in {None, ""} else raw), 30))
    except (TypeError, ValueError, OverflowError):
        return 6


__all__ = [
    "has_visual_attachment",
    "media_budget_snapshot",
    "media_burst_limit",
]
