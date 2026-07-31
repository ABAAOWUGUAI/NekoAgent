#!/usr/bin/env python3
"""AC-3 additive schema for one durable outbound delivery owner."""

from __future__ import annotations

import hashlib
import json
import sqlite3


UNIFIED_DELIVERY_FEATURE_FLAG = "unified_delivery_v1"

DELIVERY_COLUMNS = {
    "logical_response_id": "TEXT NOT NULL DEFAULT ''",
    "source_message_id": "TEXT NOT NULL DEFAULT ''",
    "engagement_decision_id": "TEXT NOT NULL DEFAULT ''",
    "platform_message_id": "TEXT NOT NULL DEFAULT ''",
    "delivery_certainty": "TEXT NOT NULL DEFAULT 'pending'",
    "thread_ref": "TEXT NOT NULL DEFAULT ''",
    "response_sequence": "INTEGER NOT NULL DEFAULT 0",
    "superseded_by": "TEXT NOT NULL DEFAULT ''",
    "delivery_class": "TEXT NOT NULL DEFAULT 'operational'",
}


def _contract_payload() -> str:
    return json.dumps(
        {
            "feature_flag": UNIFIED_DELIVERY_FEATURE_FLAG,
            "delivery_columns": DELIVERY_COLUMNS,
            "thread_sequence_table": "delivery_thread_sequences",
            "response_reservation_table": "delivery_response_reservations",
            "attempt_states": ["claimed", "sending", "confirmed", "rejected", "ambiguous"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


DELIVERY_CONTINUITY_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def apply_delivery_continuity_v1(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "delivery_outbox" not in tables:
        from bridge_delivery_outbox import ensure_delivery_outbox_table

        ensure_delivery_outbox_table(conn)

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(delivery_outbox)")}
    for name, declaration in DELIVERY_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE delivery_outbox ADD COLUMN {name} {declaration}")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox_logical_response
        ON delivery_outbox(logical_response_id)
        WHERE logical_response_id <> ''
        """,
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_delivery_outbox_thread_sequence
        ON delivery_outbox(channel,thread_ref,response_sequence,created_at,id)
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_thread_sequences (
            channel TEXT NOT NULL,
            thread_ref TEXT NOT NULL,
            current_sequence INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(channel,thread_ref)
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_response_reservations (
            channel TEXT NOT NULL,
            thread_ref TEXT NOT NULL,
            reservation_key TEXT NOT NULL,
            response_sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(channel,thread_ref,reservation_key)
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_attempts (
            id TEXT PRIMARY KEY,
            delivery_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            lease_token_hash TEXT NOT NULL,
            worker_ref TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            certainty TEXT NOT NULL,
            platform_message_id TEXT NOT NULL DEFAULT '',
            error_kind TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            send_started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(delivery_id,attempt_no)
        )
        """,
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_delivery_attempts_delivery
        ON delivery_attempts(delivery_id,attempt_no DESC)
        """,
    )


def inspect_delivery_continuity_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing_tables = sorted({
        "delivery_outbox", "delivery_attempts", "delivery_thread_sequences",
        "delivery_response_reservations",
    } - tables)
    columns: set[str] = set()
    if "delivery_outbox" in tables:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(delivery_outbox)")}
    return {
        "ok": not missing_tables and set(DELIVERY_COLUMNS).issubset(columns),
        "contract_checksum": DELIVERY_CONTINUITY_MIGRATION_CHECKSUM,
        "missing_tables": missing_tables,
        "missing_columns": sorted(set(DELIVERY_COLUMNS) - columns),
    }


__all__ = [
    "DELIVERY_COLUMNS",
    "DELIVERY_CONTINUITY_MIGRATION_CHECKSUM",
    "UNIFIED_DELIVERY_FEATURE_FLAG",
    "apply_delivery_continuity_v1",
    "inspect_delivery_continuity_schema",
]
