#!/usr/bin/env python3
"""Gate 7 additive Artifact and isolated preview schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import MigrationDriftError


ARTIFACT_PREVIEW_FEATURE_FLAG = "artifact_preview_v2"

ARTIFACT_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "artifacts": (
        "id", "owner_id", "origin_assistant_id", "source_goal_id", "source_run_id",
        "kind", "title", "summary", "current_version_id", "row_version",
        "created_at", "updated_at", "deleted_at",
    ),
    "artifact_versions": (
        "id", "artifact_id", "version_number", "source_run_id", "storage_key",
        "entrypoint_path", "manifest_sha256", "file_count", "total_bytes", "state",
        "retention_expires_at", "failure_reason", "created_at", "published_at", "deleted_at",
    ),
    "artifact_version_files": (
        "version_id", "relative_path", "storage_name", "media_type", "size_bytes", "sha256",
    ),
    "artifact_events": (
        "id", "artifact_id", "version_id", "publication_id", "event_type",
        "detail_json", "created_at",
    ),
    "preview_publications": (
        "id", "version_id", "generation", "status", "preview_expires_at",
        "row_version", "created_at", "updated_at", "stopped_at", "deleted_at",
    ),
    "preview_access_grants": (
        "id", "publication_id", "generation", "token_hash", "challenge_hash",
        "challenge_expires_at", "status", "created_by", "created_at", "expires_at",
        "consumed_at", "revoked_at",
    ),
    "preview_sessions": (
        "id", "publication_id", "generation", "session_hash", "status",
        "created_at", "expires_at", "revoked_at", "last_used_at",
    ),
}

ARTIFACT_INDEXES = (
    "idx_artifacts_owner_updated", "idx_artifacts_source_goal", "idx_artifact_versions_artifact",
    "idx_artifact_versions_storage", "idx_artifact_files_storage", "idx_artifact_events_artifact",
    "idx_preview_publications_version", "idx_preview_grants_token", "idx_preview_grants_publication",
    "idx_preview_sessions_hash", "idx_preview_sessions_publication",
)

# Column names alone are not a sufficient migration contract: losing a CHECK,
# UNIQUE, FK or DEFERRABLE clause would leave the same table shape while
# weakening the Gate 7 security/lifecycle guarantees.  These normalized
# fragments are deliberately audited from sqlite_master before cutover.
ARTIFACT_CONSTRAINTS: Mapping[str, tuple[str, ...]] = {
    "artifacts": (
        "check(kind in ('file','report','presentation','image','archive','static_site'))",
        "check(row_version >= 1)",
        "foreign key(id,current_version_id) references artifact_versions(artifact_id,id) deferrable initially deferred",
    ),
    "artifact_versions": (
        "check(version_number >= 1)", "check(file_count >= 0)", "check(total_bytes >= 0)",
        "check(state in ('preparing','available','failed'))", "unique(artifact_id,version_number)",
        "unique(artifact_id,id)", "unique(storage_key)",
        "artifact_id text not null references artifacts(id) on delete restrict",
    ),
    "artifact_version_files": (
        "check(size_bytes >= 0)", "check(length(sha256)=64)",
        "primary key(version_id,relative_path)", "unique(version_id,storage_name)",
        "version_id text not null references artifact_versions(id) on delete restrict",
    ),
    "artifact_events": (
        "check(json_valid(detail_json))",
        "artifact_id text not null references artifacts(id) on delete restrict",
    ),
    "preview_publications": (
        "unique references artifact_versions(id) on delete restrict", "check(generation >= 1)",
        "check(status in ('active','stopped','deleted'))", "check(row_version >= 1)",
    ),
    "preview_access_grants": (
        "publication_id text not null references preview_publications(id) on delete restrict",
        "check(generation >= 1)", "check(length(token_hash)=64)",
        "check(status in ('issued','consumed','revoked'))",
    ),
    "preview_sessions": (
        "publication_id text not null references preview_publications(id) on delete restrict",
        "check(generation >= 1)", "check(length(session_hash)=64)",
        "check(status in ('active','revoked'))",
    ),
}


def _normalize_schema_sql(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def apply_artifact_preview_v2(conn: sqlite3.Connection) -> None:
    required = {"goals", "runs", "tasks", "run_events"}
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - tables)
    if missing:
        raise MigrationDriftError("artifact_prerequisite_missing:" + ",".join(missing))
    conn.execute(
        """
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            origin_assistant_id TEXT NOT NULL DEFAULT '',
            source_goal_id TEXT NOT NULL DEFAULT '',
            source_run_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL CHECK(kind IN ('file','report','presentation','image','archive','static_site')),
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            current_version_id TEXT,
            row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(id,current_version_id)
                REFERENCES artifact_versions(artifact_id,id) DEFERRABLE INITIALLY DEFERRED
        )
        """,
    )
    conn.execute("CREATE INDEX idx_artifacts_owner_updated ON artifacts(owner_id,updated_at DESC)")
    conn.execute("CREATE INDEX idx_artifacts_source_goal ON artifacts(source_goal_id,updated_at DESC)")
    conn.execute(
        """
        CREATE TABLE artifact_versions (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
            version_number INTEGER NOT NULL CHECK(version_number >= 1),
            source_run_id TEXT NOT NULL DEFAULT '',
            storage_key TEXT NOT NULL,
            entrypoint_path TEXT NOT NULL DEFAULT '',
            manifest_sha256 TEXT NOT NULL DEFAULT '',
            file_count INTEGER NOT NULL DEFAULT 0 CHECK(file_count >= 0),
            total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
            state TEXT NOT NULL CHECK(state IN ('preparing','available','failed')),
            retention_expires_at TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT NOT NULL DEFAULT '',
            UNIQUE(artifact_id,version_number),
            UNIQUE(artifact_id,id),
            UNIQUE(storage_key)
        )
        """,
    )
    conn.execute("CREATE INDEX idx_artifact_versions_artifact ON artifact_versions(artifact_id,version_number DESC)")
    conn.execute("CREATE UNIQUE INDEX idx_artifact_versions_storage ON artifact_versions(storage_key)")
    conn.execute(
        """
        CREATE TABLE artifact_version_files (
            version_id TEXT NOT NULL REFERENCES artifact_versions(id) ON DELETE RESTRICT,
            relative_path TEXT NOT NULL,
            storage_name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64),
            PRIMARY KEY(version_id,relative_path),
            UNIQUE(version_id,storage_name)
        )
        """,
    )
    conn.execute("CREATE INDEX idx_artifact_files_storage ON artifact_version_files(version_id,storage_name)")
    conn.execute(
        """
        CREATE TABLE preview_publications (
            id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL UNIQUE REFERENCES artifact_versions(id) ON DELETE RESTRICT,
            generation INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
            status TEXT NOT NULL CHECK(status IN ('active','stopped','deleted')),
            preview_expires_at TEXT NOT NULL,
            row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stopped_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    conn.execute("CREATE INDEX idx_preview_publications_version ON preview_publications(version_id)")
    conn.execute(
        """
        CREATE TABLE preview_access_grants (
            id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL REFERENCES preview_publications(id) ON DELETE RESTRICT,
            generation INTEGER NOT NULL CHECK(generation >= 1),
            token_hash TEXT NOT NULL CHECK(length(token_hash)=64),
            challenge_hash TEXT NOT NULL DEFAULT '',
            challenge_expires_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('issued','consumed','revoked')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT NOT NULL DEFAULT '',
            revoked_at TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    conn.execute("CREATE UNIQUE INDEX idx_preview_grants_token ON preview_access_grants(token_hash)")
    conn.execute("CREATE INDEX idx_preview_grants_publication ON preview_access_grants(publication_id,generation,status)")
    conn.execute(
        """
        CREATE TABLE preview_sessions (
            id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL REFERENCES preview_publications(id) ON DELETE RESTRICT,
            generation INTEGER NOT NULL CHECK(generation >= 1),
            session_hash TEXT NOT NULL CHECK(length(session_hash)=64),
            status TEXT NOT NULL CHECK(status IN ('active','revoked')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    conn.execute("CREATE UNIQUE INDEX idx_preview_sessions_hash ON preview_sessions(session_hash)")
    conn.execute("CREATE INDEX idx_preview_sessions_publication ON preview_sessions(publication_id,generation,status)")
    conn.execute(
        """
        CREATE TABLE artifact_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL DEFAULT '',
            publication_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json)),
            created_at TEXT NOT NULL
        )
        """,
    )
    conn.execute("CREATE INDEX idx_artifact_events_artifact ON artifact_events(artifact_id,id DESC)")


def inspect_artifact_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_tables = sorted(set(ARTIFACT_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in ARTIFACT_COLUMNS.items():
        if table not in tables:
            continue
        present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - present)
        if missing:
            missing_columns[table] = missing
    missing_indexes = sorted(set(ARTIFACT_INDEXES) - indexes)
    missing_constraints: dict[str, list[str]] = {}
    for table, required in ARTIFACT_CONSTRAINTS.items():
        if table not in tables:
            continue
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        definition = _normalize_schema_sql(str(row[0] if row else ""))
        missing = [fragment for fragment in required if _normalize_schema_sql(fragment) not in definition]
        if missing:
            missing_constraints[table] = missing
    fk_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    invalid_hashes = 0
    invalid_current_versions = 0
    if not missing_tables:
        invalid_hashes = int(conn.execute(
            """
            SELECT
              (SELECT count(*) FROM artifact_version_files WHERE length(sha256)<>64)
              + (SELECT count(*) FROM preview_access_grants WHERE length(token_hash)<>64)
              + (SELECT count(*) FROM preview_sessions WHERE length(session_hash)<>64)
            """,
        ).fetchone()[0])
        invalid_current_versions = int(conn.execute(
            """
            SELECT count(*) FROM artifacts a
            WHERE a.current_version_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM artifact_versions v WHERE v.id=a.current_version_id AND v.artifact_id=a.id
            )
            """,
        ).fetchone()[0])
    ok = not (
        missing_tables or missing_columns or missing_indexes or missing_constraints
        or fk_errors or invalid_hashes or invalid_current_versions
    )
    return {
        "ok": ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "missing_constraints": missing_constraints,
        "foreign_key_error_count": len(fk_errors),
        "invalid_hashes": invalid_hashes,
        "invalid_current_versions": invalid_current_versions,
    }


def require_artifact_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_artifact_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError("artifact_schema_drift:" + json.dumps(audit, sort_keys=True, separators=(",", ":")))
    return audit


ARTIFACT_MIGRATION_CONTRACT = {
    "tables": {key: list(value) for key, value in ARTIFACT_COLUMNS.items()},
    "indexes": list(ARTIFACT_INDEXES),
    "constraints": {key: list(value) for key, value in ARTIFACT_CONSTRAINTS.items()},
    "feature_flag": ARTIFACT_PREVIEW_FEATURE_FLAG,
    "version_bytes": "immutable",
    "preview_authority": "tasks_sqlite_via_unix_broker",
    "publication_restore": "increment_generation",
}

ARTIFACT_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(ARTIFACT_MIGRATION_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "ARTIFACT_MIGRATION_CHECKSUM", "ARTIFACT_PREVIEW_FEATURE_FLAG", "apply_artifact_preview_v2",
    "inspect_artifact_schema", "require_artifact_schema",
]
