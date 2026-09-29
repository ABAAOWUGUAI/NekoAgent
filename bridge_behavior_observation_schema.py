#!/usr/bin/env python3
"""v46 additive storage for body-free Behavior Observations and clusters."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


BEHAVIOR_OBSERVATION_FEATURE_FLAG = "behavior_observation_v1"
BEHAVIOR_OBSERVATION_TABLE = "behavior_observations"
BEHAVIOR_OBSERVATION_CLUSTER_TABLE = "behavior_observation_clusters"
BEHAVIOR_OBSERVATION_COLUMNS = frozenset(
    {
        "id",
        "assistant_id",
        "scope_type",
        "scope_ref",
        "stage",
        "problem_code",
        "evidence_refs_json",
        "trace_ref",
        "policy_version",
        "source_kind",
        "source_ref",
        "cluster_id",
        "created_at",
        "expires_at",
    },
)
BEHAVIOR_OBSERVATION_CLUSTER_COLUMNS = frozenset(
    {
        "id",
        "problem_code",
        "stage",
        "policy_version",
        "observation_count",
        "first_observed_at",
        "last_observed_at",
    },
)
BEHAVIOR_OBSERVATION_CLUSTER_ISOLATED_COLUMNS = BEHAVIOR_OBSERVATION_CLUSTER_COLUMNS | frozenset({"assistant_id"})
_CONTRACT = {
    "feature_flag": BEHAVIOR_OBSERVATION_FEATURE_FLAG,
    "observation_table": BEHAVIOR_OBSERVATION_TABLE,
    "observation_columns": sorted(BEHAVIOR_OBSERVATION_COLUMNS),
    "cluster_table": BEHAVIOR_OBSERVATION_CLUSTER_TABLE,
    "cluster_columns": sorted(BEHAVIOR_OBSERVATION_CLUSTER_COLUMNS),
    "body_free": True,
    "default_enabled": False,
}
BEHAVIOR_OBSERVATION_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def apply_behavior_observation_v1(conn: sqlite3.Connection) -> None:
    """Create retention-bounded records without raw conversation or identity columns."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_OBSERVATION_TABLE} (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('group','private')),
            scope_ref TEXT NOT NULL,
            stage TEXT NOT NULL,
            problem_code TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)),
            trace_ref TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(source_kind,source_ref,problem_code)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_observations_cluster
            ON {BEHAVIOR_OBSERVATION_TABLE}(cluster_id,created_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_behavior_observations_expiry
            ON {BEHAVIOR_OBSERVATION_TABLE}(expires_at,id);
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_OBSERVATION_CLUSTER_TABLE} (
            id TEXT PRIMARY KEY,
            problem_code TEXT NOT NULL,
            stage TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            observation_count INTEGER NOT NULL CHECK(observation_count>=0),
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            UNIQUE(problem_code,stage,policy_version)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_observation_clusters_recent
            ON {BEHAVIOR_OBSERVATION_CLUSTER_TABLE}(last_observed_at DESC,id DESC);
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('{BEHAVIOR_OBSERVATION_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(item[0]) for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})")}


def require_behavior_observation_schema(conn: sqlite3.Connection) -> dict:
    tables = _table_names(conn)
    required_tables = {BEHAVIOR_OBSERVATION_TABLE, BEHAVIOR_OBSERVATION_CLUSTER_TABLE}
    missing_tables = sorted(required_tables - tables)
    observation_columns = _columns(conn, BEHAVIOR_OBSERVATION_TABLE) if BEHAVIOR_OBSERVATION_TABLE in tables else set()
    cluster_columns = _columns(conn, BEHAVIOR_OBSERVATION_CLUSTER_TABLE) if BEHAVIOR_OBSERVATION_CLUSTER_TABLE in tables else set()
    missing_columns = sorted(
        (BEHAVIOR_OBSERVATION_COLUMNS - observation_columns) | (BEHAVIOR_OBSERVATION_CLUSTER_COLUMNS - cluster_columns),
    )
    flag = conn.execute(
        "SELECT 1 FROM assistant_feature_flags WHERE name=?",
        (BEHAVIOR_OBSERVATION_FEATURE_FLAG,),
    ).fetchone()
    if missing_tables or missing_columns or flag is None:
        raise MigrationDriftError(
            "behavior_observation_schema_drift:"
            + ",".join(missing_tables)
            + "|"
            + ",".join(missing_columns)
            + ("|feature_flag" if flag is None else ""),
        )
    return {"ok": True}


def require_behavior_observation_assistant_isolation_schema(conn: sqlite3.Connection) -> dict:
    """Require the v52 cluster owner column without breaking v51 planning."""

    require_behavior_observation_schema(conn)
    columns = _columns(conn, BEHAVIOR_OBSERVATION_CLUSTER_TABLE)
    missing = sorted(BEHAVIOR_OBSERVATION_CLUSTER_ISOLATED_COLUMNS - columns)
    if missing:
        raise MigrationDriftError("behavior_observation_assistant_isolation_schema_drift:" + ",".join(missing))
    return {"ok": True}


def behavior_observation_enabled(conn: sqlite3.Connection) -> bool:
    try:
        require_behavior_observation_schema(conn)
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (BEHAVIOR_OBSERVATION_FEATURE_FLAG,),
        ).fetchone()
        return bool(row and int(row[0]))
    except (sqlite3.Error, MigrationDriftError):
        return False


def set_behavior_observation_feature(conn: sqlite3.Connection, *, enabled: bool) -> dict:
    require_behavior_observation_schema(conn)
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE name=?",
        (1 if enabled else 0, BEHAVIOR_OBSERVATION_FEATURE_FLAG),
    )
    return {"enabled": behavior_observation_enabled(conn)}


__all__ = [
    "BEHAVIOR_OBSERVATION_CLUSTER_TABLE",
    "BEHAVIOR_OBSERVATION_FEATURE_FLAG",
    "BEHAVIOR_OBSERVATION_MIGRATION_CHECKSUM",
    "BEHAVIOR_OBSERVATION_TABLE",
    "apply_behavior_observation_v1",
    "behavior_observation_enabled",
    "require_behavior_observation_schema",
    "require_behavior_observation_assistant_isolation_schema",
    "set_behavior_observation_feature",
]
