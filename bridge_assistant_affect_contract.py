#!/usr/bin/env python3
"""BE-1 Assistant Affect contract, deliberately separate from User Affect."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import re


ASSISTANT_AFFECT_KINDS = frozenset(
    {
        "amused", "happy", "curious", "concerned", "hurt", "annoyed",
        "disappointed", "disagreeing", "impatient",
    },
)
ASSISTANT_AFFECT_REASON_CODES = frozenset(
    {
        "visible_text_event", "visible_insult_or_mockery", "visible_disrespect",
        "visible_hostility", "grounded_media", "topic_event", "member_behavior",
        "own_task_outcome", "correction",
    },
)
ASSISTANT_AFFECT_STYLE_TARGETS = frozenset({"expression_plan"})
ASSISTANT_AFFECT_ALLOWED_INFLUENCES = frozenset({"tone", "sentence_length", "humor", "emoji", "participation_value"})

_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,179}$")
_PROHIBITED_AFFECT_FIELDS = frozenset({"human_body_claim", "human_experience", "user_emotion"})
_MANIPULATIVE_REASON_CODES = frozenset({"unanswered_by_user", "user_left", "user_replied_to_other", "need_reassurance"})


class AssistantAffectContractError(ValueError):
    """Raised when a transient Assistant Affect would cross a safe boundary."""


def _utc(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AssistantAffectContractError("assistant_affect_time_invalid") from exc
    if parsed.tzinfo is None:
        raise AssistantAffectContractError("assistant_affect_time_invalid")
    return parsed.astimezone(timezone.utc)


def _ref(value: object) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise AssistantAffectContractError("assistant_affect_ref_invalid")
    return text


def _assistant_id(value: object) -> str:
    text = str(value or "").strip()
    if not _OPAQUE_ID_RE.fullmatch(text):
        raise AssistantAffectContractError("assistant_affect_ref_invalid")
    return text


def _assert_separate_from_other_domains(value: Mapping[str, object]) -> None:
    for raw_key in value:
        key = str(raw_key or "").strip().lower()
        if key in _PROHIBITED_AFFECT_FIELDS or key.startswith(
            ("approval", "delivery", "knowledge", "memory", "permission", "relationship", "task", "user_"),
        ):
            raise AssistantAffectContractError("assistant_affect_domain_violation")


def _exact_keys(value: Mapping[str, object], allowed: frozenset[str], *, error: str) -> None:
    if set(value) - allowed:
        raise AssistantAffectContractError(error)


def make_assistant_affect_shadow(value: Mapping[str, object]) -> dict:
    """Normalize one grounded, scoped, short-lived Affect state for Shadow only."""

    if not isinstance(value, Mapping):
        raise AssistantAffectContractError("assistant_affect_invalid")
    _assert_separate_from_other_domains(value)
    _exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "assistant_id",
                "scope_type",
                "scope_ref",
                "topic_ref",
                "topic_revision",
                "target_type",
                "target_ref",
                "primary_affect",
                "valence",
                "arousal",
                "intensity",
                "confidence",
                "trigger_evidence_refs",
                "reason_code",
                "created_at",
                "last_updated_at",
                "expires_at",
                "decay_policy",
                "policy_version",
                "state",
            },
        ),
        error="assistant_affect_invalid",
    )
    if value.get("schema_version") != 1 or value.get("state") != "shadow":
        raise AssistantAffectContractError("assistant_affect_state_invalid")
    if str(value.get("scope_type") or "").strip() not in {"group", "private"}:
        raise AssistantAffectContractError("assistant_affect_scope_invalid")
    if str(value.get("target_type") or "").strip() not in {"topic", "member", "task_result", "correction"}:
        raise AssistantAffectContractError("assistant_affect_target_invalid")
    if not isinstance(value.get("topic_revision"), int) or int(value["topic_revision"]) < 0:
        raise AssistantAffectContractError("assistant_affect_topic_invalid")
    primary_affect = str(value.get("primary_affect") or "").strip()
    reason_code = str(value.get("reason_code") or "").strip()
    if primary_affect not in ASSISTANT_AFFECT_KINDS or reason_code in _MANIPULATIVE_REASON_CODES:
        raise AssistantAffectContractError("assistant_affect_prohibited")
    if reason_code not in ASSISTANT_AFFECT_REASON_CODES:
        raise AssistantAffectContractError("assistant_affect_reason_invalid")
    if str(value.get("valence") or "").strip() not in {"negative", "neutral", "positive"}:
        raise AssistantAffectContractError("assistant_affect_valence_invalid")
    if str(value.get("arousal") or "").strip() not in {"low", "medium", "high"}:
        raise AssistantAffectContractError("assistant_affect_arousal_invalid")
    try:
        intensity, confidence = float(value.get("intensity")), float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise AssistantAffectContractError("assistant_affect_strength_invalid") from exc
    if not 0.0 <= intensity <= 1.0 or not 0.0 <= confidence <= 1.0:
        raise AssistantAffectContractError("assistant_affect_strength_invalid")
    triggers = value.get("trigger_evidence_refs")
    if not isinstance(triggers, list) or not 1 <= len(triggers) <= 32:
        raise AssistantAffectContractError("assistant_affect_trigger_missing")
    created_at, updated_at, expires_at = _utc(value.get("created_at")), _utc(value.get("last_updated_at")), _utc(value.get("expires_at"))
    if not created_at <= updated_at < expires_at:
        raise AssistantAffectContractError("assistant_affect_time_invalid")
    decay_policy = str(value.get("decay_policy") or "").strip()
    if decay_policy not in {"topic_bound_short", "turns_and_time"}:
        raise AssistantAffectContractError("assistant_affect_decay_invalid")
    return {
        "schema_version": 1,
        "assistant_id": _assistant_id(value.get("assistant_id")),
        "scope_type": str(value["scope_type"]),
        "scope_ref": _ref(value.get("scope_ref")),
        "topic_ref": _ref(value.get("topic_ref")),
        "topic_revision": int(value["topic_revision"]),
        "target_type": str(value["target_type"]),
        "target_ref": _ref(value.get("target_ref")),
        "primary_affect": primary_affect,
        "valence": str(value["valence"]),
        "arousal": str(value["arousal"]),
        "intensity": intensity,
        "confidence": confidence,
        "trigger_evidence_refs": [_ref(item) for item in triggers],
        "reason_code": reason_code,
        "created_at": created_at.isoformat(),
        "last_updated_at": updated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "decay_policy": decay_policy,
        "policy_version": _ref(value.get("policy_version")),
        "state": "shadow",
    }


def assistant_affect_applies(affect: Mapping[str, object], interaction: Mapping[str, object], *, now: object | None = None) -> bool:
    """Affect never carries over across scope, topic revision, target, or expiry."""

    normalized = make_assistant_affect_shadow(affect)
    if not isinstance(interaction, Mapping):
        return False
    required = ("scope_type", "scope_ref", "topic_ref", "topic_revision", "target_type", "target_ref")
    if any(item not in interaction for item in required):
        return False
    try:
        return (
            _utc(now if now is not None else datetime.now(timezone.utc)) < _utc(normalized["expires_at"])
            and str(interaction["scope_type"]) == normalized["scope_type"]
            and _ref(interaction["scope_ref"]) == normalized["scope_ref"]
            and _ref(interaction["topic_ref"]) == normalized["topic_ref"]
            and int(interaction["topic_revision"]) == normalized["topic_revision"]
            and str(interaction["target_type"]) == normalized["target_type"]
            and _ref(interaction["target_ref"]) == normalized["target_ref"]
        )
    except (AssistantAffectContractError, TypeError, ValueError):
        return False


def render_assistant_affect_influence(
    affect: Mapping[str, object],
    interaction: Mapping[str, object],
    expression: Mapping[str, object],
    *,
    now: object | None = None,
) -> dict:
    """Produce a style signal only; routing, facts, delivery and authority stay unchanged."""

    normalized = make_assistant_affect_shadow(affect)
    if not isinstance(expression, Mapping) or set(expression) != {"target"} or expression.get("target") not in ASSISTANT_AFFECT_STYLE_TARGETS:
        raise AssistantAffectContractError("assistant_affect_style_unsafe")
    if not assistant_affect_applies(normalized, interaction, now=now):
        return {"style_only": True, "active": False, "allowed_influences": []}
    styles = {
        "amused": ("light", "short", "optional", "optional", "small_positive"),
        "happy": ("warm", "medium", "optional", "optional", "small_positive"),
        "curious": ("engaged", "medium", "off", "off", "small_positive"),
        "concerned": ("calm", "short", "off", "off", "neutral"),
        "hurt": ("firm", "short", "off", "off", "neutral"),
        "annoyed": ("sharp", "short", "off", "off", "neutral"),
        "disappointed": ("direct", "short", "off", "off", "neutral"),
        "disagreeing": ("direct", "medium", "off", "off", "neutral"),
        "impatient": ("concise", "short", "off", "off", "neutral"),
    }[normalized["primary_affect"]]
    return {
        "style_only": True,
        "active": True,
        "allowed_influences": ["tone", "sentence_length", "humor", "emoji", "participation_value"],
        "tone": styles[0],
        "sentence_length": styles[1],
        "humor": styles[2],
        "emoji": styles[3],
        "participation_value": styles[4],
    }


__all__ = [
    "ASSISTANT_AFFECT_ALLOWED_INFLUENCES",
    "ASSISTANT_AFFECT_KINDS",
    "ASSISTANT_AFFECT_REASON_CODES",
    "AssistantAffectContractError",
    "assistant_affect_applies",
    "make_assistant_affect_shadow",
    "render_assistant_affect_influence",
]
