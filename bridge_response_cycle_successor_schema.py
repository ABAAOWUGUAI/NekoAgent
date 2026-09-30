#!/usr/bin/env python3
"""Additive one-successor audit contract for private response cycles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import MigrationDriftError
from bridge_response_cycle_recovery_schema import require_response_cycle_recovery_schema


SUCCESSOR_COLUMNS: Mapping[str, str] = {
    "predecessor_cycle_id": "TEXT NOT NULL DEFAULT ''",
    "successor_cycle_id": "TEXT NOT NULL DEFAULT ''",
    "replan_depth": "INTEGER NOT NULL DEFAULT 0 CHECK(replan_depth IN (0,1))",
}
SUCCESSOR_INDEXES = {
    "idx_response_cycles_unique_predecessor": (
        "CREATE UNIQUE INDEX idx_response_cycles_unique_predecessor "
        "ON conversation_response_cycles(predecessor_cycle_id) "
        "WHERE predecessor_cycle_id<>''"
    ),
    "idx_response_cycles_unique_successor": (
        "CREATE UNIQUE INDEX idx_response_cycles_unique_successor "
        "ON conversation_response_cycles(successor_cycle_id) "
        "WHERE successor_cycle_id<>''"
    ),
}


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(conversation_response_cycles)")
    }


def _normalized_sql(value: object) -> str:
    return "".join(str(value or "").lower().split())


def apply_response_cycle_successor_v1(conn: sqlite3.Connection) -> None:
    require_response_cycle_recovery_schema(conn)
    existing = _columns(conn)
    for name, definition in SUCCESSOR_COLUMNS.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE conversation_response_cycles ADD COLUMN {name} {definition}",
            )
    for name, statement in SUCCESSOR_INDEXES.items():
        current = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        if current is None:
            conn.execute(statement)
        elif _normalized_sql(current[0]) != _normalized_sql(statement):
            raise MigrationDriftError(f"response_cycle_successor_index_drift:{name}")


def inspect_response_cycle_successor_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "conversation_response_cycles" not in tables:
        return {
            "ok": False,
            "missing_columns": sorted(SUCCESSOR_COLUMNS),
            "invalid_indexes": sorted(SUCCESSOR_INDEXES),
        }
    missing = sorted(set(SUCCESSOR_COLUMNS) - _columns(conn))
    invalid_indexes = []
    for name, statement in SUCCESSOR_INDEXES.items():
        current = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        if current is None or _normalized_sql(current[0]) != _normalized_sql(statement):
            invalid_indexes.append(name)
    return {
        "ok": not missing and not invalid_indexes,
        "missing_columns": missing,
        "invalid_indexes": invalid_indexes,
    }


def require_response_cycle_successor_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_response_cycle_successor_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "response_cycle_successor_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


SCHEMA_CONTRACT = {
    "columns": dict(SUCCESSOR_COLUMNS),
    "indexes": dict(SUCCESSOR_INDEXES),
}
RESPONSE_CYCLE_SUCCESSOR_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(SCHEMA_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "RESPONSE_CYCLE_SUCCESSOR_MIGRATION_CHECKSUM",
    "apply_response_cycle_successor_v1",
    "inspect_response_cycle_successor_schema",
    "require_response_cycle_successor_schema",
]
