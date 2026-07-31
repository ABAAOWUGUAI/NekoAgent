#!/usr/bin/env python3
"""Assistant Core v30: Automation conversation and execution contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_automation_contracts import (
    DEFAULT_OUTPUT_CONTRACT,
    normalize_output_contract,
    output_contract_hash,
)
from bridge_migrations import MigrationDriftError, utc_now


AUTOMATION_CONVERSATION_FEATURE_FLAG = "automation_conversation_contract_v1"
_REQUIRED_JOB_COLUMNS = {
    "revision",
    "output_contract_json",
    "output_contract_hash",
}
_REQUIRED_RUN_COLUMNS = {
    "job_revision",
    "config_hash",
    "output_contract_hash",
}


def _checksum() -> str:
    payload = {
        "feature": AUTOMATION_CONVERSATION_FEATURE_FLAG,
        "job_columns": sorted(_REQUIRED_JOB_COLUMNS),
        "run_columns": sorted(_REQUIRED_RUN_COLUMNS),
        "interaction_plan_schema_versions": [1, 2],
        "plan_table": [
            "id", "actor_id", "thread_ref", "source_message_id", "target_job_id",
            "target_revision", "status", "plan_json", "receipts_json",
            "clarification_key", "created_at", "updated_at", "completed_at",
        ],
        "output_contract": DEFAULT_OUTPUT_CONTRACT,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()


AUTOMATION_CONVERSATION_MIGRATION_CHECKSUM = _checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _upgrade_interaction_plan_contract(conn: sqlite3.Connection) -> None:
    """Allow persisted plans to use the additive v2 dependency contract.

    SQLite cannot alter a CHECK constraint in place.  Rebuild the table while
    preserving identifiers, message bindings, hashes, and timestamps.  Version
    1 rows remain valid and readable; new rows may use version 2.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='interaction_plans'",
    ).fetchone()
    if not row:
        raise MigrationDriftError("interaction_plan_source_schema_missing")
    source_sql = str(row[0] or "").replace(" ", "").lower()
    if "schema_versionin(1,2)" in source_sql:
        return
    conn.executescript(
        """
        CREATE TABLE interaction_plans_v30 (
            id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            thread_id TEXT NOT NULL
                REFERENCES conversation_threads(id) ON DELETE RESTRICT,
            request_message_id TEXT
                REFERENCES conversation_messages(id) ON DELETE RESTRICT,
            schema_version INTEGER NOT NULL CHECK(schema_version IN (1,2)),
            status TEXT NOT NULL CHECK(
                status IN ('planned','dispatched','completed','failed','cancelled')
            ),
            summary_mode TEXT NOT NULL CHECK(summary_mode IN ('daily','work','mixed')),
            primary_intent TEXT NOT NULL,
            intent_count INTEGER NOT NULL CHECK(intent_count BETWEEN 1 AND 8),
            action_count INTEGER NOT NULL CHECK(action_count BETWEEN 0 AND 12),
            plan_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            classifier_source TEXT NOT NULL,
            origin_channel TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO interaction_plans_v30
        SELECT * FROM interaction_plans;
        DROP TABLE interaction_plans;
        ALTER TABLE interaction_plans_v30 RENAME TO interaction_plans;
        CREATE INDEX idx_interaction_plans_owner
        ON interaction_plans(owner_actor_id,status,updated_at DESC);
        CREATE INDEX idx_interaction_plans_thread
        ON interaction_plans(thread_id,created_at DESC);
        CREATE UNIQUE INDEX idx_interaction_plans_message
        ON interaction_plans(request_message_id)
        WHERE request_message_id IS NOT NULL;
        CREATE INDEX idx_interaction_plans_hash
        ON interaction_plans(plan_hash,created_at DESC);
        """,
    )


def apply_automation_conversation_v1(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "automation_jobs" not in tables or "automation_runs" not in tables:
        raise MigrationDriftError("automation_source_schema_missing")
    _upgrade_interaction_plan_contract(conn)
    job_columns = _columns(conn, "automation_jobs")
    for name, definition in (
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ("output_contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("output_contract_hash", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in job_columns:
            conn.execute(f"ALTER TABLE automation_jobs ADD COLUMN {name} {definition}")
    run_columns = _columns(conn, "automation_runs")
    for name, definition in (
        ("job_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("config_hash", "TEXT NOT NULL DEFAULT ''"),
        ("output_contract_hash", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in run_columns:
            conn.execute(f"ALTER TABLE automation_runs ADD COLUMN {name} {definition}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS automation_action_plans (
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            thread_ref TEXT NOT NULL,
            source_message_id TEXT NOT NULL DEFAULT '',
            target_job_id TEXT,
            target_revision INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            receipts_json TEXT NOT NULL DEFAULT '[]',
            clarification_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(target_job_id) REFERENCES automation_jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_automation_action_plans_open
        ON automation_action_plans(actor_id,thread_ref,status,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_automation_action_plans_job
        ON automation_action_plans(target_job_id,updated_at DESC);
        """,
    )
    now = utc_now()
    contract = normalize_output_contract(DEFAULT_OUTPUT_CONTRACT)
    contract_json = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    contract_hash = output_contract_hash(contract)
    conn.execute(
        """UPDATE automation_jobs
           SET revision=CASE WHEN revision<1 THEN 1 ELSE revision END,
               output_contract_json=CASE WHEN output_contract_json='' OR output_contract_json='{}'
                                         THEN ? ELSE output_contract_json END,
               output_contract_hash=CASE WHEN output_contract_hash='' THEN ? ELSE output_contract_hash END""",
        (contract_json, contract_hash),
    )
    conn.execute(
        """INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
           VALUES(?,?,?)""",
        (AUTOMATION_CONVERSATION_FEATURE_FLAG, 0, now),
    )


def require_automation_conversation_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "automation_action_plans" not in tables:
        raise MigrationDriftError("automation_action_plans_missing")
    missing_jobs = sorted(_REQUIRED_JOB_COLUMNS - _columns(conn, "automation_jobs"))
    missing_runs = sorted(_REQUIRED_RUN_COLUMNS - _columns(conn, "automation_runs"))
    if missing_jobs or missing_runs:
        raise MigrationDriftError(
            "automation_conversation_schema_drift:jobs=" + ",".join(missing_jobs)
            + ";runs=" + ",".join(missing_runs),
        )
    interaction_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='interaction_plans'",
    ).fetchone()
    interaction_sql = str(interaction_sql_row[0] if interaction_sql_row else "").replace(" ", "").lower()
    if "schema_versionin(1,2)" not in interaction_sql:
        raise MigrationDriftError("interaction_plan_v2_contract_missing")
    return {
        "ok": True,
        "feature_flag": AUTOMATION_CONVERSATION_FEATURE_FLAG,
        "job_columns": sorted(_REQUIRED_JOB_COLUMNS),
        "run_columns": sorted(_REQUIRED_RUN_COLUMNS),
    }


def set_automation_conversation_enabled(conn: sqlite3.Connection, enabled: bool) -> dict:
    require_automation_conversation_schema(conn)
    now = utc_now()
    conn.execute(
        """INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
           ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
        (AUTOMATION_CONVERSATION_FEATURE_FLAG, 1 if enabled else 0, now),
    )
    return {"name": AUTOMATION_CONVERSATION_FEATURE_FLAG, "enabled": bool(enabled), "updated_at": now}


__all__ = [
    "AUTOMATION_CONVERSATION_FEATURE_FLAG",
    "AUTOMATION_CONVERSATION_MIGRATION_CHECKSUM",
    "apply_automation_conversation_v1",
    "require_automation_conversation_schema",
    "set_automation_conversation_enabled",
]
