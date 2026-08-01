#!/usr/bin/env python3
"""Assistant Core VM-1B schema for metadata-only QQ voice transport probes."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


VOICE_TRANSPORT_PROBE_FEATURE_FLAG = "voice_transport_probe_v1"
PROBE_COLUMNS = (
    "id",
    "source_fingerprint",
    "channel_type",
    "scope_type",
    "record_present",
    "external_message_id_present",
    "file_present",
    "url_present",
    "path_present",
    "declared_size_present",
    "transport_https",
    "transport_host_suffix",
    "gate_status",
    "created_at",
)
PROBE_INDEXES = ("idx_voice_transport_probes_created",)


def _checksum() -> str:
    payload = json.dumps(
        {
            "feature_flag": VOICE_TRANSPORT_PROBE_FEATURE_FLAG,
            "columns": list(PROBE_COLUMNS),
            "indexes": list(PROBE_INDEXES),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


VOICE_TRANSPORT_PROBE_MIGRATION_CHECKSUM = _checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_voice_transport_probe_v1(conn: sqlite3.Connection) -> None:
    """Create diagnostic evidence only; no audio, URL, path, or message body."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "assistant_feature_flags" not in tables:
        raise MigrationDriftError("voice_transport_probe_feature_flags_missing")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS voice_transport_probes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_fingerprint TEXT NOT NULL UNIQUE
                CHECK(length(source_fingerprint)=64),
            channel_type TEXT NOT NULL CHECK(channel_type='qq'),
            scope_type TEXT NOT NULL CHECK(scope_type='private'),
            record_present INTEGER NOT NULL CHECK(record_present IN (0,1)),
            external_message_id_present INTEGER NOT NULL
                CHECK(external_message_id_present IN (0,1)),
            file_present INTEGER NOT NULL CHECK(file_present IN (0,1)),
            url_present INTEGER NOT NULL CHECK(url_present IN (0,1)),
            path_present INTEGER NOT NULL CHECK(path_present IN (0,1)),
            declared_size_present INTEGER NOT NULL
                CHECK(declared_size_present IN (0,1)),
            transport_https INTEGER NOT NULL CHECK(transport_https IN (0,1)),
            transport_host_suffix TEXT NOT NULL DEFAULT '',
            gate_status TEXT NOT NULL CHECK(gate_status IN ('passed','incomplete')),
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_voice_transport_probes_created
        ON voice_transport_probes(created_at DESC,id DESC);
        """,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        """,
        (VOICE_TRANSPORT_PROBE_FEATURE_FLAG, utc_now()),
    )


def inspect_voice_transport_probe_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_columns = []
    if "voice_transport_probes" in tables:
        missing_columns = sorted(
            set(PROBE_COLUMNS) - _columns(conn, "voice_transport_probes"),
        )
    flag = None
    if "assistant_feature_flags" in tables:
        flag = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (VOICE_TRANSPORT_PROBE_FEATURE_FLAG,),
        ).fetchone()
    missing_indexes = sorted(set(PROBE_INDEXES) - indexes)
    return {
        "ok": (
            "voice_transport_probes" in tables
            and not missing_columns
            and not missing_indexes
            and flag is not None
        ),
        "contract_checksum": VOICE_TRANSPORT_PROBE_MIGRATION_CHECKSUM,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "feature_flag_present": flag is not None,
        "feature_enabled": bool(flag[0]) if flag is not None else False,
    }


def require_voice_transport_probe_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_voice_transport_probe_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "voice_transport_probe_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "VOICE_TRANSPORT_PROBE_FEATURE_FLAG",
    "VOICE_TRANSPORT_PROBE_MIGRATION_CHECKSUM",
    "apply_voice_transport_probe_v1",
    "inspect_voice_transport_probe_schema",
    "require_voice_transport_probe_schema",
]
