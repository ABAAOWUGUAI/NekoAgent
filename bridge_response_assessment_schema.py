#!/usr/bin/env python3
"""v49 storage for body-free, fact-first response assessments."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


RESPONSE_ASSESSMENT_FEATURE_FLAG = "response_assessment_v1"
RESPONSE_ASSESSMENT_TABLE = "response_assessments"
RESPONSE_ASSESSMENT_COLUMNS = frozenset(
    {
        "id", "scope_type", "scope_ref", "topic_ref", "topic_revision", "target_ref",
        "reply_obligation", "speech_act", "grounding_status", "stance_json",
        "research_disposition", "task_continuation", "policy_version", "source_kind",
        "source_ref", "created_at",
    },
)
RESPONSE_ASSESSMENT_ISOLATED_COLUMNS = RESPONSE_ASSESSMENT_COLUMNS | frozenset({"assistant_id"})
_CONTRACT = {
    "feature_flag": RESPONSE_ASSESSMENT_FEATURE_FLAG,
    "table": RESPONSE_ASSESSMENT_TABLE,
    "columns": sorted(RESPONSE_ASSESSMENT_COLUMNS),
    "body_free": True,
    "default_enabled": False,
}
RESPONSE_ASSESSMENT_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def apply_response_assessment_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {RESPONSE_ASSESSMENT_TABLE} (
            id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('group','private')),
            scope_ref TEXT NOT NULL,
            topic_ref TEXT NOT NULL,
            topic_revision INTEGER NOT NULL CHECK(topic_revision>=0),
            target_ref TEXT NOT NULL,
            reply_obligation TEXT NOT NULL,
            speech_act TEXT NOT NULL,
            grounding_status TEXT NOT NULL,
            stance_json TEXT NOT NULL CHECK(json_valid(stance_json)),
            research_disposition TEXT NOT NULL,
            task_continuation TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_kind,source_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_response_assessments_recent
            ON {RESPONSE_ASSESSMENT_TABLE}(created_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_response_assessments_topic
            ON {RESPONSE_ASSESSMENT_TABLE}(scope_ref,topic_ref,topic_revision,created_at DESC);
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('{RESPONSE_ASSESSMENT_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


def require_response_assessment_schema(conn: sqlite3.Connection) -> dict:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (RESPONSE_ASSESSMENT_TABLE,),
    ).fetchone()
    columns = {
        str(item[1])
        for item in conn.execute(f"PRAGMA table_info({RESPONSE_ASSESSMENT_TABLE})")
    } if table else set()
    flag = conn.execute(
        "SELECT 1 FROM assistant_feature_flags WHERE name=?",
        (RESPONSE_ASSESSMENT_FEATURE_FLAG,),
    ).fetchone()
    missing = sorted(RESPONSE_ASSESSMENT_COLUMNS - columns)
    if not table or missing or flag is None:
        raise MigrationDriftError(
            "response_assessment_schema_drift:"
            + ("table" if not table else "")
            + ("|columns=" + ",".join(missing) if missing else "")
            + ("|feature_flag" if flag is None else ""),
        )
    return {"ok": True}


def require_response_assessment_assistant_isolation_schema(conn: sqlite3.Connection) -> dict:
    require_response_assessment_schema(conn)
    columns = {str(item[1]) for item in conn.execute(f"PRAGMA table_info({RESPONSE_ASSESSMENT_TABLE})")}
    missing = sorted(RESPONSE_ASSESSMENT_ISOLATED_COLUMNS - columns)
    if missing:
        raise MigrationDriftError("response_assessment_assistant_isolation_schema_drift:" + ",".join(missing))
    return {"ok": True}


def response_assessment_enabled(conn: sqlite3.Connection) -> bool:
    try:
        require_response_assessment_schema(conn)
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (RESPONSE_ASSESSMENT_FEATURE_FLAG,),
        ).fetchone()
        return bool(row and int(row[0]))
    except (sqlite3.Error, MigrationDriftError):
        return False


def set_response_assessment_feature(conn: sqlite3.Connection, *, enabled: bool) -> dict:
    require_response_assessment_schema(conn)
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE name=?",
        (1 if enabled else 0, RESPONSE_ASSESSMENT_FEATURE_FLAG),
    )
    return {"enabled": response_assessment_enabled(conn)}


__all__ = [
    "RESPONSE_ASSESSMENT_FEATURE_FLAG",
    "RESPONSE_ASSESSMENT_MIGRATION_CHECKSUM",
    "RESPONSE_ASSESSMENT_TABLE",
    "apply_response_assessment_v1",
    "require_response_assessment_schema",
    "require_response_assessment_assistant_isolation_schema",
    "response_assessment_enabled",
    "set_response_assessment_feature",
]
