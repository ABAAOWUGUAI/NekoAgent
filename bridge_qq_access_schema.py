#!/usr/bin/env python3
"""Gate C1 QQ identity, role, and channel-access schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import MigrationDriftError, utc_now


QQ_ACCESS_FEATURE_FLAG = "qq_access_control_v2"
QQ_CHANNEL_ID = "qq-main"

QQ_ACCESS_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "qq_channel_settings": (
        "channel_id",
        "assistant_id",
        "channel_enabled",
        "access_mode",
        "private_chat_enabled",
        "group_chat_enabled",
        "config_version",
        "updated_by",
        "created_at",
        "updated_at",
    ),
    "qq_identities": (
        "id",
        "qq_id",
        "display_name",
        "status",
        "last_seen_at",
        "created_at",
        "updated_at",
    ),
    "qq_role_assignments": (
        "id",
        "identity_id",
        "role",
        "enabled",
        "created_by",
        "created_at",
        "updated_at",
    ),
    "qq_access_entries": (
        "id",
        "subject_type",
        "subject_id",
        "enabled",
        "remark",
        "created_by",
        "created_at",
        "updated_at",
    ),
}

QQ_ACCESS_REQUIRED_INDEXES = (
    "idx_qq_roles_enabled",
    "idx_qq_access_enabled",
)


def _contract_checksum() -> str:
    payload = json.dumps(
        {
            "tables": {key: list(value) for key, value in QQ_ACCESS_TABLE_COLUMNS.items()},
            "indexes": list(QQ_ACCESS_REQUIRED_INDEXES),
            "feature_flag": QQ_ACCESS_FEATURE_FLAG,
            "channel_id": QQ_CHANNEL_ID,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


QQ_ACCESS_MIGRATION_CHECKSUM = _contract_checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT id FROM assistant_instances
        WHERE status='active'
        ORDER BY updated_at DESC,id
        LIMIT 1
        """,
    ).fetchone()
    if not row:
        raise MigrationDriftError("qq_access_active_assistant_missing")
    return str(row[0])


def apply_qq_access_control_v2(conn: sqlite3.Connection) -> None:
    """Create the additive Gate C1 schema in a fail-closed state."""

    assistant_id = _active_assistant_id(conn)
    conn.executescript(
        """
        CREATE TABLE qq_channel_settings (
            channel_id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            channel_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(channel_enabled IN (0,1)),
            access_mode TEXT NOT NULL DEFAULT 'disabled'
                CHECK(access_mode IN ('disabled','admin_only','allowlist')),
            private_chat_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(private_chat_enabled IN (0,1)),
            group_chat_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(group_chat_enabled IN (0,1)),
            config_version INTEGER NOT NULL DEFAULT 1 CHECK(config_version > 0),
            updated_by TEXT NOT NULL DEFAULT 'migration',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE qq_identities (
            id TEXT PRIMARY KEY,
            qq_id TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','disabled')),
            last_seen_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE qq_role_assignments (
            id TEXT PRIMARY KEY,
            identity_id TEXT NOT NULL
                REFERENCES qq_identities(id) ON DELETE RESTRICT,
            role TEXT NOT NULL
                CHECK(role IN ('super_admin','admin','operator','user')),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(identity_id,role)
        );

        CREATE TABLE qq_access_entries (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL
                CHECK(subject_type IN ('private_user','qq_group')),
            subject_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            remark TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(subject_type,subject_id)
        );

        CREATE INDEX idx_qq_roles_enabled
        ON qq_role_assignments(identity_id,enabled,role);

        CREATE INDEX idx_qq_access_enabled
        ON qq_access_entries(subject_type,enabled,subject_id);
        """,
    )
    now = utc_now()
    conn.execute(
        """
        INSERT INTO qq_channel_settings(
            channel_id,assistant_id,channel_enabled,access_mode,
            private_chat_enabled,group_chat_enabled,config_version,
            updated_by,created_at,updated_at
        ) VALUES(?,?,0,'disabled',0,0,1,'migration',?,?)
        """,
        (QQ_CHANNEL_ID, assistant_id, now, now),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        """,
        (QQ_ACCESS_FEATURE_FLAG, now),
    )


def inspect_qq_access_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_tables = sorted(set(QQ_ACCESS_TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in QQ_ACCESS_TABLE_COLUMNS.items():
        if table in tables:
            missing = sorted(set(required) - _columns(conn, table))
            if missing:
                missing_columns[table] = missing
    missing_indexes = sorted(set(QQ_ACCESS_REQUIRED_INDEXES) - indexes)
    flag = None
    settings = None
    if "assistant_feature_flags" in tables:
        flag = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (QQ_ACCESS_FEATURE_FLAG,),
        ).fetchone()
    if "qq_channel_settings" in tables:
        settings = conn.execute(
            "SELECT config_version FROM qq_channel_settings WHERE channel_id=?",
            (QQ_CHANNEL_ID,),
        ).fetchone()
    return {
        "ok": (
            not missing_tables
            and not missing_columns
            and not missing_indexes
            and flag is not None
            and settings is not None
        ),
        "contract_checksum": QQ_ACCESS_MIGRATION_CHECKSUM,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "feature_flag_present": flag is not None,
        "settings_present": settings is not None,
    }


def require_qq_access_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_qq_access_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "qq_access_schema_drift:"
            + json.dumps(audit, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "QQ_ACCESS_FEATURE_FLAG",
    "QQ_ACCESS_MIGRATION_CHECKSUM",
    "QQ_CHANNEL_ID",
    "apply_qq_access_control_v2",
    "inspect_qq_access_schema",
    "require_qq_access_schema",
]
