#!/usr/bin/env python3
"""Additive durable ingress and response-cycle schema for private conversation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_conversation_participation_schema import require_conversation_participation_schema
from bridge_interaction_plan_schema import require_interaction_plan_schema
from bridge_migrations import MigrationDriftError, utc_now


CONTINUOUS_PRIVATE_CONVERSATION_FEATURE_FLAG = "continuous_private_conversation_v1"

ADDITIVE_COLUMNS: Mapping[str, Mapping[str, str]] = {
    "conversation_threads": {
        "latest_inbound_sequence": "INTEGER NOT NULL DEFAULT 0",
    },
    "conversation_messages": {
        "inbound_sequence": "INTEGER",
        "response_cycle_id": "TEXT",
        "logical_response_id": "TEXT NOT NULL DEFAULT ''",
        "delivery_id": "TEXT NOT NULL DEFAULT ''",
        "delivery_projection_state": "TEXT NOT NULL DEFAULT 'not_applicable'",
    },
    "interaction_plans": {
        "response_cycle_id": "TEXT",
    },
}

TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "conversation_response_cycles": (
        "id", "thread_id", "assistant_id", "owner_actor_id", "channel_type",
        "external_thread_ref", "assistant_binding_fingerprint",
        "from_inbound_sequence", "through_inbound_sequence", "source_count",
        "source_set_hash", "state", "source_frozen_at", "generation_started_at",
        "effects_admitted_at", "lease_owner", "lease_token", "lease_expires_at",
        "attempt_count", "max_attempts", "last_error", "logical_response_id",
        "outbox_dedupe_key", "delivery_id", "created_at", "updated_at",
    ),
    "conversation_response_cycle_sources": (
        "cycle_id", "source_message_id", "inbound_sequence", "source_order", "created_at",
    ),
    "interaction_plan_source_messages": (
        "plan_id", "source_message_id", "source_order", "source_role", "created_at",
    ),
}

REQUIRED_INDEXES = (
    "idx_conversation_threads_inbound_sequence",
    "idx_conversation_messages_logical_response",
    "idx_response_cycles_active_thread",
    "idx_response_cycles_thread_history",
    "idx_response_cycle_sources_message",
    "idx_interaction_plan_sources_message",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: Mapping[str, str]) -> None:
    existing = _columns(conn, table)
    if not existing:
        raise MigrationDriftError(f"continuous_private_base_table_missing:{table}")
    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply_continuous_private_conversation_v1(conn: sqlite3.Connection) -> None:
    require_conversation_participation_schema(conn)
    require_interaction_plan_schema(conn)
    for table, definitions in ADDITIVE_COLUMNS.items():
        _add_columns(conn, table, definitions)
    conn.executescript(
        """
        CREATE TABLE conversation_response_cycles (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES conversation_threads(id) ON DELETE RESTRICT,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            owner_actor_id TEXT NOT NULL,
            channel_type TEXT NOT NULL,
            external_thread_ref TEXT NOT NULL,
            assistant_binding_fingerprint TEXT NOT NULL,
            from_inbound_sequence INTEGER NOT NULL,
            through_inbound_sequence INTEGER NOT NULL,
            source_count INTEGER NOT NULL CHECK(source_count BETWEEN 1 AND 64),
            source_set_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(
                state IN ('pending','processing','commit_ready','outbox_queued','retryable','manual_hold')
            ),
            source_frozen_at TEXT NOT NULL DEFAULT '',
            generation_started_at TEXT NOT NULL DEFAULT '',
            effects_admitted_at TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 2),
            max_attempts INTEGER NOT NULL DEFAULT 2 CHECK(max_attempts = 2),
            last_error TEXT NOT NULL DEFAULT '',
            logical_response_id TEXT NOT NULL DEFAULT '',
            outbox_dedupe_key TEXT NOT NULL DEFAULT '',
            delivery_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_response_cycles_active_thread
        ON conversation_response_cycles(thread_id)
        WHERE state IN ('pending','processing','commit_ready','retryable','manual_hold');
        CREATE INDEX idx_response_cycles_thread_history
        ON conversation_response_cycles(thread_id,created_at DESC);

        CREATE TABLE conversation_response_cycle_sources (
            cycle_id TEXT NOT NULL REFERENCES conversation_response_cycles(id) ON DELETE RESTRICT,
            source_message_id TEXT NOT NULL REFERENCES conversation_messages(id) ON DELETE RESTRICT,
            inbound_sequence INTEGER NOT NULL,
            source_order INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(cycle_id,source_message_id),
            UNIQUE(cycle_id,source_order)
        );
        CREATE UNIQUE INDEX idx_response_cycle_sources_message
        ON conversation_response_cycle_sources(source_message_id);

        CREATE TABLE interaction_plan_source_messages (
            plan_id TEXT NOT NULL REFERENCES interaction_plans(id) ON DELETE RESTRICT,
            source_message_id TEXT NOT NULL REFERENCES conversation_messages(id) ON DELETE RESTRICT,
            source_order INTEGER NOT NULL,
            source_role TEXT NOT NULL CHECK(source_role IN ('context','trigger')),
            created_at TEXT NOT NULL,
            PRIMARY KEY(plan_id,source_message_id),
            UNIQUE(plan_id,source_order)
        );
        CREATE INDEX idx_interaction_plan_sources_message
        ON interaction_plan_source_messages(source_message_id,plan_id);

        CREATE UNIQUE INDEX idx_conversation_threads_inbound_sequence
        ON conversation_messages(thread_id,inbound_sequence)
        WHERE inbound_sequence IS NOT NULL;
        CREATE UNIQUE INDEX idx_conversation_messages_logical_response
        ON conversation_messages(logical_response_id)
        WHERE logical_response_id <> '';

        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('continuous_private_conversation_v1',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


def inspect_continuous_private_conversation_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_tables = sorted(set(TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, definitions in ADDITIVE_COLUMNS.items():
        if table not in tables:
            missing_tables.append(table)
            continue
        missing = sorted(set(definitions) - _columns(conn, table))
        if missing:
            missing_columns[table] = missing
    for table, required in TABLE_COLUMNS.items():
        if table in tables:
            missing = sorted(set(required) - _columns(conn, table))
            if missing:
                missing_columns[table] = missing
    flag = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (CONTINUOUS_PRIVATE_CONVERSATION_FEATURE_FLAG,),
    ).fetchone() if "assistant_feature_flags" in tables else None
    missing_indexes = sorted(set(REQUIRED_INDEXES) - indexes)
    return {
        "ok": not missing_tables and not missing_columns and not missing_indexes and flag is not None,
        "missing_tables": sorted(set(missing_tables)),
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "feature_flag_present": flag is not None,
        "feature_enabled": bool(flag and int(flag[0])),
    }


def require_continuous_private_conversation_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_continuous_private_conversation_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "continuous_private_conversation_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


SCHEMA_CONTRACT = {
    "feature_flag": CONTINUOUS_PRIVATE_CONVERSATION_FEATURE_FLAG,
    "additive_columns": {
        table: dict(definitions) for table, definitions in ADDITIVE_COLUMNS.items()
    },
    "tables": {table: list(columns) for table, columns in TABLE_COLUMNS.items()},
    "indexes": list(REQUIRED_INDEXES),
    "max_processing_attempts": 2,
}

CONTINUOUS_PRIVATE_CONVERSATION_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(SCHEMA_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "CONTINUOUS_PRIVATE_CONVERSATION_FEATURE_FLAG",
    "CONTINUOUS_PRIVATE_CONVERSATION_MIGRATION_CHECKSUM",
    "apply_continuous_private_conversation_v1",
    "inspect_continuous_private_conversation_schema",
    "require_continuous_private_conversation_schema",
]
