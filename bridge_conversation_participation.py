#!/usr/bin/env python3
"""AC-1 shadow persistence for normalized conversation participation facts.

This module observes the legacy dispatch result.  It does not choose a reply,
invoke a model, or send a channel message.  Raw inbound text is deliberately
excluded from the event and decision audit tables.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_conversation_participation_contract import (
    CandidateKind,
    ConversationEvent,
    ParticipationAction,
    ParticipationDecision,
    ParticipationReason,
    is_explicitly_addressed,
    RetentionClass,
)
from bridge_conversation_participation_schema import (
    PARTICIPATION_MIGRATION_CHECKSUM,
    PARTICIPATION_SHADOW_FEATURE_FLAG,
    require_conversation_participation_schema,
)
from bridge_migrations import utc_now


SHADOW_POLICY_VERSION = "ac1-shadow-v1"
TRANSIENT_RETENTION_MINUTES = 30


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def participation_shadow_enabled(conn: sqlite3.Connection) -> bool:
    try:
        cursor = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_feature_flags'",
        )
        table = cursor.fetchone()
    except (sqlite3.Error, AttributeError):
        # Compatibility fakes used by narrow dispatch tests intentionally do
        # not implement SQLite cursors.  They represent the pre-AC-1 path.
        return False
    if not table:
        return False
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (PARTICIPATION_SHADOW_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def participation_cutover_plan(conn: sqlite3.Connection) -> dict:
    schema = require_conversation_participation_schema(conn)
    payload = {
        "feature": PARTICIPATION_SHADOW_FEATURE_FLAG,
        "feature_enabled": participation_shadow_enabled(conn),
        "migration_checksum": PARTICIPATION_MIGRATION_CHECKSUM,
        "schema_ok": bool(schema["ok"]),
        "policy_version": SHADOW_POLICY_VERSION,
        "reversible": True,
    }
    return {
        "ok": bool(schema["ok"]),
        **payload,
        "plan_checksum": _sha256(_canonical(payload)),
    }


def set_participation_shadow_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    plan = participation_cutover_plan(conn)
    if expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_participation_cutover_plan")
    if enabled and not plan["ok"]:
        raise ValueError("participation_schema_not_ready")
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE
        SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (PARTICIPATION_SHADOW_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return participation_cutover_plan(conn)


def stable_event_id(
    *,
    channel_type: str,
    channel_instance_id: str,
    external_message_id: str,
    external_thread_ref: str,
    actor_ref: str,
    timestamp: str,
    plain_text: str,
) -> str:
    if str(external_message_id or "").strip():
        source = [channel_type, channel_instance_id, external_message_id]
    else:
        source = [
            channel_type,
            channel_instance_id,
            external_thread_ref,
            actor_ref,
            timestamp,
            _sha256(plain_text),
        ]
    return "evt_" + _sha256(_canonical(source))[:32]


def stable_decision_id(event_id: str, *, policy_version: str = SHADOW_POLICY_VERSION) -> str:
    return "dec_" + _sha256(f"{event_id}|{policy_version}")[:32]


def build_event(payload: Mapping[str, object]) -> ConversationEvent:
    normalized = dict(payload)
    normalized.setdefault("timestamp", utc_now())
    normalized.setdefault("channel_instance_id", "")
    normalized.setdefault("external_message_id", "")
    normalized.setdefault("reply_to_external_message_id", "")
    normalized.setdefault("reply_to_assistant", False)
    normalized.setdefault("message_components", [])
    normalized.setdefault("mention_targets", [])
    normalized.setdefault("attachments", [])
    normalized.setdefault("delivery_capabilities", [])
    if not str(normalized.get("event_id") or "").strip():
        normalized["event_id"] = stable_event_id(
            channel_type=str(normalized.get("channel_type") or ""),
            channel_instance_id=str(normalized.get("channel_instance_id") or ""),
            external_message_id=str(normalized.get("external_message_id") or ""),
            external_thread_ref=str(normalized.get("external_thread_ref") or ""),
            actor_ref=str(normalized.get("actor_ref") or ""),
            timestamp=str(normalized.get("timestamp") or ""),
            plain_text=str(normalized.get("plain_text") or ""),
        )
    return ConversationEvent.from_mapping(normalized)


def _event_storage(event: ConversationEvent) -> tuple[str, dict]:
    text_hash = _sha256(event.plain_text) if event.plain_text else ""
    metadata = {
        "channel_type": event.channel_type,
        "channel_instance_id": event.channel_instance_id,
        "external_message_id": event.external_message_id,
        "external_thread_ref": event.external_thread_ref,
        "assistant_id": event.assistant_id,
        "actor_ref": event.actor_ref,
        "actor_role": event.actor_role,
        "conversation_scope": event.conversation_scope,
        "message_kind": event.message_kind.value,
        "text_sha256": text_hash,
        "text_length": len(event.plain_text),
        "mention_targets": list(event.mention_targets),
        "reply_to_external_message_id": event.reply_to_external_message_id,
        "reply_to_assistant": bool(event.reply_to_assistant),
        "component_kinds": sorted({str(item.get("type") or "unknown") for item in event.message_components}),
        "attachment_count": len(event.attachments),
        "delivery_capabilities": list(event.delivery_capabilities),
    }
    return _sha256(_canonical(metadata)), metadata


def record_conversation_event(conn: sqlite3.Connection, event: ConversationEvent) -> dict:
    require_conversation_participation_schema(conn)
    fingerprint, metadata = _event_storage(event)
    row = conn.execute(
        "SELECT event_fingerprint FROM conversation_events WHERE id=?",
        (event.event_id,),
    ).fetchone()
    if row:
        if str(row[0]) != fingerprint:
            raise ValueError("conversation_event_idempotency_conflict")
        return {"created": False, "event_id": event.event_id, **metadata}
    try:
        conn.execute(
            """
            INSERT INTO conversation_events(
                id,channel_type,channel_instance_id,external_message_id,
                external_thread_ref,assistant_id,actor_ref,actor_role,
                conversation_scope,message_kind,text_sha256,text_length,
                mention_targets_json,reply_to_external_message_id,
                reply_to_assistant,component_kinds_json,attachment_count,
                delivery_capabilities_json,event_fingerprint,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.event_id,
                event.channel_type,
                event.channel_instance_id,
                event.external_message_id,
                event.external_thread_ref,
                event.assistant_id,
                event.actor_ref,
                event.actor_role,
                event.conversation_scope,
                event.message_kind.value,
                metadata["text_sha256"],
                metadata["text_length"],
                _canonical(metadata["mention_targets"]),
                event.reply_to_external_message_id,
                1 if event.reply_to_assistant else 0,
                _canonical(metadata["component_kinds"]),
                metadata["attachment_count"],
                _canonical(metadata["delivery_capabilities"]),
                fingerprint,
                event.timestamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        duplicate = conn.execute(
            """
            SELECT id,event_fingerprint FROM conversation_events
            WHERE channel_type=? AND channel_instance_id=? AND external_message_id=?
            """,
            (event.channel_type, event.channel_instance_id, event.external_message_id),
        ).fetchone()
        if duplicate and str(duplicate[1]) == fingerprint:
            return {"created": False, "event_id": str(duplicate[0]), **metadata}
        raise ValueError("conversation_event_external_id_conflict") from exc
    return {"created": True, "event_id": event.event_id, **metadata}


def decision_from_legacy(
    event: ConversationEvent,
    *,
    allowed: bool,
    legacy_reason: str,
    candidate_kind: CandidateKind | None = None,
    model_role: str = "",
    model_id: str = "",
    confidence: float = 1.0,
) -> ParticipationDecision:
    directed_to_assistant = is_explicitly_addressed(event)
    reason_text = str(legacy_reason or "").strip().lower()
    reason_map = {
        "group_disabled": ParticipationReason.GROUP_DISABLED,
        "group_not_allowed": ParticipationReason.GROUP_ACCESS_DENIED,
        "access_denied": ParticipationReason.GROUP_ACCESS_DENIED,
        "mention_required": ParticipationReason.MENTION_REQUIRED,
        "active_reply_disabled": ParticipationReason.PARTICIPATION_DISABLED,
        "quiet_hours": ParticipationReason.QUIET_HOURS,
        "cooldown": ParticipationReason.COOLDOWN,
        "model_unavailable": ParticipationReason.MODEL_UNAVAILABLE,
        "classifier_failed": ParticipationReason.CLASSIFIER_FAILED,
        "group_classifier_failed": ParticipationReason.CLASSIFIER_FAILED,
        "low_score": ParticipationReason.ENGAGEMENT_BELOW_THRESHOLD,
        "participation_threshold": ParticipationReason.ENGAGEMENT_BELOW_THRESHOLD,
        "engagement_below_threshold": ParticipationReason.ENGAGEMENT_BELOW_THRESHOLD,
        "natural_deferred": ParticipationReason.NATURAL_DEFERRED,
        "candidate_superseded": ParticipationReason.CANDIDATE_SUPERSEDED,
    }
    reason = reason_map.get(reason_text)
    if reason is None and not allowed:
        if "model" in reason_text or "provider" in reason_text or "llm" in reason_text:
            reason = ParticipationReason.MODEL_UNAVAILABLE
        elif "classifier" in reason_text:
            reason = ParticipationReason.CLASSIFIER_FAILED
    if reason is None:
        if event.conversation_scope == "private":
            reason = ParticipationReason.DIRECT_PRIVATE
        elif event.reply_to_assistant:
            reason = ParticipationReason.REPLY_TO_ASSISTANT
        elif directed_to_assistant:
            reason = ParticipationReason.EXPLICIT_MENTION
        else:
            reason = (
                ParticipationReason.ENGAGEMENT_BELOW_THRESHOLD
                if not allowed else ParticipationReason.MODEL_ENGAGEMENT_APPROVED
            )
    if allowed:
        action = (
            ParticipationAction.DIRECT_REPLY
            if event.conversation_scope == "private" or directed_to_assistant
            or event.reply_to_assistant
            else ParticipationAction.CONTEXTUAL_PARTICIPATION
        )
    else:
        action = ParticipationAction.SILENT
    if candidate_kind is None:
        candidate_kind = (
            CandidateKind.INBOUND_DIRECT
            if event.conversation_scope == "private" or directed_to_assistant
            or event.reply_to_assistant
            else CandidateKind.AMBIENT_GROUP
        )
    return ParticipationDecision(
        decision_id=stable_decision_id(event.event_id),
        event_id=event.event_id,
        candidate_kind=candidate_kind,
        action=action,
        reason=reason,
        policy_version=SHADOW_POLICY_VERSION,
        model_role=model_role,
        model_id=model_id,
        confidence=confidence,
    )


def retention_for_decision(
    event: ConversationEvent,
    decision: ParticipationDecision,
    *,
    now: datetime | None = None,
) -> tuple[RetentionClass, str]:
    if event.conversation_scope == "private":
        return RetentionClass.CONVERSATION, ""
    if decision.reason in {
        ParticipationReason.GROUP_DISABLED,
        ParticipationReason.GROUP_ACCESS_DENIED,
        ParticipationReason.MENTION_REQUIRED,
        ParticipationReason.PARTICIPATION_DISABLED,
    }:
        return RetentionClass.METADATA_ONLY, ""
    if decision.action in {
        ParticipationAction.DIRECT_REPLY,
        ParticipationAction.CONTINUATION_REPLY,
    }:
        return RetentionClass.CONVERSATION, ""
    basis = now
    if basis is None:
        try:
            basis = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        except ValueError:
            basis = datetime.now(timezone.utc)
    if basis.tzinfo is None:
        basis = basis.replace(tzinfo=timezone.utc)
    expires = basis.astimezone(timezone.utc) + timedelta(minutes=TRANSIENT_RETENTION_MINUTES)
    return RetentionClass.TRANSIENT, expires.isoformat().replace("+00:00", "Z")


def record_participation_decision(
    conn: sqlite3.Connection,
    decision: ParticipationDecision,
    *,
    assistant_id: str,
    thread_id: str = "",
    source_message_id: str = "",
    legacy_allowed: bool,
    legacy_reason: str,
    shadow_match: bool = True,
    retention_class: RetentionClass | None = None,
    conversation_frame: dict | None = None,
    interaction_decision: dict | None = None,
) -> dict:
    require_conversation_participation_schema(conn)
    payload = decision.to_dict()
    payload["retention_class"] = retention_class.value if retention_class else ""
    if conversation_frame:
        from bridge_group_context_frame import audit_group_conversation_frame

        payload["group_conversation_frame"] = audit_group_conversation_frame(conversation_frame)
    if interaction_decision:
        plan = interaction_decision.get("interaction_plan")
        plan = plan if isinstance(plan, dict) else {}
        plan_record = interaction_decision.get("interaction_plan_record")
        plan_record = plan_record if isinstance(plan_record, dict) else {}
        payload["interaction_decision"] = {
            "mode": str(interaction_decision.get("mode") or ""),
            "intent": str(interaction_decision.get("intent") or ""),
            "participation_action": str(interaction_decision.get("participation_action") or ""),
            "interaction_plan_id": str(plan_record.get("id") or ""),
            "action_types": [
                str(item.get("type") or "")
                for item in plan.get("actions") or []
                if isinstance(item, dict) and item.get("type")
            ][:12],
        }
    # v24 enriches the existing authoritative engagement decision instead of
    # creating a parallel inbound decision store. The feature flag keeps the
    # cutover reversible and leaves the legacy canonical payload untouched.
    from bridge_social_opportunity import enrich_participation_payload

    payload = enrich_participation_payload(
        conn,
        payload,
        assistant_id=assistant_id,
        thread_id=thread_id,
        source_message_id=source_message_id,
        conversation_frame=conversation_frame,
    )
    canonical = _canonical(payload)
    row = conn.execute(
        "SELECT decision_json FROM engagement_decisions WHERE id=?",
        (decision.decision_id,),
    ).fetchone()
    if row:
        if str(row[0]) != canonical:
            raise ValueError("participation_decision_idempotency_conflict")
        return {"created": False, **payload}
    conn.execute(
        """
        INSERT INTO engagement_decisions(
            id,event_id,assistant_id,thread_id,source_message_id,candidate_kind,
            action,reason_code,policy_version,legacy_allowed,legacy_reason,
            shadow_match,model_role,model_id,confidence,decision_json,
            expires_at,superseded_by,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            decision.decision_id,
            decision.event_id,
            assistant_id,
            thread_id,
            source_message_id,
            decision.candidate_kind.value,
            decision.action.value,
            decision.reason.value,
            decision.policy_version,
            1 if legacy_allowed else 0,
            str(legacy_reason or ""),
            1 if shadow_match else 0,
            decision.model_role,
            decision.model_id,
            decision.confidence,
            canonical,
            decision.expires_at,
            decision.superseded_by,
            utc_now(),
        ),
    )
    return {"created": True, **payload}


_PARTICIPATION_LIFECYCLE_STAGES = {
    "deferred", "superseded", "preflight_blocked", "model_declined",
    "delivery_queued", "delivery_failed", "ack_confirmed",
}


def transition_participation_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    stage: str,
    action: str | None = None,
    reason_code: str | None = None,
    model_role: str | None = None,
    model_id: str | None = None,
    confidence: float | None = None,
    superseded_by: str = "",
) -> dict | None:
    """Advance one existing decision without creating a parallel fact store."""

    normalized_stage = str(stage or "").strip()
    if normalized_stage not in _PARTICIPATION_LIFECYCLE_STAGES:
        raise ValueError("participation_lifecycle_stage_invalid")
    normalized_id = str(decision_id or "").strip()
    if not normalized_id:
        return None
    row = conn.execute(
        """SELECT action,reason_code,model_role,model_id,confidence,decision_json,
                  superseded_by FROM engagement_decisions WHERE id=?""",
        (normalized_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[5] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    lifecycle = payload.get("participation_lifecycle")
    lifecycle = dict(lifecycle) if isinstance(lifecycle, dict) else {}
    history = list(lifecycle.get("history") or [])
    current_reason = str(reason_code or row[1] or "")[:120]
    current_action = str(action or row[0] or "silent")[:80]
    current_model_role = str(model_role if model_role is not None else row[2] or "")[:80]
    current_model_id = str(model_id if model_id is not None else row[3] or "")[:160]
    current_confidence = max(0.0, min(float(row[4] if confidence is None else confidence), 1.0))
    event = {"stage": normalized_stage, "reason_code": current_reason, "action": current_action, "at": utc_now()}
    if current_model_role:
        event["model_role"] = current_model_role
    if current_model_id:
        event["model_id"] = current_model_id
    if confidence is not None:
        event["confidence"] = current_confidence
    history.append(event)
    lifecycle.update({"schema_version": 1, "stage": normalized_stage, "history": history[-8:]})
    payload["participation_lifecycle"] = lifecycle
    conn.execute(
        """UPDATE engagement_decisions
           SET action=?,reason_code=?,model_role=?,model_id=?,confidence=?,
               decision_json=?,superseded_by=? WHERE id=?""",
        (
            current_action, current_reason, current_model_role, current_model_id,
            current_confidence, _canonical(payload),
            str(superseded_by or row[6] or "")[:160], normalized_id,
        ),
    )
    return {"decision_id": normalized_id, **lifecycle}


def observe_legacy_decision(
    conn: sqlite3.Connection,
    event: ConversationEvent,
    *,
    allowed: bool,
    legacy_reason: str,
    thread_id: str = "",
    source_message_id: str = "",
    model_role: str = "",
    model_id: str = "",
) -> dict:
    """Persist one shadow observation; callers decide whether the flag is enabled."""

    event_result = record_conversation_event(conn, event)
    decision = decision_from_legacy(
        event,
        allowed=allowed,
        legacy_reason=legacy_reason,
        model_role=model_role,
        model_id=model_id,
    )
    retention, expires_at = retention_for_decision(event, decision)
    if expires_at:
        decision = ParticipationDecision(
            decision_id=decision.decision_id,
            event_id=decision.event_id,
            candidate_kind=decision.candidate_kind,
            action=decision.action,
            reason=decision.reason,
            policy_version=decision.policy_version,
            model_role=decision.model_role,
            model_id=decision.model_id,
            confidence=decision.confidence,
            expires_at=expires_at,
        )
    decision_result = record_participation_decision(
        conn,
        decision,
        assistant_id=event.assistant_id,
        thread_id=thread_id,
        source_message_id=source_message_id,
        legacy_allowed=allowed,
        legacy_reason=legacy_reason,
        retention_class=retention,
    )
    return {
        "event": event_result,
        "decision": decision_result,
        "retention_class": retention.value,
        "expires_at": expires_at,
    }


__all__ = [
    "SHADOW_POLICY_VERSION",
    "TRANSIENT_RETENTION_MINUTES",
    "build_event",
    "decision_from_legacy",
    "observe_legacy_decision",
    "participation_cutover_plan",
    "participation_shadow_enabled",
    "record_conversation_event",
    "record_participation_decision",
    "retention_for_decision",
    "set_participation_shadow_feature",
    "stable_decision_id",
    "stable_event_id",
    "transition_participation_decision",
]
