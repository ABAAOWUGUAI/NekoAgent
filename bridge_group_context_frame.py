#!/usr/bin/env python3
"""Derived group-conversation context shared by participation and reply planning.

The frame is a disposable read model over ``group_messages``.  It does not
create another conversation store: persisted messages remain authoritative,
while this module gives every downstream decision the same speaker roles,
attention signal and active-conversation interpretation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from collections.abc import Mapping


DEFAULT_GROUP_CONTEXT_LIMIT = 40
MAX_GROUP_CONTEXT_LIMIT = 80
# A non-mentioned follow-up is a candidate for a social action, never an
# automatic reply obligation.  Individual groups can narrow or widen this
# bounded window through their policy.
ACTIVE_CONTINUATION_SECONDS = 120
ACTIVE_TOPIC_SECONDS = 1800
MEDIA_GATE_FOLLOWUP_SECONDS = 120

_ACKNOWLEDGEMENT = re.compile(
    r"(?:好+|嗯+|哦+|行|知道了|谢谢|谢了|收到|可以|ok|OK)[呀啊呢。！!~～]*",
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MENTION_ACCOUNT_SUFFIX = re.compile(r"(@[^@\n]{1,60}?)\s*\(\d{5,20}\)")


def _assistant_media_gate_reason(item: dict | None) -> str:
    """Return a typed recent media limitation without retaining media data."""

    if not isinstance(item, dict):
        return ""
    try:
        metadata = json.loads(str(item.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if bool(metadata.get("capability_limited")):
        return str(metadata.get("media_gate_reason") or "media_capability_limited")
    content = str(item.get("content") or "")
    if "这张图我现在还看不了" in content:
        return "image_route_blocked"
    if "媒体传输 Gate" in content or "安全传给视觉模型" in content:
        return "channel_media_transport_not_connected"
    return ""


def normalize_group_context_limit(value: object, default: int = DEFAULT_GROUP_CONTEXT_LIMIT) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    return max(4, min(result, MAX_GROUP_CONTEXT_LIMIT))


def normalize_continuation_window_seconds(
    value: object,
    default: int = ACTIVE_CONTINUATION_SECONDS,
) -> int:
    """Keep automatic follow-up windows explicit and safely bounded."""

    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    return max(15, min(result, 600))


def acknowledgement_only(value: object) -> bool:
    return bool(_ACKNOWLEDGEMENT.fullmatch(str(value or "").strip()))


def normalize_group_visible_text(value: object) -> str:
    """Clean transport artefacts before text reaches a model or topic candidate."""

    text = _CONTROL_CHARACTERS.sub(" ", str(value or ""))
    text = _MENTION_ACCOUNT_SUFFIX.sub(r"\1", text)
    return " ".join(text.split())


def _utc(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _message_kind(item: dict) -> str:
    direct = str(item.get("message_kind") or "").strip()
    if direct:
        return direct
    try:
        metadata = json.loads(str(item.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    return str(metadata.get("message_kind") or "text")


def _turn(item: dict) -> dict:
    assistant = str(item.get("sender_id") or "") == "bot"
    try:
        metadata = json.loads(str(item.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    media_observation = str(
        item.get("media_observation")
        or metadata.get("media_observation")
        or "none"
    )
    media_preflight = str(
        item.get("media_preflight")
        or metadata.get("media_preflight")
        or "none"
    )
    visual_context = str(
        item.get("visual_context")
        or metadata.get("visual_context")
        or "none"
    )
    return {
        "role": "assistant" if assistant else "member",
        "actor_id": "assistant" if assistant else str(item.get("sender_id") or ""),
        "speaker": str(item.get("sender_name") or ("助手" if assistant else "成员")),
        "content": normalize_group_visible_text(item.get("content")),
        "created_at": str(item.get("created_at") or ""),
        "is_mention": bool(item.get("is_mention")),
        "message_kind": _message_kind(item),
        "row_id": str(item.get("id") or ""),
        "external_message_id": str(
            item.get("external_message_id")
            or metadata.get("external_message_id")
            or ""
        ),
        "reply_to_external_message_id": str(
            item.get("reply_to_external_message_id")
            or metadata.get("reply_to_external_message_id")
            or ""
        ),
        "reply_to_assistant": bool(item.get("reply_to_assistant") or metadata.get("reply_to_assistant")),
        "media_observation": media_observation,
        "media_preflight": media_preflight,
        "visual_context": visual_context,
    }


def _topic_summary(turns: list[dict], current: dict) -> str:
    selected = [*turns[-5:], current]
    lines = []
    for item in selected:
        content = str(item.get("content") or "").strip()
        if not content or content == "@":
            continue
        label = "[助手/self]" if item.get("role") == "assistant" else f"[成员:{item.get('speaker') or '成员'}]"
        lines.append(f"{label} {content[:240]}")
    return "\n".join(lines)[:800]


def _reply_target_id_for(current_turn: dict) -> str:
    return str(current_turn.get("reply_to_external_message_id") or "").strip()


def _resolve_reply_target(current_turn: dict, turns: list[dict]) -> tuple[str, str, str]:
    """Resolve the Reply edge, returning (target_id, target_actor, target_mid).

    ``target_id`` is the external_message_id of the message being replied to
    (empty when none).  ``target_actor`` is that author's actor_ref.  ``target_mid``
    is the store row id (integer) when available.
    """

    reply_to = _reply_target_id_for(current_turn)
    if not reply_to:
        return "", "", ""
    target_turn = next(
        (item for item in turns if item.get("external_message_id") == reply_to),
        None,
    )
    if target_turn is None:
        return reply_to, "", ""
    return reply_to, str(target_turn.get("actor_id") or ""), str(target_turn.get("row_id") or "")


def _topic_root_id(reply_target_id: str, turns: list[dict]) -> str:
    """Return a stable topic root/anchor id.

    When a Reply edge exists, the anchor is the reply target itself.  Otherwise
    it is the most recent prior member turn's external id within the bounded
    topic window, or empty when none exists.
    """

    if reply_target_id:
        return reply_target_id
    for item in reversed(turns):
        if item["role"] == "member" and item.get("external_message_id"):
            return str(item["external_message_id"])
    return ""


def _ambiguous_target(
    current_turn: dict,
    reply_target_id: str,
    turns: list[dict],
    *,
    member_actor: str,
) -> bool:
    """Flag when the Reply edge is absent and the short utterance is ambiguous.

    Single-char/question/pronoun/"这个/那个/可以" utterances with no explicit
    reply edge must not be assigned a fabricated target.
    """

    if reply_target_id:
        return False
    text = str(current_turn.get("content") or "").strip()
    if not text or len(text) <= 4 or any(token in text for token in ("？", "?", "这个", "那个", "可以", "嗯")):
        return True
    # Two different members both addressed the same recent message with no
    # reply edge: the target is genuinely ambiguous.
    if member_actor:
        recent_members = [item["actor_id"] for item in turns[-4:] if item["role"] == "member"]
        if len({actor for actor in recent_members if actor and actor != member_actor}) >= 1 and len(text) <= 12:
            return True
    return False


def build_grounding_envelope(
    current_turn: dict,
    turns: list[dict],
    *,
    reply_target_id: str,
    topic_root_id: str,
    ambiguous: bool,
) -> dict:
    """Build the server-owned Grounding Envelope (B2).

    The envelope is derived only from server facts; a model may never raise
    media state inside it.  ``allowed_claim_types`` lists what this turn may
    truthfully claim; ``forbidden_claim_types`` lists what must be blocked.
    """

    media_kind = str(current_turn.get("message_kind") or "text")
    if media_kind == "attachment" and current_turn.get("attachments"):
        media_kind = "image"
    media_observation = str(current_turn.get("media_observation") or "none")
    media_preflight = str(current_turn.get("media_preflight") or "none")
    visual_context = str(current_turn.get("visual_context") or "none")

    # Inherit the most recent attachment's media state within the bounded
    # topic window when the current turn itself carries no media fact.  A
    # follow-up question about an already-observed attachment may then
    # truthfully reference it, while an unobserved one stays blocked.
    if visual_context not in {"ready", "blocked"} or media_observation in {"none", ""}:
        for item in reversed(turns):
            item_kind = str(item.get("message_kind") or "")
            if item_kind not in {"attachment", "image", "mixed", "video", "audio"}:
                continue
            if item.get("visual_context"):
                visual_context = str(item.get("visual_context") or visual_context)
            if item.get("media_observation"):
                media_observation = str(item.get("media_observation") or media_observation)
            if item.get("media_preflight"):
                media_preflight = str(item.get("media_preflight") or media_preflight)
            media_kind = "image" if item_kind in {"attachment", "image"} else item_kind
            break

    allowed: list[str] = []
    forbidden: list[str] = []
    verification_required = False
    if visual_context != "ready":
        forbidden.append("visual_details")
        verification_required = True
    else:
        allowed.append("limited_visual_facts")
    if media_observation in {"none", "deferred", "blocked"}:
        forbidden.append("sensory_experience")
        verification_required = True
    else:
        allowed.append("observed_media_facts")
    # No memory/knowledge/action evidence is threaded here, so experience and
    # researched claims are forbidden unless a caller later attaches evidence.
    forbidden.extend(("heard_song", "audio_listen", "researched_claim", "remembered_claim", "executed_claim"))
    allowed.extend(("subjective_opinion", "greeting"))
    if not reply_target_id or ambiguous:
        forbidden.append("concrete_attribution")
    else:
        allowed.append("referenced_reply")
        # A reply that points at a concrete member statement may agree with
        # reference (the observed fact is in-window); echoing an unverified
        # specific number/price/legal claim is still forbidden below.
        allowed.append("agree_with_reference")
    return {
        "target": {
            "actor_ref": "opaque",
            "source_message_id": str(current_turn.get("external_message_id") or ""),
            "reply_target_id": reply_target_id,
            "topic_root_id": topic_root_id,
            "ambiguous": ambiguous,
        },
        "text_evidence_ids": [],
        "media": {
            "kind": media_kind,
            "observation": media_observation,
            "preflight": media_preflight,
            "visual_context": visual_context,
        },
        "allowed_claim_types": allowed,
        "forbidden_claim_types": forbidden,
        "verification_required": verification_required,
    }


def build_group_conversation_frame(
    history: list[dict],
    current: dict,
    *,
    context_limit: int = DEFAULT_GROUP_CONTEXT_LIMIT,
    continuation_window_seconds: int = ACTIVE_CONTINUATION_SECONDS,
    now: datetime | None = None,
) -> dict:
    """Build one bounded interpretation of the current group turn.

    A continuation candidate exists only when the assistant was the latest
    speaker, the same member spoke immediately before it, and that member now
    continues within a short window.  Downstream policy still decides whether
    the candidate adds enough value to speak; this read model never creates a
    reply obligation or a second conversation store.
    """

    limit = normalize_group_context_limit(context_limit)
    continuation_window = normalize_continuation_window_seconds(
        continuation_window_seconds,
    )
    current_id = current.get("id")
    prior_items = list(history[-limit:])
    if current_id is not None:
        prior_items = [item for item in prior_items if item.get("id") != current_id]
    turns = [_turn(item) for item in prior_items]
    current_turn = _turn(current)
    current_turn["role"] = "member"
    current_actor = str(current.get("sender_id") or "")
    current_kind = _message_kind(current)
    text = str(current.get("content") or "").strip()

    last_assistant_index = next(
        (index for index in range(len(turns) - 1, -1, -1) if turns[index]["role"] == "assistant"),
        -1,
    )
    last_assistant = turns[last_assistant_index] if last_assistant_index >= 0 else None
    member_before = next(
        (
            turns[index]
            for index in range(last_assistant_index - 1, -1, -1)
            if turns[index]["role"] == "member"
        ),
        None,
    ) if last_assistant else None
    after_assistant = turns[last_assistant_index + 1 :] if last_assistant else []
    other_member_after = any(
        item["role"] == "member" and item["actor_id"] != current_actor
        for item in after_assistant
    )
    same_actor_after = all(
        item["role"] == "member" and item["actor_id"] == current_actor
        for item in after_assistant
    ) if after_assistant else True
    basis = now or _utc(current.get("created_at")) or datetime.now(timezone.utc)
    assistant_at = _utc((last_assistant or {}).get("created_at"))
    seconds_since_assistant = (
        max(0, int((basis - assistant_at).total_seconds())) if assistant_at else None
    )
    same_dialogue_actor = bool(
        member_before and member_before.get("actor_id") == current_actor
    )
    assistant_is_latest = bool(last_assistant and last_assistant_index == len(turns) - 1)
    active_exchange = bool(
        last_assistant
        and same_dialogue_actor
        and not other_member_after
        and same_actor_after
        and len(after_assistant) <= 2
        and seconds_since_assistant is not None
        and seconds_since_assistant <= continuation_window
    )
    attachment_only = current_kind == "attachment" or (not text and bool(current.get("attachments")))
    acknowledgement = acknowledgement_only(text)
    meaningful_text = bool(text and not acknowledgement and not attachment_only)
    active_continuation = bool(active_exchange and meaningful_text)
    continuation_assistant_turns = 0
    if active_continuation:
        # Count only the assistant turns at the tail of this same-member
        # exchange.  It deliberately stops at another member, so a busy group
        # cannot inherit a private-looking continuation budget.
        for item in reversed(turns[: last_assistant_index + 1]):
            if item["role"] == "assistant":
                continuation_assistant_turns += 1
                continue
            if item.get("actor_id") == current_actor:
                continue
            break
    last_assistant_media_gate_reason = _assistant_media_gate_reason(
        prior_items[last_assistant_index] if last_assistant_index >= 0 else None,
    )
    media_gate_active = bool(
        last_assistant_media_gate_reason
        and seconds_since_assistant is not None
        and seconds_since_assistant <= MEDIA_GATE_FOLLOWUP_SECONDS
    )
    if bool(current.get("reply_to_assistant")):
        attention = "reply_to_assistant"
    elif bool(current.get("is_mention")):
        attention = "explicit_mention"
    elif active_continuation:
        attention = "active_continuation"
    else:
        attention = "ambient"

    participant_ids = {
        item["actor_id"] for item in turns if item["role"] == "member" and item["actor_id"]
    }
    if current_actor:
        participant_ids.add(current_actor)
    topic_summary = _topic_summary(turns, current_turn)
    latest_prior_at = _utc((turns[-1] if turns else {}).get("created_at"))
    seconds_since_topic = (
        max(0, int((basis - latest_prior_at).total_seconds())) if latest_prior_at else None
    )
    topic_active = bool(
        topic_summary
        and seconds_since_topic is not None
        and seconds_since_topic <= ACTIVE_TOPIC_SECONDS
    )

    # Frame v3: Reply edge and topic anchor (identifier-only).  No new
    # conversation table is created; the reply fact is the persisted
    # ``reply_to_external_message_id`` projected into this turn.
    reply_target_id, reply_target_actor, reply_target_mid = _resolve_reply_target(
        current_turn, turns,
    )
    topic_root_id = _topic_root_id(reply_target_id, turns)
    ambiguous = _ambiguous_target(
        current_turn,
        reply_target_id,
        turns,
        member_actor=current_actor,
    )
    direct_addressee = reply_target_actor if reply_target_id else (
        "" if ambiguous else current_actor
    )
    reply_target_in_window = bool(reply_target_id and reply_target_mid)
    grounding_envelope = build_grounding_envelope(
        current_turn,
        turns,
        reply_target_id=reply_target_id,
        topic_root_id=topic_root_id,
        ambiguous=ambiguous,
    )
    return {
        "schema_version": 3,
        "context_limit": limit,
        "context_turn_count": len(turns),
        "participant_count": len(participant_ids),
        "attention": attention,
        "assistant_active": bool(last_assistant),
        "assistant_is_latest": assistant_is_latest,
        "last_assistant_turn_distance": len(turns) - 1 - last_assistant_index if last_assistant else None,
        "seconds_since_assistant": seconds_since_assistant,
        "same_dialogue_actor": same_dialogue_actor,
        "intervening_other_actor": other_member_after,
        "active_exchange": active_exchange,
        "active_continuation": active_continuation,
        "continuation_window_seconds": continuation_window,
        "continuation_assistant_turns": continuation_assistant_turns,
        "continuation_strength": "strong" if active_continuation and assistant_is_latest else ("medium" if active_continuation else "none"),
        "acknowledgement_only": acknowledgement,
        "attachment_only": attachment_only,
        "media_gate_active": media_gate_active,
        "media_gate_reason": last_assistant_media_gate_reason,
        "message_kind": current_kind,
        "topic_summary": topic_summary,
        "topic_evidence": bool(topic_summary),
        "topic_active": topic_active,
        # v3 identifier-only target/topic projection
        "reply_target_id": reply_target_id,
        "reply_target_actor": reply_target_actor,
        "reply_target_in_window": reply_target_in_window,
        "direct_addressee": direct_addressee,
        "topic_anchor_id": topic_root_id,
        "ambiguous_target": ambiguous,
        "grounding_envelope": grounding_envelope,
    }


def group_model_history(history: list[dict], *, limit: int = DEFAULT_GROUP_CONTEXT_LIMIT) -> list[dict[str, str]]:
    """Return chat history with the assistant's own turns in assistant role."""

    result: list[dict[str, str]] = []
    for item in history[-normalize_group_context_limit(limit) :]:
        turn = _turn(item)
        if not turn["content"]:
            continue
        if turn["role"] == "assistant":
            result.append({"role": "assistant", "content": turn["content"]})
        else:
            result.append({"role": "user", "content": f"{turn['speaker']}: {turn['content']}"})
    return result


def group_expression_rhythm(history: list[dict]) -> dict:
    """Derive ephemeral, non-content style guidance from recent members."""

    lengths: list[int] = []
    for item in history[-20:]:
        if not isinstance(item, dict) or str(item.get("role") or "") != "user":
            continue
        text = normalize_group_visible_text(item.get("content"))
        if ": " in text:
            text = text.split(": ", 1)[1]
        if text:
            lengths.append(len(text))
    if not lengths:
        return {"sample_count": 0, "median_length": 0, "short_ratio": 0.0, "target": "brief"}
    ordered = sorted(lengths)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else math.floor((ordered[middle - 1] + ordered[middle]) / 2)
    )
    short_ratio = sum(1 for length in lengths if length <= 24) / len(lengths)
    target = "brief" if median <= 24 or short_ratio >= 0.65 else ("short" if median <= 48 else "balanced")
    return {
        "sample_count": len(lengths),
        "median_length": median,
        "short_ratio": round(short_ratio, 2),
        "target": target,
    }


def group_context_lines(history: list[dict], *, limit: int = DEFAULT_GROUP_CONTEXT_LIMIT) -> list[str]:
    result = []
    for item in history[-normalize_group_context_limit(limit) :]:
        turn = _turn(item)
        if not turn["content"]:
            continue
        label = "[助手/self]" if turn["role"] == "assistant" else f"[成员:{turn['speaker']}]"
        result.append(f"{label} {turn['content'][:400]}")
    return result


def audit_group_conversation_frame(frame: dict | None) -> dict:
    """Strip message text and actor identifiers before decision persistence."""

    source = frame or {}
    keys = (
        "schema_version", "context_limit", "context_turn_count", "participant_count",
        "attention", "assistant_active", "assistant_is_latest",
        "last_assistant_turn_distance", "seconds_since_assistant",
        "same_dialogue_actor", "intervening_other_actor", "active_exchange",
        "active_continuation", "continuation_window_seconds",
        "continuation_assistant_turns", "continuation_strength", "acknowledgement_only",
        "attachment_only", "message_kind", "topic_evidence", "topic_active",
        # v3 identifier/category-only projection.  No reply text is retained;
        # only the opaque external ids, whether the target is in-window, and
        # the ambiguous flag are persisted.
        "reply_target_id", "reply_target_in_window", "direct_addressee",
        "topic_anchor_id", "ambiguous_target",
    )
    result = {key: source.get(key) for key in keys}
    envelope = source.get("grounding_envelope")
    if isinstance(envelope, Mapping):
        # Keep only categories/counts from the envelope, never media data.
        result["grounding_envelope"] = {
            "media": {
                "kind": str(envelope.get("media", {}).get("kind") or "") if isinstance(envelope.get("media"), Mapping) else "",
                "observation": str(envelope.get("media", {}).get("observation") or "") if isinstance(envelope.get("media"), Mapping) else "",
                "preflight": str(envelope.get("media", {}).get("preflight") or "") if isinstance(envelope.get("media"), Mapping) else "",
                "visual_context": str(envelope.get("media", {}).get("visual_context") or "") if isinstance(envelope.get("media"), Mapping) else "",
            },
            "allowed_claim_types": list(envelope.get("allowed_claim_types") or []),
            "forbidden_claim_types": list(envelope.get("forbidden_claim_types") or []),
            "verification_required": bool(envelope.get("verification_required")),
        }
    return result


__all__ = [
    "ACTIVE_CONTINUATION_SECONDS", "DEFAULT_GROUP_CONTEXT_LIMIT",
    "MAX_GROUP_CONTEXT_LIMIT", "acknowledgement_only",
    "audit_group_conversation_frame", "build_group_conversation_frame",
    "group_context_lines", "group_expression_rhythm", "group_model_history", "normalize_group_context_limit",
    "normalize_group_visible_text",
]
