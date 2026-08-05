#!/usr/bin/env python3
"""AC-4 additive schema for deterministic natural-group guardrails."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG = "natural_group_participation_v1"
# This field belongs to the existing group policy fact source.  Keep its
# default closed so old policies cannot silently start consuming media.
MEDIA_OBSERVATION_POLICY_FIELD = "media_observation_probability"
MEDIA_OBSERVATION_POLICY_DEFAULT = 0.0
GROUP_POLICY_TABLE = "group_policies"
GROUP_POLICY_REQUIRED_COLUMNS = {
    MEDIA_OBSERVATION_POLICY_FIELD: "REAL",
}
GROUP_PARTICIPATION_BUDGET_TABLE = "group_participation_budget"
GROUP_PARTICIPATION_QUEUE_TABLE = "group_participation_queue"
GROUP_PARTICIPATION_BUDGET_COLUMNS = {
    "group_id": "TEXT",
    "day_key": "TEXT",
    "daily_reply_count": "INTEGER",
    "burst_started_at": "TEXT",
    "burst_message_count": "INTEGER",
    "last_message_at": "TEXT",
    "last_reply_at": "TEXT",
    "updated_at": "TEXT",
}
GROUP_PARTICIPATION_QUEUE_COLUMNS = {
    "group_id": "TEXT",
    "state": "TEXT",
    "first_message_at": "TEXT",
    "last_message_at": "TEXT",
    "due_at": "TEXT",
    "latest_message_id": "INTEGER",
    "latest_sender_id": "TEXT",
    "latest_sender_name": "TEXT",
    "latest_session": "TEXT",
    "latest_external_message_id": "TEXT",
    "attempt": "INTEGER",
    "lease_expires_at": "TEXT",
    "updated_at": "TEXT",
}


def _contract_payload() -> str:
    return json.dumps(
        {
            "feature_flag": NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG,
            "budget_table": GROUP_PARTICIPATION_BUDGET_TABLE,
            "budget_columns": GROUP_PARTICIPATION_BUDGET_COLUMNS,
            "queue_table": GROUP_PARTICIPATION_QUEUE_TABLE,
            "queue_columns": GROUP_PARTICIPATION_QUEUE_COLUMNS,
            "guardrails": [
                "quiet_hours",
                "cooldown",
                "burst_suppression",
                "daily_reply_budget",
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


GROUP_PARTICIPATION_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def apply_group_participation_v1(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "assistant_feature_flags" not in tables:
        raise MigrationDriftError("group_participation_feature_table_missing")
    # The policy table is an existing social fact source, but the registered
    # migration runner also needs a minimal closed default when bootstrapping a
    # legacy database that has not gone through the bridge's social DDL yet.
    # Keep this additive and idempotent; the full social bootstrap later adds
    # the remaining policy columns without replacing existing data.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {GROUP_POLICY_TABLE} (
            group_id TEXT PRIMARY KEY,
            {MEDIA_OBSERVATION_POLICY_FIELD} REAL NOT NULL DEFAULT {MEDIA_OBSERVATION_POLICY_DEFAULT}
        )
        """,
    )
    policy_columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({GROUP_POLICY_TABLE})")
    }
    if MEDIA_OBSERVATION_POLICY_FIELD not in policy_columns:
        conn.execute(
            f"ALTER TABLE {GROUP_POLICY_TABLE} ADD COLUMN "
            f"{MEDIA_OBSERVATION_POLICY_FIELD} REAL NOT NULL DEFAULT {MEDIA_OBSERVATION_POLICY_DEFAULT}",
        )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {GROUP_PARTICIPATION_BUDGET_TABLE} (
            group_id TEXT PRIMARY KEY,
            day_key TEXT NOT NULL DEFAULT '',
            daily_reply_count INTEGER NOT NULL DEFAULT 0,
            burst_started_at TEXT NOT NULL DEFAULT '',
            burst_message_count INTEGER NOT NULL DEFAULT 0,
            last_message_at TEXT NOT NULL DEFAULT '',
            last_reply_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
    )
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({GROUP_PARTICIPATION_BUDGET_TABLE})")
    }
    if "last_message_at" not in columns:
        conn.execute(
            f"ALTER TABLE {GROUP_PARTICIPATION_BUDGET_TABLE} "
            "ADD COLUMN last_message_at TEXT NOT NULL DEFAULT ''",
        )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_group_participation_budget_day "
        f"ON {GROUP_PARTICIPATION_BUDGET_TABLE}(day_key,daily_reply_count)",
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {GROUP_PARTICIPATION_QUEUE_TABLE} (
            group_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'pending',
            first_message_at TEXT NOT NULL,
            last_message_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            latest_message_id INTEGER NOT NULL,
            latest_sender_id TEXT NOT NULL DEFAULT '',
            latest_sender_name TEXT NOT NULL DEFAULT '',
            latest_session TEXT NOT NULL DEFAULT '',
            latest_external_message_id TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            lease_expires_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_group_participation_queue_due "
        f"ON {GROUP_PARTICIPATION_QUEUE_TABLE}(state,due_at)",
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        """,
        (NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG, utc_now()),
    )


def inspect_group_participation_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    flag = None
    columns: set[str] = set()
    queue_columns: set[str] = set()
    group_policy_columns: set[str] = set()
    if GROUP_PARTICIPATION_BUDGET_TABLE in tables:
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({GROUP_PARTICIPATION_BUDGET_TABLE})")
        }
    if GROUP_PARTICIPATION_QUEUE_TABLE in tables:
        queue_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({GROUP_PARTICIPATION_QUEUE_TABLE})")
        }
    if GROUP_POLICY_TABLE in tables:
        group_policy_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({GROUP_POLICY_TABLE})")
        }
    if "assistant_feature_flags" in tables:
        flag = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG,),
        ).fetchone()
    return {
        "ok": (
            GROUP_PARTICIPATION_BUDGET_TABLE in tables
            and GROUP_PARTICIPATION_QUEUE_TABLE in tables
            and set(GROUP_PARTICIPATION_BUDGET_COLUMNS).issubset(columns)
            and set(GROUP_PARTICIPATION_QUEUE_COLUMNS).issubset(queue_columns)
            and set(GROUP_POLICY_REQUIRED_COLUMNS).issubset(group_policy_columns)
            and flag is not None
        ),
        "contract_checksum": GROUP_PARTICIPATION_MIGRATION_CHECKSUM,
        "missing_tables": sorted({GROUP_PARTICIPATION_BUDGET_TABLE, GROUP_PARTICIPATION_QUEUE_TABLE, GROUP_POLICY_TABLE} - tables),
        "missing_columns": sorted(set(GROUP_PARTICIPATION_BUDGET_COLUMNS) - columns),
        "missing_queue_columns": sorted(set(GROUP_PARTICIPATION_QUEUE_COLUMNS) - queue_columns),
        "missing_policy_columns": sorted(set(GROUP_POLICY_REQUIRED_COLUMNS) - group_policy_columns),
        "feature_flag_present": flag is not None,
        "feature_enabled": bool(int(flag[0])) if flag is not None else False,
    }


def require_group_participation_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_group_participation_schema(conn)
    if not audit["ok"]:
        missing = ",".join(audit["missing_tables"])
        columns = ",".join(
            audit["missing_columns"]
            + audit.get("missing_queue_columns", [])
            + audit.get("missing_policy_columns", [])
        )
        suffix = "|feature_flag" if not audit["feature_flag_present"] else ""
        raise MigrationDriftError(
            "group_participation_schema_drift:" + missing + "|" + columns + suffix,
        )
    return audit


__all__ = [
    "GROUP_PARTICIPATION_BUDGET_TABLE",
    "GROUP_PARTICIPATION_QUEUE_TABLE",
    "GROUP_PARTICIPATION_BUDGET_COLUMNS",
    "GROUP_PARTICIPATION_QUEUE_COLUMNS",
    "GROUP_PARTICIPATION_MIGRATION_CHECKSUM",
    "NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG",
    "MEDIA_OBSERVATION_POLICY_FIELD",
    "MEDIA_OBSERVATION_POLICY_DEFAULT",
    "GROUP_POLICY_TABLE",
    "GROUP_POLICY_REQUIRED_COLUMNS",
    "apply_group_participation_v1",
    "inspect_group_participation_schema",
    "require_group_participation_schema",
]
