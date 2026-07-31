"""Extract QQ structure and bounded, adapter-owned visual payloads.

Structural metadata remains URL-free.  The optional visual payload is created
only after the caller has passed channel access policy and contains image bytes
resolved by AstrBot, never a QQ URL or a file path.
"""

from __future__ import annotations

import asyncio
import inspect
import re


MAX_VISUAL_IMAGES = 3
MAX_VISUAL_IMAGE_BYTES = 4 * 1024 * 1024
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/=\s]+)$", re.I)


def event_is_structural_group(event, *, group_id: str = "") -> bool:
    """Recognize a group from transport facts without trusting one enum only."""

    if str(group_id or "").strip():
        return True
    message_obj = getattr(event, "message_obj", None)
    if str(getattr(message_obj, "group_id", "") or "").strip():
        return True
    group = getattr(message_obj, "group", None)
    if str(
        getattr(group, "group_id", "") or getattr(group, "id", "") or ""
    ).strip():
        return True
    private_getter = getattr(event, "is_private_chat", None)
    try:
        if callable(private_getter) and private_getter():
            return False
    except Exception:
        pass
    type_getter = getattr(event, "get_message_type", None)
    try:
        message_type = type_getter() if callable(type_getter) else None
        if getattr(message_type, "name", "") == "GROUP_MESSAGE":
            return True
    except Exception:
        pass
    return False


def event_participation_metadata(event, *, bot_id: str = "") -> dict:
    getter = getattr(event, "get_messages", None)
    try:
        chain = list(getter() or []) if callable(getter) else []
    except Exception:
        chain = []
    if not chain:
        chain = list(getattr(getattr(event, "message_obj", None), "message", None) or [])
    components: list[dict] = []
    mentions: list[str] = []
    attachments: list[dict] = []
    reply_id = ""
    reply_sender = ""
    for item in chain[:64]:
        kind = type(item).__name__.strip().lower() or "unknown"
        components.append({"type": kind[:40]})
        if kind == "at":
            target = str(
                getattr(item, "qq", "")
                or getattr(item, "user_id", "")
                or getattr(item, "target", "")
            ).strip()
            if target and target not in mentions:
                mentions.append(target[:80])
        elif kind == "reply":
            reply_id = str(
                getattr(item, "id", "")
                or getattr(item, "message_id", "")
            ).strip()[:160]
            reply_sender = str(
                getattr(item, "sender_id", "")
                or getattr(item, "user_id", "")
                or getattr(item, "qq", "")
            ).strip()[:80]
        elif kind in {"image", "video", "audio", "record", "file"}:
            attachments.append({"type": kind})
    # The event-local self id is the transport fact for this exact message.
    # Runtime configuration is only a fallback because it can briefly lag
    # behind a QQ relogin or an adapter reconnect.
    self_id_getter = getattr(event, "get_self_id", None)
    try:
        event_self_id = str(self_id_getter() or "").strip() if callable(self_id_getter) else ""
    except Exception:
        event_self_id = ""
    normalized_bot_id = event_self_id or str(bot_id or "").strip()
    return {
        "message_components": components,
        "mention_targets": mentions,
        "attachments": attachments,
        "reply_to_external_message_id": reply_id,
        "reply_to_assistant": bool(
            reply_id and reply_sender and normalized_bot_id and reply_sender == normalized_bot_id
        ),
        "self_id": normalized_bot_id,
        "self_id_source": "event" if event_self_id else ("runtime" if normalized_bot_id else "missing"),
    }


def _event_chain(event) -> list:
    getter = getattr(event, "get_messages", None)
    try:
        chain = list(getter() or []) if callable(getter) else []
    except Exception:
        chain = []
    if not chain:
        chain = list(getattr(getattr(event, "message_obj", None), "message", None) or [])
    return chain


async def _image_base64(component) -> str:
    """Ask AstrBot's image component to resolve its own media reference.

    The adapter may use a local file, cache, or its own downloader internally.
    This plugin deliberately does not read ``url``/``file``/``path`` and never
    forwards those identifiers across the Bridge boundary.
    """

    converter = getattr(component, "convert_to_base64", None)
    if not callable(converter):
        return ""
    try:
        value = await asyncio.to_thread(converter)
        if inspect.isawaitable(value):
            value = await value
    except Exception:
        return ""
    return str(value or "").strip()


async def event_visual_media_payloads(event) -> list[dict]:
    """Return at most three bounded image payloads for the current event.

    It is intentionally separate from :func:`event_participation_metadata` so
    access-denied events never cause image resolution or provider forwarding.
    """

    payloads: list[dict] = []
    for item in _event_chain(event)[:64]:
        if len(payloads) >= MAX_VISUAL_IMAGES:
            break
        if type(item).__name__.strip().lower() != "image":
            continue
        encoded = await _image_base64(item)
        if not encoded:
            continue
        mime = ""
        match = _DATA_URL.match(encoded)
        if match:
            mime, encoded = match.group(1).lower(), match.group(2)
        # Base64 is approximately 4/3 of the decoded payload.  The Bridge
        # verifies exact bytes and image signatures before any model request.
        if len(encoded) > ((MAX_VISUAL_IMAGE_BYTES * 4 // 3) + 16):
            continue
        payloads.append({"type": "image", "mime": mime, "data_base64": encoded})
    return payloads


def event_addresses_assistant(event, metadata: dict) -> bool:
    """Use structural message facts when AstrBot's wake hint is incomplete.

    At-only QQ chains have been observed with ``is_at_or_wake_command`` false
    even though their sole At component targets the logged-in assistant.  The
    component target and reply sender are therefore authoritative fallbacks.
    """

    if bool(getattr(event, "is_at_or_wake_command", False)):
        return True
    self_id = str(metadata.get("self_id") or "").strip()
    targets = {
        str(item or "").strip()
        for item in (metadata.get("mention_targets") or [])
        if str(item or "").strip()
    }
    return bool(
        metadata.get("reply_to_assistant")
        or (self_id and self_id in targets)
    )


__all__ = [
    "event_addresses_assistant",
    "event_is_structural_group",
    "event_participation_metadata",
    "event_visual_media_payloads",
]
