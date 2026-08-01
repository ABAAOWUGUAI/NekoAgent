#!/usr/bin/env python3
"""Assistant Core v35: Assistant-owned voice response modality policy."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


VOICE_RESPONSE_POLICY_TABLE = "assistant_voice_response_policies"
VOICE_RESPONSE_POLICY_INSERT_TRIGGER = "trg_assistant_voice_response_policy_insert"
VOICE_RESPONSE_POLICY_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(
        {
            "table": VOICE_RESPONSE_POLICY_TABLE,
            "modes": ("text_only", "explicit_only", "emotion_auto", "always"),
            "scope": "assistant_owner_private",
            "budget": "persistent_auto_attempt_reservation",
            "new_assistant": VOICE_RESPONSE_POLICY_INSERT_TRIGGER,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
).hexdigest()


def apply_voice_response_policy_v1(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "assistant_instances" not in tables:
        raise MigrationDriftError("voice_response_policy_assistant_instances_missing")
    conn.execute(
        f"""
        CREATE TABLE {VOICE_RESPONSE_POLICY_TABLE} (
            assistant_id TEXT PRIMARY KEY
                REFERENCES assistant_instances(id) ON DELETE CASCADE,
            mode TEXT NOT NULL DEFAULT 'explicit_only'
                CHECK(mode IN ('text_only','explicit_only','emotion_auto','always')),
            emotion_kinds_json TEXT NOT NULL
                DEFAULT '["happy","sad","tired","annoyed","playful","comfort"]',
            min_emotion_confidence REAL NOT NULL DEFAULT 0.72
                CHECK(min_emotion_confidence >= 0.0 AND min_emotion_confidence <= 1.0),
            cooldown_seconds INTEGER NOT NULL DEFAULT 300
                CHECK(cooldown_seconds >= 0 AND cooldown_seconds <= 86400),
            daily_limit INTEGER NOT NULL DEFAULT 8
                CHECK(daily_limit >= 0 AND daily_limit <= 100),
            last_auto_voice_at TEXT NOT NULL DEFAULT '',
            auto_voice_day TEXT NOT NULL DEFAULT '',
            auto_voice_count INTEGER NOT NULL DEFAULT 0 CHECK(auto_voice_count >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    now = utc_now()
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {VOICE_RESPONSE_POLICY_TABLE}(
            assistant_id,created_at,updated_at
        )
        SELECT id,?,? FROM assistant_instances
        """,
        (now, now),
    )
    conn.execute(
        f"""
        CREATE TRIGGER {VOICE_RESPONSE_POLICY_INSERT_TRIGGER}
        AFTER INSERT ON assistant_instances
        BEGIN
            INSERT OR IGNORE INTO {VOICE_RESPONSE_POLICY_TABLE}(
                assistant_id,created_at,updated_at
            ) VALUES(
                NEW.id,
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
            );
        END
        """,
    )


def inspect_voice_response_policy_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    columns = set()
    if VOICE_RESPONSE_POLICY_TABLE in tables:
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({VOICE_RESPONSE_POLICY_TABLE})")
        }
    required = {
        "assistant_id", "mode", "emotion_kinds_json", "min_emotion_confidence",
        "cooldown_seconds", "daily_limit", "last_auto_voice_at", "auto_voice_day",
        "auto_voice_count", "version", "created_at", "updated_at",
    }
    missing_rows = 0
    trigger_present = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
            (VOICE_RESPONSE_POLICY_INSERT_TRIGGER,),
        ).fetchone()
    )
    if VOICE_RESPONSE_POLICY_TABLE in tables and "assistant_instances" in tables:
        missing_rows = int(
            conn.execute(
                f"""
                SELECT count(*) FROM assistant_instances a
                LEFT JOIN {VOICE_RESPONSE_POLICY_TABLE} p ON p.assistant_id=a.id
                WHERE p.assistant_id IS NULL
                """,
            ).fetchone()[0],
        )
    return {
        "ok": VOICE_RESPONSE_POLICY_TABLE in tables
        and required.issubset(columns)
        and missing_rows == 0
        and trigger_present,
        "contract_checksum": VOICE_RESPONSE_POLICY_MIGRATION_CHECKSUM,
        "missing_columns": sorted(required - columns),
        "missing_policy_rows": missing_rows,
        "insert_trigger_present": trigger_present,
    }


def require_voice_response_policy_schema(conn: sqlite3.Connection) -> dict:
    result = inspect_voice_response_policy_schema(conn)
    if not result["ok"]:
        raise MigrationDriftError(
            "voice_response_policy_schema_drift:"
            + json.dumps(result, sort_keys=True, separators=(",", ":")),
        )
    return result


__all__ = [
    "VOICE_RESPONSE_POLICY_MIGRATION_CHECKSUM",
    "VOICE_RESPONSE_POLICY_INSERT_TRIGGER",
    "VOICE_RESPONSE_POLICY_TABLE",
    "apply_voice_response_policy_v1",
    "inspect_voice_response_policy_schema",
    "require_voice_response_policy_schema",
]
