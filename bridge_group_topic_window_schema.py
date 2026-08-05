#!/usr/bin/env python3
"""Additive v2 queue fields for group-topic participation candidates."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


GROUP_TOPIC_WINDOW_QUEUE_TABLE = "group_participation_queue"
GROUP_TOPIC_WINDOW_QUEUE_COLUMNS = {
    "anchor_message_id": "INTEGER",
    "anchor_external_message_id": "TEXT",
    "anchor_sender_id": "TEXT",
    "latest_text_message_id": "INTEGER",
    "candidate_revision": "INTEGER",
}
GROUP_TOPIC_WINDOW_QUEUE_DEFAULTS = {
    "anchor_message_id": "0",
    "anchor_external_message_id": "''",
    "anchor_sender_id": "''",
    "latest_text_message_id": "0",
    "candidate_revision": "0",
}
GROUP_TOPIC_WINDOW_QUEUE_DEFINITIONS = {
    name: {
        "type": column_type,
        "notnull": 1,
        "default": GROUP_TOPIC_WINDOW_QUEUE_DEFAULTS[name],
    }
    for name, column_type in GROUP_TOPIC_WINDOW_QUEUE_COLUMNS.items()
}


def _contract_payload() -> str:
    return json.dumps(
        {
            "migration": "group_topic_window_candidate_v1",
            "queue_table": GROUP_TOPIC_WINDOW_QUEUE_TABLE,
            "queue_columns": GROUP_TOPIC_WINDOW_QUEUE_COLUMNS,
            "queue_defaults": GROUP_TOPIC_WINDOW_QUEUE_DEFAULTS,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


GROUP_TOPIC_WINDOW_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def _queue_column_definitions(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        str(row[1]): {
            "type": str(row[2]).upper(),
            "notnull": int(row[3]),
            "default": None if row[4] is None else str(row[4]),
        }
        for row in conn.execute(f"PRAGMA table_info({GROUP_TOPIC_WINDOW_QUEUE_TABLE})")
    }


def _definition_mismatches(column_definitions: dict[str, dict]) -> list[dict]:
    return [
        {
            "column": name,
            "expected": GROUP_TOPIC_WINDOW_QUEUE_DEFINITIONS[name],
            "actual": column_definitions[name],
        }
        for name in sorted(GROUP_TOPIC_WINDOW_QUEUE_DEFINITIONS)
        if (
            name in column_definitions
            and column_definitions[name] != GROUP_TOPIC_WINDOW_QUEUE_DEFINITIONS[name]
        )
    ]


def _schema_drift_error(missing_columns: list[str], definition_mismatches: list[dict]) -> MigrationDriftError:
    diagnostics = ",".join(missing_columns)
    if definition_mismatches:
        mismatch_columns = ",".join(item["column"] for item in definition_mismatches)
        diagnostics = "|".join(
            value
            for value in (diagnostics, "definition_mismatches=" + mismatch_columns)
            if value
        )
    return MigrationDriftError("group_topic_window_schema_drift:" + diagnostics)


def apply_group_topic_window_v1(conn: sqlite3.Connection) -> None:
    """Add only the v2 topic-window candidate fields missing from the queue."""

    column_definitions = _queue_column_definitions(conn)
    definition_mismatches = _definition_mismatches(column_definitions)
    if definition_mismatches:
        raise _schema_drift_error([], definition_mismatches)
    existing_columns = set(column_definitions)
    for name, column_type in GROUP_TOPIC_WINDOW_QUEUE_COLUMNS.items():
        if name in existing_columns:
            continue
        conn.execute(
            f"ALTER TABLE {GROUP_TOPIC_WINDOW_QUEUE_TABLE} ADD COLUMN "
            f"{name} {column_type} NOT NULL DEFAULT {GROUP_TOPIC_WINDOW_QUEUE_DEFAULTS[name]}",
        )


def inspect_group_topic_window_schema(conn: sqlite3.Connection) -> dict:
    """Return stable missing-column diagnostics for the v2 queue extension."""

    column_definitions = _queue_column_definitions(conn)
    missing_columns = sorted(set(GROUP_TOPIC_WINDOW_QUEUE_COLUMNS) - set(column_definitions))
    definition_mismatches = _definition_mismatches(column_definitions)
    return {
        "ok": not missing_columns and not definition_mismatches,
        "contract_checksum": GROUP_TOPIC_WINDOW_MIGRATION_CHECKSUM,
        "missing_columns": missing_columns,
        "definition_mismatches": definition_mismatches,
    }


def require_group_topic_window_schema(conn: sqlite3.Connection) -> dict:
    """Fail closed when an applied v2 queue extension has drifted."""

    audit = inspect_group_topic_window_schema(conn)
    if not audit["ok"]:
        raise _schema_drift_error(
            audit["missing_columns"],
            audit["definition_mismatches"],
        )
    return audit


__all__ = [
    "GROUP_TOPIC_WINDOW_QUEUE_TABLE",
    "GROUP_TOPIC_WINDOW_QUEUE_COLUMNS",
    "GROUP_TOPIC_WINDOW_QUEUE_DEFAULTS",
    "GROUP_TOPIC_WINDOW_QUEUE_DEFINITIONS",
    "GROUP_TOPIC_WINDOW_MIGRATION_CHECKSUM",
    "apply_group_topic_window_v1",
    "inspect_group_topic_window_schema",
    "require_group_topic_window_schema",
]
