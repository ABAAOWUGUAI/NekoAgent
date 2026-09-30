#!/usr/bin/env python3
"""Additive recovery contract for durable private response cycles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_continuous_private_conversation_schema import (
    require_continuous_private_conversation_schema,
)
from bridge_migrations import MigrationDriftError


RECOVERY_COLUMNS: Mapping[str, str] = {
    "prepared_delivery_json": "TEXT NOT NULL DEFAULT ''",
    "prepared_delivery_sha256": "TEXT NOT NULL DEFAULT ''",
    "blocks_thread": "INTEGER NOT NULL DEFAULT 1 CHECK(blocks_thread IN (0,1))",
    "blocks_effects": "INTEGER NOT NULL DEFAULT 0 CHECK(blocks_effects IN (0,1))",
    "recovery_disposition": "TEXT NOT NULL DEFAULT ''",
    "recovery_decided_at": "TEXT NOT NULL DEFAULT ''",
    "recovery_decided_by": "TEXT NOT NULL DEFAULT ''",
}

ACTIVE_INDEX = "idx_response_cycles_active_thread"
ACTIVE_STATES_SQL = "'pending','processing','commit_ready','retryable','manual_hold'"


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(conversation_response_cycles)")
    }


def _normalized_sql(value: object) -> str:
    return "".join(str(value or "").lower().split())


def _expected_index_sql(*, recovery: bool) -> str:
    blocking = " AND blocks_thread=1" if recovery else ""
    return (
        f"CREATE UNIQUE INDEX {ACTIVE_INDEX} "
        "ON conversation_response_cycles(thread_id) "
        f"WHERE state IN ({ACTIVE_STATES_SQL}){blocking}"
    )


def apply_response_cycle_recovery_v1(conn: sqlite3.Connection) -> None:
    require_continuous_private_conversation_schema(conn)
    existing = _columns(conn)
    for name, definition in RECOVERY_COLUMNS.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE conversation_response_cycles ADD COLUMN {name} {definition}",
            )

    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (ACTIVE_INDEX,),
    ).fetchone()
    if not index:
        raise MigrationDriftError("response_cycle_recovery_active_index_missing")
    current = _normalized_sql(index[0])
    accepted = {
        _normalized_sql(_expected_index_sql(recovery=False)),
        _normalized_sql(_expected_index_sql(recovery=True)),
    }
    if current not in accepted:
        raise MigrationDriftError("response_cycle_recovery_active_index_drift")
    if current != _normalized_sql(_expected_index_sql(recovery=True)):
        conn.execute(f"DROP INDEX {ACTIVE_INDEX}")
        conn.execute(_expected_index_sql(recovery=True))


def inspect_response_cycle_recovery_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "conversation_response_cycles" not in tables:
        return {
            "ok": False,
            "missing_columns": sorted(RECOVERY_COLUMNS),
            "active_index_valid": False,
        }
    missing = sorted(set(RECOVERY_COLUMNS) - _columns(conn))
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (ACTIVE_INDEX,),
    ).fetchone()
    index_valid = bool(
        index
        and _normalized_sql(index[0])
        == _normalized_sql(_expected_index_sql(recovery=True))
    )
    return {
        "ok": not missing and index_valid,
        "missing_columns": missing,
        "active_index_valid": index_valid,
    }


def require_response_cycle_recovery_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_response_cycle_recovery_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "response_cycle_recovery_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


SCHEMA_CONTRACT = {
    "columns": dict(RECOVERY_COLUMNS),
    "active_index_sql": _expected_index_sql(recovery=True),
}

RESPONSE_CYCLE_RECOVERY_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(SCHEMA_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "RESPONSE_CYCLE_RECOVERY_MIGRATION_CHECKSUM",
    "apply_response_cycle_recovery_v1",
    "inspect_response_cycle_recovery_schema",
    "require_response_cycle_recovery_schema",
]
