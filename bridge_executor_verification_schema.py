#!/usr/bin/env python3
"""Schema + migration for the executor verification state table (v39)."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_executor_verification import VERIFICATION_COLUMNS
from bridge_migrations import MigrationDriftError


def _contract_payload() -> str:
    return json.dumps(
        {
            "table": "executor_verification_state",
            "columns": list(VERIFICATION_COLUMNS),
            "host_policy": [
                "CODEX_EXECUTOR_WORKSPACE_ROOT",
                "CODEX_EXECUTOR_PROFILE_DIR",
                "CODEX_PROXY_ACCESS_KEY_FILE",
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


EXECUTOR_VERIFICATION_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def apply_executor_verification_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS executor_verification_state (
            provider_id TEXT PRIMARY KEY,
            adapter_type TEXT NOT NULL DEFAULT 'codex_cli_profile',
            verification_hash TEXT NOT NULL DEFAULT '',
            verified_at TEXT NOT NULL DEFAULT '',
            config_version_at_verify INTEGER NOT NULL DEFAULT 0,
            applied_version_at_verify INTEGER NOT NULL DEFAULT 0,
            upstream_model_id_at_verify TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','verified','failed','stale')),
            last_error TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(provider_id) REFERENCES model_providers(id) ON DELETE RESTRICT
        )
        """,
    )


def require_executor_verification_schema(conn: sqlite3.Connection) -> dict:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_verification_state'",
    ).fetchone()
    if not table:
        raise MigrationDriftError("executor_verification_schema_drift:table")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(executor_verification_state)")}
    missing = sorted(set(VERIFICATION_COLUMNS) - columns)
    if missing:
        raise MigrationDriftError("executor_verification_schema_drift:" + ",".join(missing))
    return {"ok": True, "columns": list(VERIFICATION_COLUMNS)}


__all__ = [
    "EXECUTOR_VERIFICATION_MIGRATION_CHECKSUM",
    "apply_executor_verification_v1",
    "require_executor_verification_schema",
]
