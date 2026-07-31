#!/usr/bin/env python3
"""Additive schema for the unified Assistant Learning Continuity Plane."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


LEARNING_FEATURE_FLAG = "learning_continuity_v1"
LOW_RISK_LEARNING_FEATURE_FLAG = "low_risk_learning_v1"
OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG = "owner_group_expression_feedback_v1"

LEARNING_TABLE_COLUMNS = {
    "learning_signals": (
        "id", "assistant_id", "actor_ref", "channel_type", "thread_id",
        "group_id", "source_message_id", "signal_type", "domain",
        "payload_json", "confidence", "sensitivity", "consent_basis",
        "idempotency_key", "created_at",
    ),
    "learning_candidates": (
        "id", "assistant_id", "owner_actor_id", "subject_type", "subject_id",
        "scope_type", "scope_id", "domain", "candidate_key", "value_json",
        "status", "risk_level", "confidence", "evidence_count",
        "source_signal_id", "conflict_with", "supersedes_id",
        "trial_expires_at", "created_at", "updated_at",
    ),
    "learning_applications": (
        "id", "candidate_id", "assistant_id", "target_type", "target_id",
        "previous_value_json", "applied_value_json", "status", "reason",
        "applied_at", "reverted_at",
    ),
    "learning_feedback": (
        "id", "candidate_id", "application_id", "feedback_type", "actor_ref",
        "note", "idempotency_key", "created_at",
    ),
    "learning_context_trace": (
        "id", "assistant_id", "thread_id", "message_id", "domain",
        "source_type", "source_id", "decision", "detail_json", "created_at",
    ),
}

LEARNING_INDEXES = (
    "idx_learning_signals_thread",
    "idx_learning_candidates_status",
    "idx_learning_candidates_subject",
    "idx_learning_applications_candidate",
    "idx_learning_feedback_candidate",
    "idx_learning_trace_thread",
    "idx_learning_candidate_key",
)


def _contract_payload() -> str:
    return json.dumps(
        {
            "flags": [LEARNING_FEATURE_FLAG, LOW_RISK_LEARNING_FEATURE_FLAG],
            "tables": {key: list(value) for key, value in LEARNING_TABLE_COLUMNS.items()},
            "indexes": list(LEARNING_INDEXES),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


LEARNING_MIGRATION_CHECKSUM = hashlib.sha256(_contract_payload().encode("utf-8")).hexdigest()


def _policy_v2_contract_payload() -> str:
    return json.dumps(
        {
            "feature_flag": OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG,
            "default_enabled": False,
            "scope": "owner explicit group expression feedback only",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


LEARNING_POLICY_V2_MIGRATION_CHECKSUM = hashlib.sha256(
    _policy_v2_contract_payload().encode("utf-8"),
).hexdigest()


def apply_learning_continuity_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE learning_signals (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            actor_ref TEXT NOT NULL DEFAULT '',
            channel_type TEXT NOT NULL DEFAULT '',
            thread_id TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            source_message_id TEXT NOT NULL DEFAULT '',
            signal_type TEXT NOT NULL,
            domain TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            confidence REAL NOT NULL DEFAULT 0.5,
            sensitivity TEXT NOT NULL DEFAULT 'normal'
                CHECK(sensitivity IN ('normal','private','sensitive')),
            consent_basis TEXT NOT NULL DEFAULT 'inferred',
            idempotency_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_learning_signals_idempotency
        ON learning_signals(assistant_id,idempotency_key)
        WHERE idempotency_key <> '';
        CREATE INDEX idx_learning_signals_thread
        ON learning_signals(assistant_id,thread_id,created_at DESC);

        CREATE TABLE learning_candidates (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            owner_actor_id TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL DEFAULT '',
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            value_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK(status IN (
                'observed','candidate','trial','stable','needs_confirmation',
                'conflicted','paused','rejected','expired','superseded'
            )),
            risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
            confidence REAL NOT NULL DEFAULT 0.5,
            evidence_count INTEGER NOT NULL DEFAULT 1,
            source_signal_id TEXT NOT NULL DEFAULT '',
            conflict_with TEXT NOT NULL DEFAULT '',
            supersedes_id TEXT NOT NULL DEFAULT '',
            trial_expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_learning_candidates_status
        ON learning_candidates(assistant_id,status,updated_at DESC);
        CREATE INDEX idx_learning_candidates_subject
        ON learning_candidates(assistant_id,subject_type,subject_id,status,updated_at DESC);
        CREATE UNIQUE INDEX idx_learning_candidate_key
        ON learning_candidates(assistant_id,subject_type,subject_id,domain,candidate_key)
        WHERE status NOT IN ('rejected','expired','superseded');

        CREATE TABLE learning_applications (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES learning_candidates(id) ON DELETE RESTRICT,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            previous_value_json TEXT NOT NULL DEFAULT '{}',
            applied_value_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK(status IN ('trial','accepted','reverted','rejected')),
            reason TEXT NOT NULL DEFAULT '',
            applied_at TEXT NOT NULL,
            reverted_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_learning_applications_candidate
        ON learning_applications(candidate_id,applied_at DESC);

        CREATE TABLE learning_feedback (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES learning_candidates(id) ON DELETE RESTRICT,
            application_id TEXT NOT NULL DEFAULT '',
            feedback_type TEXT NOT NULL CHECK(feedback_type IN ('accept','reject','undo','correct')),
            actor_ref TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_learning_feedback_idempotency
        ON learning_feedback(candidate_id,idempotency_key)
        WHERE idempotency_key <> '';
        CREATE INDEX idx_learning_feedback_candidate
        ON learning_feedback(candidate_id,created_at DESC);

        CREATE TABLE learning_context_trace (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            thread_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_learning_trace_thread
        ON learning_context_trace(assistant_id,thread_id,created_at DESC);

        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('learning_continuity_v1',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('low_risk_learning_v1',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


def apply_learning_policy_v2(conn: sqlite3.Connection) -> None:
    """Add the opt-in Owner group-feedback gate without changing existing data."""

    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """,
        (OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG, 0),
    )


def require_learning_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing_tables = sorted(set(LEARNING_TABLE_COLUMNS) - tables)
    missing_columns = {}
    for table, required in LEARNING_TABLE_COLUMNS.items():
        if table not in tables:
            continue
        actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - actual)
        if missing:
            missing_columns[table] = missing
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_indexes = sorted(set(LEARNING_INDEXES) - indexes)
    if missing_tables or missing_columns or missing_indexes:
        raise MigrationDriftError(
            "assistant_learning_schema_drift:" + json.dumps(
                {"tables": missing_tables, "columns": missing_columns, "indexes": missing_indexes},
                sort_keys=True,
            ),
        )
    return {
        "ok": True,
        "contract_checksum": LEARNING_MIGRATION_CHECKSUM,
        "feature_flags": [
            LEARNING_FEATURE_FLAG,
            LOW_RISK_LEARNING_FEATURE_FLAG,
            OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG,
        ],
    }


__all__ = [
    "LEARNING_FEATURE_FLAG",
    "LOW_RISK_LEARNING_FEATURE_FLAG",
    "OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG",
    "LEARNING_MIGRATION_CHECKSUM",
    "LEARNING_POLICY_V2_MIGRATION_CHECKSUM",
    "apply_learning_continuity_v1",
    "apply_learning_policy_v2",
    "require_learning_schema",
]
