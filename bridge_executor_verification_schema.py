#!/usr/bin/env python3
"""Schema + migrations for the executor verification state table (v39 + v40)."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_executor_verification import VERIFICATION_COLUMNS
from bridge_migrations import MigrationDriftError

# The v39 column set is frozen so the applied migration checksum stays stable on
# every existing deployment.  v40 adds the reason_code column.
V39_VERIFICATION_COLUMNS = (
    "provider_id",
    "adapter_type",
    "verification_hash",
    "verified_at",
    "config_version_at_verify",
    "applied_version_at_verify",
    "upstream_model_id_at_verify",
    "status",
    "last_error",
    "evidence_json",
    "created_at",
    "updated_at",
)


def _contract_payload_v1() -> str:
    return json.dumps(
        {
            "table": "executor_verification_state",
            "columns": list(V39_VERIFICATION_COLUMNS),
            "host_policy": [
                "CODEX_EXECUTOR_WORKSPACE_ROOT",
                "CODEX_EXECUTOR_PROFILE_DIR",
                "CODEX_PROXY_ACCESS_KEY_FILE",
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _contract_payload_v2() -> str:
    return json.dumps(
        {
            "table": "executor_verification_state",
            "columns": list(VERIFICATION_COLUMNS),
            "add_column": "reason_code",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


EXECUTOR_VERIFICATION_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload_v1().encode("utf-8"),
).hexdigest()

EXECUTOR_VERIFICATION_REASON_CODE_CHECKSUM = hashlib.sha256(
    _contract_payload_v2().encode("utf-8"),
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


def apply_executor_verification_reason_code_v2(conn: sqlite3.Connection) -> None:
    """v40: add the verification reason_code column (verified / file_not_mutated
    / verification_harness_read_only / verification_harness_sandbox / ...)."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(executor_verification_state)").fetchall()}
    if "reason_code" not in columns:
        conn.execute(
            "ALTER TABLE executor_verification_state ADD COLUMN reason_code TEXT NOT NULL DEFAULT ''",
        )


def require_executor_verification_schema(conn: sqlite3.Connection, *, version: int = 40) -> dict:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_verification_state'",
    ).fetchone()
    if not table:
        raise MigrationDriftError("executor_verification_schema_drift:table")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(executor_verification_state)")}
    expected = VERIFICATION_COLUMNS if int(version) >= 40 else V39_VERIFICATION_COLUMNS
    missing = sorted(set(expected) - columns)
    if missing:
        raise MigrationDriftError("executor_verification_schema_drift:" + ",".join(missing))
    return {"ok": True, "version": int(version), "columns": list(expected)}


__all__ = [
    "EXECUTOR_VERIFICATION_MIGRATION_CHECKSUM",
    "EXECUTOR_VERIFICATION_REASON_CODE_CHECKSUM",
    "apply_executor_verification_reason_code_v2",
    "apply_executor_verification_v1",
    "require_executor_verification_schema",
]
