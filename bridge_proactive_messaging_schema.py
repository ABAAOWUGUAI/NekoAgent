#!/usr/bin/env python3
"""Schema for configurable proactive-message boundaries.

The existing ``proactive_policies`` table stores per-user scheduling state.
This additive schema stores the higher-level permission boundary that decides
whether a candidate may target the owner, a user, or a group.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


PROACTIVE_MESSAGING_TABLE = "proactive_messaging_policies"
PROACTIVE_MESSAGING_SCOPE_TYPES = ("global", "owner", "user", "group")
PROACTIVE_MESSAGING_MODES = ("off", "auto", "draft", "confirm")
PROACTIVE_MESSAGING_MIGRATION_CHECKSUM = hashlib.sha256(
    b"proactive_messaging_policy_v1:scope-target-mode-intents-quiet-budget-version",
).hexdigest()


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
        raise MigrationDriftError("proactive_messaging_active_assistant_missing")
    return str(row[0])


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({PROACTIVE_MESSAGING_TABLE})").fetchall()
    }


def apply_proactive_messaging_policy_v1(conn: sqlite3.Connection) -> None:
    """Create the additive policy table and safe local defaults."""

    assistant_id = _active_assistant_id(conn)
    conn.executescript(
        """
        CREATE TABLE proactive_messaging_policies (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            target_type TEXT NOT NULL
                CHECK(target_type IN ('global','owner','user','group')),
            target_id TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'off'
                CHECK(mode IN ('off','auto','draft','confirm')),
            allowed_intents_json TEXT NOT NULL DEFAULT
                '["task_failed","task_completed","approval","security"]',
            quiet_start TEXT NOT NULL DEFAULT '23:00',
            quiet_end TEXT NOT NULL DEFAULT '08:00',
            daily_limit INTEGER NOT NULL DEFAULT 2
                CHECK(daily_limit BETWEEN 0 AND 50),
            weekly_limit INTEGER NOT NULL DEFAULT 7
                CHECK(weekly_limit BETWEEN 0 AND 200),
            unanswered_limit INTEGER NOT NULL DEFAULT 2
                CHECK(unanswered_limit BETWEEN 0 AND 20),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_by TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(assistant_id,target_type,target_id)
        );

        CREATE INDEX idx_proactive_messaging_policy_lookup
        ON proactive_messaging_policies(assistant_id,target_type,target_id);
        """,
    )
    now = utc_now()
    defaults = (
        ("global", "", "off"),
        ("owner", "", "auto"),
        ("user", "", "off"),
        ("group", "", "off"),
    )
    for target_type, target_id, mode in defaults:
        conn.execute(
            """
            INSERT OR IGNORE INTO proactive_messaging_policies(
                id,assistant_id,target_type,target_id,mode,
                allowed_intents_json,quiet_start,quiet_end,daily_limit,
                weekly_limit,unanswered_limit,version,updated_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"proactive_policy_{target_type}",
                assistant_id,
                target_type,
                target_id,
                mode,
                '["task_failed","task_completed","approval","security"]',
                "23:00",
                "08:00",
                2,
                7,
                2,
                1,
                "system",
                now,
                now,
            ),
        )


def inspect_proactive_messaging_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    }
    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'",
        ).fetchall()
    }
    required_columns = {
        "id",
        "assistant_id",
        "target_type",
        "target_id",
        "mode",
        "allowed_intents_json",
        "quiet_start",
        "quiet_end",
        "daily_limit",
        "weekly_limit",
        "unanswered_limit",
        "version",
        "updated_by",
        "created_at",
        "updated_at",
    }
    missing_columns = (
        sorted(required_columns - _columns(conn))
        if PROACTIVE_MESSAGING_TABLE in tables
        else sorted(required_columns)
    )
    missing_indexes = (
        ["idx_proactive_messaging_policy_lookup"]
        if "idx_proactive_messaging_policy_lookup" not in indexes
        else []
    )
    row_count = 0
    if PROACTIVE_MESSAGING_TABLE in tables:
        row_count = int(
            conn.execute(
                "SELECT count(*) FROM proactive_messaging_policies",
            ).fetchone()[0],
        )
    return {
        "ok": (
            PROACTIVE_MESSAGING_TABLE in tables
            and not missing_columns
            and not missing_indexes
            and row_count >= 4
        ),
        "contract_checksum": PROACTIVE_MESSAGING_MIGRATION_CHECKSUM,
        "missing_tables": (
            [] if PROACTIVE_MESSAGING_TABLE in tables else [PROACTIVE_MESSAGING_TABLE]
        ),
        "missing_columns": (
            {PROACTIVE_MESSAGING_TABLE: missing_columns} if missing_columns else {}
        ),
        "missing_indexes": missing_indexes,
        "default_policy_count": row_count,
    }


def require_proactive_messaging_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_proactive_messaging_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "proactive_messaging_schema_drift:"
            + json.dumps(audit, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "PROACTIVE_MESSAGING_MIGRATION_CHECKSUM",
    "PROACTIVE_MESSAGING_MODES",
    "PROACTIVE_MESSAGING_SCOPE_TYPES",
    "PROACTIVE_MESSAGING_TABLE",
    "apply_proactive_messaging_policy_v1",
    "inspect_proactive_messaging_schema",
    "require_proactive_messaging_schema",
]
