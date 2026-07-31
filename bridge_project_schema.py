#!/usr/bin/env python3
"""Product Gate C8 additive Project lifecycle schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


PROJECT_COLUMNS = (
    "id", "name", "path", "description", "active", "created_at", "updated_at",
    "archived_at", "lifecycle_version",
)
PROJECT_EVENT_COLUMNS = (
    "id", "project_id", "event_type", "actor_type", "previous_name", "new_name",
    "created_at",
)
PROJECT_INDEXES = (
    "idx_projects_active_updated",
    "idx_project_lifecycle_events_project",
)


def _checksum() -> str:
    payload = json.dumps(
        {
            "project_columns": list(PROJECT_COLUMNS),
            "event_columns": list(PROJECT_EVENT_COLUMNS),
            "indexes": list(PROJECT_INDEXES),
            "states": ["active", "archived"],
            "stable_fields": ["id", "path"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


PROJECT_LIFECYCLE_MIGRATION_CHECKSUM = _checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_project_lifecycle_v2(conn: sqlite3.Connection) -> None:
    """Add reversible archive metadata and append-only lifecycle events."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'",
    ).fetchone()
    if not table:
        raise MigrationDriftError("project_table_missing")
    existing = _columns(conn, "projects")
    if "description" not in existing:
        conn.execute("ALTER TABLE projects ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "archived_at" not in existing:
        conn.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT NOT NULL DEFAULT ''")
    if "lifecycle_version" not in existing:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN lifecycle_version INTEGER NOT NULL DEFAULT 1",
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL
                CHECK(event_type IN ('created','renamed','archived','restored')),
            actor_type TEXT NOT NULL DEFAULT 'admin',
            previous_name TEXT NOT NULL DEFAULT '',
            new_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_projects_active_updated
        ON projects(active,updated_at DESC,id);

        CREATE INDEX IF NOT EXISTS idx_project_lifecycle_events_project
        ON project_lifecycle_events(project_id,created_at DESC,id DESC);
        """,
    )
    conn.execute(
        "UPDATE projects SET lifecycle_version=1 WHERE lifecycle_version IS NULL OR lifecycle_version<1",
    )
    conn.execute(
        "UPDATE projects SET archived_at='' WHERE active=1 AND archived_at<>''",
    )


def inspect_project_lifecycle_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    project_missing = sorted(
        set(PROJECT_COLUMNS) - _columns(conn, "projects")
    ) if "projects" in tables else list(PROJECT_COLUMNS)
    event_missing = sorted(
        set(PROJECT_EVENT_COLUMNS) - _columns(conn, "project_lifecycle_events")
    ) if "project_lifecycle_events" in tables else list(PROJECT_EVENT_COLUMNS)
    missing_indexes = sorted(set(PROJECT_INDEXES) - indexes)
    return {
        "ok": not project_missing and not event_missing and not missing_indexes,
        "contract_checksum": PROJECT_LIFECYCLE_MIGRATION_CHECKSUM,
        "project_missing_columns": project_missing,
        "event_missing_columns": event_missing,
        "missing_indexes": missing_indexes,
    }


def require_project_lifecycle_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_project_lifecycle_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "project_lifecycle_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "PROJECT_LIFECYCLE_MIGRATION_CHECKSUM",
    "apply_project_lifecycle_v2",
    "inspect_project_lifecycle_schema",
    "require_project_lifecycle_schema",
]
