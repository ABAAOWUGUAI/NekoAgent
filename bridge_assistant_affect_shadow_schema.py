#!/usr/bin/env python3
"""v47 default-off persistence for Assistant Affect Shadow only."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG = "assistant_affect_shadow_v1"
ASSISTANT_AFFECT_SHADOW_TABLE = "assistant_affect_shadows"
ASSISTANT_AFFECT_SHADOW_COLUMNS = frozenset(
    {
        "id",
        "assistant_id",
        "scope_type",
        "scope_ref",
        "topic_ref",
        "topic_revision",
        "target_type",
        "target_ref",
        "primary_affect",
        "valence",
        "arousal",
        "intensity",
        "confidence",
        "trigger_evidence_refs_json",
        "reason_code",
        "created_at",
        "last_updated_at",
        "expires_at",
        "decay_policy",
        "policy_version",
        "state",
    },
)
_CONTRACT = {
    "table": ASSISTANT_AFFECT_SHADOW_TABLE,
    "columns": sorted(ASSISTANT_AFFECT_SHADOW_COLUMNS),
    "feature_flag": ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG,
    "default_enabled": False,
    "shadow_only": True,
}
ASSISTANT_AFFECT_SHADOW_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def apply_assistant_affect_shadow_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {ASSISTANT_AFFECT_SHADOW_TABLE} (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('group','private')),
            scope_ref TEXT NOT NULL,
            topic_ref TEXT NOT NULL,
            topic_revision INTEGER NOT NULL CHECK(topic_revision>=0),
            target_type TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            primary_affect TEXT NOT NULL,
            valence TEXT NOT NULL,
            arousal TEXT NOT NULL,
            intensity REAL NOT NULL CHECK(intensity>=0 AND intensity<=1),
            confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
            trigger_evidence_refs_json TEXT NOT NULL CHECK(json_valid(trigger_evidence_refs_json)),
            reason_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decay_policy TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state='shadow'),
            UNIQUE(assistant_id,scope_type,scope_ref,topic_ref,topic_revision,target_type,target_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_affect_shadows_expiry
            ON {ASSISTANT_AFFECT_SHADOW_TABLE}(expires_at,id);
        CREATE INDEX IF NOT EXISTS idx_assistant_affect_shadows_topic
            ON {ASSISTANT_AFFECT_SHADOW_TABLE}(scope_ref,topic_ref,topic_revision,target_ref,expires_at DESC);
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('{ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


def require_assistant_affect_shadow_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(item[0]) for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {str(item[1]) for item in conn.execute(f"PRAGMA table_info({ASSISTANT_AFFECT_SHADOW_TABLE})")} if ASSISTANT_AFFECT_SHADOW_TABLE in tables else set()
    flag = conn.execute(
        "SELECT 1 FROM assistant_feature_flags WHERE name=?",
        (ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG,),
    ).fetchone()
    missing_columns = sorted(ASSISTANT_AFFECT_SHADOW_COLUMNS - columns)
    if ASSISTANT_AFFECT_SHADOW_TABLE not in tables or missing_columns or flag is None:
        raise MigrationDriftError(
            "assistant_affect_shadow_schema_drift:"
            + ("table" if ASSISTANT_AFFECT_SHADOW_TABLE not in tables else "")
            + "|"
            + ",".join(missing_columns)
            + ("|feature_flag" if flag is None else ""),
        )
    return {"ok": True}


def assistant_affect_shadow_enabled(conn: sqlite3.Connection) -> bool:
    try:
        require_assistant_affect_shadow_schema(conn)
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG,),
        ).fetchone()
        return bool(row and int(row[0]))
    except (sqlite3.Error, MigrationDriftError):
        return False


def set_assistant_affect_shadow_feature(conn: sqlite3.Connection, *, enabled: bool) -> dict:
    require_assistant_affect_shadow_schema(conn)
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE name=?",
        (1 if enabled else 0, ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG),
    )
    return {"enabled": assistant_affect_shadow_enabled(conn)}


__all__ = [
    "ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG",
    "ASSISTANT_AFFECT_SHADOW_MIGRATION_CHECKSUM",
    "ASSISTANT_AFFECT_SHADOW_TABLE",
    "apply_assistant_affect_shadow_v1",
    "assistant_affect_shadow_enabled",
    "require_assistant_affect_shadow_schema",
    "set_assistant_affect_shadow_feature",
]
