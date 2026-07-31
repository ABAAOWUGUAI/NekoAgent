#!/usr/bin/env python3
"""Gate 3 additive Conversation Thread and scoped Memory schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Mapping

from bridge_assistant_identity_schema import DEFAULT_OWNER_ACTOR_ID, require_identity_schema
from bridge_migrations import MigrationDriftError, utc_now


MEMORY_SCOPE_FEATURE_FLAG = "memory_scope_v2"
THREAD_NAMESPACE = uuid.UUID("fd181aa8-9f0c-4a3d-a6ec-88a4f6b241a7")
MESSAGE_NAMESPACE = uuid.UUID("c8a7501f-d1e6-477c-b93a-0ae8fd714e73")
MEMORY_NAMESPACE = uuid.UUID("986224cb-c0cb-4333-9559-88b60e1c2fdb")

SCOPE_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "conversation_threads": (
        "id", "owner_actor_id", "assistant_id", "channel_type",
        "external_thread_ref", "subject_actor_ref", "project_id", "status",
        "legacy_user_id", "created_at", "updated_at",
    ),
    "conversation_messages": (
        "id", "thread_id", "role", "content", "source_type",
        "legacy_conversation_id", "created_at",
    ),
    "memory_records": (
        "id", "owner_actor_id", "assistant_id", "subject_actor_ref",
        "scope_type", "scope_id", "kind", "content", "source", "score",
        "sensitivity", "consent_basis", "source_thread_id",
        "source_message_id", "expires_at", "last_used_at", "status",
        "legacy_memory_id", "created_at", "updated_at",
    ),
}
SCOPE_REQUIRED_INDEXES = (
    "idx_conversation_threads_unique",
    "idx_conversation_threads_owner",
    "idx_conversation_messages_thread",
    "idx_conversation_messages_legacy",
    "idx_memory_records_scope",
    "idx_memory_records_owner",
    "idx_memory_records_legacy",
)


def _stable_id(namespace: uuid.UUID, *parts: object) -> str:
    return uuid.uuid5(namespace, "\x1f".join(str(part or "") for part in parts)).hex


def _active_assistant(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT id,owner_actor_id FROM assistant_instances
        WHERE status='active' ORDER BY created_at LIMIT 1
        """,
    ).fetchone()
    if not row:
        raise MigrationDriftError("conversation_memory_source_missing:active_assistant")
    return str(row[0]), str(row[1])


def _classify_legacy_key(value: str) -> str:
    key = str(value or "").strip()
    if key == "global":
        return "legacy_unknown"
    if key.startswith("group:"):
        return "qq_group"
    if key in {"web-console", "admin", "web"}:
        return "web"
    if key.isdigit():
        return "qq_private"
    return "legacy_unknown"


def conversation_memory_source_preflight(conn: sqlite3.Connection) -> dict:
    """Validate sources without exposing message, memory, account, or group data."""

    require_identity_schema(conn)
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {"conversations", "memories", "assistant_instances"}
    missing = sorted(required - tables)
    if missing:
        raise MigrationDriftError(
            "conversation_memory_source_missing:" + ",".join(missing),
        )
    assistant_id, owner_actor_id = _active_assistant(conn)
    counts = {
        "legacy_conversations": int(
            conn.execute("SELECT count(*) FROM conversations").fetchone()[0],
        ),
        "legacy_memories": int(
            conn.execute("SELECT count(*) FROM memories").fetchone()[0],
        ),
        "legacy_thread_keys": int(
            conn.execute(
                """
                SELECT count(*) FROM (
                    SELECT user_id FROM conversations
                    UNION SELECT user_id FROM memories WHERE user_id <> 'global'
                )
                """,
            ).fetchone()[0],
        ),
    }
    return {
        "ok": True,
        **counts,
        "active_assistant": bool(assistant_id),
        "owner_bound": bool(owner_actor_id),
    }


def _legacy_thread(
    assistant_id: str,
    owner_actor_id: str,
    legacy_key: str,
) -> dict:
    channel = _classify_legacy_key(legacy_key)
    thread_id = "thread-" + _stable_id(
        THREAD_NAMESPACE,
        assistant_id,
        channel,
        legacy_key,
    )
    return {
        "id": thread_id,
        "owner_actor_id": owner_actor_id,
        "assistant_id": assistant_id,
        "channel_type": channel,
        "external_thread_ref": legacy_key,
        "subject_actor_ref": legacy_key,
        "legacy_user_id": legacy_key,
    }


def apply_conversation_memory_scope_v2(conn: sqlite3.Connection) -> None:
    """Create v4 tables and conservatively backfill legacy rows."""

    conversation_memory_source_preflight(conn)
    statements = (
        """
        CREATE TABLE conversation_threads (
            id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            channel_type TEXT NOT NULL CHECK(
                channel_type IN ('web','qq_private','qq_group','legacy_unknown')
            ),
            external_thread_ref TEXT NOT NULL,
            subject_actor_ref TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','archived')),
            legacy_user_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE conversation_messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES conversation_threads(id) ON DELETE RESTRICT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            source_type TEXT NOT NULL,
            legacy_conversation_id INTEGER UNIQUE,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE memory_records (
            id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL,
            assistant_id TEXT REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            subject_actor_ref TEXT NOT NULL DEFAULT '',
            scope_type TEXT NOT NULL CHECK(
                scope_type IN (
                    'thread','qq_group','project','assistant_private',
                    'owner_private','global_preference','sensitive_private'
                )
            ),
            scope_id TEXT,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 5,
            sensitivity TEXT NOT NULL CHECK(
                sensitivity IN ('normal','private','sensitive')
            ),
            consent_basis TEXT NOT NULL,
            source_thread_id TEXT REFERENCES conversation_threads(id) ON DELETE RESTRICT,
            source_message_id TEXT REFERENCES conversation_messages(id) ON DELETE RESTRICT,
            expires_at TEXT,
            last_used_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','paused','deleted')),
            legacy_memory_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_threads_unique
        ON conversation_threads(assistant_id,channel_type,external_thread_ref)
        """,
        """
        CREATE INDEX idx_conversation_threads_owner
        ON conversation_threads(owner_actor_id,status,updated_at DESC)
        """,
        """
        CREATE INDEX idx_conversation_messages_thread
        ON conversation_messages(thread_id,created_at,id)
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_messages_legacy
        ON conversation_messages(legacy_conversation_id)
        WHERE legacy_conversation_id IS NOT NULL
        """,
        """
        CREATE INDEX idx_memory_records_scope
        ON memory_records(scope_type,scope_id,status,updated_at DESC)
        """,
        """
        CREATE INDEX idx_memory_records_owner
        ON memory_records(owner_actor_id,assistant_id,status,updated_at DESC)
        """,
        """
        CREATE UNIQUE INDEX idx_memory_records_legacy
        ON memory_records(legacy_memory_id)
        WHERE legacy_memory_id IS NOT NULL
        """,
    )
    for statement in statements:
        conn.execute(statement)

    assistant_id, owner_actor_id = _active_assistant(conn)
    legacy_keys = [
        str(row[0] or "")
        for row in conn.execute(
            """
            SELECT user_id FROM conversations
            UNION SELECT user_id FROM memories WHERE user_id <> 'global'
            ORDER BY 1
            """,
        ).fetchall()
    ]
    now = utc_now()
    threads: dict[str, dict] = {}
    for key in legacy_keys:
        item = _legacy_thread(assistant_id, owner_actor_id, key)
        threads[key] = item
        timestamps = conn.execute(
            """
            SELECT min(created_at),max(created_at) FROM (
                SELECT created_at FROM conversations WHERE user_id=?
                UNION ALL SELECT created_at FROM memories WHERE user_id=?
            )
            """,
            (key, key),
        ).fetchone()
        created_at = str(timestamps[0] or now)
        updated_at = str(timestamps[1] or created_at)
        conn.execute(
            """
            INSERT INTO conversation_threads(
                id,owner_actor_id,assistant_id,channel_type,external_thread_ref,
                subject_actor_ref,project_id,status,legacy_user_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,NULL,'active',?,?,?)
            """,
            (
                item["id"], owner_actor_id, assistant_id, item["channel_type"],
                key, key, key, created_at, updated_at,
            ),
        )

    for row in conn.execute(
        "SELECT id,user_id,role,content,created_at FROM conversations ORDER BY id",
    ):
        legacy_id, key, role, content, created_at = row
        thread = threads[str(key or "")]
        message_id = "message-" + _stable_id(MESSAGE_NAMESPACE, legacy_id)
        conn.execute(
            """
            INSERT INTO conversation_messages(
                id,thread_id,role,content,source_type,legacy_conversation_id,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                message_id, thread["id"], str(role or ""), str(content or ""),
                thread["channel_type"], int(legacy_id), str(created_at or now),
            ),
        )

    for row in conn.execute(
        """
        SELECT id,user_id,kind,content,source,score,deleted,
               created_at,updated_at,last_used_at
        FROM memories ORDER BY created_at,id
        """,
    ):
        (
            legacy_id, key, kind, content, source, score, deleted,
            created_at, updated_at, last_used_at,
        ) = row
        key = str(key or "")
        thread = threads.get(key)
        if key == "global":
            scope_type = "global_preference"
            scope_id = owner_actor_id
            source_thread_id = None
            assistant_ref = None
            sensitivity = "normal"
        else:
            source_thread_id = thread["id"]
            assistant_ref = assistant_id
            sensitivity = "private"
            if thread["channel_type"] == "qq_group":
                scope_type = "qq_group"
                scope_id = thread["external_thread_ref"]
            else:
                scope_type = "thread"
                scope_id = thread["id"]
        record_id = "memory-" + _stable_id(MEMORY_NAMESPACE, legacy_id)
        conn.execute(
            """
            INSERT INTO memory_records(
                id,owner_actor_id,assistant_id,subject_actor_ref,scope_type,
                scope_id,kind,content,source,score,sensitivity,consent_basis,
                source_thread_id,source_message_id,expires_at,last_used_at,
                status,legacy_memory_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?)
            """,
            (
                record_id, owner_actor_id, assistant_ref, key, scope_type,
                scope_id, str(kind or "fact"), str(content or ""),
                str(source or "legacy"), int(score or 0), sensitivity,
                "legacy_explicit", source_thread_id, last_used_at,
                "deleted" if int(deleted or 0) else "active", str(legacy_id),
                str(created_at or now), str(updated_at or created_at or now),
            ),
        )
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        ON CONFLICT(name) DO NOTHING
        """,
        (MEMORY_SCOPE_FEATURE_FLAG, now),
    )


def inspect_conversation_memory_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_tables = sorted(set(SCOPE_TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in SCOPE_TABLE_COLUMNS.items():
        if table not in tables:
            continue
        present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - present)
        if missing:
            missing_columns[table] = missing
    missing_indexes = sorted(set(SCOPE_REQUIRED_INDEXES) - indexes)
    foreign_key_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    duplicate_messages = 0
    duplicate_memories = 0
    if not missing_tables:
        duplicate_messages = int(
            conn.execute(
                """
                SELECT count(*) FROM (
                    SELECT legacy_conversation_id FROM conversation_messages
                    WHERE legacy_conversation_id IS NOT NULL
                    GROUP BY legacy_conversation_id HAVING count(*) > 1
                )
                """,
            ).fetchone()[0],
        )
        duplicate_memories = int(
            conn.execute(
                """
                SELECT count(*) FROM (
                    SELECT legacy_memory_id FROM memory_records
                    WHERE legacy_memory_id IS NOT NULL
                    GROUP BY legacy_memory_id HAVING count(*) > 1
                )
                """,
            ).fetchone()[0],
        )
    ok = not (
        missing_tables or missing_columns or missing_indexes
        or foreign_key_errors or duplicate_messages or duplicate_memories
    )
    return {
        "ok": ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "foreign_key_error_count": len(foreign_key_errors),
        "duplicate_legacy_messages": duplicate_messages,
        "duplicate_legacy_memories": duplicate_memories,
    }


def require_conversation_memory_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_conversation_memory_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "conversation_memory_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


SCOPE_MIGRATION_CONTRACT = {
    "tables": {key: list(value) for key, value in SCOPE_TABLE_COLUMNS.items()},
    "indexes": list(SCOPE_REQUIRED_INDEXES),
    "flag": MEMORY_SCOPE_FEATURE_FLAG,
    "legacy_policy": "fail_closed_thread_scope",
}
SCOPE_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(SCOPE_MIGRATION_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


__all__ = [
    "MEMORY_SCOPE_FEATURE_FLAG",
    "SCOPE_MIGRATION_CHECKSUM",
    "apply_conversation_memory_scope_v2",
    "conversation_memory_source_preflight",
    "inspect_conversation_memory_schema",
    "require_conversation_memory_schema",
]
