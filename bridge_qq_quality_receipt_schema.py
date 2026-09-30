#!/usr/bin/env python3
"""Additive Assistant Core schema for body-free QQ quality receipts (v41)."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import Migration, MigrationDriftError


QUALITY_RECEIPT_FEATURE_FLAG = "qq_quality_receipt_v1"
QUALITY_RECEIPT_TABLE = "qq_quality_receipts"
QUALITY_RECEIPT_COLUMNS = (
    "id", "decision_id", "inbound_ref", "conversation_ref", "outcome", "reason_code",
    "response_class", "grounding_status", "evidence_refs_json", "observation_status",
    "truth_verdict", "persona_verdict", "rewrite_or_block_code", "delivery_id",
    "delivery_status", "client_projection_status", "continuity_ref", "created_at", "updated_at",
)
QUALITY_RECEIPT_INDEXES = (
    "idx_qq_quality_receipts_conversation",
    "idx_qq_quality_receipts_decision",
    "idx_qq_quality_receipts_delivery",
)


def _payload() -> str:
    return json.dumps(
        {
            "table": QUALITY_RECEIPT_TABLE,
            "columns": list(QUALITY_RECEIPT_COLUMNS),
            "indexes": list(QUALITY_RECEIPT_INDEXES),
            "feature_flag": QUALITY_RECEIPT_FEATURE_FLAG,
            "body_free": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


QUALITY_RECEIPT_MIGRATION_CHECKSUM = hashlib.sha256(_payload().encode("utf-8")).hexdigest()


def apply_qq_quality_receipt_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {QUALITY_RECEIPT_TABLE} (
            id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            inbound_ref TEXT NOT NULL,
            conversation_ref TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('replied','silent','blocked','error')),
            reason_code TEXT NOT NULL,
            response_class TEXT NOT NULL,
            grounding_status TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            observation_status TEXT NOT NULL,
            truth_verdict TEXT NOT NULL,
            persona_verdict TEXT NOT NULL,
            rewrite_or_block_code TEXT NOT NULL DEFAULT '',
            delivery_id TEXT NOT NULL DEFAULT '',
            delivery_status TEXT NOT NULL DEFAULT 'not_applicable',
            client_projection_status TEXT NOT NULL DEFAULT 'unverified',
            continuity_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_qq_quality_receipts_conversation
        ON {QUALITY_RECEIPT_TABLE}(conversation_ref,created_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_qq_quality_receipts_decision
        ON {QUALITY_RECEIPT_TABLE}(decision_id,created_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_qq_quality_receipts_delivery
        ON {QUALITY_RECEIPT_TABLE}(delivery_id,updated_at DESC,id DESC)
        WHERE delivery_id <> '';
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('{QUALITY_RECEIPT_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


QUALITY_RECEIPT_MIGRATION = Migration(
    version=41,
    name="qq_quality_receipt_v1",
    apply=apply_qq_quality_receipt_v1,
    checksum=QUALITY_RECEIPT_MIGRATION_CHECKSUM,
)


def quality_receipt_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (QUALITY_RECEIPT_FEATURE_FLAG,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and int(row[0]))


def set_quality_receipt_feature(conn: sqlite3.Connection, *, enabled: bool) -> None:
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE name=?",
        (1 if enabled else 0, QUALITY_RECEIPT_FEATURE_FLAG),
    )


def require_qq_quality_receipt_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if QUALITY_RECEIPT_TABLE not in tables:
        raise MigrationDriftError("qq_quality_receipt_schema_drift:table")
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({QUALITY_RECEIPT_TABLE})")}
    missing_columns = sorted(set(QUALITY_RECEIPT_COLUMNS) - columns)
    missing_indexes = sorted(set(QUALITY_RECEIPT_INDEXES) - indexes)
    flag = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (QUALITY_RECEIPT_FEATURE_FLAG,),
    ).fetchone()
    if missing_columns or missing_indexes or flag is None:
        details: list[str] = []
        if missing_columns:
            details.append("columns=" + ",".join(missing_columns))
        if missing_indexes:
            details.append("indexes=" + ",".join(missing_indexes))
        if flag is None:
            details.append("feature_flag")
        raise MigrationDriftError("qq_quality_receipt_schema_drift:" + "|".join(details))
    return {
        "ok": True,
        "columns": list(QUALITY_RECEIPT_COLUMNS),
        "feature_enabled": bool(int(flag[0])),
        "contract_checksum": QUALITY_RECEIPT_MIGRATION_CHECKSUM,
    }


__all__ = [
    "QUALITY_RECEIPT_COLUMNS",
    "QUALITY_RECEIPT_FEATURE_FLAG",
    "QUALITY_RECEIPT_INDEXES",
    "QUALITY_RECEIPT_MIGRATION",
    "QUALITY_RECEIPT_MIGRATION_CHECKSUM",
    "apply_qq_quality_receipt_v1",
    "quality_receipt_enabled",
    "require_qq_quality_receipt_schema",
    "set_quality_receipt_feature",
]
