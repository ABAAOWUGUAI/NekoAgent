#!/usr/bin/env python3
"""Gate 5 additive schema for multi-intent Interaction Plans.

The legacy ``mode_sessions.mode/intent`` pair remains a compatibility summary.
Validated plans become the richer orchestration record without duplicating the
raw inbound message or creating a second task state machine.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_conversation_memory_schema import require_conversation_memory_schema
from bridge_migrations import MigrationDriftError, utc_now


INTERACTION_PLAN_FEATURE_FLAG = "interaction_plan_v2"

INTERACTION_PLAN_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "interaction_plans": (
        "id",
        "owner_actor_id",
        "assistant_id",
        "thread_id",
        "request_message_id",
        "schema_version",
        "status",
        "summary_mode",
        "primary_intent",
        "intent_count",
        "action_count",
        "plan_json",
        "plan_hash",
        "classifier_source",
        "origin_channel",
        "created_at",
        "updated_at",
    ),
}

INTERACTION_PLAN_INDEXES = (
    "idx_interaction_plans_owner",
    "idx_interaction_plans_thread",
    "idx_interaction_plans_message",
    "idx_interaction_plans_hash",
)


def apply_interaction_plan_v2(conn: sqlite3.Connection) -> None:
    """Create the additive plan store and a disabled cutover flag."""

    require_conversation_memory_schema(conn)
    conn.execute(
        """
        CREATE TABLE interaction_plans (
            id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            thread_id TEXT NOT NULL
                REFERENCES conversation_threads(id) ON DELETE RESTRICT,
            request_message_id TEXT
                REFERENCES conversation_messages(id) ON DELETE RESTRICT,
            schema_version INTEGER NOT NULL CHECK(schema_version = 1),
            status TEXT NOT NULL CHECK(
                status IN ('planned','dispatched','completed','failed','cancelled')
            ),
            summary_mode TEXT NOT NULL CHECK(
                summary_mode IN ('daily','work','mixed')
            ),
            primary_intent TEXT NOT NULL,
            intent_count INTEGER NOT NULL CHECK(intent_count BETWEEN 1 AND 8),
            action_count INTEGER NOT NULL CHECK(action_count BETWEEN 0 AND 12),
            plan_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            classifier_source TEXT NOT NULL,
            origin_channel TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE INDEX idx_interaction_plans_owner
        ON interaction_plans(owner_actor_id,status,updated_at DESC)
        """,
    )
    conn.execute(
        """
        CREATE INDEX idx_interaction_plans_thread
        ON interaction_plans(thread_id,created_at DESC)
        """,
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_interaction_plans_message
        ON interaction_plans(request_message_id)
        WHERE request_message_id IS NOT NULL
        """,
    )
    conn.execute(
        """
        CREATE INDEX idx_interaction_plans_hash
        ON interaction_plans(plan_hash,created_at DESC)
        """,
    )
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        ON CONFLICT(name) DO NOTHING
        """,
        (INTERACTION_PLAN_FEATURE_FLAG, utc_now()),
    )


def inspect_interaction_plan_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_tables = sorted(set(INTERACTION_PLAN_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in INTERACTION_PLAN_COLUMNS.items():
        if table not in tables:
            continue
        present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - present)
        if missing:
            missing_columns[table] = missing
    missing_indexes = sorted(set(INTERACTION_PLAN_INDEXES) - indexes)
    foreign_key_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    invalid_json = 0
    invalid_counts = 0
    if not missing_tables:
        invalid_json = int(
            conn.execute(
                "SELECT count(*) FROM interaction_plans WHERE NOT json_valid(plan_json)",
            ).fetchone()[0],
        )
        invalid_counts = int(
            conn.execute(
                """
                SELECT count(*) FROM interaction_plans
                WHERE intent_count < 1 OR intent_count > 8
                   OR action_count < 0 OR action_count > 12
                """,
            ).fetchone()[0],
        )
    ok = not (
        missing_tables
        or missing_columns
        or missing_indexes
        or foreign_key_errors
        or invalid_json
        or invalid_counts
    )
    return {
        "ok": ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "foreign_key_error_count": len(foreign_key_errors),
        "invalid_plan_json": invalid_json,
        "invalid_plan_counts": invalid_counts,
    }


def require_interaction_plan_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_interaction_plan_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "interaction_plan_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


INTERACTION_PLAN_MIGRATION_CONTRACT = {
    "tables": {key: list(value) for key, value in INTERACTION_PLAN_COLUMNS.items()},
    "indexes": list(INTERACTION_PLAN_INDEXES),
    "flag": INTERACTION_PLAN_FEATURE_FLAG,
    "schema_version": 1,
    "legacy_summary": "mode_sessions.mode_and_intent",
    "raw_message_policy": "reference_only",
}

INTERACTION_PLAN_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(
        INTERACTION_PLAN_MIGRATION_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
).hexdigest()


__all__ = [
    "INTERACTION_PLAN_FEATURE_FLAG",
    "INTERACTION_PLAN_MIGRATION_CHECKSUM",
    "apply_interaction_plan_v2",
    "inspect_interaction_plan_schema",
    "require_interaction_plan_schema",
]
