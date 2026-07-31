#!/usr/bin/env python3
"""Additive schema for Memory Intelligence and the Living Knowledge wiki."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


KNOWLEDGE_INTELLIGENCE_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'fact'",
    "summary": "TEXT NOT NULL DEFAULT ''",
    "tags_json": "TEXT NOT NULL DEFAULT '[]'",
    "confidence": "REAL NOT NULL DEFAULT 1.0",
    "source_memory_id": "TEXT NOT NULL DEFAULT ''",
    "source_thread_id": "TEXT NOT NULL DEFAULT ''",
    "source_scope_type": "TEXT NOT NULL DEFAULT ''",
    "consent_basis": "TEXT NOT NULL DEFAULT 'explicit'",
    "supersedes_id": "TEXT NOT NULL DEFAULT ''",
    "review_note": "TEXT NOT NULL DEFAULT ''",
}

CONTINUITY_TABLE_COLUMNS = {
    "memory_candidates": (
        "id", "assistant_id", "owner_actor_id", "subject_actor_ref",
        "scope_type", "scope_id", "kind", "content", "confidence",
        "consent_basis", "source_thread_id", "source_message_id", "status",
        "duplicate_of", "conflict_with", "reviewed_by", "created_at", "updated_at",
    ),
    "assistant_knowledge_relations": (
        "id", "assistant_id", "from_item_id", "to_item_id", "relation_type",
        "created_by", "created_at",
    ),
}

CONTINUITY_INDEXES = (
    "idx_memory_candidates_review",
    "idx_knowledge_kind_status",
    "idx_knowledge_relations_from",
    "idx_knowledge_relations_to",
)


def _contract_payload() -> str:
    return json.dumps(
        {
            "knowledge_columns": KNOWLEDGE_INTELLIGENCE_COLUMNS,
            "tables": {key: list(value) for key, value in CONTINUITY_TABLE_COLUMNS.items()},
            "indexes": list(CONTINUITY_INDEXES),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


CONTINUITY_MIGRATION_CHECKSUM = hashlib.sha256(_contract_payload().encode("utf-8")).hexdigest()


def apply_assistant_continuity_v1(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(assistant_knowledge_items)")}
    for name, definition in KNOWLEDGE_INTELLIGENCE_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE assistant_knowledge_items ADD COLUMN {name} {definition}")
    conn.executescript(
        """
        CREATE TABLE memory_candidates (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            owner_actor_id TEXT NOT NULL,
            subject_actor_ref TEXT NOT NULL DEFAULT '',
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            consent_basis TEXT NOT NULL,
            source_thread_id TEXT NOT NULL DEFAULT '',
            source_message_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','merged')),
            duplicate_of TEXT NOT NULL DEFAULT '',
            conflict_with TEXT NOT NULL DEFAULT '',
            reviewed_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_memory_candidates_review
        ON memory_candidates(assistant_id,status,updated_at DESC);

        CREATE TABLE assistant_knowledge_relations (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            from_item_id TEXT NOT NULL REFERENCES assistant_knowledge_items(id) ON DELETE RESTRICT,
            to_item_id TEXT NOT NULL REFERENCES assistant_knowledge_items(id) ON DELETE RESTRICT,
            relation_type TEXT NOT NULL CHECK(
                relation_type IN ('relates_to','depends_on','supersedes','implements','derived_from')
            ),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(assistant_id,from_item_id,to_item_id,relation_type)
        );
        CREATE INDEX idx_knowledge_kind_status
        ON assistant_knowledge_items(assistant_id,kind,status,updated_at DESC);
        CREATE INDEX idx_knowledge_relations_from
        ON assistant_knowledge_relations(assistant_id,from_item_id,created_at DESC);
        CREATE INDEX idx_knowledge_relations_to
        ON assistant_knowledge_relations(assistant_id,to_item_id,created_at DESC);
        """,
    )


def require_assistant_continuity_schema(conn: sqlite3.Connection) -> dict:
    knowledge_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(assistant_knowledge_items)")}
    missing_knowledge = sorted(set(KNOWLEDGE_INTELLIGENCE_COLUMNS) - knowledge_columns)
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing_tables = sorted(set(CONTINUITY_TABLE_COLUMNS) - tables)
    missing_columns = {}
    for table, required in CONTINUITY_TABLE_COLUMNS.items():
        if table not in tables:
            continue
        actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if missing := sorted(set(required) - actual):
            missing_columns[table] = missing
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_indexes = sorted(set(CONTINUITY_INDEXES) - indexes)
    if missing_knowledge or missing_tables or missing_columns or missing_indexes:
        raise MigrationDriftError(
            "assistant_continuity_schema_drift:"
            + json.dumps(
                {
                    "knowledge": missing_knowledge,
                    "tables": missing_tables,
                    "columns": missing_columns,
                    "indexes": missing_indexes,
                },
                sort_keys=True,
            )
        )
    return {"ok": True, "contract_checksum": CONTINUITY_MIGRATION_CHECKSUM}


__all__ = [
    "CONTINUITY_MIGRATION_CHECKSUM",
    "apply_assistant_continuity_v1",
    "require_assistant_continuity_schema",
]
