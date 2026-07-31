#!/usr/bin/env python3
"""Gate C2 additive schema for QQ object ownership and actor project state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import MigrationDriftError, utc_now


QQ_OBJECT_FEATURE_FLAG = "qq_object_authorization_v2"

QQ_OBJECT_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "qq_project_owners": (
        "project_id", "identity_id", "created_by", "created_at",
    ),
    "qq_actor_project_bindings": (
        "identity_id", "project_id", "updated_at",
    ),
}

QQ_OBJECT_REQUIRED_INDEXES = (
    "idx_qq_project_owners_identity",
    "idx_qq_actor_project_project",
)


def _contract_checksum() -> str:
    payload = json.dumps(
        {
            "tables": {key: list(value) for key, value in QQ_OBJECT_TABLE_COLUMNS.items()},
            "indexes": list(QQ_OBJECT_REQUIRED_INDEXES),
            "feature_flag": QQ_OBJECT_FEATURE_FLAG,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


QQ_OBJECT_MIGRATION_CHECKSUM = _contract_checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_qq_object_authorization_v2(conn: sqlite3.Connection) -> None:
    """Create the C2 schema without assigning legacy projects to QQ users."""

    conn.executescript(
        """
        CREATE TABLE qq_project_owners (
            project_id TEXT PRIMARY KEY
                REFERENCES projects(id) ON DELETE RESTRICT,
            identity_id TEXT NOT NULL
                REFERENCES qq_identities(id) ON DELETE RESTRICT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE qq_actor_project_bindings (
            identity_id TEXT PRIMARY KEY
                REFERENCES qq_identities(id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL
                REFERENCES projects(id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX idx_qq_project_owners_identity
        ON qq_project_owners(identity_id,project_id);

        CREATE INDEX idx_qq_actor_project_project
        ON qq_actor_project_bindings(project_id,identity_id);
        """,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        """,
        (QQ_OBJECT_FEATURE_FLAG, utc_now()),
    )


def inspect_qq_object_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_tables = sorted(set(QQ_OBJECT_TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in QQ_OBJECT_TABLE_COLUMNS.items():
        if table in tables:
            missing = sorted(set(required) - _columns(conn, table))
            if missing:
                missing_columns[table] = missing
    missing_indexes = sorted(set(QQ_OBJECT_REQUIRED_INDEXES) - indexes)
    flag = None
    if "assistant_feature_flags" in tables:
        flag = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (QQ_OBJECT_FEATURE_FLAG,),
        ).fetchone()
    return {
        "ok": not missing_tables and not missing_columns and not missing_indexes and flag is not None,
        "contract_checksum": QQ_OBJECT_MIGRATION_CHECKSUM,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "feature_flag_present": flag is not None,
    }


def require_qq_object_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_qq_object_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "qq_object_schema_drift:"
            + json.dumps(audit, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "QQ_OBJECT_FEATURE_FLAG",
    "QQ_OBJECT_MIGRATION_CHECKSUM",
    "apply_qq_object_authorization_v2",
    "inspect_qq_object_schema",
    "require_qq_object_schema",
]
