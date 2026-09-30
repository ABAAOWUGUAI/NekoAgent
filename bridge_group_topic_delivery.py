#!/usr/bin/env python3
"""R7-A topic-bound QQ Delivery context and send-time validation.

This module deliberately stores only opaque topic facts in the Delivery
payload.  The current group message body is read from the Assistant database
only while the claimed Delivery is still unsent, then discarded after the
allow/cancel decision.  It never copies a group transcript into Task DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping


GROUP_TOPIC_DELIVERY_FEATURE_FLAG = "group_topic_delivery_v1"
_UTC = timezone.utc
_CJK_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9._-]{2,}")


def _utc(value=None) -> datetime:
    if value is None:
        return datetime.now(_UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _timestamp(value=None) -> str:
    return _utc(value).isoformat(timespec="seconds")


def _topic_tokens(value: object) -> set[str]:
    """Return bounded lexical evidence; never persist the resulting tokens."""

    text = " ".join(str(value or "").lower().split())[:800]
    tokens = set(_ASCII_WORD.findall(text))
    for run in _CJK_RUN.findall(text):
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return {token for token in tokens if len(token) >= 2}


def topic_fingerprint(value: object) -> str:
    """Hash a normalized anchor without retaining its text in Delivery DB."""

    tokens = sorted(_topic_tokens(value))
    if not tokens:
        return ""
    payload = "\x1f".join(tokens).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_topic_delivery_context(
    *,
    group_id: object,
    anchor: Mapping[str, object],
    candidate_revision: object,
    reply_kind: object,
    now=None,
    ttl_seconds: object = 60,
) -> dict:
    """Create the opaque R7-A contract attached to a natural reply."""

    group = str(group_id or "").strip()[:180]
    try:
        anchor_id = max(0, int(anchor.get("id") or 0))
        revision = max(1, int(candidate_revision or 0))
        ttl = max(15, min(int(ttl_seconds or 60), 180))
    except (TypeError, ValueError) as exc:
        raise ValueError("topic_delivery_context_invalid") from exc
    fingerprint = topic_fingerprint(anchor.get("content"))
    external_ref = str(anchor.get("external_message_id") or "").strip()[:180]
    kind = str(reply_kind or "native_contribution").strip()
    if not group or not anchor_id or not fingerprint or kind not in {
        "native_contribution", "research_contribution",
    }:
        raise ValueError("topic_delivery_context_invalid")
    return {
        "topic_fingerprint": fingerprint,
        "topic_revision": revision,
        "anchor_message_id": anchor_id,
        "anchor_external_ref": external_ref,
        "expires_at": _timestamp(_utc(now) + timedelta(seconds=ttl)),
        "reply_kind": kind,
    }


def normalize_topic_delivery_context(value: object) -> dict | None:
    """Fail closed on malformed client/result metadata before Outbox enqueue."""

    if not isinstance(value, Mapping):
        return None
    fingerprint = str(value.get("topic_fingerprint") or "").strip().lower()
    external_ref = str(value.get("anchor_external_ref") or "").strip()[:180]
    reply_kind = str(value.get("reply_kind") or "").strip()
    try:
        anchor_id = max(0, int(value.get("anchor_message_id") or 0))
        revision = max(1, int(value.get("topic_revision") or 0))
        expires_at = _timestamp(value.get("expires_at"))
    except (TypeError, ValueError):
        return None
    if (
        len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
        or not anchor_id
        or reply_kind not in {"native_contribution", "research_contribution"}
    ):
        return None
    return {
        "topic_fingerprint": fingerprint,
        "topic_revision": revision,
        "anchor_message_id": anchor_id,
        "anchor_external_ref": external_ref,
        "expires_at": expires_at,
        "reply_kind": reply_kind,
    }


def topic_delivery_feature_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (GROUP_TOPIC_DELIVERY_FEATURE_FLAG,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and int(row[0]))


def _metadata(value: object) -> dict:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_is_explicit_continuation(latest: Mapping[str, object], anchor_ref: str) -> bool:
    if not anchor_ref:
        return False
    metadata = _metadata(latest.get("metadata_json"))
    reply_ref = str(
        latest.get("reply_to_external_message_id")
        or metadata.get("reply_to_external_message_id")
        or metadata.get("reply_to")
        or ""
    ).strip()
    return bool(reply_ref and reply_ref == anchor_ref)


def _same_topic(anchor_text: object, latest_text: object) -> bool:
    anchor_tokens, latest_tokens = _topic_tokens(anchor_text), _topic_tokens(latest_text)
    if not anchor_tokens or not latest_tokens:
        return False
    shared = anchor_tokens & latest_tokens
    if len(shared) >= 2:
        return True
    return len(shared) == 1 and len(anchor_tokens) == 1 and len(latest_tokens) == 1


def _non_text_interruption(item: Mapping[str, object]) -> bool:
    """Identify an attachment-only turn without inspecting media contents.

    A new ungrounded image must not become evidence for a reply, but it also
    must not erase a separately selected text contribution about the still
    current anchor.  The message store already carries this bounded type fact
    in metadata; legacy placeholder text is only a compatibility fallback.
    """

    metadata = _metadata(item.get("metadata_json"))
    kind = str(metadata.get("message_kind") or item.get("message_kind") or "").strip().lower()
    if kind in {"attachment", "image", "video", "audio"}:
        return True
    text = str(item.get("content") or "").strip().casefold()
    return text in {"[attachment]", "[image]", "[图片]", "[视频]", "[语音]"}


def _cancel(reason: str) -> dict:
    return {"action": "cancel", "reason": reason, "policy_kind": "topic_social"}


def evaluate_topic_context(
    conn: sqlite3.Connection,
    *,
    group_id: object,
    context: object,
    now=None,
    require_feature: bool = True,
) -> dict:
    """Validate an opaque topic context at a worker or Outbox boundary.

    The same check is deliberately shared by the participation worker and
    Outbox policy.  Otherwise a same-topic follow-up could be rejected before
    enqueue merely because a newer, but relevant, message changed the queue
    revision.  The lexical fallback is intentionally narrow: an explicit reply
    edge is always a continuation; absent that edge, two independent topic
    tokens must still be shared.  Ambiguous continuation is cancelled, never
    guessed.
    """

    context = normalize_topic_delivery_context(context)
    group_id = str(group_id or "").strip()
    if not context or not group_id:
        return _cancel("topic_context_invalid")
    if require_feature and not topic_delivery_feature_enabled(conn):
        return _cancel("topic_delivery_disabled")
    try:
        if _utc(now) >= _utc(context["expires_at"]):
            return _cancel("topic_expired")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(group_messages)").fetchall()
        }
        required = {"id", "group_id", "sender_id", "content", "external_message_id"}
        if not required.issubset(columns):
            return _cancel("topic_context_unavailable")
        metadata_column = "metadata_json" if "metadata_json" in columns else "'' AS metadata_json"
        anchor = conn.execute(
            f"""SELECT id,content,external_message_id,{metadata_column}
                FROM group_messages WHERE group_id=? AND id=?""",
            (group_id, int(context["anchor_message_id"])),
        ).fetchone()
        if not anchor:
            return _cancel("topic_context_unavailable")
        anchor = dict(anchor)
        if topic_fingerprint(anchor.get("content")) != context["topic_fingerprint"]:
            return _cancel("topic_context_changed")
        recent = conn.execute(
            f"""SELECT id,content,external_message_id,{metadata_column}
                FROM group_messages
                WHERE group_id=? AND sender_id<>'bot' AND id>=?
                ORDER BY id DESC LIMIT 8""",
            (group_id, int(context["anchor_message_id"])),
        ).fetchall()
    except (sqlite3.Error, TypeError, ValueError):
        return _cancel("topic_context_unavailable")
    latest = next((dict(row) for row in recent if not _non_text_interruption(dict(row))), None)
    if not latest:
        return _cancel("topic_context_unavailable")
    if int(latest.get("id") or 0) == int(context["anchor_message_id"]):
        return {"action": "allow", "reason": "topic_current", "policy_kind": "topic_social"}
    if _latest_is_explicit_continuation(latest, context["anchor_external_ref"]):
        return {"action": "allow", "reason": "topic_current", "policy_kind": "topic_social"}
    if _same_topic(anchor.get("content"), latest.get("content")):
        return {"action": "allow", "reason": "topic_current", "policy_kind": "topic_social"}
    return _cancel("topic_stale")


def evaluate_topic_delivery(
    conn: sqlite3.Connection,
    delivery: Mapping[str, object],
    *,
    now=None,
) -> dict:
    """Decide whether a claimed topic reply is still safe to send."""

    payload = delivery.get("payload") if isinstance(delivery, Mapping) else None
    payload = payload if isinstance(payload, Mapping) else {}
    return evaluate_topic_context(
        conn,
        group_id=payload.get("group_id"),
        context=payload.get("topic_delivery"),
        now=now,
    )


__all__ = [
    "GROUP_TOPIC_DELIVERY_FEATURE_FLAG", "build_topic_delivery_context",
    "evaluate_topic_context", "evaluate_topic_delivery", "normalize_topic_delivery_context",
    "topic_delivery_feature_enabled", "topic_fingerprint",
]
