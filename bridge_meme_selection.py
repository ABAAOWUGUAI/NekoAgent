"""Stateful meme candidate pooling and optional visual selection.

The asset table remains the source of truth. This module adds the missing
selection evidence without coupling the meme domain to a specific provider.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from bridge_meme_social import utc_now


def _canonical_emotion(value: object) -> str:
    emotion = str(value or "daily").strip().lower() or "daily"
    return {
        "question": "curious",
        "care": "comfort",
        "rest": "comfort",
        "celebrate": "happy",
        "greeting": "happy",
        "thanks": "happy",
        "awkward": "playful",
    }.get(emotion, emotion)


def ensure_meme_selection_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meme_selection_events (
            id TEXT PRIMARY KEY,
            selection_id TEXT NOT NULL,
            method TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            selected_index INTEGER NOT NULL DEFAULT 0,
            model_role TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meme_selection_selection ON meme_selection_events(selection_id)",
    )


def meme_pool_health(conn: sqlite3.Connection) -> dict:
    rows = [dict(row) for row in conn.execute("SELECT * FROM meme_assets").fetchall()]
    approved = [
        item for item in rows
        if int(item.get("enabled") or 0) and item.get("review_status") == "approved"
    ]
    available = [item for item in approved if Path(str(item.get("file_path") or "")).is_file()]
    emotions: dict[str, int] = {}
    for item in available:
        key = _canonical_emotion(item.get("emotion"))
        emotions[key] = emotions.get(key, 0) + 1
    required = ("daily", "happy", "comfort", "playful", "curious", "work")
    gaps = [item for item in required if not emotions.get(item)]
    selection_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meme_selection_events'",
    ).fetchone()
    event_rows = []
    if selection_table:
        event_rows = conn.execute(
            """
            SELECT reason_code, COUNT(*) AS count FROM (
                SELECT reason_code FROM meme_selection_events ORDER BY created_at DESC LIMIT 50
            ) GROUP BY reason_code ORDER BY count DESC
            """,
        ).fetchall()
    state = "ready" if len(available) >= 6 and len(gaps) <= 2 else "degraded" if available else "blocked"
    return {
        "state": state,
        "total": len(rows),
        "enabled_approved": len(approved),
        "available_files": len(available),
        "pending_review": sum(item.get("review_status") == "pending" for item in rows),
        "missing_files": len(approved) - len(available),
        "emotions": emotions,
        "coverage_gaps": gaps,
        "recent_selection_reasons": {str(row["reason_code"]): int(row["count"]) for row in event_rows},
    }


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _emotion(text: str, mode: str, intent: str, hint: str) -> str:
    if str(hint or "").strip():
        return str(hint).strip().lower()
    joined = f"{text or ''} {mode or ''} {intent or ''}".lower()
    if mode == "work" or intent in {"ops", "code", "research", "analysis"}:
        return "work"
    if any(word in joined for word in ("难受", "失败", "累", "烦", "哭")):
        return "comfort"
    if any(word in joined for word in ("开心", "好耶", "庆祝", "成功", "谢谢")):
        return "happy"
    if any(word in joined for word in ("疑惑", "什么", "问号")):
        return "curious"
    if any(word in joined for word in ("无语", "尴尬", "坏笑")):
        return "playful"
    return "daily"


def _recent_ids(conn: sqlite3.Connection, user_id: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT meme_id FROM meme_send_history
        WHERE user_id = ? AND status = 'sent'
        ORDER BY sent_at DESC LIMIT 3
        """,
        (str(user_id or ""),),
    ).fetchall()
    return {str(row["meme_id"]) for row in rows}


def _sent_today(conn: sqlite3.Connection, meme_id: str, day_start: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count FROM meme_send_history
        WHERE meme_id = ? AND status = 'sent' AND sent_at >= ?
        """,
        (meme_id, day_start),
    ).fetchone()
    return int(row["count"] if row else 0)


def candidate_pool(
    conn: sqlite3.Connection,
    *,
    text: str,
    mode: str,
    intent: str,
    emotion_hint: str,
    user_id: str,
    allow_recent_reuse: bool,
) -> tuple[list[dict], dict]:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    target = _emotion(text, mode, intent, emotion_hint)
    recent = set() if allow_recent_reuse else _recent_ids(conn, user_id)
    rows = conn.execute(
        """
        SELECT * FROM meme_assets
        WHERE enabled = 1 AND review_status = 'approved'
        ORDER BY usage_count ASC, weight DESC, updated_at DESC LIMIT 200
        """,
    ).fetchall()
    matching: list[dict] = []
    broad: list[dict] = []
    excluded = {"recent": 0, "cooldown": 0, "daily_limit": 0, "invalid": 0}
    for row in rows:
        item = dict(row)
        path = str(item.get("file_path") or "").strip()
        if not path or not Path(path).is_file():
            excluded["invalid"] += 1
            continue
        if item["id"] in recent:
            excluded["recent"] += 1
            continue
        if _sent_today(conn, item["id"], day_start) >= int(item.get("max_daily") or 3):
            excluded["daily_limit"] += 1
            continue
        last_used = _parse_time(item.get("last_used_at"))
        if last_used and now - last_used < timedelta(minutes=max(0, int(item.get("cooldown_minutes") or 60))):
            excluded["cooldown"] += 1
            continue
        emotion = _canonical_emotion(item.get("emotion"))
        tags = str(item.get("tags") or "").lower()
        exact = emotion == target or bool(target and target in tags)
        score = 40 if emotion == target else 30 if emotion == "daily" else 20 if exact else 0
        ranked = {**item, "_score": score}
        broad.append(ranked)
        if exact:
            matching.append(ranked)
    pool = matching or broad
    pool.sort(key=lambda item: (-int(item["_score"]), int(item.get("usage_count") or 0), -int(item.get("weight") or 1)))
    reason = "matched_emotion" if matching else ("fallback_any_approved" if broad else "pool_exhausted")
    return pool, {
        "requested_emotion": target,
        "candidate_count": len(pool),
        "matched_count": len(matching),
        "approved_count": len(rows),
        "excluded": excluded,
        "reason_code": reason,
    }


def _image_part(item: dict) -> dict | None:
    path = Path(str(item.get("file_path") or ""))
    if not path.is_file():
        return None
    mime = str(item.get("mime_type") or "image/webp").strip() or "image/webp"
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def _vision_select(
    candidates: list[dict],
    *,
    selection_context: str,
    settings: dict | None,
    call_model: Callable | None,
    record_model: Callable | None,
    user_id: str,
) -> tuple[int, str, str]:
    caps = {str(item).strip() for item in (settings or {}).get("model_capabilities") or []}
    if not settings or not settings.get("model_registry_id") or not {"text", "vision"}.issubset(caps):
        return 0, "vision_selector_unavailable", ""
    transport = str(settings.get("model_transport") or "openai_chat_completions")
    if (
        str(settings.get("chat_provider") or "") != "openai-compatible"
        or transport not in {"openai_chat_completions", "azure_openai_chat_completions"}
        or not callable(call_model)
    ):
        return 0, "vision_selector_transport_unsupported", ""
    parts = [
        {
            "type": "text",
            "text": (
                "从候选表情中选择最适合当前消息的一张。只返回 JSON："
                '{"index":1,"reason":"不超过 30 字"}。'
                f"\n选择上下文：{str(selection_context or '')[:200]}\n候选数量：{len(candidates)}"
            ),
        },
    ]
    for index, item in enumerate(candidates, 1):
        image = _image_part(item)
        if image:
            parts.append({"type": "text", "text": f"候选 {index}：{str(item.get('name') or '')[:80]}"})
            parts.append(image)
    result = call_model(
        settings,
        [
            {"role": "system", "content": "你是受控的表情选择器，不输出图片之外的隐私内容。"},
            {"role": "user", "content": parts},
        ],
        45,
    )
    if callable(record_model):
        try:
            record_model(settings, result, source="meme_selection", user_id=user_id)
        except Exception:
            pass
    if not result.get("ok"):
        return 0, "vision_selector_failed", ""
    raw = str(result.get("reply") or "").strip()
    try:
        parsed = json.loads(raw)
        index = int(parsed.get("index") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0, "vision_selector_invalid_response", ""
    if not 1 <= index <= len(candidates):
        return 0, "vision_selector_index_invalid", ""
    return index, "vision_selected", str(parsed.get("reason") or "").strip()[:120]


def select_and_reserve_meme(
    conn: sqlite3.Connection,
    *,
    text: str,
    mode: str,
    intent: str,
    emotion_hint: str,
    user_id: str,
    session: str,
    allow_recent_reuse: bool,
    vision_settings: dict | None = None,
    call_model: Callable | None = None,
    record_model: Callable | None = None,
) -> tuple[dict | None, dict]:
    ensure_meme_selection_tables(conn)
    candidates, diagnostics = candidate_pool(
        conn,
        text=text,
        mode=mode,
        intent=intent,
        emotion_hint=emotion_hint,
        user_id=user_id,
        allow_recent_reuse=allow_recent_reuse,
    )
    if not candidates:
        return None, diagnostics
    index, method, reason = _vision_select(
        candidates[: min(len(candidates), 12)],
        selection_context=(
            f"情绪={diagnostics['requested_emotion']};"
            f"模式={str(mode or 'daily')[:20]};意图={str(intent or 'chat')[:40]}"
        ),
        settings=vision_settings,
        call_model=call_model,
        record_model=record_model,
        user_id=user_id,
    )
    if not index:
        index = 1
    chosen = dict(candidates[index - 1])
    selection_id = uuid.uuid4().hex[:16]
    emotion = str(chosen.get("emotion") or diagnostics["requested_emotion"] or "daily")
    conn.execute(
        """
        INSERT INTO meme_send_history(
            id, meme_id, user_id, session, mode, emotion, status, error, created_at, sent_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'selected', '', ?, '')
        """,
        (selection_id, chosen["id"], str(user_id or ""), str(session or ""), mode, emotion, utc_now()),
    )
    conn.execute(
        """
        INSERT INTO meme_selection_events(
            id, selection_id, method, reason_code, candidate_count, selected_index, model_role, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex[:16],
            selection_id,
            method if index > 1 or method == "vision_selected" else "deterministic",
            method if method != "vision_selected" else "vision_selected",
            len(candidates),
            index,
            "vision_caption" if method == "vision_selected" else "",
            utc_now(),
        ),
    )
    chosen.update({
        "selection_id": selection_id,
        "selected_emotion": diagnostics["requested_emotion"],
        "selection_method": method if index > 1 or method == "vision_selected" else "deterministic",
        "selection_reason": reason,
        "selection_diagnostics": diagnostics,
    })
    return chosen, diagnostics


__all__ = ["ensure_meme_selection_tables", "meme_pool_health", "select_and_reserve_meme"]
