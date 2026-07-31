#!/usr/bin/env python3
"""Gate C3 additive schema for durable inbound and cross-database dispatch."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_automation_schema import ensure_automation_tables
from bridge_migrations import MigrationDriftError, utc_now


RELIABILITY_FEATURE_FLAG = "task_message_reliability_v2"

RELIABILITY_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "assistant_action_outbox": (
        "id", "kind", "aggregate_type", "aggregate_id", "dedupe_key",
        "payload_json", "status", "delivery_id", "attempt_count",
        "next_attempt_at", "last_error", "created_at", "updated_at",
    ),
    "qq_inbound_receipts": (
        "platform_message_id", "actor_id", "conversation_ref", "payload_hash",
        "trace_id", "status", "response_json", "lease_until", "created_at",
        "updated_at",
    ),
}

AUTOMATION_RUN_COLUMNS = (
    "lease_owner", "lease_until", "attempt_count", "terminal_source",
)

RELIABILITY_REQUIRED_INDEXES = (
    "idx_assistant_action_outbox_pending",
    "idx_assistant_action_outbox_aggregate",
    "idx_qq_inbound_receipts_status",
)


def _checksum() -> str:
    payload = json.dumps(
        {
            "tables": {key: list(value) for key, value in RELIABILITY_TABLE_COLUMNS.items()},
            "automation_run_columns": list(AUTOMATION_RUN_COLUMNS),
            "indexes": list(RELIABILITY_REQUIRED_INDEXES),
            "feature_flag": RELIABILITY_FEATURE_FLAG,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


RELIABILITY_MIGRATION_CHECKSUM = _checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_task_message_reliability_v2(conn: sqlite3.Connection) -> None:
    ensure_automation_tables(conn)
    conn.executescript(
        """
        CREATE TABLE assistant_action_outbox (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','linked','failed')),
            delivery_id TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_assistant_action_outbox_pending
        ON assistant_action_outbox(status,next_attempt_at,created_at);
        CREATE INDEX idx_assistant_action_outbox_aggregate
        ON assistant_action_outbox(aggregate_type,aggregate_id);

        CREATE TABLE qq_inbound_receipts (
            platform_message_id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            conversation_ref TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('processing','completed','failed')),
            response_json TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_qq_inbound_receipts_status
        ON qq_inbound_receipts(status,lease_until,updated_at);
        """,
    )
    run_columns = _columns(conn, "automation_runs")
    for name, definition in (
        ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
        ("lease_until", "TEXT NOT NULL DEFAULT ''"),
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("terminal_source", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in run_columns:
            conn.execute(f"ALTER TABLE automation_runs ADD COLUMN {name} {definition}")
    conn.execute(
        "INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,0,?)",
        (RELIABILITY_FEATURE_FLAG, utc_now()),
    )


def inspect_reliability_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_tables = sorted(set(RELIABILITY_TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in RELIABILITY_TABLE_COLUMNS.items():
        if table in tables:
            missing = sorted(set(required) - _columns(conn, table))
            if missing:
                missing_columns[table] = missing
    automation_missing = (
        sorted(set(AUTOMATION_RUN_COLUMNS) - _columns(conn, "automation_runs"))
        if "automation_runs" in tables else list(AUTOMATION_RUN_COLUMNS)
    )
    flag = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (RELIABILITY_FEATURE_FLAG,),
    ).fetchone() if "assistant_feature_flags" in tables else None
    return {
        "ok": not missing_tables and not missing_columns and not automation_missing
        and not sorted(set(RELIABILITY_REQUIRED_INDEXES) - indexes) and flag is not None,
        "contract_checksum": RELIABILITY_MIGRATION_CHECKSUM,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "automation_run_missing_columns": automation_missing,
        "missing_indexes": sorted(set(RELIABILITY_REQUIRED_INDEXES) - indexes),
        "feature_flag_present": flag is not None,
    }


def require_reliability_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_reliability_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "task_message_reliability_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "RELIABILITY_FEATURE_FLAG", "RELIABILITY_MIGRATION_CHECKSUM",
    "apply_task_message_reliability_v2", "inspect_reliability_schema",
    "require_reliability_schema",
]
