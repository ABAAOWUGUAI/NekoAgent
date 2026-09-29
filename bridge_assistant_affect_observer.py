#!/usr/bin/env python3
"""BE-3 producer for grounded, zero-send Assistant Affect shadows.

The inbound text is inspected only while its normal channel turn is in memory.
This module persists an opaque trigger reference and a bounded Affect state, not
the message body, group/member identifier, reply, or a delivery instruction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
import sqlite3

from bridge_assistant_affect_contract import AssistantAffectContractError
from bridge_assistant_affect_shadow import record_assistant_affect_shadow
from bridge_assistant_affect_shadow_schema import assistant_affect_shadow_enabled
from bridge_behavior_observation import observe_assistant_affect_boundary_failure


_DIRECTED_INSULT = re.compile(
    r"(?:你(?:个|这个|真是|就是|也太|是不是|怎么这么|怎么那么|可真|真|太)?"
    r"(?:垃圾|废物|没用|蠢|傻|恶心|脑残))"
    r"|(?:你(?:给我)?(?:闭嘴|滚(?:开)?)(?:[，,。.!！?？\s]|$))"
    r"|(?:(?:^|[，,。.!！?？\s])(?:你)?(?:给我)?(?:闭嘴|滚(?:开)?)"
    r"(?:[，,。.!！?？\s]|$))"
    r"|(?:^\s*(?:垃圾|废物|没用|蠢|傻|恶心|脑残)\s*[。.!！]?\s*$)",
    re.IGNORECASE,
)
_HOSTILE_WORDS = frozenset({"闭嘴", "滚", "滚开"})
_DIRECTED_CORRECTION = re.compile(
    r"(?:搞错|错了|不对|理解错|误会|别再?这样叫|别再?这么叫|别再?叫我|不要叫我|"
    r"以后叫我|之后叫我|请叫我|没那么熟|不熟|保持距离)",
    re.IGNORECASE,
)
_DIRECTED_APPRECIATION = re.compile(r"(?:谢谢你?|多亏你?|辛苦了?|做得好)", re.IGNORECASE)
_DIRECTED_AMUSEMENT = re.compile(r"(?:哈哈|好笑|逗乐|笑死)", re.IGNORECASE)
_AFFECT_POLICY_REF = "policy:assistant-affect-expression-recovery-r1"
_AFFECT_VALENCE = {
    "happy": "positive",
    "amused": "positive",
    "curious": "positive",
    "concerned": "negative",
    "hurt": "negative",
    "annoyed": "negative",
}


def _utc(value: object | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("assistant_affect_observer_time_invalid")
    return parsed.astimezone(timezone.utc)


def _opaque_ref(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return f"{prefix}:sha256-{digest}"


def _directed_affect(message: object, *, directed_to_assistant: bool) -> tuple[str, str, float, float] | None:
    """Classify a small, explicit set of assistant-directed visible events.

    Criticism of code, a product, or an unspecified third party does not create
    an Assistant Affect.  This is deliberately narrower than a sentiment
    classifier: it records the assistant's grounded interaction event, not a
    guess about the member's mental state.
    """

    text = str(message or "").strip()
    if not directed_to_assistant or not text:
        return None
    if _DIRECTED_INSULT.search(text):
        if any(word in text for word in _HOSTILE_WORDS):
            return "annoyed", "visible_insult_or_mockery", 0.78, 0.93
        return "hurt", "visible_disrespect", 0.64, 0.87
    # A correction is a real interaction fact, not an inferred user emotion.
    # It lets the Assistant become more careful in the same bounded topic;
    # it cannot change its factual answer, permissions or delivery route.
    if _DIRECTED_CORRECTION.search(text):
        return "concerned", "correction", 0.52, 0.82
    if _DIRECTED_AMUSEMENT.search(text):
        return "amused", "visible_text_event", 0.46, 0.76
    if _DIRECTED_APPRECIATION.search(text):
        return "happy", "visible_text_event", 0.42, 0.72
    return None


def _observe_visible_affect(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: object,
    target_id: object,
    source_message_id: object,
    message: object,
    directed_to_assistant: bool,
    assistant_id: object,
    topic_revision: object,
    now: object | None = None,
) -> dict | None:
    """Record one grounded event without persisting body or raw identities.

    Returning ``None`` means either the Shadow flag is off or the message is
    not an explicit event directed at the assistant.  In both cases the
    channel's actual reply, action, approval and delivery paths are untouched.
    """

    signal = _directed_affect(message, directed_to_assistant=bool(directed_to_assistant))
    if signal is None:
        return None
    # Most inbound messages are not Affect evidence.  Reject them before any
    # schema/feature lookup so the optional observer remains a true no-op for
    # ordinary turns and for channel adapters with minimal connection stubs.
    if not assistant_affect_shadow_enabled(conn):
        return None
    owner = str(assistant_id or "").strip()
    if not owner:
        return None
    try:
        revision = int(topic_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("assistant_affect_observer_topic_invalid") from exc
    if revision < 0:
        raise ValueError("assistant_affect_observer_topic_invalid")
    created_at = _utc(now)
    affect, reason_code, intensity, confidence = signal
    # Valence is the assistant's own bounded reaction to the grounded event,
    # not a label for the member's emotion.  Persisting every affect as
    # negative made appreciation and amusement indistinguishable from an
    # insult to downstream Shadow evaluation.
    valence = _AFFECT_VALENCE.get(affect)
    if valence is None:
        raise ValueError("assistant_affect_observer_affect_invalid")
    source_ref = _opaque_ref("event", source_message_id)
    topic_source = source_message_id or f"{scope_id}:{revision}"
    shadow = {
        "schema_version": 1,
        "assistant_id": owner,
        "scope_type": scope_type,
        "scope_ref": _opaque_ref("scope", scope_id),
        "topic_ref": _opaque_ref("topic", topic_source),
        "topic_revision": revision,
        "target_type": "member",
        "target_ref": _opaque_ref("member", target_id),
        "primary_affect": affect,
        "valence": valence,
        "arousal": "high" if affect == "annoyed" else "medium",
        "intensity": intensity,
        "confidence": confidence,
        "trigger_evidence_refs": [source_ref],
        "reason_code": reason_code,
        "created_at": created_at.isoformat(),
        "last_updated_at": created_at.isoformat(),
        "expires_at": (created_at + timedelta(minutes=10)).isoformat(),
        "decay_policy": "turns_and_time",
        "policy_version": _AFFECT_POLICY_REF,
        "state": "shadow",
    }
    try:
        return record_assistant_affect_shadow(conn, shadow)
    except AssistantAffectContractError as exc:
        # A rejected shadow must never change the inbound turn.  When the
        # rejection is one of the known safety contracts, retain a separate
        # opaque BE-2 observation; never store the trigger text or raw IDs.
        try:
            observe_assistant_affect_boundary_failure(
                conn,
                assistant_id=owner,
                group_id=scope_id,
                source_message_id=source_message_id,
                topic_revision=revision,
                contract_error=str(exc),
                now=created_at,
            )
        except (sqlite3.Error, ValueError):
            pass
        return None


def observe_visible_group_affect(
    conn: sqlite3.Connection,
    *,
    group_id: object,
    sender_id: object,
    source_message_id: object,
    message: object,
    directed_to_assistant: bool,
    assistant_id: object,
    topic_revision: object,
    now: object | None = None,
) -> dict | None:
    """Observe an explicit event in an already-authorized group turn."""

    return _observe_visible_affect(
        conn,
        scope_type="group",
        scope_id=group_id,
        target_id=sender_id,
        source_message_id=source_message_id,
        message=message,
        directed_to_assistant=directed_to_assistant,
        assistant_id=assistant_id,
        topic_revision=topic_revision,
        now=now,
    )


def observe_visible_private_affect(
    conn: sqlite3.Connection,
    *,
    thread_id: object,
    user_id: object,
    source_message_id: object,
    message: object,
    assistant_id: object,
    topic_revision: object,
    now: object | None = None,
) -> dict | None:
    """Observe an Owner-private event; private input is directed by definition."""

    return _observe_visible_affect(
        conn,
        scope_type="private",
        scope_id=thread_id,
        target_id=user_id,
        source_message_id=source_message_id,
        message=message,
        directed_to_assistant=True,
        assistant_id=assistant_id,
        topic_revision=topic_revision,
        now=now,
    )


__all__ = ["observe_visible_group_affect", "observe_visible_private_affect"]
