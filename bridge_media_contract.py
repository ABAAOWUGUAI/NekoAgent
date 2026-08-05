"""Typed, privacy-safe media facts shared by the adapter and Bridge.

Only media identity and bounded transport facts cross this boundary.  The
contract deliberately never reads adapter URL/path attributes and never
decodes or stores media bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


_GIF_COMPONENTS = frozenset({"gif", "mface", "marketface", "market_face", "dynamicface", "dynamic_face"})
_VIDEO_COMPONENTS = frozenset({"video"})
_IMAGE_COMPONENTS = frozenset({"image", "photo", "picture"})
_KNOWN_COMPONENTS = _GIF_COMPONENTS | _VIDEO_COMPONENTS | _IMAGE_COMPONENTS
_SUPPORTED_MIME = {
    "image/gif": "image/gif",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/*": "image/*",
    "image/webp": "image/webp",
    "video/mp4": "video/mp4",
    "video/webm": "video/webm",
    "video/quicktime": "video/quicktime",
    "video/*": "video/*",
    # The adapter uses this generic MIME when it resolves a local video file.
    "application/octet-stream": "application/octet-stream",
}
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_ALLOWED_RESULT_KEYS = frozenset({"state", "media_kind", "source_component", "reason", "safe_mime"})


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    media_kind: str
    source_component: str
    declared_mime: str
    is_visual: bool


def _text(value: object, *, limit: int = 80) -> str:
    return str(value or "").strip().lower()[:limit]


def _mime(value: object) -> str:
    # MIME parameters are not useful for routing and can contain arbitrary
    # data.  Keep only the bounded type/subtype portion.
    value = _text(value, limit=120).split(";", 1)[0].strip()
    return {"image/jpg": "image/jpeg"}.get(value, value)


def classify_media_component(component_kind: object, declared_mime: object = "") -> MediaDescriptor:
    """Normalize adapter component names without touching media payloads."""

    source = _text(component_kind).replace("-", "_")
    mime = _mime(declared_mime)
    if source not in _KNOWN_COMPONENTS:
        if mime.startswith("video/"):
            source = "video"
        elif mime == "image/gif":
            source = "gif"
        elif mime.startswith("image/"):
            source = "image"
        else:
            source = ""
    if source in _GIF_COMPONENTS or mime == "image/gif":
        return MediaDescriptor("gif", source, mime, True)
    if source in _VIDEO_COMPONENTS or mime.startswith("video/"):
        return MediaDescriptor("video", source, mime, True)
    if source in _IMAGE_COMPONENTS or mime.startswith("image/"):
        return MediaDescriptor("image", source, mime, True)
    return MediaDescriptor("unknown", source, mime, False)


def _result(
    *,
    state: str,
    descriptor: MediaDescriptor,
    reason: str = "",
    safe_mime: str = "",
) -> dict:
    # Construct this dictionary explicitly so URL/path/raw payload fields can
    # never leak through even when a caller passes an adapter-shaped object.
    result = {
        "state": str(state)[:40],
        "media_kind": descriptor.media_kind,
        "source_component": descriptor.source_component,
        "reason": str(reason)[:80],
        "safe_mime": str(safe_mime)[:80],
    }
    return {key: result[key] for key in _ALLOWED_RESULT_KEYS}


def media_preflight(item: object, *, transport_available: bool, max_bytes: int) -> dict:
    """Validate typed media metadata before any decoding occurs.

    ``item`` is intentionally treated as a mapping only.  Adapter objects
    with URL/path properties are not introspected at all; callers must resolve
    bounded bytes through their adapter and pass a typed mapping here.
    """

    if not isinstance(item, Mapping):
        descriptor = classify_media_component(type(item).__name__ if item is not None else "")
        return _result(state="rejected", descriptor=descriptor, reason="media_payload_invalid")
    component = item.get("source_component") or item.get("type") or item.get("media_kind")
    declared = item.get("declared_mime") or item.get("mime") or ""
    descriptor = classify_media_component(component, declared)
    safe_mime = _SUPPORTED_MIME.get(descriptor.declared_mime, "")
    if descriptor.media_kind == "unknown":
        return _result(state="rejected", descriptor=descriptor, reason="media_component_unsupported")
    if not transport_available:
        return _result(state="unavailable", descriptor=descriptor, reason="media_transport_unavailable", safe_mime=safe_mime)
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return _result(state="rejected", descriptor=descriptor, reason="media_size_limit_invalid", safe_mime=safe_mime)
    # A video MIME is required for a video component; generic octet-stream is
    # accepted only for the adapter's bounded local-file fallback.
    if descriptor.declared_mime and not safe_mime:
        return _result(state="rejected", descriptor=descriptor, reason="media_mime_unsupported")
    if descriptor.media_kind == "video" and descriptor.declared_mime and not (
        descriptor.declared_mime.startswith("video/") or descriptor.declared_mime == "application/octet-stream"
    ):
        return _result(state="rejected", descriptor=descriptor, reason="media_mime_mismatch")
    encoded = item.get("data_base64")
    if encoded is None:
        return _result(state="rejected", descriptor=descriptor, reason="media_payload_missing", safe_mime=safe_mime)
    if isinstance(encoded, bytes):
        encoded_length = len(encoded)
        encoded_text = ""
        if encoded_length == 0 or not encoded.strip():
            return _result(state="rejected", descriptor=descriptor, reason="media_payload_invalid", safe_mime=safe_mime)
    else:
        encoded_text = str(encoded or "").strip()
        encoded_length = len(encoded_text)
        if not encoded_text:
            return _result(state="rejected", descriptor=descriptor, reason="media_payload_invalid", safe_mime=safe_mime)
    # Avoid decoding solely to check the bound.  Base64 expands bytes by
    # 4/3; the small allowance covers padding and whitespace.
    encoded_limit = (limit * 4 + 2) // 3 + 16
    if encoded_length > encoded_limit:
        return _result(state="rejected", descriptor=descriptor, reason=f"{descriptor.media_kind}_too_large", safe_mime=safe_mime)
    if encoded_text and not _BASE64_RE.fullmatch(encoded_text):
        return _result(state="rejected", descriptor=descriptor, reason="media_payload_invalid", safe_mime=safe_mime)
    return _result(state="ready", descriptor=descriptor, reason="", safe_mime=safe_mime or descriptor.declared_mime)


__all__ = ["MediaDescriptor", "classify_media_component", "media_preflight"]
