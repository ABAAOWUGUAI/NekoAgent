#!/usr/bin/env python3
"""Additive schema for the Assistant Continuity Kernel V1."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


CONTINUITY_KERNEL_FEATURE_FLAG = "continuity_kernel_v1"
KERNEL_TABLE_COLUMNS = {
    "continuity_turns": (
        "id", "assistant_id", "actor_ref", "channel_type", "thread_ref",
        "trace_ref", "idempotency_key", "message_digest", "plan_id",
        "skill_plan_id", "primary_intent", "summary_mode", "action_type",
        "capability_id", "dispatch", "status", "goal_id", "run_id",
        "task_id", "delivery_id", "error_kind", "started_at", "updated_at",
        "completed_at",
    ),
    "continuity_skill_plans": (
        "id", "turn_id", "assistant_id", "status", "selected_json",
        "required_capabilities_json", "missing_capabilities_json", "plan_hash",
        "created_at", "updated_at", "completed_at",
    ),
    "continuity_events": (
        "id", "turn_id", "event_type", "outcome", "detail_json",
        "idempotency_key", "created_at",
    ),
}
KERNEL_INDEXES = (
    "idx_continuity_turns_status",
    "idx_continuity_turns_actor",
    "idx_continuity_turns_trace",
    "idx_continuity_turns_plan",
    "idx_continuity_turns_task",
    "idx_continuity_turns_delivery",
    "idx_continuity_skill_turn",
    "idx_continuity_events_turn",
    "idx_continuity_events_idempotency",
)
SKILL_OUTCOME_COLUMNS = {
    "success_count": "INTEGER NOT NULL DEFAULT 0",
    "failure_count": "INTEGER NOT NULL DEFAULT 0",
    "last_outcome_at": "TEXT NOT NULL DEFAULT ''",
}


def _contract_payload() -> str:
    return json.dumps(
        {
            "feature": CONTINUITY_KERNEL_FEATURE_FLAG,
            "tables": {key: list(value) for key, value in KERNEL_TABLE_COLUMNS.items()},
            "indexes": list(KERNEL_INDEXES),
            "skill_columns": SKILL_OUTCOME_COLUMNS,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


CONTINUITY_KERNEL_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def apply_continuity_kernel_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE continuity_turns (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            actor_ref TEXT NOT NULL DEFAULT '',
            channel_type TEXT NOT NULL DEFAULT '',
            thread_ref TEXT NOT NULL DEFAULT '',
            trace_ref TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '',
            message_digest TEXT NOT NULL,
            plan_id TEXT NOT NULL DEFAULT '',
            skill_plan_id TEXT NOT NULL DEFAULT '',
            primary_intent TEXT NOT NULL DEFAULT '',
            summary_mode TEXT NOT NULL DEFAULT '',
            action_type TEXT NOT NULL DEFAULT '',
            capability_id TEXT NOT NULL DEFAULT '',
            dispatch TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN (
                'planning','admitted','running','waiting_approval','waiting_delivery',
                'succeeded','failed','blocked','cancelled'
            )),
            goal_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            delivery_id TEXT NOT NULL DEFAULT '',
            error_kind TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX idx_continuity_turns_trace
        ON continuity_turns(assistant_id,idempotency_key)
        WHERE idempotency_key<>'';
        CREATE INDEX idx_continuity_turns_status
        ON continuity_turns(assistant_id,status,updated_at DESC);
        CREATE INDEX idx_continuity_turns_actor
        ON continuity_turns(assistant_id,actor_ref,updated_at DESC);
        CREATE INDEX idx_continuity_turns_plan
        ON continuity_turns(plan_id) WHERE plan_id<>'';
        CREATE INDEX idx_continuity_turns_task
        ON continuity_turns(task_id) WHERE task_id<>'';
        CREATE INDEX idx_continuity_turns_delivery
        ON continuity_turns(delivery_id) WHERE delivery_id<>'';

        CREATE TABLE continuity_skill_plans (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL UNIQUE REFERENCES continuity_turns(id) ON DELETE CASCADE,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK(status IN (
                'selected','admitted','applied','succeeded','failed','not_applied',
                'missing_capability','unavailable'
            )),
            selected_json TEXT NOT NULL DEFAULT '[]',
            required_capabilities_json TEXT NOT NULL DEFAULT '[]',
            missing_capabilities_json TEXT NOT NULL DEFAULT '[]',
            plan_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX idx_continuity_skill_turn
        ON continuity_skill_plans(turn_id);

        CREATE TABLE continuity_events (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL REFERENCES continuity_turns(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_continuity_events_turn
        ON continuity_events(turn_id,created_at,id);
        CREATE UNIQUE INDEX idx_continuity_events_idempotency
        ON continuity_events(turn_id,idempotency_key)
        WHERE idempotency_key<>'';

        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('continuity_kernel_v1',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )
    skill_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "skill_registry" in skill_tables:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(skill_registry)")
        }
        for name, declaration in SKILL_OUTCOME_COLUMNS.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE skill_registry ADD COLUMN {name} {declaration}",
                )


def require_continuity_kernel_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing_tables = sorted(set(KERNEL_TABLE_COLUMNS) - tables)
    missing_columns = {}
    for table, required in KERNEL_TABLE_COLUMNS.items():
        if table not in tables:
            continue
        actual = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(set(required) - actual)
        if missing:
            missing_columns[table] = missing
    if "skill_registry" in tables:
        skill_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(skill_registry)")
        }
        missing = sorted(set(SKILL_OUTCOME_COLUMNS) - skill_columns)
        if missing:
            missing_columns["skill_registry"] = missing
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_indexes = sorted(set(KERNEL_INDEXES) - indexes)
    if missing_tables or missing_columns or missing_indexes:
        raise MigrationDriftError(
            "continuity_kernel_schema_drift:"
            + json.dumps(
                {
                    "tables": missing_tables,
                    "columns": missing_columns,
                    "indexes": missing_indexes,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return {
        "ok": True,
        "contract_checksum": CONTINUITY_KERNEL_MIGRATION_CHECKSUM,
        "feature_flag": CONTINUITY_KERNEL_FEATURE_FLAG,
    }


__all__ = [
    "CONTINUITY_KERNEL_FEATURE_FLAG",
    "CONTINUITY_KERNEL_MIGRATION_CHECKSUM",
    "apply_continuity_kernel_v1",
    "require_continuity_kernel_schema",
]
