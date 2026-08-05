#!/usr/bin/env python3
"""QQ-to-participation shadow adapter kept outside the legacy Bridge monolith."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable

import bridge_assistant_identity as assistant_identity
from bridge_media_observation import select_media_observation
from bridge_media_observation_helpers import (
    has_visual_attachment as _has_visual_attachment,
    media_budget_snapshot as _media_budget_snapshot,
    media_burst_limit as _media_burst_limit,
)
from bridge_participation_shadow_records import (
    record_group_shadow_decision,
    transition_group_participation,
)
from bridge_conversation_participation import (
    build_media_delivery_trace,
    media_trace_categories,
    build_event,
    decision_from_legacy,
    participation_shadow_enabled,
    record_conversation_event,
    record_participation_decision,
    retention_for_decision,
)
from bridge_conversation_participation_contract import GroupParticipationMode, ParticipationAction, group_mode_from_legacy
from bridge_conversation_participation_engine import (
    deterministic_inbound_decision,
    deterministic_participation_enabled,
    participation_state,
)
from bridge_group_participation_policy import (
    group_active_topic_window_seconds,
    group_final_action_gate,
    natural_group_participation_enabled,
    natural_group_preflight,
    observe_group_message,
    project_media_observation_policy,
    record_group_reply,
)
from bridge_migrations import utc_now
from bridge_group_participation_queue import enqueue_group_candidate
from bridge_group_context_frame import (
    DEFAULT_GROUP_CONTEXT_LIMIT,
    build_group_conversation_frame,
)
from bridge_social_reply import group_reply_style_issues
from bridge_group_message_store import group_context, record_group_message
from bridge_social_engine import (
    get_group_policy,
    group_hard_gate,
    mark_group_decision,
    upsert_group_policy,
)


def _assistant_id(conn: sqlite3.Connection) -> str:
    try:
        current = assistant_identity.current_assistant(conn)
    except (sqlite3.Error, ValueError):
        current = None
    return str((current or {}).get("id") or "assistant-default")


def qq_participation_event(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    scope: str,
    actor_id: str,
    thread_ref: str,
    plain_text: str,
    is_mention: bool = False,
):
    assistant_id = _assistant_id(conn)
    components = list(payload.get("message_components") or []) if isinstance(payload.get("message_components"), list) else []
    attachments = list(payload.get("attachments") or []) if isinstance(payload.get("attachments"), list) else []
    mentions = list(payload.get("mention_targets") or []) if isinstance(payload.get("mention_targets"), list) else []
    if is_mention and assistant_id not in mentions:
        mentions.append(assistant_id)
    if not components:
        if str(plain_text or "").strip():
            components.append({"type": "text"})
        if is_mention:
            components.append({"type": "mention"})
    return build_event(
        {
            "channel_type": "qq",
            "channel_instance_id": str(payload.get("_qq_self_id") or "default"),
            "external_message_id": str(payload.get("_external_message_id") or ""),
            "external_thread_ref": thread_ref,
            "assistant_id": assistant_id,
            "actor_ref": f"qq:{actor_id}",
            "actor_role": str(payload.get("_qq_actor_role") or "user"),
            "conversation_scope": scope,
            "message_components": components,
            "plain_text": str(plain_text or "").strip(),
            "mention_targets": mentions,
            "reply_to_external_message_id": str(payload.get("reply_to_external_message_id") or ""),
            "reply_to_assistant": bool(payload.get("reply_to_assistant")),
            "timestamp": str(payload.get("_event_timestamp") or utc_now()),
            "attachments": attachments,
            "delivery_capabilities": ["text", "image"],
        },
    )


def prepare_group_shadow(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    group_id: str,
    sender_id: str,
    plain_text: str,
    is_mention: bool,
    allowed: bool,
    reason: str,
    policy: dict | None = None,
    topic_active: bool = False,
) -> tuple[object | None, object | None, dict]:
    if not participation_shadow_enabled(conn) and not deterministic_participation_enabled(conn):
        return None, None, {}
    event = qq_participation_event(
        conn,
        payload,
        scope="qq_group",
        actor_id=sender_id,
        thread_ref=f"qq:group:{group_id}",
        plain_text=plain_text,
        is_mention=is_mention,
    )
    record_conversation_event(conn, event)
    preliminary = decision_from_legacy(event, allowed=allowed, legacy_reason=reason)
    retention, expires_at = retention_for_decision(event, preliminary)
    metadata = {
        "event_id": event.event_id,
        "message_kind": event.message_kind.value,
    }
    if _has_visual_attachment(event.attachments):
        media_policy = project_media_observation_policy(policy)
        burst_count, daily_remaining = _media_budget_snapshot(conn, group_id, media_policy)
        media_decision = select_media_observation(
            event_id=event.event_id,
            participation_mode=str(media_policy.get("participation_mode") or ""),
            addressed=bool(is_mention or payload.get("reply_to_assistant") or payload.get("visual_question") or payload.get("media_question")),
            topic_active=bool(topic_active or payload.get("topic_active") or payload.get("_topic_active")),
            probability=media_policy.get("media_observation_probability", 0.0),
            burst_count=burst_count,
            daily_remaining=daily_remaining,
            burst_limit=_media_burst_limit(media_policy),
        )
        metadata["media_observation"] = media_decision
    return event, retention, {
        "external_message_id": payload.get("_external_message_id") or "",
        "retention_class": retention.value,
        "expires_at": expires_at,
        "engagement_decision_id": preliminary.decision_id,
        "metadata": metadata,
    }


def record_group_inbound(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    policy: dict,
    group_id: str,
    sender_id: str,
    sender_name: str,
    session: str,
    plain_text: str,
    is_mention: bool,
    allowed: bool,
    reason: str,
) -> tuple[object | None, dict, list[dict]]:
    event, _, shadow_payload = prepare_group_shadow(
        conn,
        payload,
        group_id=group_id,
        sender_id=sender_id,
        plain_text=str(payload.get("message") or ""),
        is_mention=is_mention,
        allowed=allowed,
        reason=reason,
        policy=policy,
        topic_active=bool(payload.get("topic_active") or payload.get("_topic_active")),
    )
    current = record_group_message(
        conn,
        {
            "group_id": group_id,
            "group_name": payload.get("group_name") or policy.get("group_name") or "",
            "session": session,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message": plain_text,
            "is_mention": is_mention,
            **shadow_payload,
        },
    )
    context = group_context(
        conn, group_id, int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
    )
    # The current inbound message is required for the immediate decision
    # pipeline even when its transient retention window has already elapsed
    # because the external event timestamp was delayed or skewed.  Keep it in
    # this in-memory context only; the retention filter still governs all
    # subsequent reads from the durable group message store.
    current_id = int(current.get("id") or 0)
    if current_id and not any(int(item.get("id") or 0) == current_id for item in context):
        context.append(current)
    return event, current, context


def finalize_group_shadow(
    conn: sqlite3.Connection,
    event,
    allowed: bool,
    reason: str,
    group_id: str,
    payload: dict,
    classifier_settings: dict | None = None,
    decision_override=None,
    conversation_frame: dict | None = None,
    interaction_decision: dict | None = None,
) -> None:
    if event is None:
        return
    settings = classifier_settings or {}
    if decision_override is None:
        existing = conn.execute(
            "SELECT id FROM engagement_decisions WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
            (str(event.event_id or ""),),
        ).fetchone()
        if existing:
            transition_group_participation(
                conn,
                decision_id=str(existing[0]),
                stage="delivery_queued" if allowed else "model_declined",
                action="contextual_participation" if allowed else "silent",
                reason_code=str(reason or "model_engagement_declined"),
                model_role="conversation_engagement" if settings else None,
                model_id=str(settings.get("chat_model") or "") if settings else None,
            )
            return
        record_group_shadow_decision(
            conn,
            event,
            allowed=allowed,
            reason=reason,
            group_id=group_id,
            source_message_id=str(payload.get("_external_message_id") or ""),
            model_role="conversation_engagement" if settings else "",
            model_id=str(settings.get("chat_model") or ""),
            conversation_frame=conversation_frame,
            interaction_decision=interaction_decision,
        )
        return
    retention, _ = retention_for_decision(event, decision_override)
    record_participation_decision(
        conn,
        decision_override,
        assistant_id=event.assistant_id,
        thread_id=f"qq:group:{group_id}",
        source_message_id=str(payload.get("_external_message_id") or ""),
        legacy_allowed=allowed,
        legacy_reason=reason,
        retention_class=retention,
        conversation_frame=conversation_frame,
        interaction_decision=interaction_decision,
    )


def prepare_group_dispatch(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    group_id: str,
    sender_id: str,
    sender_name: str,
    session: str,
    message: str,
    is_mention: bool,
) -> dict:
    policy = get_group_policy(conn, group_id)
    if not policy:
        policy = upsert_group_policy(
            conn,
            {
                "group_id": group_id,
                "group_name": payload.get("group_name") or "",
                "session": session,
                "enabled": "0",
                "mention_only": "1",
            },
        )
    deterministic_decision = None
    context_before = group_context(
        conn, group_id, int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
    )
    conversation_frame = build_group_conversation_frame(
        context_before,
        {
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": message,
            "is_mention": is_mention,
            "reply_to_assistant": bool(payload.get("reply_to_assistant")),
            "attachments": list(payload.get("attachments") or []),
            "created_at": str(payload.get("_event_timestamp") or utc_now()),
            "message_kind": "",
        },
        context_limit=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
        continuation_window_seconds=int(policy.get("continuation_window_seconds") or 120),
    )
    if deterministic_participation_enabled(conn):
        probe_event = qq_participation_event(
            conn,
            payload,
            scope="qq_group",
            actor_id=sender_id,
            thread_ref=f"qq:group:{group_id}",
            plain_text=str(payload.get("message") or ""),
            is_mention=is_mention,
        )
        conversation_frame["message_kind"] = probe_event.message_kind.value
        # A media reason cannot be emitted until this bounded, send-independent
        # policy has run.  The deterministic participation engine may still
        # keep the event silent; its decision is now made after this preflight.
        if _has_visual_attachment(probe_event.attachments):
            media_policy = project_media_observation_policy(policy)
            burst_count, daily_remaining = _media_budget_snapshot(conn, group_id, media_policy)
            conversation_frame["media_observation"] = select_media_observation(
                event_id=probe_event.event_id,
                participation_mode=str(media_policy.get("participation_mode") or ""),
                addressed=bool(is_mention or payload.get("reply_to_assistant") or payload.get("visual_question") or payload.get("media_question")),
                topic_active=bool(conversation_frame.get("topic_active") or payload.get("topic_active") or payload.get("_topic_active")),
                probability=media_policy.get("media_observation_probability", 0.0),
                burst_count=burst_count,
                daily_remaining=daily_remaining,
                burst_limit=_media_burst_limit(media_policy),
            )
        deterministic_decision = deterministic_inbound_decision(
            probe_event,
            group_policy=policy,
            state=participation_state(conn, probe_event),
            conversation_frame=conversation_frame,
        )
    if deterministic_decision is not None:
        allowed = deterministic_decision.action is not ParticipationAction.SILENT
        reason = deterministic_decision.reason.value
    else:
        allowed, reason = group_hard_gate(
            policy,
            is_mention=is_mention,
            continuation_candidate=bool(conversation_frame.get("active_continuation")),
        )
    event, current, context = record_group_inbound(
        conn,
        payload,
        policy=policy,
        group_id=group_id,
        sender_id=sender_id,
        sender_name=sender_name,
        session=session,
        plain_text=message,
        is_mention=is_mention,
        allowed=allowed,
        reason=reason,
    )
    policy = get_group_policy(conn, group_id) or policy
    natural_guard = None
    is_directed = bool(is_mention or payload.get("reply_to_assistant"))
    if (
        deterministic_decision is None
        and natural_group_participation_enabled(conn)
        and group_mode_from_legacy(policy) is GroupParticipationMode.NATURAL_PARTICIPATION
        and not is_directed
    ):
        observe_group_message(
            conn,
            group_id=group_id,
            created_at=str(current.get("created_at") or utc_now()),
            burst_window_seconds=int(policy.get("burst_window_seconds") or 12),
        )
        queue = enqueue_group_candidate(
            conn,
            group_id=group_id,
            current=current,
            session=session,
            sender_id=sender_id,
            sender_name=sender_name,
            external_message_id=str(payload.get("_external_message_id") or ""),
            quiet_gap_seconds=int(
                8 if policy.get("quiet_gap_seconds") in {None, ""} else policy["quiet_gap_seconds"]
            ),
            active_topic_window_seconds=group_active_topic_window_seconds(policy),
            message_kind=str(conversation_frame.get("message_kind") or ""),
        )
        allowed = False
        reason = "natural_deferred"
        natural_guard = {"should_reply": False, "reason": reason, "queue": queue}
    elif allowed and not is_directed:
        observation = observe_group_message(
            conn,
            group_id=group_id,
            created_at=str(current.get("created_at") or utc_now()),
            burst_window_seconds=int(policy.get("burst_window_seconds") or 12),
        )
        candidate_kind = (
            "continuation"
            if (
                str(getattr(deterministic_decision, "action", "").value if deterministic_decision else "")
                == ParticipationAction.CONTINUATION_REPLY.value
                or conversation_frame.get("active_continuation")
            )
            else "ambient"
        )
        natural_guard = group_final_action_gate(
            conn,
            policy=policy,
            group_id=group_id,
            current=current,
            observation=observation,
            conversation_frame=conversation_frame,
            candidate_kind=candidate_kind,
            directed=is_directed,
        )
        if natural_guard is not None:
            allowed = False
            reason = str(natural_guard.get("reason") or "natural_group_guard")
    blocked = None
    if not allowed:
        decision = {"should_reply": False, "reason": reason}
        finalize_group_shadow(
            conn, event, False, reason, group_id, payload,
            decision_override=deterministic_decision,
            conversation_frame=conversation_frame,
            interaction_decision=decision,
        )
        if natural_guard and natural_guard.get("queue"):
            current_decision_id = str(current.get("engagement_decision_id") or "")
            transition_group_participation(
                conn,
                decision_id=current_decision_id,
                stage="deferred",
                action="silent",
                reason_code="natural_deferred",
            )
            replaced_message_id = int(natural_guard["queue"].get("replaced_message_id") or 0)
            if natural_guard["queue"].get("joined_active_topic"):
                reason = "topic_context_coalesced"
                decision = {"should_reply": False, "reason": reason}
                transition_group_participation(
                    conn,
                    decision_id=current_decision_id,
                    stage="preflight_blocked",
                    action="silent",
                    reason_code=reason,
                )
            elif replaced_message_id:
                replaced = conn.execute(
                    "SELECT engagement_decision_id FROM group_messages WHERE id=? AND group_id=?",
                    (replaced_message_id, group_id),
                ).fetchone()
                if replaced and str(replaced[0] or ""):
                    transition_group_participation(
                        conn,
                        decision_id=str(replaced[0]),
                        stage="superseded",
                        action="silent",
                        reason_code="candidate_superseded",
                        superseded_by=current_decision_id,
                    )
        mark_group_decision(
            conn,
            message_id=int(current["id"]),
            group_id=group_id,
            decision=decision,
            replied=False,
        )
        blocked = {
            "ok": True,
            "dispatch": "silent",
            "should_reply": False,
            "reason": reason,
            "group": policy,
        }
        if natural_guard and natural_guard.get("queue"):
            blocked["natural_queue"] = natural_guard["queue"]
    return {
        "policy": policy,
        "allowed": allowed,
        "reason": reason,
        "event": event,
        "current": current,
        "context": context,
        "blocked": blocked,
        "deterministic_decision": deterministic_decision,
        "conversation_frame": conversation_frame,
    }


def observe_group_access_denied(
    connect: Callable[[], sqlite3.Connection],
    payload: dict,
    group_id: str,
    sender_id: str,
    is_mention: bool,
    reason: str,
) -> None:
    try:
        with connect() as conn:
            if not participation_shadow_enabled(conn):
                return
            event = qq_participation_event(
                conn,
                payload,
                scope="qq_group",
                actor_id=sender_id,
                thread_ref=f"qq:group:{group_id}",
                plain_text=str(payload.get("message") or ""),
                is_mention=is_mention,
            )
            record_conversation_event(conn, event)
            record_group_shadow_decision(
                conn,
                event,
                allowed=False,
                reason=reason,
                group_id=group_id,
                source_message_id=str(payload.get("_external_message_id") or ""),
            )
    except (sqlite3.Error, ValueError) as exc:
        print(f"participation_shadow_observe_failed scope=group_access error={type(exc).__name__}", flush=True)


def with_qq_transport_metadata(payload: dict, headers, *, default_actor: str = "") -> dict:
    result = dict(payload)
    result["_external_message_id"] = (
        headers.get("X-QQ-Message-ID", "")
        or str(payload.get("external_message_id") or "")
    )
    result["_qq_actor_id"] = headers.get("X-QQ-Actor-ID", "") or default_actor
    result["_qq_actor_role"] = headers.get("X-QQ-Actor-Role", "") or "user"
    result["_qq_self_id"] = headers.get("X-QQ-Self-ID", "") or str(payload.get("self_id") or "")
    return result


def complete_group_dispatch(
    conn: sqlite3.Connection,
    *,
    event,
    deterministic_decision,
    decision: dict,
    group_id: str,
    payload: dict,
    classifier_settings: dict,
    current: dict,
    result: dict,
    assistant_name: str,
    conversation_frame: dict | None = None,
) -> bool:
    reply = str(result.get("reply") or "").strip()
    # Carry only bounded media lifecycle categories into the Delivery payload;
    # raw attachment data remains at the media boundary.
    result.update(media_trace_categories(conversation_frame))
    if (
        reply
        and str((conversation_frame or {}).get("attention") or "") == "active_continuation"
        and "uninvited_targeted_judgement" in group_reply_style_issues(
            str(payload.get("message") or ""), reply, uninvited=True,
        )
    ):
        result.update({
            "reply": "",
            "output": "",
            "group_safety_blocked": True,
            "group_safety_reason": "uninvited_targeted_judgement",
        })
        reply = ""
    planned_reply = bool(result.get("ok") and reply)
    finalize_group_shadow(
        conn,
        event,
        planned_reply,
        str(decision.get("reason") or "direct_mention"),
        group_id,
        payload,
        classifier_settings,
        decision_override=deterministic_decision,
        conversation_frame=conversation_frame,
        interaction_decision=decision,
    )
    mark_group_decision(
        conn,
        message_id=int(current["id"]),
        group_id=group_id,
        decision=decision,
        # A generated reply is only a plan.  Delivery confirmation projects the
        # actual assistant message and flips this inbound row to ``replied``.
        replied=False,
    )
    if planned_reply:
        # ``finalize_group_shadow`` persists the authoritative decision, while
        # ``current`` still holds the preliminary AC-1 projection.  Carry the
        # final ID into Delivery; do not fabricate a bot context row yet.
        result["engagement_decision_id"] = str(
            getattr(deterministic_decision, "decision_id", "")
            or current.get("engagement_decision_id")
            or ""
        )
        result["assistant_name"] = str(assistant_name or "助手")
    return planned_reply


def confirm_group_delivery(conn: sqlite3.Connection, delivery: dict) -> dict | None:
    """Project a QQ group reply only after its Delivery Outbox ACK.

    The delivery row is the source of truth for the outbound fact.  This keeps
    failed, superseded and ambiguous attempts out of the next group context and
    makes reply counts/rhythm reflect what the group could actually see.
    """
    payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
    group_id = str(payload.get("group_id") or "").strip()
    content = str(payload.get("content") or "").strip()[:4000]
    delivery_id = str(delivery.get("id") or "").strip()
    if payload.get("kind") != "assistant_reply" or not group_id or not content or not delivery_id:
        return None
    source_message_id = str(payload.get("source_message_id") or delivery.get("source_message_id") or "").strip()
    existing = conn.execute(
        """
        SELECT id FROM group_messages
        WHERE group_id=? AND sender_id='bot' AND instr(metadata_json, ?) > 0
        LIMIT 1
        """,
        (group_id, f'"delivery_id":"{delivery_id}"'),
    ).fetchone()
    if existing:
        return {"projected": False, "group_message_id": int(existing[0]), "delivery_id": delivery_id}
    now = utc_now()
    media_trace = build_media_delivery_trace(
        engagement_decision_id=str(delivery.get("engagement_decision_id") or ""),
        delivery_id=delivery_id,
        **media_trace_categories(payload.get("media_trace")),
        delivery_state=str(delivery.get("delivery_certainty") or "sent"),
        ack_state=(
            "confirmed"
            if str(delivery.get("delivery_certainty") or "").strip().lower() == "confirmed"
            or str(delivery.get("acked_at") or "").strip()
            else "pending"
        ),
    )
    metadata = json.dumps(
        {
            "delivery_id": delivery_id,
            "platform_message_id": str(delivery.get("platform_message_id") or ""),
            "source_external_message_id": source_message_id,
            "dispatch": str(payload.get("response_kind") or ""),
            "social_action": str(payload.get("social_action") or ""),
            "delivery_state": "confirmed",
            "media_trace": {
                key: media_trace[key]
                for key in (
                    "media_kind", "media_preflight_state", "visual_context_state",
                    "media_observation_decision",
                )
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO group_messages(
            group_id, sender_id, sender_name, content, is_mention,
            decision, decision_reason, replied, created_at,
            content_sha256, content_length, engagement_decision_id, metadata_json
        ) VALUES (?, 'bot', ?, ?, 0, 'assistant_reply', 'delivery_confirmed', 1, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            str(payload.get("assistant_name") or "助手")[:120],
            content,
            now,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            len(content),
            str(delivery.get("engagement_decision_id") or "")[:160],
            metadata,
        ),
    )
    if source_message_id:
        conn.execute(
            "UPDATE group_messages SET replied=1 WHERE group_id=? AND external_message_id=? AND sender_id<>'bot'",
            (group_id, source_message_id),
        )
    conn.execute(
        """
        UPDATE group_policies
        SET last_reply_at=?, reply_count=reply_count+1, updated_at=?
        WHERE group_id=?
        """,
        (now, now, group_id),
    )
    record_group_reply(
        conn,
        group_id=group_id,
        replied_at=now,
        count_towards_budget=bool(payload.get("uninvited_group_action")),
    )
    transition_group_participation(
        conn,
        decision_id=str(delivery.get("engagement_decision_id") or ""),
        stage="ack_confirmed",
        action="contextual_participation",
        reason_code="delivery_confirmed",
    )
    message_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return {
        "projected": True,
        "group_message_id": message_id,
        "delivery_id": delivery_id,
        "delivery_trace": media_trace,
    }


def observe_private_participation(
    connect: Callable[[], sqlite3.Connection],
    payload: dict,
    result: dict,
) -> dict | None:
    """Record shadow facts without allowing observation I/O to break replies."""

    try:
        with connect() as conn:
            if not participation_shadow_enabled(conn):
                return None
            user_id = str(payload.get("user_id") or "default").strip()
            event = qq_participation_event(
                conn,
                payload,
                scope="private",
                actor_id=user_id,
                thread_ref=f"qq:private:{user_id}",
                plain_text=str(payload.get("message") or ""),
            )
            record_conversation_event(conn, event)
            reason = "direct_private" if result.get("ok") else str(result.get("error") or "model_unavailable")
            decision = (
                deterministic_inbound_decision(event)
                if deterministic_participation_enabled(conn)
                else None
            ) or decision_from_legacy(
                event, allowed=bool(result.get("ok")), legacy_reason=reason,
            )
            retention, _ = retention_for_decision(event, decision)
            saved = record_participation_decision(
                conn,
                decision,
                assistant_id=event.assistant_id,
                thread_id=f"qq:private:{user_id}",
                source_message_id=str(payload.get("_external_message_id") or ""),
                legacy_allowed=bool(result.get("ok")),
                legacy_reason=reason,
                retention_class=retention,
            )
            return {
                "engagement_decision_id": str(saved.get("decision_id") or ""),
                "source_message_id": str(payload.get("_external_message_id") or ""),
            }
    except (sqlite3.Error, ValueError) as exc:
        print(f"participation_shadow_observe_failed scope=private error={type(exc).__name__}", flush=True)
        return None


__all__ = [
    "complete_group_dispatch",
    "confirm_group_delivery",
    "observe_private_participation",
    "observe_group_access_denied",
    "participation_shadow_enabled",
    "qq_participation_event",
    "prepare_group_shadow",
    "prepare_group_dispatch",
    "record_group_inbound",
    "finalize_group_shadow",
    "transition_group_participation",
    "record_conversation_event",
    "record_group_shadow_decision",
    "retention_for_decision",
    "with_qq_transport_metadata",
    "decision_from_legacy",
]
