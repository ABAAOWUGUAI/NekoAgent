#!/usr/bin/env python3
"""Gate 8 relationship, notification, and proactive-event schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import MigrationDriftError, utc_now


RELATIONSHIP_PROACTIVE_FEATURE_FLAG = "relationship_proactive_v2"
RELATIONSHIP_PROACTIVE_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "relationship_states": (
        "id",
        "assistant_id",
        "user_id",
        "scope_type",
        "scope_id",
        "preferred_address",
        "interaction_style",
        "familiarity_context",
        "allowed_topics_json",
        "blocked_topics_json",
        "social_proactive_enabled",
        "version",
        "created_at",
        "updated_at",
    ),
    "operational_notification_policies": (
        "id",
        "assistant_id",
        "user_id",
        "channel_scope",
        "enabled_categories_json",
        "quiet_start",
        "quiet_end",
        "critical_bypass_quiet",
        "group_window_minutes",
        "version",
        "created_at",
        "updated_at",
    ),
    "assistant_idempotency_records": (
        "action",
        "idempotency_key",
        "request_hash",
        "response_json",
        "created_at",
    ),
}
RELATIONSHIP_PROACTIVE_REQUIRED_INDEXES = (
    "idx_relationship_states_lookup",
    "idx_operational_notification_policy_lookup",
    "idx_proactive_events_idempotency",
    "idx_proactive_events_assistant",
)
PROACTIVE_POLICY_COLUMNS: Mapping[str, str] = {
    "assistant_id": "TEXT NOT NULL DEFAULT ''",
    "policy_kind": "TEXT NOT NULL DEFAULT 'social'",
    "policy_version": "INTEGER NOT NULL DEFAULT 1",
    "trigger_reason_required": "INTEGER NOT NULL DEFAULT 1",
    "condition_contract_json": "TEXT NOT NULL DEFAULT '{}'",
}
PROACTIVE_EVENT_COLUMNS: Mapping[str, str] = {
    "assistant_id": "TEXT NOT NULL DEFAULT ''",
    "policy_kind": "TEXT NOT NULL DEFAULT 'social'",
    "policy_version": "INTEGER NOT NULL DEFAULT 1",
    "trigger_reason": "TEXT NOT NULL DEFAULT ''",
    "condition_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
    "idempotency_key": "TEXT NOT NULL DEFAULT ''",
    "request_hash": "TEXT NOT NULL DEFAULT ''",
    "blocked_reason": "TEXT NOT NULL DEFAULT ''",
}


def _contract_checksum() -> str:
    payload = json.dumps(
        {
            "tables": {
                key: list(value)
                for key, value in RELATIONSHIP_PROACTIVE_TABLE_COLUMNS.items()
            },
            "indexes": list(RELATIONSHIP_PROACTIVE_REQUIRED_INDEXES),
            "policy_columns": PROACTIVE_POLICY_COLUMNS,
            "event_columns": PROACTIVE_EVENT_COLUMNS,
            "feature_flag": RELATIONSHIP_PROACTIVE_FEATURE_FLAG,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


RELATIONSHIP_PROACTIVE_MIGRATION_CHECKSUM = _contract_checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


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
        raise MigrationDriftError("relationship_proactive_active_assistant_missing")
    return str(row[0])


def apply_relationship_proactive_v2(conn: sqlite3.Connection) -> None:
    """Create additive Gate 8 storage and bind legacy policies deterministically."""

    # Existing tests and maintenance tools can invoke the Assistant migration
    # runner without the normal Bridge bootstrap. Ensure the legacy automation
    # source exists before adding versioned Gate 8 columns.
    from bridge_automation import ensure_automation_tables

    ensure_automation_tables(conn)
    assistant_id = _active_assistant_id(conn)
    conn.executescript(
        """
        CREATE TABLE relationship_states (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            user_id TEXT NOT NULL,
            scope_type TEXT NOT NULL
                CHECK(scope_type IN (
                    'private_user','channel_thread','qq_group','project',
                    'global_preference','sensitive_private'
                )),
            scope_id TEXT NOT NULL DEFAULT '',
            preferred_address TEXT NOT NULL DEFAULT '',
            interaction_style TEXT NOT NULL DEFAULT 'natural',
            familiarity_context TEXT NOT NULL DEFAULT 'new'
                CHECK(familiarity_context IN ('new','familiar','long_term')),
            allowed_topics_json TEXT NOT NULL DEFAULT '[]',
            blocked_topics_json TEXT NOT NULL DEFAULT '[]',
            social_proactive_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(social_proactive_enabled IN (0,1)),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(assistant_id,user_id,scope_type,scope_id)
        );

        CREATE TABLE operational_notification_policies (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            user_id TEXT NOT NULL,
            channel_scope TEXT NOT NULL DEFAULT 'owner',
            enabled_categories_json TEXT NOT NULL DEFAULT
                '["approval","task_completed","task_failed","delivery_failed","security","resource"]',
            quiet_start TEXT NOT NULL DEFAULT '23:30',
            quiet_end TEXT NOT NULL DEFAULT '09:00',
            critical_bypass_quiet INTEGER NOT NULL DEFAULT 1
                CHECK(critical_bypass_quiet IN (0,1)),
            group_window_minutes INTEGER NOT NULL DEFAULT 10
                CHECK(group_window_minutes BETWEEN 0 AND 1440),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(assistant_id,user_id,channel_scope)
        );

        CREATE TABLE assistant_idempotency_records (
            action TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            response_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY(action,idempotency_key)
        );

        CREATE INDEX idx_relationship_states_lookup
        ON relationship_states(assistant_id,user_id,scope_type,scope_id);

        CREATE INDEX idx_operational_notification_policy_lookup
        ON operational_notification_policies(assistant_id,user_id,channel_scope);
        """,
    )
    policy_columns = _columns(conn, "proactive_policies")
    for name, definition in PROACTIVE_POLICY_COLUMNS.items():
        if name not in policy_columns:
            conn.execute(
                f"ALTER TABLE proactive_policies ADD COLUMN {name} {definition}",
            )
    event_columns = _columns(conn, "proactive_events")
    for name, definition in PROACTIVE_EVENT_COLUMNS.items():
        if name not in event_columns:
            conn.execute(
                f"ALTER TABLE proactive_events ADD COLUMN {name} {definition}",
            )
    conn.execute(
        """
        UPDATE proactive_policies
        SET assistant_id=?,
            policy_kind='social',
            policy_version=CASE WHEN policy_version < 1 THEN 1 ELSE policy_version END,
            trigger_reason_required=1
        WHERE assistant_id=''
        """,
        (assistant_id,),
    )
    conn.execute(
        """
        UPDATE proactive_events
        SET assistant_id=?,
            policy_kind='social',
            policy_version=CASE WHEN policy_version < 1 THEN 1 ELSE policy_version END
        WHERE assistant_id=''
        """,
        (assistant_id,),
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_proactive_events_idempotency
        ON proactive_events(idempotency_key)
        WHERE idempotency_key<>''
        """,
    )
    conn.execute(
        """
        CREATE INDEX idx_proactive_events_assistant
        ON proactive_events(assistant_id,user_id,decision_at DESC)
        """,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        """,
        (RELATIONSHIP_PROACTIVE_FEATURE_FLAG, utc_now()),
    )


def inspect_relationship_proactive_schema(conn: sqlite3.Connection) -> dict:
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
    missing_tables = sorted(
        set(RELATIONSHIP_PROACTIVE_TABLE_COLUMNS) - tables,
    )
    missing_columns: dict[str, list[str]] = {}
    for table, required in RELATIONSHIP_PROACTIVE_TABLE_COLUMNS.items():
        if table in tables:
            missing = sorted(set(required) - _columns(conn, table))
            if missing:
                missing_columns[table] = missing
    if "proactive_policies" in tables:
        missing = sorted(set(PROACTIVE_POLICY_COLUMNS) - _columns(conn, "proactive_policies"))
        if missing:
            missing_columns["proactive_policies"] = missing
    else:
        missing_tables.append("proactive_policies")
    if "proactive_events" in tables:
        missing = sorted(set(PROACTIVE_EVENT_COLUMNS) - _columns(conn, "proactive_events"))
        if missing:
            missing_columns["proactive_events"] = missing
    else:
        missing_tables.append("proactive_events")
    missing_indexes = sorted(
        set(RELATIONSHIP_PROACTIVE_REQUIRED_INDEXES) - indexes,
    )
    flag = None
    if "assistant_feature_flags" in tables:
        flag = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (RELATIONSHIP_PROACTIVE_FEATURE_FLAG,),
        ).fetchone()
    unbound_policies = 0
    unbound_events = 0
    if "proactive_policies" in tables and "assistant_id" in _columns(conn, "proactive_policies"):
        unbound_policies = int(
            conn.execute(
                "SELECT count(*) FROM proactive_policies WHERE assistant_id=''",
            ).fetchone()[0],
        )
    if "proactive_events" in tables and "assistant_id" in _columns(conn, "proactive_events"):
        unbound_events = int(
            conn.execute(
                "SELECT count(*) FROM proactive_events WHERE assistant_id=''",
            ).fetchone()[0],
        )
    return {
        "ok": (
            not missing_tables
            and not missing_columns
            and not missing_indexes
            and flag is not None
            and unbound_policies == 0
            and unbound_events == 0
        ),
        "contract_checksum": RELATIONSHIP_PROACTIVE_MIGRATION_CHECKSUM,
        "missing_tables": sorted(set(missing_tables)),
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "feature_flag_present": flag is not None,
        "unbound_policies": unbound_policies,
        "unbound_events": unbound_events,
    }


def require_relationship_proactive_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_relationship_proactive_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "relationship_proactive_schema_drift:"
            + json.dumps(audit, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "RELATIONSHIP_PROACTIVE_FEATURE_FLAG",
    "RELATIONSHIP_PROACTIVE_MIGRATION_CHECKSUM",
    "apply_relationship_proactive_v2",
    "inspect_relationship_proactive_schema",
    "require_relationship_proactive_schema",
]
