#!/usr/bin/env python3
"""Server-owned validation for structured natural-group model decisions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable


SILENT_REASON_CODES = {
    "no_concrete_anchor", "sensitive_topic", "interpersonal_conflict",
    "already_answered", "topic_closed", "low_relevance",
}


def _decision_object(raw: object) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _confidence(data: dict, is_mention: bool) -> float:
    value = data.get("confidence", 0.5 if is_mention else 0.0)
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.5 if is_mention else 0.0


def parse_group_decision_contract(
    raw: object,
    *,
    is_mention: bool,
    expected_anchor_message_id: int,
    normalize_action: Callable[..., str],
    normalize_cues: Callable[[dict], dict],
    clip: Callable[[object, int], str],
    action_approach: dict[str, str],
) -> dict:
    """Parse model JSON without allowing it to choose durable server codes."""

    data = _decision_object(raw)
    confidence = _confidence(data, is_mention)
    requested_reply = bool(data.get("should_reply")) or is_mention
    social_action = normalize_action(
        data.get("social_action"), approach=data.get("approach"), should_reply=requested_reply,
    )
    should_reply = bool(requested_reply and social_action != "silent")
    mode = str(data.get("mode") or "daily").strip().lower()
    intent = str(data.get("intent") or "chat").strip().lower()
    expected_anchor = max(0, int(expected_anchor_message_id or 0))
    silent_reason = str(data.get("silent_reason") or "").strip()
    if expected_anchor:
        valid = (
            type(data.get("anchor_message_id")) is int
            and int(data["anchor_message_id"]) == expected_anchor
        ) if should_reply else social_action == "silent" and silent_reason in SILENT_REASON_CODES
        if not valid:
            should_reply, social_action, reason = False, "silent", "engagement_contract_invalid"
        elif should_reply:
            reason = "direct_mention" if is_mention else "model_engagement_approved"
        else:
            reason = silent_reason
    else:
        reason = "direct_mention" if is_mention else (
            "model_engagement_approved" if should_reply else "model_engagement_declined"
        )
    return {
        **data,
        **normalize_cues(data),
        "should_reply": should_reply,
        "social_action": social_action,
        "approach": action_approach.get(social_action, ""),
        "group_action_plan": {"schema_version": 1, "action": social_action},
        "confidence": confidence,
        "reason": reason,
        "model_reason": clip(data.get("reason"), 500),
        "mode": mode if mode in {"daily", "work", "mixed"} else "daily",
        "intent": intent if intent in {"chat", "analysis", "research", "code", "ops"} else "chat",
    }


__all__ = ["SILENT_REASON_CODES", "parse_group_decision_contract"]
