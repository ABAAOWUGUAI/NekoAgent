#!/usr/bin/env python3
"""PI-4 additive schema for auditable Living Wiki projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


LIVING_WIKI_COLUMNS = {
    "content_hash": "TEXT NOT NULL DEFAULT ''",
    "source_hash": "TEXT NOT NULL DEFAULT ''",
    "evidence_refs_json": "TEXT NOT NULL DEFAULT '[]'",
    "freshness_status": "TEXT NOT NULL DEFAULT 'unverified'",
    "fresh_until": "TEXT NOT NULL DEFAULT ''",
    "last_verified_at": "TEXT NOT NULL DEFAULT ''",
}

LIVING_WIKI_TABLE_COLUMNS = {
    "assistant_knowledge_revisions": (
        "id", "assistant_id", "item_id", "version", "operation",
        "snapshot_json", "content_hash", "source_hash", "evidence_refs_json",
        "freshness_status", "created_by", "created_at",
    ),
    "assistant_knowledge_retrieval_audit": (
        "id", "assistant_id", "query_hash", "channel", "item_id",
        "signals_json", "score", "injected", "reason", "created_at",
    ),
    "assistant_knowledge_search_meta": (
        "id", "backend", "updated_at",
    ),
}

LIVING_WIKI_INDEXES = (
    "idx_knowledge_revisions_item",
    "idx_knowledge_retrieval_recent",
    "idx_knowledge_freshness",
)

LIVING_WIKI_TRIGGERS = (
    "trg_knowledge_revisions_immutable_update",
    "trg_knowledge_revisions_immutable_delete",
)


def _contract_payload() -> str:
    return json.dumps(
        {
            "knowledge_columns": LIVING_WIKI_COLUMNS,
            "tables": {key: list(value) for key, value in LIVING_WIKI_TABLE_COLUMNS.items()},
            "indexes": list(LIVING_WIKI_INDEXES),
            "triggers": list(LIVING_WIKI_TRIGGERS),
            "search_backends": ["fts5_trigram", "like_fallback"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


LIVING_WIKI_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def _content_hash(content: object) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _source_hash(row: dict) -> str:
    payload = {
        key: str(row.get(key) or "")
        for key in (
            "source_type", "source_ref", "source_memory_id", "source_thread_id",
            "source_scope_type", "consent_basis",
        )
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()


def _rows(conn: sqlite3.Connection, query: str) -> list[dict]:
    cursor = conn.execute(query)
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _snapshot(row: dict) -> str:
    fields = (
        "title", "content", "audience", "status", "source_type", "source_ref",
        "version", "created_by", "reviewed_by", "published_at", "kind", "summary",
        "tags_json", "confidence", "source_memory_id", "source_thread_id",
        "source_scope_type", "consent_basis", "supersedes_id", "review_note",
        "content_hash", "source_hash", "evidence_refs_json", "freshness_status",
        "fresh_until", "last_verified_at",
    )
    return json.dumps(
        {key: row.get(key) for key in fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _create_search_projection(conn: sqlite3.Connection) -> str:
    try:
        conn.execute(
            """CREATE VIRTUAL TABLE assistant_knowledge_search_fts USING fts5(
                item_id UNINDEXED,assistant_id UNINDEXED,title,summary,content,tags,
                tokenize='trigram'
            )""",
        )
        return "fts5_trigram"
    except sqlite3.OperationalError:
        conn.executescript(
            """
            CREATE TABLE assistant_knowledge_search_fallback (
                item_id TEXT PRIMARY KEY,
                assistant_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_knowledge_search_fallback_assistant
            ON assistant_knowledge_search_fallback(assistant_id,item_id);
            """,
        )
        return "like_fallback"


def _rebuild_search_projection(conn: sqlite3.Connection, backend: str) -> None:
    rows = _rows(
        conn,
        """SELECT id,assistant_id,title,summary,content,tags_json
           FROM assistant_knowledge_items""",
    )
    table = (
        "assistant_knowledge_search_fts"
        if backend == "fts5_trigram"
        else "assistant_knowledge_search_fallback"
    )
    conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        f"""INSERT INTO {table}(item_id,assistant_id,title,summary,content,tags)
            VALUES(?,?,?,?,?,?)""",
        [
            (
                row["id"], row["assistant_id"], row["title"], row["summary"],
                row["content"], row["tags_json"],
            )
            for row in rows
        ],
    )


def apply_living_wiki_v2(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(assistant_knowledge_items)")}
    for name, definition in LIVING_WIKI_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE assistant_knowledge_items ADD COLUMN {name} {definition}")
    conn.executescript(
        """
        CREATE TABLE assistant_knowledge_revisions (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            item_id TEXT NOT NULL REFERENCES assistant_knowledge_items(id) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK(version > 0),
            operation TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            freshness_status TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(item_id,version)
        );
        CREATE TABLE assistant_knowledge_retrieval_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            query_hash TEXT NOT NULL,
            channel TEXT NOT NULL,
            item_id TEXT NOT NULL DEFAULT '',
            signals_json TEXT NOT NULL DEFAULT '{}',
            score REAL NOT NULL DEFAULT 0,
            injected INTEGER NOT NULL DEFAULT 0 CHECK(injected IN (0,1)),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE assistant_knowledge_search_meta (
            id INTEGER PRIMARY KEY CHECK(id=1),
            backend TEXT NOT NULL CHECK(backend IN ('fts5_trigram','like_fallback')),
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_knowledge_revisions_item
        ON assistant_knowledge_revisions(assistant_id,item_id,version DESC);
        CREATE INDEX idx_knowledge_retrieval_recent
        ON assistant_knowledge_retrieval_audit(assistant_id,created_at DESC);
        CREATE INDEX idx_knowledge_freshness
        ON assistant_knowledge_items(assistant_id,status,freshness_status,fresh_until);
        CREATE TRIGGER trg_knowledge_revisions_immutable_update
        BEFORE UPDATE ON assistant_knowledge_revisions
        BEGIN SELECT RAISE(ABORT,'knowledge_revision_immutable'); END;
        CREATE TRIGGER trg_knowledge_revisions_immutable_delete
        BEFORE DELETE ON assistant_knowledge_revisions
        BEGIN SELECT RAISE(ABORT,'knowledge_revision_immutable'); END;
        """,
    )
    backend = _create_search_projection(conn)
    now = utc_now()
    conn.execute(
        "INSERT INTO assistant_knowledge_search_meta(id,backend,updated_at) VALUES(1,?,?)",
        (backend, now),
    )
    rows = _rows(conn, "SELECT * FROM assistant_knowledge_items")
    for row in rows:
        content_hash = _content_hash(row.get("content"))
        source_hash = _source_hash(row)
        conn.execute(
            """UPDATE assistant_knowledge_items
               SET content_hash=?,source_hash=? WHERE id=?""",
            (content_hash, source_hash, row["id"]),
        )
        row.update({"content_hash": content_hash, "source_hash": source_hash})
        conn.execute(
            """INSERT OR IGNORE INTO assistant_knowledge_revisions(
                id,assistant_id,item_id,version,operation,snapshot_json,content_hash,
                source_hash,evidence_refs_json,freshness_status,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"knowledge-revision-{row['id']}-v{row['version']}", row["assistant_id"],
                row["id"], row["version"], "migration_backfill", _snapshot(row),
                content_hash, source_hash, row.get("evidence_refs_json") or "[]",
                row.get("freshness_status") or "unverified", "migration-v18", now,
            ),
        )
    _rebuild_search_projection(conn, backend)


def require_living_wiki_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(assistant_knowledge_items)")}
    missing_item_columns = sorted(set(LIVING_WIKI_COLUMNS) - columns)
    missing_tables = sorted(set(LIVING_WIKI_TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, expected in LIVING_WIKI_TABLE_COLUMNS.items():
        if table in tables:
            actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if missing := sorted(set(expected) - actual):
                missing_columns[table] = missing
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_indexes = sorted(set(LIVING_WIKI_INDEXES) - indexes)
    triggers = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    missing_triggers = sorted(set(LIVING_WIKI_TRIGGERS) - triggers)
    meta = conn.execute(
        "SELECT backend FROM assistant_knowledge_search_meta WHERE id=1",
    ).fetchone() if "assistant_knowledge_search_meta" in tables else None
    backend = str(meta[0]) if meta else ""
    search_table = (
        "assistant_knowledge_search_fts" if backend == "fts5_trigram"
        else "assistant_knowledge_search_fallback" if backend == "like_fallback" else ""
    )
    search_missing = not search_table or search_table not in tables
    if any((missing_item_columns, missing_tables, missing_columns, missing_indexes, missing_triggers, search_missing)):
        raise MigrationDriftError(
            "living_wiki_schema_drift:"
            + json.dumps(
                {
                    "item_columns": missing_item_columns,
                    "tables": missing_tables,
                    "columns": missing_columns,
                    "indexes": missing_indexes,
                    "triggers": missing_triggers,
                    "search": search_table if search_missing else "",
                },
                sort_keys=True,
            ),
        )
    return {
        "ok": True,
        "contract_checksum": LIVING_WIKI_MIGRATION_CHECKSUM,
        "search_backend": backend,
    }


__all__ = [
    "LIVING_WIKI_MIGRATION_CHECKSUM",
    "apply_living_wiki_v2",
    "require_living_wiki_schema",
]
