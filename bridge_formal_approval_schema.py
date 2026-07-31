#!/usr/bin/env python3
"""Gate 6 additive schema for formal task approvals.

Approval state lives beside Task/Goal/Run in ``tasks.sqlite3`` so pausing and
resuming a task is one SQLite transaction.  The Assistant Core database owns
only the cutover feature flag.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import MigrationDriftError


FORMAL_APPROVAL_FEATURE_FLAG = "formal_approval_v2"

FORMAL_APPROVAL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "approval_requests": (
        "id",
        "approval_code",
        "goal_id",
        "run_id",
        "legacy_task_id",
        "actor_id",
        "assistant_id",
        "action_name",
        "action_arguments_json",
        "action_hash",
        "target_environment",
        "action_summary",
        "approval_version",
        "request_idempotency_key",
        "status",
        "allowed_decisions_json",
        "allowed_edit_fields_json",
        "requested_channel",
        "requested_by",
        "created_at",
        "updated_at",
        "expires_at",
        "decided_at",
        "decided_by",
        "decision_channel",
        "decision_kind",
        "decision_reason",
    ),
    "approval_decisions": (
        "id",
        "approval_id",
        "approval_version",
        "decision",
        "actor_id",
        "channel",
        "idempotency_key",
        "request_fingerprint",
        "edit_patch_json",
        "original_action_hash",
        "resulting_action_hash",
        "outcome",
        "reason",
        "created_at",
    ),
}

FORMAL_APPROVAL_INDEXES = (
    "idx_approval_requests_code",
    "idx_approval_requests_actor_status",
    "idx_approval_requests_run",
    "idx_approval_requests_task",
    "idx_approval_requests_task_pending",
    "idx_approval_requests_request_key",
    "idx_approval_decisions_approval",
    "idx_approval_decisions_idempotency",
)


def apply_formal_approval_v2(conn: sqlite3.Connection) -> None:
    """Create approval request and append-only decision records."""

    required = {"tasks", "goals", "runs", "run_events"}
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(required - tables)
    if missing:
        raise MigrationDriftError("formal_approval_prerequisite_missing:" + ",".join(missing))
    conn.execute(
        """
        CREATE TABLE approval_requests (
            id TEXT PRIMARY KEY,
            approval_code TEXT NOT NULL,
            goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
            legacy_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
            actor_id TEXT NOT NULL,
            assistant_id TEXT NOT NULL DEFAULT '',
            action_name TEXT NOT NULL,
            action_arguments_json TEXT NOT NULL CHECK(json_valid(action_arguments_json)),
            action_hash TEXT NOT NULL,
            target_environment TEXT NOT NULL,
            action_summary TEXT NOT NULL,
            approval_version INTEGER NOT NULL CHECK(approval_version >= 1),
            request_idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'pending','approved','rejected','expired','superseded'
            )),
            allowed_decisions_json TEXT NOT NULL CHECK(json_valid(allowed_decisions_json)),
            allowed_edit_fields_json TEXT NOT NULL CHECK(json_valid(allowed_edit_fields_json)),
            requested_channel TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT NOT NULL DEFAULT '',
            decided_by TEXT NOT NULL DEFAULT '',
            decision_channel TEXT NOT NULL DEFAULT '',
            decision_kind TEXT NOT NULL DEFAULT '',
            decision_reason TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_approval_requests_code ON approval_requests(approval_code)",
    )
    conn.execute(
        """
        CREATE INDEX idx_approval_requests_actor_status
        ON approval_requests(actor_id,status,created_at DESC)
        """,
    )
    conn.execute(
        "CREATE INDEX idx_approval_requests_run ON approval_requests(run_id,created_at DESC)",
    )
    conn.execute(
        "CREATE INDEX idx_approval_requests_task ON approval_requests(legacy_task_id,created_at DESC)",
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_approval_requests_task_pending
        ON approval_requests(legacy_task_id) WHERE status='pending'
        """,
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_approval_requests_request_key
        ON approval_requests(request_idempotency_key)
        """,
    )
    conn.execute(
        """
        CREATE TABLE approval_decisions (
            id TEXT PRIMARY KEY,
            approval_id TEXT NOT NULL REFERENCES approval_requests(id) ON DELETE RESTRICT,
            approval_version INTEGER NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('approve','edit','reject')),
            actor_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            edit_patch_json TEXT NOT NULL CHECK(json_valid(edit_patch_json)),
            original_action_hash TEXT NOT NULL,
            resulting_action_hash TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('applied','replayed')),
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE INDEX idx_approval_decisions_approval
        ON approval_decisions(approval_id,created_at DESC)
        """,
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_approval_decisions_idempotency
        ON approval_decisions(idempotency_key)
        """,
    )


def inspect_formal_approval_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_tables = sorted(set(FORMAL_APPROVAL_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in FORMAL_APPROVAL_COLUMNS.items():
        if table not in tables:
            continue
        present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - present)
        if missing:
            missing_columns[table] = missing
    missing_indexes = sorted(set(FORMAL_APPROVAL_INDEXES) - indexes)
    foreign_key_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    invalid_json = 0
    invalid_hashes = 0
    invalid_versions = 0
    if not missing_tables:
        invalid_json = int(
            conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM approval_requests
                   WHERE NOT json_valid(action_arguments_json)
                      OR NOT json_valid(allowed_decisions_json)
                      OR NOT json_valid(allowed_edit_fields_json))
                  +
                  (SELECT count(*) FROM approval_decisions
                   WHERE NOT json_valid(edit_patch_json))
                """,
            ).fetchone()[0],
        )
        invalid_hashes = int(
            conn.execute(
                """
                SELECT count(*) FROM approval_requests
                WHERE length(action_hash) <> 64
                """,
            ).fetchone()[0],
        )
        invalid_versions = int(
            conn.execute(
                "SELECT count(*) FROM approval_requests WHERE approval_version < 1",
            ).fetchone()[0],
        )
    ok = not (
        missing_tables
        or missing_columns
        or missing_indexes
        or foreign_key_errors
        or invalid_json
        or invalid_hashes
        or invalid_versions
    )
    return {
        "ok": ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "foreign_key_error_count": len(foreign_key_errors),
        "invalid_json": invalid_json,
        "invalid_action_hashes": invalid_hashes,
        "invalid_versions": invalid_versions,
    }


def require_formal_approval_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_formal_approval_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "formal_approval_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


FORMAL_APPROVAL_MIGRATION_CONTRACT = {
    "tables": {key: list(value) for key, value in FORMAL_APPROVAL_COLUMNS.items()},
    "indexes": list(FORMAL_APPROVAL_INDEXES),
    "feature_flag": FORMAL_APPROVAL_FEATURE_FLAG,
    "action_hash": "sha256_canonical_json",
    "task_resume": "waiting_approval_to_queued_same_transaction",
    "edit_fields": ["timeout_seconds"],
}

FORMAL_APPROVAL_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(
        FORMAL_APPROVAL_MIGRATION_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
).hexdigest()


__all__ = [
    "FORMAL_APPROVAL_FEATURE_FLAG",
    "FORMAL_APPROVAL_MIGRATION_CHECKSUM",
    "apply_formal_approval_v2",
    "inspect_formal_approval_schema",
    "require_formal_approval_schema",
]
