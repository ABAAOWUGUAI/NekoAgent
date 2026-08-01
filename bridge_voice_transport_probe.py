#!/usr/bin/env python3
"""Validate and persist VM-1B metadata-only QQ voice transport evidence."""

from __future__ import annotations

import ipaddress
import re
import sqlite3
from collections.abc import Mapping

from bridge_migrations import utc_now
from bridge_qq_access_schema import QQ_CHANNEL_ID
from bridge_voice_transport_probe_schema import (
    VOICE_TRANSPORT_PROBE_FEATURE_FLAG,
    require_voice_transport_probe_schema,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
)
BOOLEAN_FIELDS = (
    "record_present",
    "external_message_id_present",
    "file_present",
    "url_present",
    "path_present",
    "declared_size_present",
    "transport_https",
)
ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "source_kind",
        "channel_type",
        "scope_type",
        "source_fingerprint",
        "transport_host_suffix",
        *BOOLEAN_FIELDS,
    },
)
SENSITIVE_FIELD_FRAGMENTS = (
    "url",
    "path_value",
    "query",
    "rkey",
    "token",
    "secret",
    "cookie",
    "message",
    "content",
    "sender",
    "user_id",
    "qq_id",
    "file_handle",
)


class VoiceTransportProbeError(ValueError):
    """Fail-closed metadata probe error."""


def voice_transport_probe_enabled(conn: sqlite3.Connection) -> bool:
    require_voice_transport_probe_schema(conn)
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (VOICE_TRANSPORT_PROBE_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and row[0])


def set_voice_transport_probe_enabled(
    conn: sqlite3.Connection,
    enabled: bool,
) -> dict:
    """Change the Bridge-owned flag and version the Channel projection."""

    require_voice_transport_probe_schema(conn)
    if not isinstance(enabled, bool):
        raise VoiceTransportProbeError("voice_transport_probe_enabled_boolean_required")
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?
        """,
        (int(enabled), now, VOICE_TRANSPORT_PROBE_FEATURE_FLAG),
    )
    if cursor.rowcount != 1:
        raise VoiceTransportProbeError("voice_transport_probe_feature_flag_missing")
    cursor = conn.execute(
        """
        UPDATE qq_channel_settings
        SET config_version=config_version+1,updated_at=?
        WHERE channel_id=?
        """,
        (now, QQ_CHANNEL_ID),
    )
    if cursor.rowcount != 1:
        raise VoiceTransportProbeError("voice_transport_probe_channel_config_missing")
    row = conn.execute(
        "SELECT config_version FROM qq_channel_settings WHERE channel_id=?",
        (QQ_CHANNEL_ID,),
    ).fetchone()
    return {"enabled": enabled, "config_version": int(row[0]), "updated_at": now}


def _normalized_host(value: object) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise VoiceTransportProbeError("voice_transport_probe_host_invalid") from exc
    if not HOST_RE.fullmatch(host):
        raise VoiceTransportProbeError("voice_transport_probe_host_invalid")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise VoiceTransportProbeError("voice_transport_probe_host_forbidden")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise VoiceTransportProbeError("voice_transport_probe_host_ip_forbidden")


def _probe_from_row(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "channel_type": row["channel_type"],
        "scope_type": row["scope_type"],
        **{name: bool(row[name]) for name in BOOLEAN_FIELDS},
        "transport_host_suffix": row["transport_host_suffix"],
        "gate_status": row["gate_status"],
        "created_at": row["created_at"],
    }


def record_voice_transport_probe(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
) -> dict:
    require_voice_transport_probe_schema(conn)
    if not voice_transport_probe_enabled(conn):
        raise VoiceTransportProbeError("voice_transport_probe_disabled")
    if not isinstance(payload, Mapping):
        raise VoiceTransportProbeError("voice_transport_probe_payload_invalid")
    unknown = {str(key) for key in payload} - ALLOWED_FIELDS
    if unknown:
        if any(
            fragment in key.lower()
            for key in unknown
            for fragment in SENSITIVE_FIELD_FRAGMENTS
        ):
            raise VoiceTransportProbeError("voice_transport_probe_sensitive_field")
        raise VoiceTransportProbeError("voice_transport_probe_unknown_field")
    if payload.get("schema_version") != 1:
        raise VoiceTransportProbeError("voice_transport_probe_schema_invalid")
    if payload.get("source_kind") != "llbot_onebot_record":
        raise VoiceTransportProbeError("voice_transport_probe_source_invalid")
    if payload.get("channel_type") != "qq" or payload.get("scope_type") != "private":
        raise VoiceTransportProbeError("voice_transport_probe_private_scope_required")
    booleans: dict[str, bool] = {}
    for name in BOOLEAN_FIELDS:
        value = payload.get(name)
        if not isinstance(value, bool):
            raise VoiceTransportProbeError(f"voice_transport_probe_{name}_boolean_required")
        booleans[name] = value
    fingerprint = str(payload.get("source_fingerprint") or "").strip().lower()
    if not SHA256_RE.fullmatch(fingerprint):
        raise VoiceTransportProbeError("voice_transport_probe_fingerprint_invalid")
    host = _normalized_host(payload.get("transport_host_suffix"))
    if host and (not booleans["url_present"] or not booleans["transport_https"]):
        raise VoiceTransportProbeError("voice_transport_probe_host_without_https_url")
    passed = bool(
        booleans["record_present"]
        and booleans["external_message_id_present"]
        and booleans["file_present"]
        and booleans["url_present"]
        and booleans["transport_https"]
        and host
    )
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO voice_transport_probes(
            source_fingerprint,channel_type,scope_type,record_present,
            external_message_id_present,file_present,url_present,path_present,
            declared_size_present,transport_https,transport_host_suffix,
            gate_status,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            fingerprint,
            "qq",
            "private",
            *(int(booleans[name]) for name in BOOLEAN_FIELDS),
            host,
            "passed" if passed else "incomplete",
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM voice_transport_probes WHERE source_fingerprint=?",
        (fingerprint,),
    ).fetchone()
    return _probe_from_row(row)


def latest_voice_transport_probe(conn: sqlite3.Connection) -> dict | None:
    require_voice_transport_probe_schema(conn)
    row = conn.execute(
        "SELECT * FROM voice_transport_probes ORDER BY id DESC LIMIT 1",
    ).fetchone()
    return _probe_from_row(row) if row else None


__all__ = [
    "VoiceTransportProbeError",
    "latest_voice_transport_probe",
    "record_voice_transport_probe",
    "set_voice_transport_probe_enabled",
    "voice_transport_probe_enabled",
]
