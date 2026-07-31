#!/usr/bin/env python3
"""Frozen AC-0 contracts for channel-neutral conversation participation.

This module is deliberately side-effect free.  It does not read SQLite, call a
model, send a channel message, or alter the current Bridge dispatch path.  AC-1
can depend on these types after the current behavior has been characterized.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


CONTRACT_SCHEMA_VERSION = 1


class _TextEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class CandidateKind(_TextEnum):
    INBOUND_DIRECT = "inbound_direct"
    CONTINUATION = "continuation"
    AMBIENT_GROUP = "ambient_group"
    SOCIAL_TIMER = "social_timer"
    OPERATIONAL_EVENT = "operational_event"
    AUTOMATION_EVENT = "automation_event"
    CONTROL_COMMAND = "control_command"


class ParticipationAction(_TextEnum):
    DIRECT_REPLY = "direct_reply"
    CONTINUATION_REPLY = "continuation_reply"
    CONTEXTUAL_PARTICIPATION = "contextual_participation"
    SOCIAL_INITIATION = "social_initiation"
    OPERATIONAL_NOTIFICATION = "operational_notification"
    CREATE_OR_REVISE_GOAL = "create_or_revise_goal"
    DETERMINISTIC_CONTROL_ACTION = "deterministic_control_action"
    SILENT = "silent"


class ParticipationReason(_TextEnum):
    DIRECT_PRIVATE = "direct_private"
    EXPLICIT_MENTION = "explicit_mention"
    REPLY_TO_ASSISTANT = "reply_to_assistant"
    WAITING_FOR_ACTOR = "waiting_for_actor"
    ACTIVE_CONVERSATION_CONTINUATION = "active_conversation_continuation"
    CONTINUATION_ACKNOWLEDGEMENT = "continuation_acknowledgement"
    MEDIA_AMBIENT_UNADDRESSED = "media_ambient_unaddressed"
    MEDIA_GATE_FOLLOWUP_UNADDRESSED = "media_gate_followup_unaddressed"
    ADMIN_COMMAND = "admin_command"
    GROUP_DISABLED = "group_disabled"
    GROUP_ACCESS_DENIED = "group_access_denied"
    MENTION_REQUIRED = "mention_required"
    PARTICIPATION_DISABLED = "participation_disabled"
    QUIET_HOURS = "quiet_hours"
    COOLDOWN = "cooldown"
    PARTICIPATION_BUDGET_EXHAUSTED = "participation_budget_exhausted"
    MODEL_UNAVAILABLE = "model_unavailable"
    CLASSIFIER_FAILED = "classifier_failed"
    ENGAGEMENT_BELOW_THRESHOLD = "engagement_below_threshold"
    MODEL_ENGAGEMENT_APPROVED = "model_engagement_approved"
    EMPTY_EVENT = "empty_event"
    UNSUPPORTED_EVENT = "unsupported_event"


class GroupParticipationMode(_TextEnum):
    DISABLED = "disabled"
    MENTIONS_ONLY = "mentions_only"
    DIRECTED_CONTEXT = "directed_context"
    NATURAL_PARTICIPATION = "natural_participation"


class RetentionClass(_TextEnum):
    METADATA_ONLY = "metadata_only"
    TRANSIENT = "transient"
    CONVERSATION = "conversation"
    GOVERNED = "governed"


class MessageKind(_TextEnum):
    TEXT = "text"
    MENTION_ONLY = "mention_only"
    MIXED = "mixed"
    ATTACHMENT = "attachment"
    SYSTEM = "system"


class ModelScenarioRole(_TextEnum):
    INTERACTION_CLASSIFIER = "interaction_classifier"
    CONVERSATION_ENGAGEMENT = "conversation_engagement"
    CONVERSATION_REPLY = "conversation_reply"
    WORK_PLANNER = "work_planner"
    WORK_EXECUTOR = "work_executor"
    MEMORY_REVIEW = "memory_review"
    VISION_CAPTION = "vision_caption"


def _text(value: object, *, field: str, required: bool = False, limit: int = 500) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field}_required")
    if len(result) > limit:
        raise ValueError(f"{field}_too_long")
    return result


def _string_tuple(value: object, *, limit: int = 64, item_limit: int = 300) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[object] = (value,)
    elif isinstance(value, Mapping):
        raise ValueError("string_list_invalid")
    elif isinstance(value, Iterable):
        values = value
    else:
        raise ValueError("string_list_invalid")
    result: list[str] = []
    for item in values:
        text = _text(item, field="string_item", limit=item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) > limit:
            raise ValueError("string_list_too_long")
    return tuple(result)


def _mapping_tuple(value: object, *, field: str, limit: int = 64) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ValueError(f"{field}_invalid")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}_invalid")
        result.append({str(key): item[key] for key in item})
        if len(result) > limit:
            raise ValueError(f"{field}_too_long")
    return tuple(result)


def _message_kind(
    value: object,
    *,
    plain_text: str,
    mention_targets: tuple[str, ...],
    attachments: tuple[dict[str, Any], ...],
) -> MessageKind:
    if value:
        try:
            return MessageKind(str(value))
        except ValueError as exc:
            raise ValueError("message_kind_invalid") from exc
    if not plain_text and mention_targets and not attachments:
        return MessageKind.MENTION_ONLY
    if attachments and not plain_text:
        return MessageKind.ATTACHMENT
    if attachments or mention_targets:
        return MessageKind.MIXED
    return MessageKind.TEXT


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    event_id: str
    channel_type: str
    channel_instance_id: str
    external_message_id: str
    external_thread_ref: str
    assistant_id: str
    actor_ref: str
    actor_role: str
    conversation_scope: str
    message_components: tuple[dict[str, Any], ...]
    plain_text: str
    mention_targets: tuple[str, ...]
    reply_to_external_message_id: str
    reply_to_assistant: bool
    timestamp: str
    attachments: tuple[dict[str, Any], ...]
    delivery_capabilities: tuple[str, ...]
    message_kind: MessageKind
    schema_version: int = CONTRACT_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ConversationEvent":
        if not isinstance(payload, Mapping):
            raise ValueError("conversation_event_invalid")
        plain_text = _text(payload.get("plain_text"), field="plain_text", limit=12000)
        mention_targets = _string_tuple(payload.get("mention_targets"), item_limit=160)
        components = _mapping_tuple(payload.get("message_components"), field="message_components")
        attachments = _mapping_tuple(payload.get("attachments"), field="attachments", limit=20)
        kind = _message_kind(
            payload.get("message_kind"),
            plain_text=plain_text,
            mention_targets=mention_targets,
            attachments=attachments,
        )
        if not plain_text and not mention_targets and not attachments and not components:
            raise ValueError("conversation_event_content_required")
        return cls(
            event_id=_text(payload.get("event_id"), field="event_id", required=True, limit=160),
            channel_type=_text(payload.get("channel_type"), field="channel_type", required=True, limit=80),
            channel_instance_id=_text(payload.get("channel_instance_id"), field="channel_instance_id", limit=160),
            external_message_id=_text(payload.get("external_message_id"), field="external_message_id", limit=300),
            external_thread_ref=_text(payload.get("external_thread_ref"), field="external_thread_ref", required=True, limit=500),
            assistant_id=_text(payload.get("assistant_id"), field="assistant_id", required=True, limit=160),
            actor_ref=_text(payload.get("actor_ref"), field="actor_ref", required=True, limit=300),
            actor_role=_text(payload.get("actor_role") or "user", field="actor_role", limit=80),
            conversation_scope=_text(payload.get("conversation_scope"), field="conversation_scope", required=True, limit=160),
            message_components=components,
            plain_text=plain_text,
            mention_targets=mention_targets,
            reply_to_external_message_id=_text(
                payload.get("reply_to_external_message_id"),
                field="reply_to_external_message_id",
                limit=300,
            ),
            reply_to_assistant=bool(payload.get("reply_to_assistant")),
            timestamp=_text(payload.get("timestamp"), field="timestamp", required=True, limit=80),
            attachments=attachments,
            delivery_capabilities=_string_tuple(payload.get("delivery_capabilities"), item_limit=80),
            message_kind=kind,
            schema_version=CONTRACT_SCHEMA_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["message_kind"] = self.message_kind.value
        result["message_components"] = [dict(item) for item in self.message_components]
        result["mention_targets"] = list(self.mention_targets)
        result["attachments"] = [dict(item) for item in self.attachments]
        result["delivery_capabilities"] = list(self.delivery_capabilities)
        return result


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    decision_id: str
    event_id: str
    candidate_kind: CandidateKind
    action: ParticipationAction
    reason: ParticipationReason
    policy_version: str
    model_role: str = ""
    model_id: str = ""
    confidence: float = 1.0
    expires_at: str = ""
    superseded_by: str = ""
    schema_version: int = CONTRACT_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ParticipationDecision":
        if not isinstance(payload, Mapping):
            raise ValueError("participation_decision_invalid")
        try:
            candidate = CandidateKind(str(payload.get("candidate_kind") or ""))
            action = ParticipationAction(str(payload.get("action") or ""))
            reason = ParticipationReason(str(payload.get("reason") or ""))
        except ValueError as exc:
            raise ValueError("participation_decision_enum_invalid") from exc
        try:
            confidence = float(payload.get("confidence", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("participation_confidence_invalid") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("participation_confidence_invalid")
        return cls(
            decision_id=_text(payload.get("decision_id"), field="decision_id", required=True, limit=160),
            event_id=_text(payload.get("event_id"), field="event_id", required=True, limit=160),
            candidate_kind=candidate,
            action=action,
            reason=reason,
            policy_version=_text(payload.get("policy_version"), field="policy_version", required=True, limit=160),
            model_role=_text(payload.get("model_role"), field="model_role", limit=80),
            model_id=_text(payload.get("model_id"), field="model_id", limit=160),
            confidence=confidence,
            expires_at=_text(payload.get("expires_at"), field="expires_at", limit=80),
            superseded_by=_text(payload.get("superseded_by"), field="superseded_by", limit=160),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["candidate_kind"] = self.candidate_kind.value
        result["action"] = self.action.value
        result["reason"] = self.reason.value
        return result


def is_explicitly_addressed(
    event: ConversationEvent,
    *,
    assistant_actor_refs: Iterable[str] = (),
) -> bool:
    """Return only deterministic address evidence; no model inference occurs."""

    if event.conversation_scope in {"private", "private_user", "owner_private"}:
        return True
    if event.reply_to_assistant:
        return True
    identities = {event.assistant_id, *[str(item).strip() for item in assistant_actor_refs]}
    return bool(identities.intersection(event.mention_targets))


def group_mode_from_legacy(policy: Mapping[str, object] | None) -> GroupParticipationMode:
    """Map legacy booleans without widening behavior during a future migration."""

    source = policy or {}
    explicit_mode = str(source.get("participation_mode") or "").strip()
    if explicit_mode:
        try:
            return GroupParticipationMode(explicit_mode)
        except ValueError as exc:
            raise ValueError("group_participation_mode_invalid") from exc
    if not bool(int(source.get("enabled") or 0)):
        return GroupParticipationMode.DISABLED
    if bool(int(source.get("mention_only") or 0)):
        return GroupParticipationMode.MENTIONS_ONLY
    if not bool(int(source.get("active_reply") or 0)):
        return GroupParticipationMode.MENTIONS_ONLY
    return GroupParticipationMode.NATURAL_PARTICIPATION


def legacy_group_flags_for_mode(mode: GroupParticipationMode | str) -> dict[str, int]:
    """Project one authoritative mode onto the legacy runtime flags."""

    try:
        normalized = mode if isinstance(mode, GroupParticipationMode) else GroupParticipationMode(str(mode))
    except ValueError as exc:
        raise ValueError("group_participation_mode_invalid") from exc
    return {
        GroupParticipationMode.DISABLED: {"enabled": 0, "mention_only": 1, "active_reply": 0},
        GroupParticipationMode.MENTIONS_ONLY: {"enabled": 1, "mention_only": 1, "active_reply": 0},
        GroupParticipationMode.DIRECTED_CONTEXT: {"enabled": 1, "mention_only": 0, "active_reply": 0},
        GroupParticipationMode.NATURAL_PARTICIPATION: {"enabled": 1, "mention_only": 0, "active_reply": 1},
    }[normalized]


def participation_contract_snapshot() -> dict[str, object]:
    """Return a stable, serializable snapshot for docs and migration tests."""

    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "candidate_kinds": [item.value for item in CandidateKind],
        "actions": [item.value for item in ParticipationAction],
        "reason_codes": [item.value for item in ParticipationReason],
        "group_modes": [item.value for item in GroupParticipationMode],
        "retention_classes": [item.value for item in RetentionClass],
        "message_kinds": [item.value for item in MessageKind],
        "model_scenario_roles": [item.value for item in ModelScenarioRole],
    }


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "CandidateKind",
    "ConversationEvent",
    "GroupParticipationMode",
    "MessageKind",
    "ModelScenarioRole",
    "ParticipationAction",
    "ParticipationDecision",
    "ParticipationReason",
    "RetentionClass",
    "group_mode_from_legacy",
    "is_explicitly_addressed",
    "participation_contract_snapshot",
]
