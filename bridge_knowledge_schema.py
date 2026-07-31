#!/usr/bin/env python3
"""Gate C9 curated shared-knowledge schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import MigrationDriftError


KNOWLEDGE_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "assistant_knowledge_items": (
        "id", "assistant_id", "title", "content", "audience", "status",
        "source_type", "source_ref", "version", "created_by", "reviewed_by",
        "created_at", "updated_at", "published_at",
    ),
}
KNOWLEDGE_REQUIRED_INDEXES = (
    "idx_assistant_knowledge_status",
    "idx_assistant_knowledge_updated",
)


def _contract_payload() -> str:
    return json.dumps(
        {"columns": {k: list(v) for k, v in KNOWLEDGE_TABLE_COLUMNS.items()}, "indexes": list(KNOWLEDGE_REQUIRED_INDEXES)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


KNOWLEDGE_MIGRATION_CHECKSUM = hashlib.sha256(_contract_payload().encode("utf-8")).hexdigest()


def apply_assistant_knowledge_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE assistant_knowledge_items (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            audience TEXT NOT NULL CHECK(audience IN ('private_all','group_all','all_channels')),
            status TEXT NOT NULL CHECK(status IN ('draft','published','archived','rejected')),
            source_type TEXT NOT NULL DEFAULT 'admin',
            source_ref TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL,
            reviewed_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(assistant_id) REFERENCES assistant_instances(id)
        );
        CREATE INDEX idx_assistant_knowledge_status
        ON assistant_knowledge_items(assistant_id,status,audience,updated_at DESC);
        CREATE INDEX idx_assistant_knowledge_updated
        ON assistant_knowledge_items(updated_at DESC);
        """,
    )


def require_assistant_knowledge_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assistant_knowledge_items" not in tables:
        raise MigrationDriftError("assistant_knowledge_schema_drift:table")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(assistant_knowledge_items)")}
    missing_columns = sorted(set(KNOWLEDGE_TABLE_COLUMNS["assistant_knowledge_items"]) - columns)
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_indexes = sorted(set(KNOWLEDGE_REQUIRED_INDEXES) - indexes)
    if missing_columns or missing_indexes:
        raise MigrationDriftError(
            "assistant_knowledge_schema_drift:"
            + ";".join(filter(None, [
                "columns=" + ",".join(missing_columns) if missing_columns else "",
                "indexes=" + ",".join(missing_indexes) if missing_indexes else "",
            ]))
        )
    return {"ok": True, "contract_checksum": KNOWLEDGE_MIGRATION_CHECKSUM}


__all__ = [
    "KNOWLEDGE_MIGRATION_CHECKSUM",
    "apply_assistant_knowledge_v1",
    "require_assistant_knowledge_schema",
]
