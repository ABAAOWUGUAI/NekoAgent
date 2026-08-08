"""Knowledge ingestion operational metadata schema (C2).

These tables store only connector configuration, file identity, scan cursors,
run status and error categories.  Business knowledge body always lives in
``assistant_knowledge_items`` Drafts; these tables never become a second
Published-knowledge truth source.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now

from bridge_knowledge_ingestion import SOURCE_TYPES

FEATURE_FLAG = "knowledge_ingestion_v1"

SOURCE_COLUMNS = (
    "id", "source_type", "root_path", "enabled", "config_revision",
    "config_json", "created_at", "updated_at",
)
DOCUMENT_COLUMNS = (
    "source_id", "file_path", "file_sha256", "size_bytes", "mtime_iso",
    "first_seen_at", "last_seen_at", "last_content_hash", "status",
    "error_kind", "superseded_item_id", "superseded_at",
)
RUN_COLUMNS = (
    "id", "source_id", "config_revision", "started_at", "finished_at",
    "duration_seconds", "discovered", "unchanged", "changed", "deleted",
    "failed", "chunks", "candidates", "drafts", "conflicts", "rejected",
    "stop_reason", "error_kind",
)

INGESTION_TABLE_COLUMNS = {
    "assistant_knowledge_sources": SOURCE_COLUMNS,
    "assistant_knowledge_source_documents": DOCUMENT_COLUMNS,
    "assistant_knowledge_ingestion_runs": RUN_COLUMNS,
}

INGESTION_INDEXES = (
    "idx_knowledge_sources_type",
    "idx_knowledge_source_documents_file",
    "idx_knowledge_source_documents_status",
    "idx_knowledge_ingestion_runs_recent",
)


def _contract_payload() -> str:
    return json.dumps(
        {
            "feature_flag": FEATURE_FLAG,
            "tables": {k: list(v) for k, v in INGESTION_TABLE_COLUMNS.items()},
            "indexes": list(INGESTION_INDEXES),
            "source_types": sorted(SOURCE_TYPES),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


KNOWLEDGE_INGESTION_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def apply_knowledge_ingestion_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE assistant_knowledge_sources (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL CHECK(source_type IN ('obsidian_vault','llm_wiki_export')),
            root_path TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            config_revision INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(config_json)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_knowledge_sources_type
        ON assistant_knowledge_sources(source_type,enabled);

        CREATE TABLE assistant_knowledge_source_documents (
            source_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            mtime_iso TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_content_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','missing','over_budget','encoding_invalid','ignored')),
            error_kind TEXT NOT NULL DEFAULT '',
            superseded_item_id TEXT NOT NULL DEFAULT '',
            superseded_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(source_id,file_path),
            FOREIGN KEY(source_id) REFERENCES assistant_knowledge_sources(id)
        );
        CREATE INDEX idx_knowledge_source_documents_file
        ON assistant_knowledge_source_documents(file_sha256,status);
        CREATE INDEX idx_knowledge_source_documents_status
        ON assistant_knowledge_source_documents(source_id,status,last_seen_at);

        CREATE TABLE assistant_knowledge_ingestion_runs (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            config_revision INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            duration_seconds REAL NOT NULL DEFAULT 0,
            discovered INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            changed INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            chunks INTEGER NOT NULL DEFAULT 0,
            candidates INTEGER NOT NULL DEFAULT 0,
            drafts INTEGER NOT NULL DEFAULT 0,
            conflicts INTEGER NOT NULL DEFAULT 0,
            rejected INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT NOT NULL DEFAULT '',
            error_kind TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(source_id) REFERENCES assistant_knowledge_sources(id)
        );
        CREATE INDEX idx_knowledge_ingestion_runs_recent
        ON assistant_knowledge_ingestion_runs(source_id,started_at DESC);
        """
    )
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?) ON CONFLICT(name) DO NOTHING
        """,
        (FEATURE_FLAG, utc_now()),
    )


def _schema_parts(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    columns: set[str] = set()
    for table in INGESTION_TABLE_COLUMNS:
        if table in tables:
            columns.update(
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})")
            )
    return tables, columns, indexes


def require_knowledge_ingestion_schema(conn: sqlite3.Connection) -> dict:
    tables, columns, indexes = _schema_parts(conn)
    missing_tables = sorted(set(INGESTION_TABLE_COLUMNS) - tables)
    missing_columns = sorted(
        set(sum(INGESTION_TABLE_COLUMNS.values(), ())) - columns
    )
    missing_indexes = sorted(set(INGESTION_INDEXES) - indexes)
    if missing_tables or missing_columns or missing_indexes:
        raise MigrationDriftError(
            "knowledge_ingestion_schema_drift:"
            + ";".join(
                filter(
                    None,
                    [
                        "tables=" + ",".join(missing_tables) if missing_tables else "",
                        "columns=" + ",".join(missing_columns) if missing_columns else "",
                        "indexes=" + ",".join(missing_indexes) if missing_indexes else "",
                    ],
                )
            )
        )
    return {
        "ok": True,
        "contract_checksum": KNOWLEDGE_INGESTION_MIGRATION_CHECKSUM,
    }


__all__ = [
    "FEATURE_FLAG",
    "KNOWLEDGE_INGESTION_MIGRATION_CHECKSUM",
    "apply_knowledge_ingestion_v1",
    "require_knowledge_ingestion_schema",
]
