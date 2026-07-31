#!/usr/bin/env python3
"""Gate C5 QQ channel runtime configuration and receipt schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now
from bridge_qq_access_schema import QQ_CHANNEL_ID


RUNTIME_SETTING_COLUMNS = (
    "expected_bot_id",
    "command_prefixes_json",
    "auto_private_chat",
    "reply_max_chars",
    "delivery_poll_seconds",
    "notification_interval_seconds",
)
RUNTIME_RECEIPT_COLUMNS = (
    "channel_instance_id",
    "channel_id",
    "actual_bot_id",
    "adapter_id",
    "applied_version",
    "capabilities_json",
    "last_heartbeat_at",
    "last_sync_error",
    "created_at",
    "updated_at",
)
RUNTIME_REQUIRED_INDEXES = ("idx_qq_runtime_channel_heartbeat",)


def _checksum() -> str:
    payload = json.dumps(
        {
            "settings_columns": list(RUNTIME_SETTING_COLUMNS),
            "receipt_columns": list(RUNTIME_RECEIPT_COLUMNS),
            "indexes": list(RUNTIME_REQUIRED_INDEXES),
            "defaults": {
                "command_prefixes": ["/codex", "/c", "/agent", "/a"],
                "auto_private_chat": True,
                "reply_max_chars": 3600,
                "delivery_poll_seconds": 12,
                "notification_interval_seconds": 90,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


QQ_RUNTIME_MIGRATION_CHECKSUM = _checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_qq_channel_runtime_v2(conn: sqlite3.Connection) -> None:
    """Add versioned runtime settings and plugin heartbeat receipts."""

    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qq_channel_settings'",
    ).fetchone():
        raise MigrationDriftError("qq_runtime_settings_table_missing")

    existing = _columns(conn, "qq_channel_settings")
    additions = {
        "expected_bot_id": "TEXT NOT NULL DEFAULT ''",
        "command_prefixes_json": (
            "TEXT NOT NULL DEFAULT '[\"/codex\",\"/c\",\"/agent\",\"/a\"]'"
        ),
        "auto_private_chat": "INTEGER NOT NULL DEFAULT 1 CHECK(auto_private_chat IN (0,1))",
        "reply_max_chars": "INTEGER NOT NULL DEFAULT 3600 CHECK(reply_max_chars BETWEEN 500 AND 10000)",
        "delivery_poll_seconds": (
            "INTEGER NOT NULL DEFAULT 12 CHECK(delivery_poll_seconds BETWEEN 5 AND 300)"
        ),
        "notification_interval_seconds": (
            "INTEGER NOT NULL DEFAULT 90 CHECK(notification_interval_seconds BETWEEN 10 AND 3600)"
        ),
    }
    for name, ddl in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE qq_channel_settings ADD COLUMN {name} {ddl}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS qq_channel_runtime_receipts (
            channel_instance_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL
                REFERENCES qq_channel_settings(channel_id) ON DELETE CASCADE,
            actual_bot_id TEXT NOT NULL DEFAULT '',
            adapter_id TEXT NOT NULL DEFAULT '',
            applied_version INTEGER NOT NULL DEFAULT 0 CHECK(applied_version >= 0),
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            last_heartbeat_at TEXT NOT NULL,
            last_sync_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_qq_runtime_channel_heartbeat
        ON qq_channel_runtime_receipts(channel_id,last_heartbeat_at DESC);
        """,
    )
    now = utc_now()
    conn.execute(
        """
        UPDATE qq_channel_settings
        SET command_prefixes_json=COALESCE(NULLIF(command_prefixes_json,''),?),
            updated_at=COALESCE(NULLIF(updated_at,''),?)
        WHERE channel_id=?
        """,
        ('["/codex","/c","/agent","/a"]', now, QQ_CHANNEL_ID),
    )


def inspect_qq_runtime_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    settings_missing = []
    receipt_missing = []
    if "qq_channel_settings" in tables:
        settings_missing = sorted(
            set(RUNTIME_SETTING_COLUMNS) - _columns(conn, "qq_channel_settings"),
        )
    if "qq_channel_runtime_receipts" in tables:
        receipt_missing = sorted(
            set(RUNTIME_RECEIPT_COLUMNS) - _columns(conn, "qq_channel_runtime_receipts"),
        )
    missing_indexes = sorted(set(RUNTIME_REQUIRED_INDEXES) - indexes)
    settings = None
    if "qq_channel_settings" in tables and not settings_missing:
        settings = conn.execute(
            "SELECT command_prefixes_json FROM qq_channel_settings WHERE channel_id=?",
            (QQ_CHANNEL_ID,),
        ).fetchone()
    return {
        "ok": (
            "qq_channel_settings" in tables
            and "qq_channel_runtime_receipts" in tables
            and not settings_missing
            and not receipt_missing
            and not missing_indexes
            and settings is not None
        ),
        "contract_checksum": QQ_RUNTIME_MIGRATION_CHECKSUM,
        "settings_missing_columns": settings_missing,
        "receipt_missing_columns": receipt_missing,
        "missing_indexes": missing_indexes,
        "settings_present": settings is not None,
    }


def require_qq_runtime_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_qq_runtime_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "qq_runtime_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "QQ_RUNTIME_MIGRATION_CHECKSUM",
    "apply_qq_channel_runtime_v2",
    "inspect_qq_runtime_schema",
    "require_qq_runtime_schema",
]
