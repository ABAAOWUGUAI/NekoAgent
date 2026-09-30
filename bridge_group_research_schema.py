#!/usr/bin/env python3
"""R7-B/R7-C schema for bounded group research and knowledge provenance.

The schema stores an approved public query and public-source evidence, never a
group transcript.  It is deliberately separate from expression learning:
research facts have their own policies, audit trail and retrieval scope.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


GROUP_RESEARCH_FEATURE_FLAG = "group_research_v1"
GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG = "group_research_autoknowledge_v1"
GROUP_RESEARCH_POLICY_TABLE = "group_research_policies"
GROUP_RESEARCH_RUN_TABLE = "group_research_runs"
GROUP_RESEARCH_EVIDENCE_TABLE = "group_research_evidence"
GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE = "group_research_knowledge_links"
KNOWLEDGE_SCOPE_TABLE = "assistant_knowledge_item_scopes"
GROUP_RESEARCH_SCOPE_MODE_SINGLE = "single_group"
GROUP_RESEARCH_SCOPE_MODE_ADMITTED = "admitted_groups"
GROUP_RESEARCH_SCOPE_MODES = {
    GROUP_RESEARCH_SCOPE_MODE_SINGLE,
    GROUP_RESEARCH_SCOPE_MODE_ADMITTED,
}

GROUP_RESEARCH_MIGRATION_CHECKSUM = hashlib.sha256(
    b"group_research_v1:opt-in-pilot-public-query-evidence-provenance-scoped-retrieval",
).hexdigest()
GROUP_RESEARCH_ADMITTED_SCOPE_MIGRATION_CHECKSUM = hashlib.sha256(
    b"group_research_v2:runtime-qq-allowlist-admitted-groups-scope",
).hexdigest()
GROUP_RESEARCH_QUERY_PRIVACY_MIGRATION_CHECKSUM = hashlib.sha256(
    b"group_research_v3:replace-legacy-query-text-with-opaque-query-hash-label",
).hexdigest()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_group_research_v1(conn: sqlite3.Connection) -> None:
    """Create disabled-by-default R7 policies and auditable public evidence."""

    conn.executescript(
        """
        CREATE TABLE group_research_policies (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL UNIQUE
                REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            pilot_group_id TEXT NOT NULL DEFAULT '',
            auto_publish_low_public INTEGER NOT NULL DEFAULT 0
                CHECK(auto_publish_low_public IN (0,1)),
            max_runs_per_day INTEGER NOT NULL DEFAULT 3 CHECK(max_runs_per_day BETWEEN 0 AND 20),
            max_runs_per_group_day INTEGER NOT NULL DEFAULT 2 CHECK(max_runs_per_group_day BETWEEN 0 AND 10),
            freshness_hours INTEGER NOT NULL DEFAULT 168 CHECK(freshness_hours BETWEEN 1 AND 720),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_by TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE group_research_runs (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            group_id TEXT NOT NULL,
            anchor_message_id TEXT NOT NULL DEFAULT '',
            query_redacted TEXT NOT NULL DEFAULT '',
            query_hash TEXT NOT NULL,
            risk_tier TEXT NOT NULL CHECK(risk_tier IN ('low_public','review_required','restricted_private','prohibited')),
            stage TEXT NOT NULL CHECK(stage IN ('blocked','running','succeeded','failed','expired')),
            reason_code TEXT NOT NULL DEFAULT '',
            source_count INTEGER NOT NULL DEFAULT 0 CHECK(source_count >= 0),
            result_summary TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE group_research_evidence (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES group_research_runs(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL,
            source_domain TEXT NOT NULL,
            source_title TEXT NOT NULL DEFAULT '',
            public_excerpt TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL,
            retrieved_at TEXT NOT NULL
        );

        CREATE TABLE assistant_knowledge_item_scopes (
            knowledge_item_id TEXT NOT NULL
                REFERENCES assistant_knowledge_items(id) ON DELETE CASCADE,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('qq_group')),
            scope_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(knowledge_item_id,scope_type,scope_id)
        );

        CREATE TABLE group_research_knowledge_links (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE REFERENCES group_research_runs(id) ON DELETE RESTRICT,
            knowledge_item_id TEXT NOT NULL UNIQUE
                REFERENCES assistant_knowledge_items(id) ON DELETE RESTRICT,
            sensitivity_tier TEXT NOT NULL CHECK(sensitivity_tier IN ('low_public','review_required')),
            publication_mode TEXT NOT NULL CHECK(publication_mode IN ('auto_published_provisional','review_draft')),
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_group_research_runs_group_created
        ON group_research_runs(assistant_id,group_id,created_at DESC);
        CREATE INDEX idx_group_research_runs_stage
        ON group_research_runs(assistant_id,stage,updated_at DESC);
        CREATE INDEX idx_group_research_evidence_run
        ON group_research_evidence(run_id,retrieved_at DESC);
        CREATE INDEX idx_knowledge_item_scopes_lookup
        ON assistant_knowledge_item_scopes(scope_type,scope_id,knowledge_item_id);
        """,
    )
    now = utc_now()
    assistant_ids = [
        str(row[0])
        for row in conn.execute("SELECT id FROM assistant_instances ORDER BY id").fetchall()
    ]
    if not assistant_ids:
        raise MigrationDriftError("group_research_assistant_missing")
    for assistant_id in assistant_ids:
        conn.execute(
            """INSERT OR IGNORE INTO group_research_policies(
                id,assistant_id,enabled,pilot_group_id,auto_publish_low_public,
                max_runs_per_day,max_runs_per_group_day,freshness_hours,version,
                updated_by,created_at,updated_at
            ) VALUES(?,?,0,'',0,3,2,168,1,'system',?,?)""",
            (f"group-research-policy-{assistant_id}", assistant_id, now, now),
        )
    for flag in (GROUP_RESEARCH_FEATURE_FLAG, GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG):
        conn.execute(
            """INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
               VALUES(?,0,?)""",
            (flag, now),
        )


def apply_group_research_admitted_scope_v2(conn: sqlite3.Connection) -> None:
    """Add a dynamic scope mode without copying QQ admission into R7 state."""

    tables = _tables(conn)
    if GROUP_RESEARCH_POLICY_TABLE not in tables:
        raise MigrationDriftError("group_research_policy_table_missing")
    columns = _columns(conn, GROUP_RESEARCH_POLICY_TABLE)
    if "scope_mode" not in columns:
        conn.execute(
            """ALTER TABLE group_research_policies
               ADD COLUMN scope_mode TEXT NOT NULL DEFAULT 'single_group'
               CHECK(scope_mode IN ('single_group','admitted_groups'))""",
        )
    conn.execute(
        """UPDATE group_research_policies SET scope_mode=?
           WHERE scope_mode NOT IN (?,?) OR scope_mode IS NULL OR scope_mode=''""",
        (
            GROUP_RESEARCH_SCOPE_MODE_SINGLE,
            GROUP_RESEARCH_SCOPE_MODE_SINGLE,
            GROUP_RESEARCH_SCOPE_MODE_ADMITTED,
        ),
    )


def apply_group_research_query_privacy_v3(conn: sqlite3.Connection) -> None:
    """Remove pre-v3 group-derived query text from durable research records.

    The semantic query hash remains for duplicate suppression.  The previous
    ``query_redacted`` value could be a shortened group utterance, so retaining
    it would keep the privacy flaw alive after the runtime caller is fixed.
    """

    if GROUP_RESEARCH_RUN_TABLE not in _tables(conn):
        raise MigrationDriftError("group_research_runs_table_missing")
    conn.execute(
        f"""UPDATE {GROUP_RESEARCH_RUN_TABLE}
            SET query_redacted='public-query:' || substr(query_hash, 1, 20)
            WHERE query_redacted NOT LIKE 'public-query:%'""",
    )
    # v1 used the stored query as the title of automatically published items
    # and embedded it in review Drafts.  Repair those derived copies too; a
    # database-only scrub that left the Knowledge projection untouched would
    # merely move the same privacy leak to another owner surface/search index.
    linked = conn.execute(
        f"""SELECT link.knowledge_item_id,link.run_id,link.publication_mode,run.query_hash
            FROM {GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE} AS link
            JOIN {GROUP_RESEARCH_RUN_TABLE} AS run ON run.id=link.run_id""",
    ).fetchall()
    if not linked:
        return
    from bridge_living_wiki import content_hash, sync_search_item

    now = utc_now()
    revised_ids: list[str] = []
    # Knowledge revisions are normally immutable.  This narrow privacy
    # migration is the exceptional case: legacy snapshots may contain the
    # same copied group query, so preserving immutability would preserve the
    # leak.  Recreate the exact guard before the migration returns.
    conn.execute("DROP TRIGGER IF EXISTS trg_knowledge_revisions_immutable_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_knowledge_revisions_immutable_delete")
    try:
        for row in linked:
            item_id = str(row[0])
            run_id = str(row[1])
            mode = str(row[2])
            label = "public-query:" + str(row[3])[:20]
            if mode == "auto_published_provisional":
                source = conn.execute(
                    f"""SELECT source_title FROM {GROUP_RESEARCH_EVIDENCE_TABLE}
                        WHERE run_id=? ORDER BY retrieved_at,id LIMIT 1""",
                    (run_id,),
                ).fetchone()
                title = str(source[0]).strip()[:120] if source and str(source[0]).strip() else "公开来源临时参考"
                conn.execute(
                    """UPDATE assistant_knowledge_items SET title=?,updated_at=? WHERE id=?""",
                    (title, now, item_id),
                )
            elif mode == "review_draft":
                content = (
                    "该条仅记录一个需 Owner 批量审核的公开议题请求（审计引用：" + label
                    + "）。它尚未经过可用证据核验，不能作为事实回复或检索知识。"
                )
                conn.execute(
                    """UPDATE assistant_knowledge_items
                       SET title=?,content=?,summary=?,content_hash=?,updated_at=? WHERE id=?""",
                    (
                        "待审核公开议题（未留存群聊正文）",
                        content,
                        "高影响公开议题；等待 Owner 审核研究范围与证据。",
                        content_hash(content),
                        now,
                        item_id,
                    ),
                )
            else:
                continue
            conn.execute(
                """UPDATE assistant_knowledge_revisions
                   SET snapshot_json=? WHERE item_id=?""",
                (json.dumps({"redacted": "group_research_query_privacy_v3"}, separators=(",", ":")), item_id),
            )
            revised_ids.append(item_id)
    finally:
        conn.executescript(
            """
            CREATE TRIGGER trg_knowledge_revisions_immutable_update
            BEFORE UPDATE ON assistant_knowledge_revisions
            BEGIN SELECT RAISE(ABORT,'knowledge_revision_immutable'); END;
            CREATE TRIGGER trg_knowledge_revisions_immutable_delete
            BEFORE DELETE ON assistant_knowledge_revisions
            BEGIN SELECT RAISE(ABORT,'knowledge_revision_immutable'); END;
            """,
        )
    for item_id in revised_ids:
        sync_search_item(conn, item_id)


def inspect_group_research_schema(conn: sqlite3.Connection) -> dict:
    tables = _tables(conn)
    required_columns = {
        GROUP_RESEARCH_POLICY_TABLE: {
            "id", "assistant_id", "enabled", "pilot_group_id", "auto_publish_low_public",
            "max_runs_per_day", "max_runs_per_group_day", "freshness_hours", "version", "updated_at",
        },
        GROUP_RESEARCH_RUN_TABLE: {
            "id", "assistant_id", "group_id", "anchor_message_id", "query_redacted", "query_hash",
            "risk_tier", "stage", "reason_code", "source_count", "result_summary", "expires_at",
        },
        GROUP_RESEARCH_EVIDENCE_TABLE: {
            "id", "run_id", "source_url", "source_domain", "source_title", "public_excerpt", "source_hash",
        },
        KNOWLEDGE_SCOPE_TABLE: {"knowledge_item_id", "scope_type", "scope_id", "created_at"},
        GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE: {
            "id", "run_id", "knowledge_item_id", "sensitivity_tier", "publication_mode", "created_at",
        },
    }
    missing_tables = sorted(set(required_columns) - tables)
    missing_columns = {
        table: sorted(columns - _columns(conn, table))
        for table, columns in required_columns.items()
        if table in tables and columns - _columns(conn, table)
    }
    flags = set()
    if "assistant_feature_flags" in tables:
        flags = {
            str(row[0])
            for row in conn.execute("SELECT name FROM assistant_feature_flags").fetchall()
        }
    missing_flags = sorted(
        {GROUP_RESEARCH_FEATURE_FLAG, GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG} - flags,
    )
    policy_count = (
        int(conn.execute("SELECT count(*) FROM group_research_policies").fetchone()[0])
        if GROUP_RESEARCH_POLICY_TABLE in tables else 0
    )
    assistant_count = (
        int(conn.execute("SELECT count(*) FROM assistant_instances").fetchone()[0])
        if "assistant_instances" in tables else 0
    )
    return {
        "ok": not missing_tables and not missing_columns and not missing_flags
        and assistant_count >= 1 and policy_count >= assistant_count,
        "contract_checksum": GROUP_RESEARCH_MIGRATION_CHECKSUM,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_flags": missing_flags,
        "assistant_count": assistant_count,
        "policy_count": policy_count,
    }


def require_group_research_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_group_research_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "group_research_schema_drift:"
            + json.dumps(audit, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    return audit


def inspect_group_research_scope_schema(conn: sqlite3.Connection) -> dict:
    """Inspect the additive v44 scope extension without rewriting v43 history."""

    audit = inspect_group_research_schema(conn)
    columns = _columns(conn, GROUP_RESEARCH_POLICY_TABLE) if GROUP_RESEARCH_POLICY_TABLE in _tables(conn) else set()
    invalid_count = 0
    if "scope_mode" in columns:
        invalid_count = int(
            conn.execute(
                """SELECT count(*) FROM group_research_policies
                   WHERE scope_mode NOT IN (?,?) OR scope_mode IS NULL OR scope_mode=''""",
                (GROUP_RESEARCH_SCOPE_MODE_SINGLE, GROUP_RESEARCH_SCOPE_MODE_ADMITTED),
            ).fetchone()[0],
        )
    return {
        **audit,
        "ok": bool(audit["ok"] and "scope_mode" in columns and invalid_count == 0),
        "scope_mode_present": "scope_mode" in columns,
        "invalid_scope_mode_count": invalid_count,
        "scope_contract_checksum": GROUP_RESEARCH_ADMITTED_SCOPE_MIGRATION_CHECKSUM,
    }


def require_group_research_scope_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_group_research_scope_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "group_research_scope_schema_drift:"
            + json.dumps(audit, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    return audit


__all__ = [
    "GROUP_RESEARCH_ADMITTED_SCOPE_MIGRATION_CHECKSUM",
    "GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG", "GROUP_RESEARCH_EVIDENCE_TABLE",
    "GROUP_RESEARCH_FEATURE_FLAG", "GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE",
    "GROUP_RESEARCH_MIGRATION_CHECKSUM", "GROUP_RESEARCH_POLICY_TABLE",
    "GROUP_RESEARCH_RUN_TABLE", "KNOWLEDGE_SCOPE_TABLE", "apply_group_research_v1",
    "GROUP_RESEARCH_SCOPE_MODE_ADMITTED", "GROUP_RESEARCH_SCOPE_MODE_SINGLE",
    "GROUP_RESEARCH_SCOPE_MODES", "apply_group_research_admitted_scope_v2",
    "GROUP_RESEARCH_QUERY_PRIVACY_MIGRATION_CHECKSUM", "apply_group_research_query_privacy_v3",
    "inspect_group_research_schema", "inspect_group_research_scope_schema",
    "require_group_research_schema", "require_group_research_scope_schema",
]
