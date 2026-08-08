#!/usr/bin/env python3
"""Additive persistence for a user-confirmable Interaction Plan action."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_interaction_plan_schema import require_interaction_plan_schema
from bridge_migrations import MigrationDriftError, utc_now


ACTION_COMMITMENT_FEATURE_FLAG = "action_commitment_v1"
ACTION_COMMITMENT_COLUMNS = (
    "id", "assistant_id", "owner_actor_id", "thread_ref", "origin_plan_id",
    "action_type", "action_json", "action_hash", "approval_policy",
    "rendered_reply_hash", "state", "expires_at", "action_receipt_id",
    "created_at", "updated_at",
)
ACTION_COMMITMENT_INDEXES = (
    "idx_action_commitments_open_scope",
    "idx_action_commitments_plan",
)
_STATES = "'proposed','accepted','amended','declined','expired','executed','failed'"


def apply_action_commitment_v1(conn: sqlite3.Connection) -> None:
    """Create a short-lived child of Interaction Plan, never an action queue."""

    require_interaction_plan_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE interaction_action_commitments (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            owner_actor_id TEXT NOT NULL,
            thread_ref TEXT NOT NULL,
            origin_plan_id TEXT NOT NULL
                REFERENCES interaction_plans(id) ON DELETE RESTRICT,
            action_type TEXT NOT NULL,
            action_json TEXT NOT NULL CHECK(json_valid(action_json)),
            action_hash TEXT NOT NULL,
            approval_policy TEXT NOT NULL,
            rendered_reply_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ({_STATES})),
            expires_at TEXT NOT NULL,
            action_receipt_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_action_commitments_open_scope
        ON interaction_action_commitments(
            assistant_id,owner_actor_id,thread_ref,state,expires_at DESC,updated_at DESC
        );
        CREATE INDEX idx_action_commitments_plan
        ON interaction_action_commitments(origin_plan_id,created_at DESC);
        """,
    )
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?) ON CONFLICT(name) DO NOTHING
        """,
        (ACTION_COMMITMENT_FEATURE_FLAG, utc_now()),
    )


def _schema_parts(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(interaction_action_commitments)")
    } if "interaction_action_commitments" in tables else set()
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    return tables, columns, indexes


def require_action_commitment_schema(conn: sqlite3.Connection) -> dict:
    """Fail closed when the additive commitment contract is not exact."""

    require_interaction_plan_schema(conn)
    tables, columns, indexes = _schema_parts(conn)
    missing_tables = [] if "interaction_action_commitments" in tables else ["interaction_action_commitments"]
    missing_columns = sorted(set(ACTION_COMMITMENT_COLUMNS) - columns)
    missing_indexes = sorted(set(ACTION_COMMITMENT_INDEXES) - indexes)
    foreign_key_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    invalid_json = 0
    if not missing_tables:
        invalid_json = int(conn.execute(
            "SELECT count(*) FROM interaction_action_commitments WHERE NOT json_valid(action_json)",
        ).fetchone()[0])
    if missing_tables or missing_columns or missing_indexes or foreign_key_errors or invalid_json:
        raise MigrationDriftError(
            "action_commitment_schema_drift:" + json.dumps(
                {
                    "tables": missing_tables,
                    "columns": missing_columns,
                    "indexes": missing_indexes,
                    "foreign_key_error_count": len(foreign_key_errors),
                    "invalid_action_json": invalid_json,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return {"ok": True, "feature_flag": ACTION_COMMITMENT_FEATURE_FLAG}


def action_commitment_feature_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (ACTION_COMMITMENT_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def set_action_commitment_feature(conn: sqlite3.Connection, enabled: bool) -> dict:
    require_action_commitment_schema(conn)
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (ACTION_COMMITMENT_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return {"name": ACTION_COMMITMENT_FEATURE_FLAG, "enabled": bool(enabled)}


_CONTRACT = {
    "feature": ACTION_COMMITMENT_FEATURE_FLAG,
    "table": {"interaction_action_commitments": list(ACTION_COMMITMENT_COLUMNS)},
    "indexes": list(ACTION_COMMITMENT_INDEXES),
    "states": _STATES,
}
ACTION_COMMITMENT_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "ACTION_COMMITMENT_FEATURE_FLAG",
    "ACTION_COMMITMENT_MIGRATION_CHECKSUM",
    "action_commitment_feature_enabled",
    "apply_action_commitment_v1",
    "require_action_commitment_schema",
    "set_action_commitment_feature",
]
