#!/usr/bin/env python3
"""Fail-closed routing for inbound media.

QQ image bytes can reach only the current, bounded visual turn.  A later
message never reopens or reuses that media; it must carry a new image.
"""

from __future__ import annotations

import re


MEDIA_TYPES = {"image", "video", "audio", "record", "file"}


_MEDIA_RETRY_PATTERN = re.compile(
    r"(?:再(?:试|来|看)|重新(?:试|来|看)|重试|现在.*(?:试|看)|还能.*(?:试|看)|"
    r"(?:要开|打开|同意|授权|可以)(?:了|吗|吧)?(?:.*(?:图|看|读))?)",
    re.IGNORECASE,
)

_RECENT_MEDIA_NOTICE_MARKERS = {
    "这张图我现在还看不了": "image_route_blocked",
    "媒体传输 Gate": "channel_media_transport_not_connected",
    "安全传给视觉模型": "channel_media_transport_not_connected",
    "识图理解": "vision_model_unbound",
    "这张图这次没读出来": "vision_caption_unavailable",
    "这张图这次没有收到可读的图片数据": "media_payload_missing",
    "刚才那张图没有保留": "media_resend_required",
    # Compatibility for a reply that an older ordinary conversation model
    # generated before this typed Gate was installed.  It only redirects the
    # immediate follow-up to a resend; it does not infer image content.
    "没有直接扫图片的权限": "legacy_media_notice",
    "这次我看不到具体内容": "legacy_media_notice",
    "直接读图看看": "legacy_media_notice",
}


def _recent_media_block_reason(history: object) -> str:
    """Return a bounded media-Gate reason from the current conversation only.

    A capability notice is a real turn outcome, not disposable UI text.  We
    deliberately infer only from the most recent assistant messages in this
    exact thread; no attachment URL, media bytes, or cross-thread history is
    involved.
    """

    if not isinstance(history, list):
        return ""
    for item in reversed(history[-6:]):
        if not isinstance(item, dict) or str(item.get("role") or "") != "assistant":
            continue
        content = str(item.get("content") or "")
        for marker, reason in _RECENT_MEDIA_NOTICE_MARKERS.items():
            if marker in content:
                return reason
    return ""


def inbound_media_retry_notice(
    message: object,
    history: object,
    *,
    vision_settings: dict | None,
    media_transport_connected: bool = True,
) -> dict | None:
    """Resume a recent image request without turning it into unrelated work.

    A previous image is never retained as a provider-ready payload.  If the
    transport implementation is available, the user must resend it; otherwise
    this returns the current, typed Gate result before a planner or executor runs.
    """

    text = " ".join(str(message or "").split())
    if not text or not _MEDIA_RETRY_PATTERN.search(text):
        return None
    previous_reason = _recent_media_block_reason(history)
    if not previous_reason:
        return None

    route = _image_route(
        vision_settings,
        media_transport_connected=media_transport_connected,
        images=1,
    )
    if route["status"] == "ready":
        reply = "可以，但之前那张图没有保留。请重新发送原图，我会直接按这次图片理解。"
        reason = "media_resend_required"
    else:
        reply = route["message"]
        reason = route["reason"]
    return {
        "ok": True,
        "dispatch": "capability_notice",
        "reply": reply,
        "output": reply,
        "capability_limited": True,
        "reason": reason,
        "media_retry": True,
        "attachment_kinds": ["image"],
        "model_role": "conversation_reply",
        "vision_model_role": "vision_caption",
    }


def _image_route(
    vision_settings: dict | None,
    *,
    media_transport_connected: bool,
    images: int,
) -> dict:
    """Resolve the image route without ever forwarding untrusted media."""

    if not isinstance(vision_settings, dict) or (
        vision_settings.get("model_registry_fallback")
        or not vision_settings.get("model_registry_id")
    ):
        return {
            "status": "blocked",
            "reason": "vision_model_unbound",
            "message": "这张图我现在还看不了，先别让我瞎猜。",
        }

    capabilities = {
        str(item).strip().lower()
        for item in vision_settings.get("model_capabilities") or []
    }
    if not {"text", "vision"}.issubset(capabilities):
        return {
            "status": "blocked",
            "reason": "vision_model_capability_mismatch",
            "message": "这张图我现在还看不了，先别让我瞎猜。",
        }
    if not media_transport_connected:
        return {
            "status": "blocked",
            "reason": "channel_media_transport_not_connected",
            "message": "这张图我现在还看不了，先别让我瞎猜。",
        }
    return {"status": "ready", "reason": "vision_route_ready", "message": ""}


def inbound_media_notice(
    settings: dict,
    attachments: object,
    *,
    vision_settings: dict | None = None,
    media_transport_connected: bool = False,
    suppress_repeated_notice: bool = False,
) -> dict | None:
    if not isinstance(attachments, list):
        return None
    kinds = [
        str(item.get("type") or "").strip().lower()
        for item in attachments
        if isinstance(item, dict)
    ]
    kinds = [item for item in kinds if item in MEDIA_TYPES]
    if not kinds:
        return None

    images = sum(1 for item in kinds if item == "image")
    if images:
        states = {
            str(item.get("visual_context_state") or "").strip()
            for item in attachments
            if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "image"
        }
        if "ready" in states:
            return None
        if "unavailable" in states:
            reply = "这张图这次没读出来，你再发一张原图，或者补一句文字也行。"
            reason = "vision_caption_unavailable"
        elif "none" in states:
            # Report an unbound/misdeclared role honestly before reporting a
            # missing current payload.  Either way, ordinary reply generation
            # stays closed until there is actual visual evidence.
            route = _image_route(
                vision_settings,
                media_transport_connected=True,
                images=images,
            )
            if route["status"] == "ready":
                reply = "这张图这次没有收到可读的图片数据。请重新发送原图，或补一行文字。"
                reason = "media_payload_missing"
            else:
                reply = route["message"]
                reason = route["reason"]
        elif vision_settings is None:
            # Preserve the old direct-call behavior for callers that have not
            # opted into the explicit vision role yet.
            capabilities = {
                str(item).strip().lower()
                for item in settings.get("model_capabilities") or []
            }
            if "vision" not in capabilities:
                reply = "这张图我现在还看不了，先别让我瞎猜。"
                reason = "conversation_model_vision_unsupported"
            else:
                reply = "这张图我现在还看不了，先别让我瞎猜。"
                reason = "channel_media_transport_not_connected"
        else:
            route = _image_route(
                vision_settings,
                media_transport_connected=media_transport_connected,
                images=images,
            )
            if route["status"] == "ready":
                # A configured route is not itself image evidence.  This
                # branch is reachable only when a caller omitted the typed
                # result from the current visual turn.
                reply = "这张图这次没有收到可读的图片数据。请重新发送原图，或补一行文字。"
                reason = "media_payload_missing"
            else:
                reply = route["message"]
                reason = route["reason"]
    else:
        reply = "这个附件我现在还读不了，先用文字说下重点吧。"
        reason = "channel_media_transport_not_connected"
    if suppress_repeated_notice:
        return {
            "ok": True,
            "dispatch": "silent",
            "reply": "",
            "output": "",
            "capability_limited": True,
            "reason": "media_notice_suppressed",
            "attachment_kinds": sorted(set(kinds)),
            "model_role": "conversation_reply",
            "vision_model_role": "vision_caption" if images else "",
        }
    return {
        "ok": True,
        "dispatch": "capability_notice",
        "reply": reply,
        "output": reply,
        "capability_limited": True,
        "reason": reason,
        "attachment_kinds": sorted(set(kinds)),
        "model_role": "conversation_reply",
        "vision_model_role": "vision_caption" if images else "",
    }


__all__ = ["inbound_media_notice", "inbound_media_retry_notice"]
