#!/usr/bin/env python3
"""v54 durable, Assistant-scoped Cutover state for paired Shadow.

The row is a control-plane binding, not a behavior policy.  It contains no
chat body, group ID, model output, benchmark case, Rubric, Gold, or reply.
Live admission is rechecked against ``qq_access_entries`` for every turn.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


PAIRED_SHADOW_CUTOVER_TABLE = "behavior_paired_shadow_cutovers"
PAIRED_SHADOW_CUTOVER_COLUMNS = frozenset({
    "assistant_id", "enabled", "scope_refs_json", "candidate_ref",
    "candidate_policy_bundle_hash", "reference_policy_ref", "benchmark_ref",
    "benchmark_hash", "authorization_ref", "authorization_binding_hash",
    "expires_at", "plan_checksum", "created_at", "updated_at",
})
_CONTRACT = {
    "table": PAIRED_SHADOW_CUTOVER_TABLE,
    "columns": sorted(PAIRED_SHADOW_CUTOVER_COLUMNS),
    "assistant_primary_key": True,
    "default_enabled": False,
    "body_free": True,
    "authorization_binding": True,
    "schema_definition_version": 2,
}
PAIRED_SHADOW_CUTOVER_MIGRATION_CHECKSUM = "sha256:" + hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def apply_behavior_paired_shadow_cutover_v1(conn: sqlite3.Connection) -> None:
    """Register the additive v54 Cutover row; this does not enable Shadow."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PAIRED_SHADOW_CUTOVER_TABLE} (
            assistant_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)) DEFAULT 0,
            scope_refs_json TEXT NOT NULL CHECK(json_valid(scope_refs_json)) DEFAULT '[]',
            candidate_ref TEXT NOT NULL DEFAULT '',
            candidate_policy_bundle_hash TEXT NOT NULL DEFAULT '',
            reference_policy_ref TEXT NOT NULL DEFAULT '',
            benchmark_ref TEXT NOT NULL DEFAULT '',
            benchmark_hash TEXT NOT NULL DEFAULT '',
            authorization_ref TEXT NOT NULL DEFAULT '',
            authorization_binding_hash TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            plan_checksum TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_behavior_paired_shadow_cutovers_enabled
            ON {PAIRED_SHADOW_CUTOVER_TABLE}(enabled,updated_at DESC);
        """,
    )
    # ``CREATE TABLE IF NOT EXISTS`` leaves a manually-created same-name table
    # untouched.  Validate before the migration runner records v54, so a
    # malformed pre-existing table aborts this transaction and cannot acquire
    # valid-looking migration history.
    require_behavior_paired_shadow_cutover_schema(conn)


def _normalized_sql(value: object) -> str:
    return "".join(str(value or "").lower().split())


def _definition_fragments() -> tuple[str, ...]:
    return (
        "assistant_idtextprimarykey",
        "enabledintegernotnullcheck(enabledin(0,1))default0",
        "scope_refs_jsontextnotnullcheck(json_valid(scope_refs_json))default'[]'",
        "authorization_reftextnotnulldefault''",
        "authorization_binding_hashtextnotnulldefault''",
        "expires_attextnotnulldefault''",
    )


def require_behavior_paired_shadow_cutover_schema(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (PAIRED_SHADOW_CUTOVER_TABLE,),
    ).fetchone()
    present = {str(item[1]) for item in conn.execute(f"PRAGMA table_info({PAIRED_SHADOW_CUTOVER_TABLE})")} if row else set()
    missing = sorted(PAIRED_SHADOW_CUTOVER_COLUMNS - present)
    definition = _normalized_sql(row[0]) if row else ""
    malformed = [fragment for fragment in _definition_fragments() if fragment not in definition]
    if row is None or missing or malformed:
        raise MigrationDriftError(
            "behavior_paired_shadow_cutover_schema_drift:"
            + ("table|" if row is None else "")
            + ",".join(missing)
            + "|"
            + ",".join(malformed)
        )
    return {"ok": True}


__all__ = [
    "PAIRED_SHADOW_CUTOVER_MIGRATION_CHECKSUM",
    "PAIRED_SHADOW_CUTOVER_TABLE",
    "apply_behavior_paired_shadow_cutover_v1",
    "require_behavior_paired_shadow_cutover_schema",
]
