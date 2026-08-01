#!/usr/bin/env python3
"""Assistant Core v34: fail-closed TTS and QQ voice-delivery flags."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


VOICE_OUTPUT_FEATURE_FLAG = "voice_output_v1"
VOICE_DELIVERY_FEATURE_FLAG = "voice_delivery_v1"


VOICE_OUTPUT_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(
        {
            "flags": (VOICE_OUTPUT_FEATURE_FLAG, VOICE_DELIVERY_FEATURE_FLAG),
            "scope": "owner_qq_private_explicit_request",
            "artifact": "immutable_wav",
            "delivery": "lease_bound_record",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
).hexdigest()


def apply_voice_output_v1(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {
        "assistant_feature_flags",
        "assistant_instances",
        "voice_packs",
    }
    missing = sorted(required - tables)
    if missing:
        raise MigrationDriftError("voice_output_prerequisite_missing:" + ",".join(missing))
    now = utc_now()
    for name in (VOICE_OUTPUT_FEATURE_FLAG, VOICE_DELIVERY_FEATURE_FLAG):
        conn.execute(
            "INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,0,?)",
            (name, now),
        )


def inspect_voice_output_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {
        "assistant_feature_flags",
        "assistant_instances",
        "voice_packs",
    }
    rows = {}
    if "assistant_feature_flags" in tables:
        rows = dict(
            conn.execute(
                "SELECT name,enabled FROM assistant_feature_flags WHERE name IN (?,?)",
                (VOICE_OUTPUT_FEATURE_FLAG, VOICE_DELIVERY_FEATURE_FLAG),
            ),
        )
    return {
        "ok": required.issubset(tables) and all(
            name in rows for name in (VOICE_OUTPUT_FEATURE_FLAG, VOICE_DELIVERY_FEATURE_FLAG)
        ),
        "contract_checksum": VOICE_OUTPUT_MIGRATION_CHECKSUM,
        "missing_tables": sorted(required - tables),
        "voice_output_enabled": bool(rows.get(VOICE_OUTPUT_FEATURE_FLAG)),
        "voice_delivery_enabled": bool(rows.get(VOICE_DELIVERY_FEATURE_FLAG)),
    }


def require_voice_output_schema(conn: sqlite3.Connection) -> dict:
    result = inspect_voice_output_schema(conn)
    if not result["ok"]:
        raise MigrationDriftError(
            "voice_output_schema_drift:"
            + json.dumps(result, sort_keys=True, separators=(",", ":")),
        )
    return result


__all__ = [
    "VOICE_DELIVERY_FEATURE_FLAG",
    "VOICE_OUTPUT_FEATURE_FLAG",
    "VOICE_OUTPUT_MIGRATION_CHECKSUM",
    "apply_voice_output_v1",
    "inspect_voice_output_schema",
    "require_voice_output_schema",
]
