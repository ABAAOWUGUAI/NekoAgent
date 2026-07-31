#!/usr/bin/env python3
"""Deterministic AC-2 participation decisions shared by every channel."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from bridge_conversation_participation import stable_decision_id
from bridge_conversation_participation_contract import (
    CandidateKind,
    ConversationEvent,
    GroupParticipationMode,
    MessageKind,
    ParticipationAction,
    ParticipationDecision,
    ParticipationReason,
    group_mode_from_legacy,
    is_explicitly_addressed,
)
from bridge_conversation_participation_routing_schema import (
    DETERMINISTIC_PARTICIPATION_FEATURE_FLAG,
    PARTICIPATION_ROUTING_MIGRATION_CHECKSUM,
    require_conversation_participation_routing_schema,
)
from bridge_conversation_participation import participation_shadow_enabled
from bridge_migrations import utc_now


DETERMINISTIC_POLICY_VERSION = "ac2-deterministic-v1"


def deterministic_participation_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (DETERMINISTIC_PARTICIPATION_FEATURE_FLAG,),
        ).fetchone()
    except (sqlite3.Error, AttributeError):
        return False
    return bool(row and int(row[0]))


def deterministic_cutover_plan(conn: sqlite3.Connection) -> dict:
    schema = require_conversation_participation_routing_schema(conn)
    payload = {
        "feature": DETERMINISTIC_PARTICIPATION_FEATURE_FLAG,
        "feature_enabled": deterministic_participation_enabled(conn),
        "shadow_enabled": participation_shadow_enabled(conn),
        "migration_checksum": PARTICIPATION_ROUTING_MIGRATION_CHECKSUM,
        "schema_ok": bool(schema["ok"]),
        "policy_version": DETERMINISTIC_POLICY_VERSION,
        "reversible": True,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        "ok": bool(schema["ok"]),
        **payload,
        "plan_checksum": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def set_deterministic_participation_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    plan = deterministic_cutover_plan(conn)
    if expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_deterministic_participation_plan")
    if enabled and not plan["shadow_enabled"]:
        raise ValueError("participation_shadow_required")
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE
        SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (DETERMINISTIC_PARTICIPATION_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return deterministic_cutover_plan(conn)


def _utc(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def participation_state(conn: sqlite3.Connection, event: ConversationEvent) -> dict | None:
    try:
        row = conn.execute(
            """
            SELECT * FROM conversation_participation_state
            WHERE thread_ref=? AND assistant_id=?
            """,
            (event.external_thread_ref, event.assistant_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def _decision(
    event: ConversationEvent,
    *,
    candidate: CandidateKind,
    action: ParticipationAction,
    reason: ParticipationReason,
) -> ParticipationDecision:
    return ParticipationDecision(
        decision_id=stable_decision_id(
            event.event_id,
            policy_version=DETERMINISTIC_POLICY_VERSION,
        ),
        event_id=event.event_id,
        candidate_kind=candidate,
        action=action,
        reason=reason,
        policy_version=DETERMINISTIC_POLICY_VERSION,
        confidence=1.0,
    )


def deterministic_inbound_decision(
    event: ConversationEvent,
    *,
    group_policy: dict | None = None,
    state: dict | None = None,
    assistant_actor_refs: tuple[str, ...] = (),
    is_control_action: bool = False,
    conversation_frame: dict | None = None,
) -> ParticipationDecision | None:
    """Decide only rules that must never depend on an LLM.

    ``None`` means the event is ambient and may be evaluated by the dedicated
    engagement role later.  It never means a direct message may be ignored.
    """

    if is_control_action:
        return _decision(
            event,
            candidate=CandidateKind.CONTROL_COMMAND,
            action=ParticipationAction.DETERMINISTIC_CONTROL_ACTION,
            reason=ParticipationReason.ADMIN_COMMAND,
        )
    if event.conversation_scope in {"private", "private_user", "owner_private"}:
        return _decision(
            event,
            candidate=CandidateKind.INBOUND_DIRECT,
            action=ParticipationAction.DIRECT_REPLY,
            reason=ParticipationReason.DIRECT_PRIVATE,
        )

    mode = group_mode_from_legacy(group_policy)
    if mode is GroupParticipationMode.DISABLED:
        return _decision(
            event,
            candidate=CandidateKind.AMBIENT_GROUP,
            action=ParticipationAction.SILENT,
            reason=ParticipationReason.GROUP_DISABLED,
        )

    if event.reply_to_assistant:
        return _decision(
            event,
            candidate=CandidateKind.INBOUND_DIRECT,
            action=ParticipationAction.DIRECT_REPLY,
            reason=ParticipationReason.REPLY_TO_ASSISTANT,
        )
    if is_explicitly_addressed(event, assistant_actor_refs=assistant_actor_refs):
        return _decision(
            event,
            candidate=CandidateKind.INBOUND_DIRECT,
            action=ParticipationAction.DIRECT_REPLY,
            reason=ParticipationReason.EXPLICIT_MENTION,
        )

    current = state or {}
    waiting_until = _utc(current.get("waiting_until"))
    waiting_actor = str(current.get("waiting_for_actor_ref") or "").strip()
    waiting = (
        str(current.get("phase") or "") == "waiting_for_actor"
        and (not waiting_actor or waiting_actor == event.actor_ref)
        and (waiting_until is None or waiting_until >= datetime.now(timezone.utc))
    )
    if waiting and event.conversation_scope not in {"qq_group", "group"}:
        return _decision(
            event,
            candidate=CandidateKind.CONTINUATION,
            action=ParticipationAction.CONTINUATION_REPLY,
            reason=ParticipationReason.WAITING_FOR_ACTOR,
        )

    frame = conversation_frame or {}
    # A capability notice is not an invitation for the whole group to keep
    # talking to the assistant.  Until a member explicitly @s or replies to
    # it, side comments after a failed media turn stay with the group.
    if frame.get("media_gate_active"):
        return _decision(
            event,
            candidate=CandidateKind.AMBIENT_GROUP,
            action=ParticipationAction.SILENT,
            reason=ParticipationReason.MEDIA_GATE_FOLLOWUP_UNADDRESSED,
        )
    # Passive images and stickers without a Bridge-derived visual description
    # are ordinary group activity.  A ready description falls through to the
    # normal engagement role, which can still choose silence rather than
    # treating the mere presence of an image as an invitation to speak.
    visual_ready = any(
        bool(item.get("visual_context_ready"))
        for item in event.attachments
        if isinstance(item, dict)
    )
    if event.attachments and not visual_ready:
        return _decision(
            event,
            candidate=CandidateKind.AMBIENT_GROUP,
            action=ParticipationAction.SILENT,
            reason=ParticipationReason.MEDIA_AMBIENT_UNADDRESSED,
        )
    if frame.get("active_exchange") and frame.get("acknowledgement_only"):
        return _decision(
            event,
            candidate=CandidateKind.CONTINUATION,
            action=ParticipationAction.SILENT,
            reason=ParticipationReason.CONTINUATION_ACKNOWLEDGEMENT,
        )
    # A same-member follow-up can be socially meaningful, but it is not a
    # deterministic reply obligation.  In modes that permit natural context,
    # let the guarded engagement path choose a concrete action (or silence)
    # after freshness, density, budget and value checks.  A strict
    # mentions-only group keeps its explicit no-automatic-reply contract.
    if (
        frame.get("active_continuation")
        and mode in {
            GroupParticipationMode.DIRECTED_CONTEXT,
            GroupParticipationMode.NATURAL_PARTICIPATION,
        }
    ):
        return None

    if mode is GroupParticipationMode.MENTIONS_ONLY:
        return _decision(
            event,
            candidate=CandidateKind.AMBIENT_GROUP,
            action=ParticipationAction.SILENT,
            reason=ParticipationReason.MENTION_REQUIRED,
        )
    # ``directed_context`` deliberately falls through to the engagement role:
    # deterministic routing has already handled @, reply and waiting states,
    # while the model may assess a clearly addressed natural-language turn.
    return None


def deterministic_acknowledgement(
    event: ConversationEvent,
    *,
    assistant_name: str = "助手",
    conversation_frame: dict | None = None,
) -> str:
    if event.message_kind is not MessageKind.MENTION_ONLY:
        return ""
    # A bare @ is an attention signal.  When the group has usable topic
    # context, let the normal Interaction Plan and reply model interpret it;
    # only context-free attention receives a deterministic acknowledgement.
    if (conversation_frame or {}).get("topic_active"):
        return ""
    name = str(assistant_name or "助手").strip()
    return f"{name}在。直接告诉我想聊什么，或要我做什么就可以。"


__all__ = [
    "DETERMINISTIC_POLICY_VERSION",
    "deterministic_acknowledgement",
    "deterministic_inbound_decision",
    "deterministic_cutover_plan",
    "deterministic_participation_enabled",
    "participation_state",
    "set_deterministic_participation_feature",
]
