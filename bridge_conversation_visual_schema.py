#!/usr/bin/env python3
"""Durable, body-free visual facts bound to private response cycles."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError
from bridge_response_cycle_successor_schema import require_response_cycle_successor_schema


TABLE = "conversation_visual_observations"
INDEX = "idx_conversation_visual_observations_thread_time"
REQUIRED_COLUMNS = (
    "response_cycle_id",
    "thread_id",
    "assistant_id",
    "source_set_hash",
    "schema_version",
    "status",
    "observation_json",
    "observation_sha256",
    "observed_at",
)
FOREIGN_KEYS = {
    "response_cycle_id": ("conversation_response_cycles", "id", "RESTRICT"),
    "thread_id": ("conversation_threads", "id", "RESTRICT"),
    "assistant_id": ("assistant_instances", "id", "RESTRICT"),
}


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def apply_conversation_visual_observation_v1(conn: sqlite3.Connection) -> None:
    require_response_cycle_successor_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE {TABLE} (
            response_cycle_id TEXT PRIMARY KEY
                REFERENCES conversation_response_cycles(id) ON DELETE RESTRICT,
            thread_id TEXT NOT NULL
                REFERENCES conversation_threads(id) ON DELETE RESTRICT,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            source_set_hash TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version=1),
            status TEXT NOT NULL CHECK(status='ready'),
            observation_json TEXT NOT NULL,
            observation_sha256 TEXT NOT NULL,
            observed_at TEXT NOT NULL
        );
        CREATE INDEX {INDEX}
        ON {TABLE}(thread_id,observed_at);
        """,
    )


def inspect_conversation_visual_observation_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if TABLE not in tables:
        return {
            "ok": False,
            "missing_columns": list(REQUIRED_COLUMNS),
            "missing_indexes": [INDEX],
            "invalid_foreign_keys": sorted(FOREIGN_KEYS),
        }
    columns = _columns(conn)
    indexes = {
        str(row[1])
        for row in conn.execute(f"PRAGMA index_list({TABLE})")
    }
    actual_foreign_keys = {
        str(row[3]): (str(row[2]), str(row[4]), str(row[6]).upper())
        for row in conn.execute(f"PRAGMA foreign_key_list({TABLE})")
    }
    invalid_foreign_keys = sorted(
        column
        for column, expected in FOREIGN_KEYS.items()
        if actual_foreign_keys.get(column) != expected
    )
    missing_columns = sorted(set(REQUIRED_COLUMNS) - columns)
    unexpected_columns = sorted(columns - set(REQUIRED_COLUMNS))
    missing_indexes = [] if INDEX in indexes else [INDEX]
    return {
        "ok": (
            not missing_columns
            and not unexpected_columns
            and not missing_indexes
            and not invalid_foreign_keys
        ),
        "missing_columns": missing_columns,
        "unexpected_columns": unexpected_columns,
        "missing_indexes": missing_indexes,
        "invalid_foreign_keys": invalid_foreign_keys,
    }


def require_conversation_visual_observation_schema(
    conn: sqlite3.Connection,
) -> dict:
    audit = inspect_conversation_visual_observation_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "conversation_visual_observation_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


SCHEMA_CONTRACT = {
    "table": TABLE,
    "columns": list(REQUIRED_COLUMNS),
    "index": INDEX,
    "foreign_keys": {
        column: list(binding) for column, binding in FOREIGN_KEYS.items()
    },
    "schema_version": 1,
    "status": "ready",
}
CONVERSATION_VISUAL_OBSERVATION_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(SCHEMA_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "CONVERSATION_VISUAL_OBSERVATION_MIGRATION_CHECKSUM",
    "apply_conversation_visual_observation_v1",
    "inspect_conversation_visual_observation_schema",
    "require_conversation_visual_observation_schema",
]
