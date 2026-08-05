"""Extract QQ structure and bounded, adapter-owned visual payloads.

Structural metadata remains URL-free.  The optional visual payload is created
only after the caller has passed channel access policy and contains image bytes
resolved by AstrBot, never a QQ URL or a file path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import os
import re
import unicodedata

try:
    # The plugin is also loaded from an isolated package in AstrBot.  Keep a
    # fail-closed fallback for that loader while using the shared contract
    # whenever the Bridge snapshot is importable.
    from bridge_media_contract import classify_media_component
except ImportError:  # pragma: no cover - exercised only by the isolated loader
    classify_media_component = None


MAX_VISUAL_IMAGES = 3
MAX_VISUAL_IMAGE_BYTES = 4 * 1024 * 1024
MAX_VISUAL_VIDEO_BYTES = 16 * 1024 * 1024
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/=\s]+)$", re.I)
_VISUAL_COMPONENT_KINDS = frozenset({
    "image", "gif", "video", "mface", "marketface", "market_face",
    "dynamicface", "dynamic_face",
})
_ATTACHMENT_COMPONENT_KINDS = _VISUAL_COMPONENT_KINDS | frozenset({
    "audio", "record", "file",
})


def _normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def event_external_message_id(event) -> str:
    message_obj = getattr(event, "message_obj", None)
    raw = str(getattr(message_obj, "message_id", "") or "").strip()
    platform_getter = getattr(event, "get_platform_id", None)
    try:
        platform = str(platform_getter() or "").strip() if callable(platform_getter) else ""
    except Exception:
        platform = ""
    if raw and platform:
        return f"{platform}:{raw}"[:300]
    return (raw or platform)[:300]


def event_message_text(event) -> str:
    """Read text without letting a malformed media component abort the event."""

    getter = getattr(event, "get_message_str", None)
    if not callable(getter):
        return ""
    try:
        return _normalized_text(getter())
    except Exception:
        return ""


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
    reply_text = ""
    for item in chain[:64]:
        kind = type(item).__name__.strip().lower() or "unknown"
        component = {"type": kind[:40]}
        descriptor = classify_media_component(kind) if callable(classify_media_component) else None
        if descriptor is not None and descriptor.is_visual:
            # Structural facts are safe to persist; no adapter URL/path or
            # media bytes are copied into metadata.
            component["media_kind"] = descriptor.media_kind
            component["source_component"] = descriptor.source_component
        components.append(component)
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
            reply_text = _normalized_text(
                getattr(item, "message_str", "")
                or getattr(item, "text", "")
            )
        elif kind in _ATTACHMENT_COMPONENT_KINDS:
            if kind in _VISUAL_COMPONENT_KINDS:
                attachment_type = "video" if kind == "video" else "image"
                attachment = {"type": attachment_type}
                if kind not in {"image", "video"}:
                    attachment["source_component"] = kind
                attachments.append(attachment)
            else:
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
    self_id_match = bool(normalized_bot_id and normalized_bot_id in mentions)
    return {
        "external_message_id": event_external_message_id(event),
        "message_components": components,
        "mention_targets": mentions,
        "attachments": attachments,
        "reply_to_external_message_id": reply_id,
        "reply_text_sha256": hashlib.sha256(reply_text.encode("utf-8")).hexdigest() if reply_text else "",
        "reply_text_length": len(reply_text),
        "reply_to_assistant": bool(
            reply_id and reply_sender and normalized_bot_id and reply_sender == normalized_bot_id
        ),
        "self_id": normalized_bot_id,
        "self_id_source": "event" if event_self_id else ("runtime" if normalized_bot_id else "missing"),
        "mention_target_count": len(mentions),
        "self_id_match": self_id_match,
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


async def _media_base64(component, *, max_bytes: int) -> str:
    """Ask AstrBot's media component to resolve its own media reference.

    The adapter may use a local file, cache, or its own downloader internally.
    This plugin deliberately does not read ``url``/``file``/``path`` and never
    forwards those identifiers across the Bridge boundary.
    """

    converter = getattr(component, "convert_to_base64", None)
    value = ""
    if callable(converter):
        try:
            value = await asyncio.to_thread(converter)
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            value = ""
    # AstrBot's Video component intentionally exposes convert_to_file_path()
    # instead of convert_to_base64().  Resolve through the adapter, then read
    # only bounded bytes locally; the path never crosses the Bridge boundary.
    if not str(value or "").strip():
        path_converter = getattr(component, "convert_to_file_path", None)
        if callable(path_converter):
            try:
                path_value = await asyncio.to_thread(path_converter)
                if inspect.isawaitable(path_value):
                    path_value = await path_value
                path = str(path_value or "").strip()
                if path and os.path.isfile(path):
                    if os.path.getsize(path) > max(1, int(max_bytes)):
                        return ""
                    with open(path, "rb") as handle:
                        raw = handle.read(max(1, int(max_bytes)) + 1)
                    if len(raw) > max(1, int(max_bytes)):
                        return ""
                    value = "data:application/octet-stream;base64," + base64.b64encode(raw).decode("ascii")
            except Exception:
                value = ""
    return str(value or "").strip()


async def event_visual_media_payloads(event) -> list[dict]:
    """Return at most three bounded image/video payloads for the current event.

    It is intentionally separate from :func:`event_participation_metadata` so
    access-denied events never cause image resolution or provider forwarding.
    """

    payloads: list[dict] = []
    for item in _event_chain(event)[:64]:
        if len(payloads) >= MAX_VISUAL_IMAGES:
            break
        kind = type(item).__name__.strip().lower()
        descriptor = classify_media_component(kind) if callable(classify_media_component) else None
        if descriptor is not None:
            if not descriptor.is_visual:
                continue
            media_kind = descriptor.media_kind
        else:
            if kind not in _VISUAL_COMPONENT_KINDS:
                continue
            media_kind = "video" if kind == "video" else "image"
        if kind not in _VISUAL_COMPONENT_KINDS:
            continue
        limit = MAX_VISUAL_VIDEO_BYTES if media_kind == "video" else MAX_VISUAL_IMAGE_BYTES
        encoded = await _media_base64(item, max_bytes=limit)
        if not encoded:
            continue
        mime = ""
        match = _DATA_URL.match(encoded)
        if match:
            mime, encoded = match.group(1).lower(), match.group(2)
        # Base64 is approximately 4/3 of the decoded payload.  The Bridge
        # verifies exact bytes and extracts a bounded frame before any model
        # request.  Video bytes are accepted only as a transport payload; no
        # original video URL/path crosses the adapter boundary.
        if len(encoded) > ((limit * 4 // 3) + 16):
            continue
        if not mime:
            mime = "video/*" if media_kind == "video" else "image/*"
        payloads.append({
            "type": "video" if media_kind == "video" else "image",
            "mime": mime,
            "data_base64": encoded,
        })
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
    "MAX_VISUAL_VIDEO_BYTES",
    "event_addresses_assistant",
    "event_is_structural_group",
    "event_external_message_id",
    "event_message_text",
    "event_participation_metadata",
    "event_visual_media_payloads",
]
