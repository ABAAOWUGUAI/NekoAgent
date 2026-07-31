#!/usr/bin/env python3
"""Additive AC-1 schema for normalized events and shadow participation facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


PARTICIPATION_SHADOW_FEATURE_FLAG = "conversation_participation_shadow_v1"

CONVERSATION_MESSAGE_ADDITIVE_COLUMNS = {
    "actor_ref": "TEXT NOT NULL DEFAULT ''",
    "external_message_id": "TEXT NOT NULL DEFAULT ''",
    "reply_to_external_message_id": "TEXT NOT NULL DEFAULT ''",
    "directed_to_assistant": "INTEGER NOT NULL DEFAULT 0",
    "message_kind": "TEXT NOT NULL DEFAULT 'text'",
    "retention_class": "TEXT NOT NULL DEFAULT 'conversation'",
    "expires_at": "TEXT NOT NULL DEFAULT ''",
    "body_redacted_at": "TEXT NOT NULL DEFAULT ''",
    "engagement_decision_id": "TEXT NOT NULL DEFAULT ''",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}

GROUP_MESSAGE_ADDITIVE_COLUMNS = {
    "external_message_id": "TEXT NOT NULL DEFAULT ''",
    "content_sha256": "TEXT NOT NULL DEFAULT ''",
    "content_length": "INTEGER NOT NULL DEFAULT 0",
    "retention_class": "TEXT NOT NULL DEFAULT 'conversation'",
    "expires_at": "TEXT NOT NULL DEFAULT ''",
    "body_redacted_at": "TEXT NOT NULL DEFAULT ''",
    "engagement_decision_id": "TEXT NOT NULL DEFAULT ''",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}

GROUP_MESSAGE_BASE_COLUMNS = (
    "id", "group_id", "sender_id", "sender_name", "content", "is_mention",
    "decision", "decision_reason", "replied", "created_at",
)

PARTICIPATION_TABLE_COLUMNS = {
    "conversation_events": (
        "id", "channel_type", "channel_instance_id", "external_message_id",
        "external_thread_ref", "assistant_id", "actor_ref", "actor_role",
        "conversation_scope", "message_kind", "text_sha256", "text_length",
        "mention_targets_json", "reply_to_external_message_id",
        "reply_to_assistant", "component_kinds_json", "attachment_count",
        "delivery_capabilities_json", "event_fingerprint", "created_at",
    ),
    "engagement_decisions": (
        "id", "event_id", "assistant_id", "thread_id", "source_message_id",
        "candidate_kind", "action", "reason_code", "policy_version",
        "legacy_allowed", "legacy_reason", "shadow_match", "model_role",
        "model_id", "confidence", "decision_json", "expires_at",
        "superseded_by", "created_at",
    ),
    "conversation_participation_state": (
        "thread_ref", "assistant_id", "phase", "waiting_for_actor_ref",
        "waiting_until", "last_assistant_message_id", "latest_response_id",
        "cooldown_until", "unanswered_count", "policy_version", "updated_at",
    ),
}

PARTICIPATION_INDEXES = (
    "idx_conversation_events_thread",
    "idx_conversation_events_external",
    "idx_engagement_decisions_event",
    "idx_engagement_decisions_thread",
    "idx_group_messages_retention",
    "idx_conversation_messages_external",
)


def _contract_payload() -> str:
    return json.dumps(
        {
            "feature_flag": PARTICIPATION_SHADOW_FEATURE_FLAG,
            "conversation_message_columns": CONVERSATION_MESSAGE_ADDITIVE_COLUMNS,
            "group_message_columns": GROUP_MESSAGE_ADDITIVE_COLUMNS,
            "group_message_base_columns": list(GROUP_MESSAGE_BASE_COLUMNS),
            "tables": {key: list(value) for key, value in PARTICIPATION_TABLE_COLUMNS.items()},
            "indexes": list(PARTICIPATION_INDEXES),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


PARTICIPATION_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    existing = _columns(conn, table)
    if not existing:
        raise MigrationDriftError(f"participation_base_table_missing:{table}")
    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply_conversation_participation_v1(conn: sqlite3.Connection) -> None:
    # ``group_messages`` predates the registered migration chain and used to be
    # created only by the social compatibility bootstrap.  Create the exact
    # legacy base shape here as well so a fresh registered database does not
    # depend on call order outside the migration runner.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            sender_id TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL, is_mention INTEGER NOT NULL DEFAULT 0,
            decision TEXT NOT NULL DEFAULT '', decision_reason TEXT NOT NULL DEFAULT '',
            replied INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        )""",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_messages_group ON group_messages(group_id,id)",
    )
    _add_columns(conn, "conversation_messages", CONVERSATION_MESSAGE_ADDITIVE_COLUMNS)
    _add_columns(conn, "group_messages", GROUP_MESSAGE_ADDITIVE_COLUMNS)
    conn.executescript(
        """
        CREATE TABLE conversation_events (
            id TEXT PRIMARY KEY,
            channel_type TEXT NOT NULL,
            channel_instance_id TEXT NOT NULL DEFAULT '',
            external_message_id TEXT NOT NULL DEFAULT '',
            external_thread_ref TEXT NOT NULL,
            assistant_id TEXT NOT NULL,
            actor_ref TEXT NOT NULL,
            actor_role TEXT NOT NULL DEFAULT 'user',
            conversation_scope TEXT NOT NULL,
            message_kind TEXT NOT NULL,
            text_sha256 TEXT NOT NULL DEFAULT '',
            text_length INTEGER NOT NULL DEFAULT 0,
            mention_targets_json TEXT NOT NULL DEFAULT '[]',
            reply_to_external_message_id TEXT NOT NULL DEFAULT '',
            reply_to_assistant INTEGER NOT NULL DEFAULT 0,
            component_kinds_json TEXT NOT NULL DEFAULT '[]',
            attachment_count INTEGER NOT NULL DEFAULT 0,
            delivery_capabilities_json TEXT NOT NULL DEFAULT '[]',
            event_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_conversation_events_thread
        ON conversation_events(assistant_id,external_thread_ref,created_at DESC);
        CREATE UNIQUE INDEX idx_conversation_events_external
        ON conversation_events(channel_type,channel_instance_id,external_message_id)
        WHERE external_message_id <> '';

        CREATE TABLE engagement_decisions (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES conversation_events(id) ON DELETE RESTRICT,
            assistant_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            source_message_id TEXT NOT NULL DEFAULT '',
            candidate_kind TEXT NOT NULL,
            action TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            legacy_allowed INTEGER NOT NULL DEFAULT 0,
            legacy_reason TEXT NOT NULL DEFAULT '',
            shadow_match INTEGER NOT NULL DEFAULT 1,
            model_role TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 1.0,
            decision_json TEXT NOT NULL DEFAULT '{}',
            expires_at TEXT NOT NULL DEFAULT '',
            superseded_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_engagement_decisions_event
        ON engagement_decisions(event_id,created_at DESC);
        CREATE INDEX idx_engagement_decisions_thread
        ON engagement_decisions(assistant_id,thread_id,created_at DESC);

        CREATE TABLE conversation_participation_state (
            thread_ref TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'idle',
            waiting_for_actor_ref TEXT NOT NULL DEFAULT '',
            waiting_until TEXT NOT NULL DEFAULT '',
            last_assistant_message_id TEXT NOT NULL DEFAULT '',
            latest_response_id TEXT NOT NULL DEFAULT '',
            cooldown_until TEXT NOT NULL DEFAULT '',
            unanswered_count INTEGER NOT NULL DEFAULT 0,
            policy_version TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE INDEX idx_group_messages_retention
        ON group_messages(group_id,retention_class,expires_at,id DESC);
        CREATE INDEX idx_conversation_messages_external
        ON conversation_messages(external_message_id)
        WHERE external_message_id <> '';

        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('conversation_participation_shadow_v1',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


def inspect_conversation_participation_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_tables = sorted(set(PARTICIPATION_TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in PARTICIPATION_TABLE_COLUMNS.items():
        if table in tables:
            missing = sorted(set(required) - _columns(conn, table))
            if missing:
                missing_columns[table] = missing
    for table, required in (
        ("conversation_messages", CONVERSATION_MESSAGE_ADDITIVE_COLUMNS),
        (
            "group_messages",
            (*GROUP_MESSAGE_BASE_COLUMNS, *GROUP_MESSAGE_ADDITIVE_COLUMNS),
        ),
    ):
        if table not in tables:
            missing_tables.append(table)
            continue
        missing = sorted(set(required) - _columns(conn, table))
        if missing:
            missing_columns[table] = missing
    flag = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (PARTICIPATION_SHADOW_FEATURE_FLAG,),
    ).fetchone() if "assistant_feature_flags" in tables else None
    missing_indexes = sorted(set(PARTICIPATION_INDEXES) - indexes)
    return {
        "ok": not missing_tables and not missing_columns and not missing_indexes and flag is not None,
        "contract_checksum": PARTICIPATION_MIGRATION_CHECKSUM,
        "missing_tables": sorted(set(missing_tables)),
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "feature_flag_present": flag is not None,
        "feature_enabled": bool(int(flag[0])) if flag is not None else False,
    }


def require_conversation_participation_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_conversation_participation_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "conversation_participation_schema_drift:"
            + json.dumps(
                {
                    "tables": audit["missing_tables"],
                    "columns": audit["missing_columns"],
                    "indexes": audit["missing_indexes"],
                    "feature_flag": audit["feature_flag_present"],
                },
                sort_keys=True,
            ),
        )
    return audit


__all__ = [
    "CONVERSATION_MESSAGE_ADDITIVE_COLUMNS",
    "GROUP_MESSAGE_ADDITIVE_COLUMNS",
    "GROUP_MESSAGE_BASE_COLUMNS",
    "PARTICIPATION_MIGRATION_CHECKSUM",
    "PARTICIPATION_SHADOW_FEATURE_FLAG",
    "apply_conversation_participation_v1",
    "inspect_conversation_participation_schema",
    "require_conversation_participation_schema",
]
