#!/usr/bin/env python3
"""Additive task-side schema for Goal revisions, checkpoints and feedback."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


GOAL_CONTINUITY_COLUMNS = {
    "goal_revisions": (
        "id", "goal_id", "revision_number", "parent_revision_id", "instruction",
        "status", "actor_id", "channel", "source_run_id", "feedback_json", "idempotency_key",
        "created_at", "updated_at",
    ),
    "run_checkpoints": (
        "id", "run_id", "step_key", "status", "summary", "payload_json", "created_at", "updated_at",
    ),
    "goal_feedback": (
        "id", "goal_id", "revision_id", "run_id", "artifact_id", "kind", "message",
        "actor_id", "channel", "idempotency_key", "experience_candidate_json", "created_at",
    ),
}

GOAL_CONTINUITY_INDEXES = (
    "idx_goal_revisions_goal",
    "idx_goal_revisions_status",
    "idx_goal_revisions_idempotency",
    "idx_run_checkpoints_run",
    "idx_goal_feedback_goal",
    "idx_goal_feedback_revision",
    "idx_goal_feedback_idempotency",
)


def _contract_payload() -> str:
    return json.dumps(
        {"tables": {key: list(value) for key, value in GOAL_CONTINUITY_COLUMNS.items()}, "indexes": list(GOAL_CONTINUITY_INDEXES)},
        sort_keys=True,
        separators=(",", ":"),
    )


GOAL_CONTINUITY_MIGRATION_CHECKSUM = hashlib.sha256(_contract_payload().encode("utf-8")).hexdigest()


def apply_goal_continuity_v1(conn: sqlite3.Connection) -> None:
    required = {"goals", "runs", "run_events"}
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - tables)
    if missing:
        raise MigrationDriftError("goal_continuity_prerequisite_missing:" + ",".join(missing))
    script = """
        CREATE TABLE goal_revisions (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
            parent_revision_id TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','superseded','accepted','rejected')),
            actor_id TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            source_run_id TEXT NOT NULL DEFAULT '',
            feedback_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(goal_id, revision_number),
            CHECK(json_valid(feedback_json))
        );
        CREATE INDEX idx_goal_revisions_goal ON goal_revisions(goal_id,revision_number DESC);
        CREATE INDEX idx_goal_revisions_status ON goal_revisions(goal_id,status,updated_at DESC);
        CREATE UNIQUE INDEX idx_goal_revisions_idempotency
        ON goal_revisions(idempotency_key) WHERE idempotency_key<>'';
        CREATE TABLE run_checkpoints (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            step_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','skipped')),
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id,step_key),
            CHECK(json_valid(payload_json))
        );
        CREATE INDEX idx_run_checkpoints_run ON run_checkpoints(run_id,updated_at DESC);
        CREATE TABLE goal_feedback (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            revision_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            artifact_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL CHECK(kind IN ('accepted','needs_change','rejected','corrected')),
            message TEXT NOT NULL DEFAULT '',
            actor_id TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            experience_candidate_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            CHECK(json_valid(experience_candidate_json))
        );
        CREATE INDEX idx_goal_feedback_goal ON goal_feedback(goal_id,created_at DESC);
        CREATE INDEX idx_goal_feedback_revision ON goal_feedback(revision_id,created_at DESC);
        CREATE UNIQUE INDEX idx_goal_feedback_idempotency ON goal_feedback(idempotency_key);
        """
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def inspect_goal_continuity_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing_tables = sorted(set(GOAL_CONTINUITY_COLUMNS) - tables)
    missing_columns = {}
    for table, required in GOAL_CONTINUITY_COLUMNS.items():
        if table not in tables:
            continue
        actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - actual)
        if missing:
            missing_columns[table] = missing
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_indexes = sorted(set(GOAL_CONTINUITY_INDEXES) - indexes)
    fk_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    ok = not (missing_tables or missing_columns or missing_indexes or fk_errors)
    return {
        "ok": ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "foreign_key_error_count": len(fk_errors),
        "contract_checksum": GOAL_CONTINUITY_MIGRATION_CHECKSUM,
    }


def require_goal_continuity_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_goal_continuity_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError("goal_continuity_schema_drift:" + json.dumps(audit, sort_keys=True, separators=(",", ":")))
    return audit


__all__ = [
    "GOAL_CONTINUITY_MIGRATION_CHECKSUM",
    "apply_goal_continuity_v1",
    "inspect_goal_continuity_schema",
    "require_goal_continuity_schema",
]
