#!/usr/bin/env python3
"""AC-2 additive cutover for deterministic participation and model roles."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


DETERMINISTIC_PARTICIPATION_FEATURE_FLAG = "deterministic_participation_v1"
MODEL_ROLE_CUTOVER = {
    "interaction_classifier": "classifier",
    "conversation_engagement": "classifier",
    "conversation_reply": "daily_chat",
}


def _contract_payload() -> str:
    return json.dumps(
        {
            "feature_flag": DETERMINISTIC_PARTICIPATION_FEATURE_FLAG,
            "model_role_cutover": MODEL_ROLE_CUTOVER,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


PARTICIPATION_ROUTING_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def apply_conversation_participation_routing_v1(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    required = {"assistant_feature_flags"}
    if not required.issubset(tables):
        raise MigrationDriftError(
            "participation_routing_base_table_missing:"
            + ",".join(sorted(required - tables)),
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_role_bindings (
            role TEXT PRIMARY KEY,
            primary_model_id TEXT NOT NULL DEFAULT '',
            fallback_model_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
    )
    now = utc_now()
    for new_role, legacy_role in MODEL_ROLE_CUTOVER.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO model_role_bindings(
                role,primary_model_id,fallback_model_id,updated_at
            )
            SELECT ?,primary_model_id,fallback_model_id,?
            FROM model_role_bindings WHERE role=?
            """,
            (new_role, now, legacy_role),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO model_role_bindings(
                role,primary_model_id,fallback_model_id,updated_at
            ) VALUES(?,'','',?)
            """,
            (new_role, now),
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES(?,0,?)
        """,
        (DETERMINISTIC_PARTICIPATION_FEATURE_FLAG, now),
    )


def inspect_conversation_participation_routing_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing_tables = sorted(
        {"assistant_feature_flags", "model_role_bindings"} - tables,
    )
    roles: set[str] = set()
    flag = None
    if "model_role_bindings" in tables:
        roles = {
            str(row[0])
            for row in conn.execute("SELECT role FROM model_role_bindings").fetchall()
        }
    if "assistant_feature_flags" in tables:
        flag = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (DETERMINISTIC_PARTICIPATION_FEATURE_FLAG,),
        ).fetchone()
    missing_roles = sorted(set(MODEL_ROLE_CUTOVER) - roles)
    return {
        "ok": not missing_tables and not missing_roles and flag is not None,
        "contract_checksum": PARTICIPATION_ROUTING_MIGRATION_CHECKSUM,
        "missing_tables": missing_tables,
        "missing_model_roles": missing_roles,
        "feature_flag_present": flag is not None,
        "feature_enabled": bool(int(flag[0])) if flag is not None else False,
    }


def require_conversation_participation_routing_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_conversation_participation_routing_schema(conn)
    if not audit["ok"]:
        parts: list[str] = []
        if audit["missing_tables"]:
            parts.append("tables=" + ",".join(audit["missing_tables"]))
        if audit["missing_model_roles"]:
            parts.append("roles=" + ",".join(audit["missing_model_roles"]))
        if not audit["feature_flag_present"]:
            parts.append("feature_flag")
        raise MigrationDriftError("participation_routing_schema_drift:" + "|".join(parts))
    return audit


__all__ = [
    "DETERMINISTIC_PARTICIPATION_FEATURE_FLAG",
    "MODEL_ROLE_CUTOVER",
    "PARTICIPATION_ROUTING_MIGRATION_CHECKSUM",
    "apply_conversation_participation_routing_v1",
    "inspect_conversation_participation_routing_schema",
    "require_conversation_participation_routing_schema",
]
