from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def compact_projection(item: dict, fields: tuple[str, ...]) -> dict:
    return {field: item.get(field) for field in fields if item.get(field) not in (None, "")}


def slugify(value: str, fallback: str = "project") -> str:
    value = (value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-._")
    if slug:
        return slug[:48]
    digest = hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"{fallback}-{digest}"


def memory_from_row(row: Any) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "kind": row["kind"],
        "content": row["content"],
        "source": row["source"],
        "score": row["score"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_used_at": row["last_used_at"],
    }


def clip_text(value: object, limit: int = 800) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def qq_event_from_row(row: Any) -> dict:
    return {
        "id": row["id"],
        "trace_id": row["trace_id"],
        "user_id": row["user_id"],
        "stage": row["stage"],
        "action": row["action"],
        "status": row["status"],
        "task_id": row["task_id"],
        "message": row["message"],
        "detail": row["detail"],
        "created_at": row["created_at"],
    }


def quality_event_from_row(row: Any) -> dict:
    try:
        checks = json.loads(row["checks"] or "{}")
    except json.JSONDecodeError:
        checks = {}
    try:
        issues = json.loads(row["issues"] or "[]")
    except json.JSONDecodeError:
        issues = []
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "intent": row["intent"],
        "provider": row["provider"],
        "request": row["request"],
        "response": row["response"],
        "checks": checks,
        "status": row["status"],
        "issues": issues,
        "tool": row["tool"],
        "fallback": bool(row["fallback"]),
        "duration": row["duration"],
        "created_at": row["created_at"],
    }


def mode_session_from_row(row: Any) -> dict | None:
    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "mode": row["mode"],
        "intent": row["intent"],
        "confidence": row["confidence"],
        "reason": row["reason"],
        "source": row["source"],
        "work_lifecycle": row["work_lifecycle"],
        "turn_count": row["turn_count"],
        "work_turns": row["work_turns"],
        "expires_at": row["expires_at"],
        "ended_reason": row["ended_reason"],
        "updated_at": row["updated_at"],
    }
