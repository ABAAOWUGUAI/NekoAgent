#!/usr/bin/env python3
"""Pure, bounded floor for natural group-participation decisions."""

from __future__ import annotations

import json
import sqlite3

from bridge_conversation_participation_contract import GroupParticipationMode, group_mode_from_legacy


NATURAL_PARTICIPATION_FLOOR_WINDOW_COUNT = 8
_FLOOR_ELIGIBLE_SILENT_REASONS = {"low_relevance"}
_FLOOR_TERMINAL_STAGES = {"model_declined", "delivery_queued", "ack_confirmed"}


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
    return bool(
        _is_floor_terminal(payload)
        and str(row["source_message_id"] or "")
        and str(row["model_role"] or "") == "conversation_engagement"
        and str(row["action"] or "") == "silent"
        and str(row["reason_code"] or "") in _FLOOR_ELIGIBLE_SILENT_REASONS
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
    """Promote only the ninth consecutive eligible natural-group silence.

    The worker has already loaded and validated the current text anchor. This
    helper observes only its identifier and durable lifecycle/decision
    metadata; it never reads message content or creates a counter. The
    optional current decision id excludes the worker's still-deferred current
    candidate from the historical consecutive window.
    """

    result = dict(decision or {})
    result["participation_floor_applied"] = False
    if group_mode_from_legacy(policy) is not GroupParticipationMode.NATURAL_PARTICIPATION:
        return result
    if int(anchor.get("id") or 0) <= 0:
        return result
    frame = conversation_frame if isinstance(conversation_frame, dict) else {}
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
    for row in rows:
        payload = _decision_payload(row)
        if not _is_floor_eligible_silence(row, payload):
            return result
        eligible_count += 1
        if eligible_count == NATURAL_PARTICIPATION_FLOOR_WINDOW_COUNT:
            return {
                **result,
                "should_reply": True,
                "social_action": "ack",
                "reason": "participation_floor_applied",
                "participation_floor_applied": True,
            }
    return result


__all__ = ["NATURAL_PARTICIPATION_FLOOR_WINDOW_COUNT", "apply_natural_participation_floor"]
