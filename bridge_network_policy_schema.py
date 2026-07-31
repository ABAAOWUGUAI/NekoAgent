#!/usr/bin/env python3
"""Schema for the Assistant-owned network access policy.

This policy intentionally does not grant arbitrary shell egress. It controls
the fixed network Capabilities and the Codex native Web Search tool separately.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


NETWORK_POLICY_TABLE = "assistant_network_policies"
NETWORK_EVENT_TABLE = "assistant_network_policy_events"
NETWORK_BASE_MODES = ("off", "capability_only")
NETWORK_POLICY_MIGRATION_CHECKSUM = hashlib.sha256(
    b"assistant_network_policy_v1:all-assistants-base-mode-owner-search-expiry-version-events",
).hexdigest()


def ensure_network_policy_for_assistant(
    conn: sqlite3.Connection,
    assistant_id: str,
    *,
    now: str | None = None,
) -> None:
    """Create the fail-closed policy owned by one Assistant Instance."""

    timestamp = now or utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_network_policies(
            id,assistant_id,base_mode,owner_web_search_enabled,
            owner_web_search_expires_at,version,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            f"network-policy-{assistant_id}",
            assistant_id,
            "capability_only",
            0,
            "",
            1,
            "system",
            timestamp,
            timestamp,
        ),
    )


def apply_network_policy_v1(conn: sqlite3.Connection) -> None:
    """Create the additive policy and a safe, useful default."""

    conn.executescript(
        """
        CREATE TABLE assistant_network_policies (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL UNIQUE
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            base_mode TEXT NOT NULL DEFAULT 'capability_only'
                CHECK(base_mode IN ('off','capability_only')),
            owner_web_search_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(owner_web_search_enabled IN (0,1)),
            owner_web_search_expires_at TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_by TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE assistant_network_policy_events (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            action TEXT NOT NULL,
            actor_ref TEXT NOT NULL,
            channel TEXT NOT NULL,
            previous_json TEXT NOT NULL DEFAULT '{}',
            current_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_network_policy_events_created
        ON assistant_network_policy_events(assistant_id,created_at DESC);
        """,
    )
    now = utc_now()
    assistant_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT id FROM assistant_instances ORDER BY id",
        ).fetchall()
    ]
    if not assistant_ids:
        raise MigrationDriftError("network_policy_assistant_missing")
    for assistant_id in assistant_ids:
        ensure_network_policy_for_assistant(conn, assistant_id, now=now)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def inspect_network_policy_schema(conn: sqlite3.Connection) -> dict:
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
    policy_columns = {
        "id",
        "assistant_id",
        "base_mode",
        "owner_web_search_enabled",
        "owner_web_search_expires_at",
        "version",
        "updated_by",
        "created_at",
        "updated_at",
    }
    event_columns = {
        "id",
        "assistant_id",
        "action",
        "actor_ref",
        "channel",
        "previous_json",
        "current_json",
        "created_at",
    }
    missing_tables = sorted(
        {NETWORK_POLICY_TABLE, NETWORK_EVENT_TABLE} - tables,
    )
    missing_columns: dict[str, list[str]] = {}
    if NETWORK_POLICY_TABLE in tables:
        missing = sorted(policy_columns - _columns(conn, NETWORK_POLICY_TABLE))
        if missing:
            missing_columns[NETWORK_POLICY_TABLE] = missing
    if NETWORK_EVENT_TABLE in tables:
        missing = sorted(event_columns - _columns(conn, NETWORK_EVENT_TABLE))
        if missing:
            missing_columns[NETWORK_EVENT_TABLE] = missing
    missing_indexes = (
        []
        if "idx_network_policy_events_created" in indexes
        else ["idx_network_policy_events_created"]
    )
    policy_count = 0
    assistant_count = 0
    if "assistant_instances" in tables:
        assistant_count = int(
            conn.execute("SELECT count(*) FROM assistant_instances").fetchone()[0],
        )
    if NETWORK_POLICY_TABLE in tables:
        policy_count = int(
            conn.execute(
                "SELECT count(*) FROM assistant_network_policies",
            ).fetchone()[0],
        )
    return {
        "ok": (
            not missing_tables
            and not missing_columns
            and not missing_indexes
            and assistant_count >= 1
            and policy_count >= assistant_count
        ),
        "contract_checksum": NETWORK_POLICY_MIGRATION_CHECKSUM,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "policy_count": policy_count,
        "assistant_count": assistant_count,
    }


def require_network_policy_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_network_policy_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "network_policy_schema_drift:"
            + json.dumps(
                audit,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return audit


__all__ = [
    "NETWORK_BASE_MODES",
    "NETWORK_EVENT_TABLE",
    "NETWORK_POLICY_MIGRATION_CHECKSUM",
    "NETWORK_POLICY_TABLE",
    "apply_network_policy_v1",
    "ensure_network_policy_for_assistant",
    "inspect_network_policy_schema",
    "require_network_policy_schema",
]
