"""Extract a bounded QQ private-record source from AstrBot events.

LLBot's OneBot ``record`` component is the channel fact.  ``file`` is an
opaque media handle used only to derive an idempotent digest; ``url`` is a
short-lived transport reference that must be validated and consumed by the
Bridge.  Host paths, base64 payloads, and chat bodies never cross this module.
"""

from __future__ import annotations

import hashlib
import ipaddress
from urllib.parse import urlsplit


MAX_RECORD_COMPONENTS = 1
MAX_EXTERNAL_MESSAGE_ID = 300
MAX_FILE_HANDLE_LENGTH = 1024
MAX_TRANSPORT_URL_LENGTH = 8192


class VoiceMessageSourceError(ValueError):
    """Fail-closed source extraction error."""


def _event_chain(event) -> list:
    getter = getattr(event, "get_messages", None)
    try:
        chain = list(getter() or []) if callable(getter) else []
    except Exception:
        chain = []
    if not chain:
        chain = list(getattr(getattr(event, "message_obj", None), "message", None) or [])
    return chain[:64]


def _require_private_event(event) -> None:
    message_obj = getattr(event, "message_obj", None)
    if str(getattr(message_obj, "group_id", "") or "").strip():
        raise VoiceMessageSourceError("qq_voice_private_scope_required")
    type_getter = getattr(event, "get_message_type", None)
    try:
        message_type = type_getter() if callable(type_getter) else None
    except Exception:
        message_type = None
    if getattr(message_type, "name", "") == "GROUP_MESSAGE":
        raise VoiceMessageSourceError("qq_voice_private_scope_required")
    private_getter = getattr(event, "is_private_chat", None)
    try:
        is_private = bool(private_getter()) if callable(private_getter) else False
    except Exception:
        is_private = False
    if not is_private and getattr(message_type, "name", "") != "FRIEND_MESSAGE":
        raise VoiceMessageSourceError("qq_voice_private_scope_unproven")


def _record_source(component, *, external_message_id: str, attachment_index: int) -> dict:
    file_handle = str(getattr(component, "file", "") or "").strip()
    transport_url = str(getattr(component, "url", "") or "").strip()
    if not file_handle or len(file_handle) > MAX_FILE_HANDLE_LENGTH:
        raise VoiceMessageSourceError("qq_voice_record_handle_invalid")
    if not transport_url or len(transport_url) > MAX_TRANSPORT_URL_LENGTH:
        raise VoiceMessageSourceError("qq_voice_transport_url_invalid")
    parsed = urlsplit(transport_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise VoiceMessageSourceError("qq_voice_transport_https_required")
    if parsed.username or parsed.password or parsed.fragment:
        raise VoiceMessageSourceError("qq_voice_transport_url_unsafe")
    return {
        "schema_version": 1,
        "source_kind": "llbot_onebot_record",
        "channel_type": "qq",
        "scope_type": "private",
        "external_message_id": external_message_id,
        "attachment_index": attachment_index,
        "file_handle_sha256": hashlib.sha256(file_handle.encode("utf-8")).hexdigest(),
        # Ephemeral.  The Bridge must remove this field before persistence.
        "transport_url": transport_url,
    }


def extract_owner_private_record_source(event, *, external_message_id: str) -> dict | None:
    """Return one normalized source, or ``None`` when the event has no record.

    Owner authorization must already have passed at the caller.  This function
    independently proves private scope so a future call-site cannot reuse it for
    group media by accident.
    """

    _require_private_event(event)
    message_id = str(external_message_id or "").strip()
    if not message_id or len(message_id) > MAX_EXTERNAL_MESSAGE_ID:
        raise VoiceMessageSourceError("qq_voice_external_message_id_invalid")
    records = [
        (index, item)
        for index, item in enumerate(_event_chain(event))
        if type(item).__name__.strip().lower() == "record"
    ]
    if not records:
        return None
    if len(records) > MAX_RECORD_COMPONENTS:
        raise VoiceMessageSourceError("qq_voice_record_count_exceeded")
    index, component = records[0]
    return _record_source(
        component,
        external_message_id=message_id,
        attachment_index=index,
    )


def build_owner_private_record_transport_probe(
    event,
    *,
    external_message_id: str,
) -> dict | None:
    """Return VM-1B metadata without returning any media locator or identity.

    The exact normalized hostname is used as the first fail-closed suffix
    candidate.  URL path/query, file/path values, QQ identity and message body
    never leave this function.
    """

    _require_private_event(event)
    message_id = str(external_message_id or "").strip()
    if not message_id or len(message_id) > MAX_EXTERNAL_MESSAGE_ID:
        raise VoiceMessageSourceError("qq_voice_external_message_id_invalid")
    records = [
        item
        for item in _event_chain(event)
        if type(item).__name__.strip().lower() == "record"
    ]
    if not records:
        return None
    if len(records) > MAX_RECORD_COMPONENTS:
        raise VoiceMessageSourceError("qq_voice_record_count_exceeded")
    component = records[0]
    file_handle = str(getattr(component, "file", "") or "").strip()
    transport_url = str(getattr(component, "url", "") or "").strip()
    host = ""
    https = False
    if transport_url and len(transport_url) <= MAX_TRANSPORT_URL_LENGTH:
        parsed = urlsplit(transport_url)
        https = bool(
            parsed.scheme.lower() == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
        if https:
            try:
                candidate = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
                ipaddress.ip_address(candidate)
            except UnicodeError:
                candidate = ""
            except ValueError:
                if candidate not in {"localhost", "localhost.localdomain"} and not candidate.endswith(".local"):
                    host = candidate
            try:
                port = parsed.port
            except ValueError:
                port = -1
            if port not in (None, 443):
                https = False
                host = ""
    fingerprint_material = f"{message_id}\n{file_handle}"
    return {
        "schema_version": 1,
        "source_kind": "llbot_onebot_record",
        "channel_type": "qq",
        "scope_type": "private",
        "source_fingerprint": hashlib.sha256(
            fingerprint_material.encode("utf-8"),
        ).hexdigest(),
        "record_present": True,
        "external_message_id_present": bool(message_id),
        "file_present": bool(file_handle),
        "url_present": bool(transport_url),
        "path_present": bool(str(getattr(component, "path", "") or "").strip()),
        "declared_size_present": bool(
            str(getattr(component, "file_size", "") or "").strip()
        ),
        "transport_https": https,
        "transport_host_suffix": host,
    }


__all__ = [
    "VoiceMessageSourceError",
    "build_owner_private_record_transport_probe",
    "extract_owner_private_record_source",
]
