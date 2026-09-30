#!/usr/bin/env python3
"""v53 Assistant-scoped, body-free Owner Authorization Registry schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


BEHAVIOR_OWNER_AUTHORIZATION_TABLE = "behavior_owner_authorizations"
BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE = "behavior_owner_authorization_audit"
BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_UPDATE_TRIGGER = "trg_behavior_owner_authorization_audit_no_update"
BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_DELETE_TRIGGER = "trg_behavior_owner_authorization_audit_no_delete"
BEHAVIOR_OWNER_AUTHORIZATION_COLUMNS = frozenset(
    {
        "authorization_ref",
        "assistant_id",
        "purpose",
        "binding_json",
        "binding_hash",
        "state",
        "issuer_ref",
        "issued_at",
        "expires_at",
        "revoked_at",
        "consumed_at",
    },
)
BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_COLUMNS = frozenset(
    {
        "authorization_ref",
        "assistant_id",
        "sequence",
        "event",
        "occurred_at",
        "actor_ref",
    },
)
_CONTRACT = {
    "authorization_table": BEHAVIOR_OWNER_AUTHORIZATION_TABLE,
    "authorization_columns": sorted(BEHAVIOR_OWNER_AUTHORIZATION_COLUMNS),
    "audit_table": BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE,
    "audit_columns": sorted(BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_COLUMNS),
    "purposes": [
        "reference_baseline_freeze",
        "private_candidate_evaluation",
        "paired_shadow_enable",
    ],
    "states": ["active", "consumed", "revoked", "expired"],
    "assistant_scoped": True,
    "body_free": True,
    "append_only_audit": True,
    "schema_definition_version": 2,
}
BEHAVIOR_OWNER_AUTHORIZATION_MIGRATION_CHECKSUM = "sha256:" + hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def apply_behavior_owner_authorization_v1(conn: sqlite3.Connection) -> None:
    """Create the inert v53 registry; it has no executor or delivery path."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_OWNER_AUTHORIZATION_TABLE} (
            authorization_ref TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            purpose TEXT NOT NULL CHECK(purpose IN (
                'reference_baseline_freeze','private_candidate_evaluation','paired_shadow_enable'
            )),
            binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
            binding_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active','consumed','revoked','expired')),
            issuer_ref TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_owner_authorizations_assistant_state
            ON {BEHAVIOR_OWNER_AUTHORIZATION_TABLE}(assistant_id,purpose,state,expires_at,authorization_ref);
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE} (
            authorization_ref TEXT NOT NULL,
            assistant_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence>=1),
            event TEXT NOT NULL CHECK(event IN ('issued','consumed','revoked','expired')),
            occurred_at TEXT NOT NULL,
            actor_ref TEXT,
            PRIMARY KEY(authorization_ref,sequence),
            FOREIGN KEY(authorization_ref) REFERENCES {BEHAVIOR_OWNER_AUTHORIZATION_TABLE}(authorization_ref) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_owner_authorization_audit_assistant
            ON {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE}(assistant_id,authorization_ref,sequence);
        CREATE TRIGGER IF NOT EXISTS {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_UPDATE_TRIGGER}
        BEFORE UPDATE ON {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'behavior_owner_authorization_audit_append_only');
        END;
        CREATE TRIGGER IF NOT EXISTS {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_DELETE_TRIGGER}
        BEFORE DELETE ON {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'behavior_owner_authorization_audit_append_only');
        END;
        """,
    )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _normalized_sql(value: object) -> str:
    return "".join(str(value or "").lower().split())


def _definition_fragments() -> dict[str, tuple[str, ...]]:
    return {
        BEHAVIOR_OWNER_AUTHORIZATION_TABLE: (
            "authorization_reftextprimarykey",
            "assistant_idtextnotnull",
            "purposetextnotnullcheck(purposein('reference_baseline_freeze','private_candidate_evaluation','paired_shadow_enable'))",
            "binding_jsontextnotnullcheck(json_valid(binding_json))",
            "statetextnotnullcheck(statein('active','consumed','revoked','expired'))",
        ),
        BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE: (
            "authorization_reftextnotnull",
            "assistant_idtextnotnull",
            "sequenceintegernotnullcheck(sequence>=1)",
            "eventtextnotnullcheck(eventin('issued','consumed','revoked','expired'))",
            "primarykey(authorization_ref,sequence)",
            f"foreignkey(authorization_ref)references{BEHAVIOR_OWNER_AUTHORIZATION_TABLE}(authorization_ref)ondeleterestrict",
        ),
        "idx_behavior_owner_authorizations_assistant_state": (
            f"on{BEHAVIOR_OWNER_AUTHORIZATION_TABLE}(assistant_id,purpose,state,expires_at,authorization_ref)",
        ),
        "idx_behavior_owner_authorization_audit_assistant": (
            f"on{BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE}(assistant_id,authorization_ref,sequence)",
        ),
        BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_UPDATE_TRIGGER: (
            f"beforeupdateon{BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE}",
            "selectraise(abort,'behavior_owner_authorization_audit_append_only');",
        ),
        BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_DELETE_TRIGGER: (
            f"beforedeleteon{BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE}",
            "selectraise(abort,'behavior_owner_authorization_audit_append_only');",
        ),
    }


def require_behavior_owner_authorization_schema(conn: sqlite3.Connection) -> dict:
    """Fail closed if body-free registry or immutable audit schema drifts."""

    expected = {
        BEHAVIOR_OWNER_AUTHORIZATION_TABLE: BEHAVIOR_OWNER_AUTHORIZATION_COLUMNS,
        BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE: BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_COLUMNS,
    }
    tables = _tables(conn)
    missing_tables = sorted(set(expected) - tables)
    missing_columns = sorted(
        f"{table}.{column}"
        for table, columns in expected.items()
        if table in tables
        for column in columns - _columns(conn, table)
    )
    triggers = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    missing_triggers = sorted(
        {
            BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_UPDATE_TRIGGER,
            BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_DELETE_TRIGGER,
        }
        - triggers,
    )
    definitions = {
        str(row[0]): _normalized_sql(row[1])
        for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name IN (?,?,?,?,?,?)",
            (
                BEHAVIOR_OWNER_AUTHORIZATION_TABLE,
                BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE,
                "idx_behavior_owner_authorizations_assistant_state",
                "idx_behavior_owner_authorization_audit_assistant",
                BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_UPDATE_TRIGGER,
                BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_NO_DELETE_TRIGGER,
            ),
        )
    }
    malformed_definitions = sorted(
        name
        for name, fragments in _definition_fragments().items()
        if any(fragment not in definitions.get(name, "") for fragment in fragments)
    )
    if missing_tables or missing_columns or missing_triggers or malformed_definitions:
        raise MigrationDriftError(
            "behavior_owner_authorization_schema_drift:"
            + ",".join(missing_tables)
            + "|"
            + ",".join(missing_columns)
            + "|"
            + ",".join(missing_triggers)
            + "|"
            + ",".join(malformed_definitions),
        )
    return {"ok": True}


__all__ = [
    "BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE",
    "BEHAVIOR_OWNER_AUTHORIZATION_MIGRATION_CHECKSUM",
    "BEHAVIOR_OWNER_AUTHORIZATION_TABLE",
    "apply_behavior_owner_authorization_v1",
    "require_behavior_owner_authorization_schema",
]
