#!/usr/bin/env python3
"""Additive mapping from Goal Revision to the Runs that implement it."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


REVISION_RUN_COLUMNS = (
    "run_id", "revision_id", "binding_kind", "created_at",
)
REVISION_RUN_INDEXES = ("idx_goal_revision_runs_revision",)


def _contract_payload() -> str:
    return json.dumps(
        {"table": "goal_revision_runs", "columns": list(REVISION_RUN_COLUMNS), "indexes": list(REVISION_RUN_INDEXES)},
        sort_keys=True,
        separators=(",", ":"),
    )


GOAL_REVISION_LINK_MIGRATION_CHECKSUM = hashlib.sha256(_contract_payload().encode("utf-8")).hexdigest()


def _baseline_revision_id(goal_id: str) -> str:
    return "goal-revision-baseline-" + hashlib.sha256(str(goal_id).encode("utf-8")).hexdigest()[:24]


def apply_goal_revision_link_v1(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"goals", "runs", "goal_revisions"}
    missing = sorted(required - tables)
    if missing:
        raise MigrationDriftError("goal_revision_link_prerequisite_missing:" + ",".join(missing))
    conn.execute(
        """CREATE TABLE goal_revision_runs (
            run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
            revision_id TEXT NOT NULL REFERENCES goal_revisions(id) ON DELETE CASCADE,
            binding_kind TEXT NOT NULL CHECK(binding_kind IN ('initial','revision','follow_up','retry','migration')),
            created_at TEXT NOT NULL
        )""",
    )
    conn.execute(
        "CREATE INDEX idx_goal_revision_runs_revision ON goal_revision_runs(revision_id,created_at DESC)",
    )

    now = utc_now()
    goals = conn.execute(
        "SELECT id,status,title,actor_id,channel,created_at FROM goals ORDER BY created_at,id",
    ).fetchall()
    for goal_id_raw, goal_status, title, actor_id, channel, created_at in goals:
        goal_id = str(goal_id_raw)
        revision = conn.execute(
            "SELECT id FROM goal_revisions WHERE goal_id=? ORDER BY revision_number ASC LIMIT 1",
            (goal_id,),
        ).fetchone()
        if revision is None:
            revision_id = _baseline_revision_id(goal_id)
            status = "accepted" if str(goal_status) == "completed" else "active"
            conn.execute(
                """INSERT INTO goal_revisions(
                    id,goal_id,revision_number,parent_revision_id,instruction,status,actor_id,channel,
                    source_run_id,feedback_json,idempotency_key,created_at,updated_at
                ) VALUES(?,?,1,'',?,?,?,?,?,'{}',?,?,?)""",
                (
                    revision_id, goal_id, str(title or "Legacy goal"), status,
                    str(actor_id or ""), str(channel or ""), "",
                    "migration-baseline:" + goal_id, str(created_at or now), now,
                ),
            )
        else:
            revision_id = str(revision["id"])
        conn.execute(
            """INSERT OR IGNORE INTO goal_revision_runs(run_id,revision_id,binding_kind,created_at)
            SELECT id,?,'migration',? FROM runs WHERE goal_id=?""",
            (revision_id, now, goal_id),
        )


def inspect_goal_revision_link_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing_tables = [] if "goal_revision_runs" in tables else ["goal_revision_runs"]
    missing_columns = []
    if not missing_tables:
        actual = {str(row[1]) for row in conn.execute("PRAGMA table_info(goal_revision_runs)")}
        missing_columns = sorted(set(REVISION_RUN_COLUMNS) - actual)
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_indexes = sorted(set(REVISION_RUN_INDEXES) - indexes)
    fk_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    return {
        "ok": not (missing_tables or missing_columns or missing_indexes or fk_errors),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "foreign_key_error_count": len(fk_errors),
        "contract_checksum": GOAL_REVISION_LINK_MIGRATION_CHECKSUM,
    }


def require_goal_revision_link_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_goal_revision_link_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError("goal_revision_link_schema_drift:" + json.dumps(audit, sort_keys=True, separators=(",", ":")))
    return audit


__all__ = [
    "GOAL_REVISION_LINK_MIGRATION_CHECKSUM",
    "apply_goal_revision_link_v1",
    "inspect_goal_revision_link_schema",
    "require_goal_revision_link_schema",
]
