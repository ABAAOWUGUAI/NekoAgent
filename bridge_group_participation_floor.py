#!/usr/bin/env python3
"""Read-only opportunity diagnostic; never override a model silence."""

from __future__ import annotations

import json
import sqlite3

from bridge_conversation_participation_contract import GroupParticipationMode, group_mode_from_legacy


NATURAL_PARTICIPATION_FLOOR_WINDOW_COUNT = 8
_FLOOR_ELIGIBLE_SILENT_REASONS = {"low_relevance", "no_concrete_anchor"}
_FLOOR_DEFAULT_WINDOW_COUNT = 8
_FLOOR_MEDIUM_WINDOW_COUNT = 5
_FLOOR_ACTIVE_WINDOW_COUNT = 3
_FLOOR_TERMINAL_STAGES = {
    "model_declined", "truth_blocked", "delivery_queued", "ack_confirmed",
}


def participation_floor_window(policy: dict) -> int:
    """Return the consecutive-silence window for the current group strength."""

    try:
        strength = max(0.0, min(float(policy.get("reply_probability") or 0.2), 1.0))
    except (TypeError, ValueError):
        strength = 0.2
    if strength >= 0.70:
        return _FLOOR_ACTIVE_WINDOW_COUNT
    if strength >= 0.45:
        return _FLOOR_MEDIUM_WINDOW_COUNT
    return _FLOOR_DEFAULT_WINDOW_COUNT


def _decision_payload(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(str(row["decision_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _is_floor_terminal(payload: dict) -> bool:
    lifecycle = payload.get("participation_lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    return str(lifecycle.get("stage") or "") in _FLOOR_TERMINAL_STAGES


def _is_floor_eligible_silence(row: sqlite3.Row, payload: dict) -> bool:
    frame = payload.get("group_conversation_frame")
    frame = frame if isinstance(frame, dict) else {}
    message_kind = str(frame.get("message_kind") or "")
    return bool(
        _is_floor_terminal(payload)
        and str(row["source_message_id"] or "")
        and str(row["model_role"] or "") in {"conversation_engagement", "conversation_reply"}
        and str(row["action"] or "") == "silent"
        and str(row["reason_code"] or "") in _FLOOR_ELIGIBLE_SILENT_REASONS
        and message_kind in {"text", "mixed"}
        and not bool(frame.get("acknowledgement_only"))
        and not bool(frame.get("attachment_only"))
    )


def apply_natural_participation_floor(
    conn: sqlite3.Connection,
    *,
    policy: dict,
    group_id: str,
    anchor: dict,
    decision: dict,
    conversation_frame: dict,
    current_decision_id: str = "",
) -> dict:
    """Observe a bounded run of eligible silences without changing the decision.

    The worker has already loaded and validated the current text anchor. This
    helper observes only its identifier and durable lifecycle/decision
    metadata; it never reads message content or creates a counter. The
    optional current decision id excludes the worker's still-deferred current
    candidate from the historical consecutive window. Higher configured
    participation strength lowers the floor threshold, but safety, budget and
    content gates still run before this helper.
    """

    result = dict(decision or {})
    result["participation_floor_applied"] = False
    if group_mode_from_legacy(policy) is not GroupParticipationMode.NATURAL_PARTICIPATION:
        return result
    if int(anchor.get("id") or 0) <= 0:
        return result
    frame = conversation_frame if isinstance(conversation_frame, dict) else {}
    message_kind = str(frame.get("message_kind") or "")
    if message_kind == "attachment":
        return result
    if str(result.get("reason") or "") == "no_concrete_anchor":
        if not str(anchor.get("content") or "").strip() or message_kind not in {"text", "mixed"}:
            return result
    if bool(frame.get("acknowledgement_only")) or bool(frame.get("attachment_only")):
        return result
    if (
        bool(result.get("should_reply"))
        or str(result.get("social_action") or "silent") != "silent"
        or str(result.get("reason") or "") not in _FLOOR_ELIGIBLE_SILENT_REASONS
    ):
        return result

    query = """SELECT source_message_id,action,reason_code,model_role,decision_json
                 FROM engagement_decisions
                 WHERE thread_id=?"""
    parameters: list[str] = [f"qq:group:{str(group_id or '').strip()}"]
    if str(current_decision_id or "").strip():
        query += " AND id<>?"
        parameters.append(str(current_decision_id).strip())
    rows = conn.execute(
        f"{query} ORDER BY created_at DESC,id DESC LIMIT 64",
        parameters,
    ).fetchall()
    eligible_count = 0
    floor_window = participation_floor_window(policy)
    for row in rows:
        payload = _decision_payload(row)
        if not _is_floor_eligible_silence(row, payload):
            break
        eligible_count += 1
        if eligible_count >= floor_window:
            break
    result["opportunity_diagnostic"] = {
        "eligible_silent_run": eligible_count,
        "window": floor_window,
        "would_have_promoted": eligible_count >= floor_window,
    }
    return result


__all__ = [
    "NATURAL_PARTICIPATION_FLOOR_WINDOW_COUNT",
    "apply_natural_participation_floor",
    "participation_floor_window",
]
