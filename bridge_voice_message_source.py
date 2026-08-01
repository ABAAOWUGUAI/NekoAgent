#!/usr/bin/env python3
"""Server-side contract for ephemeral LLBot OneBot record sources."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


MAX_QQ_VOICE_BYTES = 10 * 1024 * 1024
MAX_TRANSPORT_URL_LENGTH = 8192
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class VoiceMessageSourceError(ValueError):
    """Fail-closed Bridge source validation error."""


def _normalized_suffixes(values: Iterable[object]) -> tuple[str, ...]:
    suffixes = []
    for value in values:
        suffix = str(value or "").strip().lower().strip(".")
        if not suffix or "/" in suffix or ":" in suffix or "*" in suffix:
            raise VoiceMessageSourceError("qq_voice_host_suffix_invalid")
        try:
            ipaddress.ip_address(suffix)
        except ValueError:
            pass
        else:
            raise VoiceMessageSourceError("qq_voice_host_suffix_ip_forbidden")
        suffixes.append(suffix.encode("idna").decode("ascii"))
    result = tuple(sorted(set(suffixes)))
    if not result:
        raise VoiceMessageSourceError("qq_voice_host_policy_required")
    return result


def _host_allowed(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def validate_qq_private_record_source(
    payload: Mapping[str, object],
    *,
    allowed_host_suffixes: Iterable[object],
) -> dict:
    """Validate an authenticated adapter payload before any network access.

    DNS resolution and peer-IP pinning belong to the later fetcher.  This Gate
    deliberately requires an explicit hostname allowlist and never performs a
    permissive download itself.
    """

    if int(payload.get("schema_version") or 0) != 1:
        raise VoiceMessageSourceError("qq_voice_source_schema_invalid")
    if payload.get("source_kind") != "llbot_onebot_record":
        raise VoiceMessageSourceError("qq_voice_source_kind_invalid")
    if payload.get("channel_type") != "qq" or payload.get("scope_type") != "private":
        raise VoiceMessageSourceError("qq_voice_private_scope_required")
    external_message_id = str(payload.get("external_message_id") or "").strip()
    if not external_message_id or len(external_message_id) > 300:
        raise VoiceMessageSourceError("qq_voice_external_message_id_invalid")
    try:
        attachment_index = int(payload.get("attachment_index"))
    except (TypeError, ValueError) as exc:
        raise VoiceMessageSourceError("qq_voice_attachment_index_invalid") from exc
    if attachment_index < 0 or attachment_index >= 64:
        raise VoiceMessageSourceError("qq_voice_attachment_index_invalid")
    file_digest = str(payload.get("file_handle_sha256") or "").strip().lower()
    if not SHA256_RE.fullmatch(file_digest):
        raise VoiceMessageSourceError("qq_voice_file_handle_digest_invalid")
    transport_url = str(payload.get("transport_url") or "").strip()
    if not transport_url or len(transport_url) > MAX_TRANSPORT_URL_LENGTH:
        raise VoiceMessageSourceError("qq_voice_transport_url_invalid")
    parsed = urlsplit(transport_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise VoiceMessageSourceError("qq_voice_transport_https_required")
    if parsed.username or parsed.password or parsed.fragment:
        raise VoiceMessageSourceError("qq_voice_transport_url_unsafe")
    try:
        port = parsed.port
    except ValueError as exc:
        raise VoiceMessageSourceError("qq_voice_transport_port_invalid") from exc
    if port not in (None, 443):
        raise VoiceMessageSourceError("qq_voice_transport_port_invalid")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise VoiceMessageSourceError("qq_voice_transport_host_invalid") from exc
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise VoiceMessageSourceError("qq_voice_transport_host_forbidden")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise VoiceMessageSourceError("qq_voice_transport_ip_literal_forbidden")
    suffixes = _normalized_suffixes(allowed_host_suffixes)
    if not _host_allowed(host, suffixes):
        raise VoiceMessageSourceError("qq_voice_transport_host_not_allowed")
    declared_size = payload.get("declared_size_bytes")
    if declared_size not in (None, ""):
        try:
            declared_size = int(declared_size)
        except (TypeError, ValueError) as exc:
            raise VoiceMessageSourceError("qq_voice_declared_size_invalid") from exc
        if declared_size <= 0 or declared_size > MAX_QQ_VOICE_BYTES:
            raise VoiceMessageSourceError("qq_voice_declared_size_invalid")
    else:
        declared_size = None
    canonical_url = urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    source_material = f"qq\n{external_message_id}\n{attachment_index}\n{file_digest}"
    return {
        "schema_version": 1,
        "source_kind": "llbot_onebot_record",
        "channel_type": "qq",
        "scope_type": "private",
        "external_message_id": external_message_id,
        "attachment_index": attachment_index,
        "file_handle_sha256": file_digest,
        "source_id": hashlib.sha256(source_material.encode("utf-8")).hexdigest(),
        "transport_url": canonical_url,
        "transport_url_sha256": hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
        "transport_host_policy": "allowlisted_qq_media",
        "declared_size_bytes": declared_size,
    }


def qq_record_receipt_metadata(validated: Mapping[str, object]) -> dict:
    """Return the persistence-safe subset; the signed URL is never retained."""

    return {
        key: validated.get(key)
        for key in (
            "schema_version",
            "source_kind",
            "channel_type",
            "scope_type",
            "external_message_id",
            "attachment_index",
            "file_handle_sha256",
            "source_id",
            "transport_url_sha256",
            "transport_host_policy",
            "declared_size_bytes",
        )
    }


__all__ = [
    "MAX_QQ_VOICE_BYTES",
    "VoiceMessageSourceError",
    "qq_record_receipt_metadata",
    "validate_qq_private_record_source",
]
