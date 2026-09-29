#!/usr/bin/env python3
"""v60 cutover flag for expression-only Assistant Affect recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_assistant_affect_shadow_schema import require_assistant_affect_shadow_schema
from bridge_migrations import MigrationDriftError


ASSISTANT_AFFECT_EXPRESSION_FEATURE_FLAG = "assistant_affect_expression_v1"
_CONTRACT = {
    "feature_flag": ASSISTANT_AFFECT_EXPRESSION_FEATURE_FLAG,
    "requires": ["assistant_affect_shadow_v1"],
    "cutover_enabled": True,
    "allowed_domain": "expression_plan",
    "forbidden_domains": [
        "facts", "permissions", "tasks", "approvals", "model_routing",
        "relationship", "delivery", "response_ownership",
    ],
}
ASSISTANT_AFFECT_EXPRESSION_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def apply_assistant_affect_expression_v1(conn: sqlite3.Connection) -> None:
    """Create the separately reversible expression cutover.

    The v47 observation flag remains independent and default-off on a fresh
    installation.  This approved product release enables only the expression
    side; without v47 collection there is no state to project.
    """

    require_assistant_affect_shadow_schema(conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """,
        (ASSISTANT_AFFECT_EXPRESSION_FEATURE_FLAG,),
    )


def require_assistant_affect_expression_schema(conn: sqlite3.Connection) -> dict:
    require_assistant_affect_shadow_schema(conn)
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (ASSISTANT_AFFECT_EXPRESSION_FEATURE_FLAG,),
    ).fetchone()
    if row is None:
        raise MigrationDriftError("assistant_affect_expression_schema_drift:feature_flag")
    return {"ok": True}


def assistant_affect_expression_enabled(conn: sqlite3.Connection) -> bool:
    try:
        require_assistant_affect_expression_schema(conn)
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (ASSISTANT_AFFECT_EXPRESSION_FEATURE_FLAG,),
        ).fetchone()
        return bool(row and int(row[0]))
    except (sqlite3.Error, MigrationDriftError, TypeError, ValueError):
        return False


def set_assistant_affect_expression_feature(
    conn: sqlite3.Connection,
    enabled: bool,
) -> dict:
    if type(enabled) is not bool:
        raise ValueError("assistant_affect_expression_enabled_boolean_required")
    require_assistant_affect_expression_schema(conn)
    cursor = conn.execute(
        "UPDATE assistant_feature_flags "
        "SET enabled=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE name=?",
        (1 if enabled else 0, ASSISTANT_AFFECT_EXPRESSION_FEATURE_FLAG),
    )
    if int(cursor.rowcount or 0) != 1:
        raise MigrationDriftError("assistant_affect_expression_schema_drift:feature_flag")
    return {"enabled": assistant_affect_expression_enabled(conn)}


__all__ = [
    "ASSISTANT_AFFECT_EXPRESSION_FEATURE_FLAG",
    "ASSISTANT_AFFECT_EXPRESSION_MIGRATION_CHECKSUM",
    "apply_assistant_affect_expression_v1",
    "assistant_affect_expression_enabled",
    "require_assistant_affect_expression_schema",
    "set_assistant_affect_expression_feature",
]
