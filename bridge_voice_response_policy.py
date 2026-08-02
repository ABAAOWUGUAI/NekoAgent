#!/usr/bin/env python3
"""Server-owned response modality policy for Assistant voice delivery."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Mapping

from bridge_migrations import utc_now
from bridge_voice_response_policy_schema import VOICE_RESPONSE_POLICY_TABLE


VOICE_RESPONSE_MODES = {"text_only", "explicit_only", "emotion_auto", "always"}
EMOTION_KINDS = {"happy", "sad", "tired", "annoyed", "playful", "comfort"}
DEFAULT_EMOTIONS = ("happy", "sad", "tired", "annoyed", "playful", "comfort")

_EXPLICIT_PATTERNS = (
    re.compile(r"(?:请|麻烦|可以|能不能)?(?:用|以)语音(?:来)?(?:回复|回答|告诉|说|发|读|念)"),
    re.compile(r"(?:请|麻烦)?(?:给我)?发(?:一条|一个|个)?语音"),
    re.compile(r"(?:语音回复|语音回答|读给我听|念给我听|读出来|念出来)"),
)
_NEGATIVE_PATTERN = re.compile(r"(?:不要|别|不用|无需).{0,5}(?:语音|读|念)")


class VoiceResponsePolicyError(ValueError):
    pass


def explicit_voice_request(text: object) -> bool:
    message = " ".join(str(text or "").split())
    if not message or _NEGATIVE_PATTERN.search(message):
        return False
    return any(pattern.search(message) for pattern in _EXPLICIT_PATTERNS)


def negative_voice_request(text: object) -> bool:
    return bool(_NEGATIVE_PATTERN.search(" ".join(str(text or "").split())))


def voice_response_prompt_context(
    conn: sqlite3.Connection,
    message: object,
    *,
    scope: str,
    owner_authorized: bool,
) -> dict:
    """Project configured server capability into a prompt-safe fact object."""

    if scope != "private" or not owner_authorized or negative_voice_request(message):
        return {"available": False, "requested": False, "policy_may_select": False}
    row = conn.execute(
        f"""
        SELECT p.mode,v.id,v.status,
               COALESCE(MAX(CASE WHEN f.name='voice_output_v1' THEN f.enabled END),0),
               COALESCE(MAX(CASE WHEN f.name='voice_delivery_v1' THEN f.enabled END),0)
        FROM assistant_instances a
        JOIN {VOICE_RESPONSE_POLICY_TABLE} p ON p.assistant_id=a.id
        LEFT JOIN voice_packs v ON v.id=a.active_voice_pack_id
        LEFT JOIN assistant_feature_flags f
          ON f.name IN ('voice_output_v1','voice_delivery_v1')
        WHERE a.status='active'
        GROUP BY p.mode,v.id,v.status
        LIMIT 1
        """,
    ).fetchone()
    mode = str(row[0] or "text_only") if row else "text_only"
    available = bool(
        row
        and row[1]
        and str(row[2]) == "active"
        and bool(row[3])
        and bool(row[4])
        and mode != "text_only"
    )
    return {
        "available": available,
        "requested": bool(available and explicit_voice_request(message)),
        "policy_may_select": bool(available and mode in {"emotion_auto", "always"}),
        "policy_mode": mode,
    }


def _parse_emotions(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise VoiceResponsePolicyError("voice_response_emotions_invalid") from exc
    if not isinstance(value, (list, tuple)):
        raise VoiceResponsePolicyError("voice_response_emotions_invalid")
    result = tuple(dict.fromkeys(str(item).strip().lower() for item in value))
    if not result or any(item not in EMOTION_KINDS for item in result):
        raise VoiceResponsePolicyError("voice_response_emotions_invalid")
    return result


def _policy(row: Mapping[str, object]) -> dict:
    return {
        "assistant_id": str(row["assistant_id"]),
        "mode": str(row["mode"]),
        "emotion_kinds": list(_parse_emotions(row["emotion_kinds_json"])),
        "min_emotion_confidence": float(row["min_emotion_confidence"]),
        "cooldown_seconds": int(row["cooldown_seconds"]),
        "daily_limit": int(row["daily_limit"]),
        "version": int(row["version"]),
        "updated_at": str(row["updated_at"]),
    }


def active_voice_response_policy(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        f"""
        SELECT p.* FROM assistant_instances a
        JOIN {VOICE_RESPONSE_POLICY_TABLE} p ON p.assistant_id=a.id
        WHERE a.status='active' ORDER BY a.updated_at DESC,a.id LIMIT 1
        """,
    ).fetchone()
    if not row:
        raise VoiceResponsePolicyError("voice_response_policy_missing")
    return _policy(row)


def update_active_voice_response_policy(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
) -> dict:
    current = active_voice_response_policy(conn)
    mode = str(payload.get("mode") or current["mode"]).strip().lower()
    if mode not in VOICE_RESPONSE_MODES:
        raise VoiceResponsePolicyError("voice_response_mode_invalid")
    emotions = _parse_emotions(payload.get("emotion_kinds", current["emotion_kinds"]))
    try:
        confidence = float(payload.get("min_emotion_confidence", current["min_emotion_confidence"]))
        cooldown = int(payload.get("cooldown_seconds", current["cooldown_seconds"]))
        daily_limit = int(payload.get("daily_limit", current["daily_limit"]))
        expected_version = int(payload.get("expected_version") or current["version"])
    except (TypeError, ValueError) as exc:
        raise VoiceResponsePolicyError("voice_response_policy_value_invalid") from exc
    if not 0.5 <= confidence <= 1.0:
        raise VoiceResponsePolicyError("voice_response_confidence_invalid")
    if not 0 <= cooldown <= 86400 or not 0 <= daily_limit <= 100:
        raise VoiceResponsePolicyError("voice_response_budget_invalid")
    now = utc_now()
    cursor = conn.execute(
        f"""
        UPDATE {VOICE_RESPONSE_POLICY_TABLE}
        SET mode=?,emotion_kinds_json=?,min_emotion_confidence=?,cooldown_seconds=?,
            daily_limit=?,version=version+1,updated_at=?
        WHERE assistant_id=? AND version=?
        """,
        (
            mode,
            json.dumps(emotions, ensure_ascii=False, separators=(",", ":")),
            confidence,
            cooldown,
            daily_limit,
            now,
            current["assistant_id"],
            expected_version,
        ),
    )
    if cursor.rowcount != 1:
        raise VoiceResponsePolicyError("voice_response_policy_version_conflict")
    conn.commit()
    return active_voice_response_policy(conn)


def _affect(result: Mapping[str, object]) -> dict:
    mode = result.get("mode_decision") if isinstance(result.get("mode_decision"), dict) else {}
    plan = mode.get("interaction_plan") if isinstance(mode.get("interaction_plan"), dict) else {}
    affect = plan.get("affect") if isinstance(plan.get("affect"), dict) else {}
    kind = str(affect.get("kind") or mode.get("emotion") or "neutral").strip().lower()
    try:
        confidence = float(affect.get("confidence", mode.get("emotion_confidence", 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    present = bool(affect.get("expression_present", kind != "neutral"))
    support = max(
        (
            float(item.get("confidence") or 0.0)
            for item in plan.get("intents") or []
            if isinstance(item, dict) and item.get("type") == "emotional_support"
        ),
        default=0.0,
    )
    if kind == "neutral" and support > confidence:
        kind, confidence, present = "comfort", support, True
    return {"kind": kind, "confidence": max(confidence, support), "present": present}


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decide_and_reserve_voice_response(
    conn: sqlite3.Connection,
    result: Mapping[str, object],
    transport: Mapping[str, object],
    *,
    scope: str,
    owner_authorized: bool,
) -> dict | None:
    """Choose voice and atomically reserve auto budget; explicit requests ignore budget."""

    if (
        scope != "private"
        or not owner_authorized
        or negative_voice_request(transport.get("message"))
    ):
        return None
    row = conn.execute(
        f"""
        SELECT a.id,p.* FROM assistant_instances a
        JOIN {VOICE_RESPONSE_POLICY_TABLE} p ON p.assistant_id=a.id
        WHERE a.status='active' ORDER BY a.updated_at DESC,a.id LIMIT 1
        """,
    ).fetchone()
    if not row:
        raise VoiceResponsePolicyError("voice_response_policy_missing")
    policy = _policy(row)
    explicit = explicit_voice_request(transport.get("message"))
    if policy["mode"] == "text_only":
        return None
    if explicit:
        return {"trigger": "explicit", "policy": policy, "affect": _affect(result)}
    if policy["mode"] == "explicit_only":
        return None
    if str(result.get("dispatch") or "chat") != "chat":
        return None
    affect = _affect(result)
    if policy["mode"] == "emotion_auto" and not (
        affect["present"]
        and affect["kind"] in set(policy["emotion_kinds"])
        and affect["confidence"] >= policy["min_emotion_confidence"]
    ):
        return None

    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    last = _parse_time(row[7])
    count = int(row[9] or 0) if str(row[8] or "") == day else 0
    if policy["daily_limit"] == 0 or count >= policy["daily_limit"]:
        return None
    if last and (now - last).total_seconds() < policy["cooldown_seconds"]:
        return None
    cursor = conn.execute(
        f"""
        UPDATE {VOICE_RESPONSE_POLICY_TABLE}
        SET last_auto_voice_at=?,auto_voice_day=?,auto_voice_count=?,updated_at=?
        WHERE assistant_id=? AND version=?
        """,
        (now.isoformat(), day, count + 1, utc_now(), policy["assistant_id"], policy["version"]),
    )
    if cursor.rowcount != 1:
        raise VoiceResponsePolicyError("voice_response_policy_reservation_conflict")
    conn.commit()
    return {
        "trigger": "emotion" if policy["mode"] == "emotion_auto" else "always",
        "policy": policy,
        "affect": affect,
        "reservation": {
            "assistant_id": policy["assistant_id"],
            "policy_version": policy["version"],
            "reserved_at": now.isoformat(),
            "reserved_day": day,
            "reserved_count": count + 1,
            "previous_last_auto_voice_at": str(row[7] or ""),
            "previous_auto_voice_day": str(row[8] or ""),
            "previous_auto_voice_count": int(row[9] or 0),
        },
    }


def release_voice_response_reservation(
    conn: sqlite3.Connection,
    decision: Mapping[str, object],
) -> bool:
    """Refund one failed automatic-voice reservation using a strict CAS."""

    reservation = decision.get("reservation")
    if not isinstance(reservation, Mapping):
        return False
    try:
        cursor = conn.execute(
            f"""
            UPDATE {VOICE_RESPONSE_POLICY_TABLE}
            SET last_auto_voice_at=?,auto_voice_day=?,auto_voice_count=?,updated_at=?
            WHERE assistant_id=? AND version=?
              AND last_auto_voice_at=? AND auto_voice_day=? AND auto_voice_count=?
            """,
            (
                str(reservation.get("previous_last_auto_voice_at") or ""),
                str(reservation.get("previous_auto_voice_day") or ""),
                int(reservation.get("previous_auto_voice_count") or 0),
                utc_now(),
                str(reservation.get("assistant_id") or ""),
                int(reservation.get("policy_version") or 0),
                str(reservation.get("reserved_at") or ""),
                str(reservation.get("reserved_day") or ""),
                int(reservation.get("reserved_count") or 0),
            ),
        )
    except (TypeError, ValueError) as exc:
        raise VoiceResponsePolicyError("voice_response_reservation_invalid") from exc
    conn.commit()
    return cursor.rowcount == 1


__all__ = [
    "DEFAULT_EMOTIONS",
    "EMOTION_KINDS",
    "VOICE_RESPONSE_MODES",
    "VoiceResponsePolicyError",
    "active_voice_response_policy",
    "decide_and_reserve_voice_response",
    "explicit_voice_request",
    "negative_voice_request",
    "release_voice_response_reservation",
    "update_active_voice_response_policy",
    "voice_response_prompt_context",
]
