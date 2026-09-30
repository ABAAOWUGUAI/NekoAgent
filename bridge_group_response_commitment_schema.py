#!/usr/bin/env python3
"""Body-free single-owner contract for one group response attempt."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


TABLE = "group_response_commitments"
INDEXES = (
    "idx_group_response_commitments_group_state",
    "idx_group_response_commitments_source",
)
REQUIRED_COLUMNS = (
    "id",
    "event_id",
    "assistant_id",
    "group_id",
    "source_message_id",
    "source_set_hash",
    "situation_revision",
    "owner_kind",
    "vision_started",
    "plan_started",
    "state",
    "logical_response_id",
    "outbox_dedupe_key",
    "delivery_id",
    "created_at",
    "updated_at",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_group_response_commitment_v1(conn: sqlite3.Connection) -> None:
    event_columns = _columns(conn, "conversation_events")
    if "id" not in event_columns:
        raise MigrationDriftError("group_response_commitment_event_schema_missing")
    conn.executescript(
        f"""
        CREATE TABLE {TABLE} (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE
                REFERENCES conversation_events(id) ON DELETE RESTRICT,
            assistant_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            source_set_hash TEXT NOT NULL,
            situation_revision TEXT NOT NULL,
            owner_kind TEXT NOT NULL CHECK(owner_kind IN ('direct','ambient')),
            vision_started INTEGER NOT NULL DEFAULT 0
                CHECK(vision_started IN (0,1)),
            plan_started INTEGER NOT NULL DEFAULT 0
                CHECK(plan_started IN (0,1)),
            state TEXT NOT NULL DEFAULT 'owned'
                CHECK(state IN ('owned','silent','planned','committed','held')),
            logical_response_id TEXT NOT NULL UNIQUE,
            outbox_dedupe_key TEXT NOT NULL UNIQUE,
            delivery_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_group_response_commitments_group_state
        ON {TABLE}(assistant_id,group_id,state,updated_at);
        CREATE INDEX idx_group_response_commitments_source
        ON {TABLE}(group_id,source_message_id);
        """,
    )


def inspect_group_response_commitment_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if TABLE not in tables:
        return {
            "ok": False,
            "missing_columns": list(REQUIRED_COLUMNS),
            "missing_indexes": list(INDEXES),
            "event_foreign_key_valid": False,
        }
    columns = _columns(conn, TABLE)
    indexes = {
        str(row[1])
        for row in conn.execute(f"PRAGMA index_list({TABLE})")
    }
    foreign_keys = conn.execute(f"PRAGMA foreign_key_list({TABLE})").fetchall()
    event_foreign_key_valid = any(
        str(row[2]) == "conversation_events"
        and str(row[3]) == "event_id"
        and str(row[4]) == "id"
        and str(row[6]).upper() == "RESTRICT"
        for row in foreign_keys
    )
    missing_columns = sorted(set(REQUIRED_COLUMNS) - columns)
    missing_indexes = sorted(set(INDEXES) - indexes)
    return {
        "ok": not missing_columns and not missing_indexes and event_foreign_key_valid,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "event_foreign_key_valid": event_foreign_key_valid,
    }


def require_group_response_commitment_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_group_response_commitment_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "group_response_commitment_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


SCHEMA_CONTRACT = {
    "table": TABLE,
    "columns": list(REQUIRED_COLUMNS),
    "indexes": list(INDEXES),
    "event_foreign_key": "conversation_events(id) ON DELETE RESTRICT",
    "owner_kinds": ["direct", "ambient"],
    "states": ["owned", "silent", "planned", "committed", "held"],
}

GROUP_RESPONSE_COMMITMENT_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(SCHEMA_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "GROUP_RESPONSE_COMMITMENT_MIGRATION_CHECKSUM",
    "apply_group_response_commitment_v1",
    "inspect_group_response_commitment_schema",
    "require_group_response_commitment_schema",
]
