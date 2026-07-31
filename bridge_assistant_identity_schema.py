#!/usr/bin/env python3
"""Gate 2 additive schema and backfill for assistant identity ownership."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Mapping

from bridge_migrations import MigrationDriftError, utc_now


DEFAULT_OWNER_ACTOR_ID = "owner-local"
IDENTITY_FEATURE_FLAG = "assistant_identity_v2"
IDENTITY_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "persona_packs": (
        "id", "owner_actor_id", "name", "status", "source_type",
        "created_at", "updated_at", "archived_at",
    ),
    "persona_versions": (
        "id", "persona_pack_id", "version", "persona_text", "speaking_style",
        "relationship_label", "behavior_boundaries_json", "snapshot_hash", "created_at",
    ),
    "voice_packs": (
        "id", "owner_actor_id", "name", "status", "source_type", "config_json",
        "created_at", "updated_at", "archived_at",
    ),
    "assistant_instances": (
        "id", "owner_actor_id", "display_name", "status",
        "active_persona_version_id", "active_appearance_pack_id",
        "active_voice_pack_id", "created_at", "updated_at", "archived_at",
    ),
    "assistant_feature_flags": ("name", "enabled", "updated_at"),
    "assistant_instance_events": (
        "id", "assistant_id", "event_type", "actor_type", "channel",
        "detail_json", "created_at",
    ),
}
IDENTITY_REQUIRED_INDEXES = (
    "idx_assistant_instances_one_active",
    "idx_assistant_instances_owner",
    "idx_persona_packs_owner",
    "idx_persona_versions_pack",
    "idx_voice_packs_owner",
    "idx_assistant_instance_events",
)
PET_OWNERSHIP_COLUMNS: Mapping[str, str] = {
    "owner_actor_id": "TEXT NOT NULL DEFAULT 'owner-local'",
    "status": "TEXT NOT NULL DEFAULT 'active'",
    "source_type": "TEXT NOT NULL DEFAULT 'user_import'",
    "deleted_at": "TEXT NOT NULL DEFAULT ''",
}


def _settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT key, value FROM settings
        WHERE key IN ('display_name','persona','style','relationship','admin_pet_pack_id')
        """,
    ).fetchall()
    return {str(row[0]): str(row[1] or "") for row in rows}


def identity_source_preflight(conn: sqlite3.Connection) -> dict:
    """Validate the legacy identity source without returning user content."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "pet_packs" not in tables:
        raise MigrationDriftError("assistant_identity_source_missing:pet_packs")
    pet_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(pet_packs)")}
    required_pet = {
        "id", "name", "asset_name", "mime_type", "manifest_json",
        "built_in", "created_at", "updated_at",
    }
    missing_pet = sorted(required_pet - pet_columns)
    if missing_pet:
        raise MigrationDriftError(
            "assistant_identity_source_drift:pet_packs:" + ",".join(missing_pet),
        )
    settings = _settings(conn)
    missing_settings = sorted(
        {"display_name", "persona", "style", "relationship"} - set(settings),
    )
    if missing_settings:
        raise MigrationDriftError(
            "assistant_identity_source_missing:settings:" + ",".join(missing_settings),
        )
    pet_count = int(conn.execute("SELECT count(*) FROM pet_packs").fetchone()[0])
    selected = settings.get("admin_pet_pack_id") or ""
    selected_exists = bool(
        selected
        and conn.execute("SELECT 1 FROM pet_packs WHERE id=?", (selected,)).fetchone()
    )
    return {
        "ok": True,
        "identity_settings": 4,
        "appearance_packs": pet_count,
        "selected_appearance_exists": selected_exists,
    }


def _snapshot_hash(
    persona_text: str,
    speaking_style: str,
    relationship_label: str,
    behavior_boundaries_json: str,
) -> str:
    payload = json.dumps(
        {
            "persona_text": persona_text,
            "speaking_style": speaking_style,
            "relationship_label": relationship_label,
            "behavior_boundaries_json": behavior_boundaries_json,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_assistant_identity_v2(conn: sqlite3.Connection) -> None:
    """Create Gate 2 tables and migrate the current global identity once."""

    identity_source_preflight(conn)
    pet_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(pet_packs)")}
    for name, column_type in PET_OWNERSHIP_COLUMNS.items():
        if name not in pet_columns:
            conn.execute(f"ALTER TABLE pet_packs ADD COLUMN {name} {column_type}")

    statements = (
        """
        CREATE TABLE persona_packs (
            id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','archived')),
            source_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE persona_versions (
            id TEXT PRIMARY KEY,
            persona_pack_id TEXT NOT NULL REFERENCES persona_packs(id) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK(version > 0),
            persona_text TEXT NOT NULL,
            speaking_style TEXT NOT NULL,
            relationship_label TEXT NOT NULL,
            behavior_boundaries_json TEXT NOT NULL DEFAULT '{}',
            snapshot_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(persona_pack_id, version)
        )
        """,
        """
        CREATE TABLE voice_packs (
            id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','archived')),
            source_type TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE assistant_instances (
            id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','archived')),
            active_persona_version_id TEXT NOT NULL REFERENCES persona_versions(id) ON DELETE RESTRICT,
            active_appearance_pack_id TEXT REFERENCES pet_packs(id) ON DELETE RESTRICT,
            active_voice_pack_id TEXT REFERENCES voice_packs(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE assistant_feature_flags (
            name TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE assistant_instance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL,
            actor_type TEXT NOT NULL DEFAULT 'system',
            channel TEXT NOT NULL DEFAULT 'migration',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX idx_assistant_instances_one_active
        ON assistant_instances(owner_actor_id) WHERE status='active'
        """,
        "CREATE INDEX idx_assistant_instances_owner ON assistant_instances(owner_actor_id,status)",
        "CREATE INDEX idx_persona_packs_owner ON persona_packs(owner_actor_id,status)",
        "CREATE INDEX idx_persona_versions_pack ON persona_versions(persona_pack_id,version DESC)",
        "CREATE INDEX idx_voice_packs_owner ON voice_packs(owner_actor_id,status)",
        "CREATE INDEX idx_assistant_instance_events ON assistant_instance_events(assistant_id,id DESC)",
    )
    for statement in statements:
        conn.execute(statement)

    now = utc_now()
    settings = _settings(conn)
    persona_pack_id = "persona-" + uuid.uuid4().hex
    persona_version_id = "persona-version-" + uuid.uuid4().hex
    assistant_id = "assistant-" + uuid.uuid4().hex
    boundaries = "{}"
    snapshot_hash = _snapshot_hash(
        settings["persona"],
        settings["style"],
        settings["relationship"],
        boundaries,
    )
    conn.execute(
        """
        INSERT INTO persona_packs(
            id,owner_actor_id,name,status,source_type,created_at,updated_at,archived_at
        ) VALUES(?,?,?,'active','legacy_settings',?,?, '')
        """,
        (
            persona_pack_id,
            DEFAULT_OWNER_ACTOR_ID,
            f"{settings['display_name']} 人格",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO persona_versions(
            id,persona_pack_id,version,persona_text,speaking_style,
            relationship_label,behavior_boundaries_json,snapshot_hash,created_at
        ) VALUES(?,?,1,?,?,?,?,?,?)
        """,
        (
            persona_version_id,
            persona_pack_id,
            settings["persona"],
            settings["style"],
            settings["relationship"],
            boundaries,
            snapshot_hash,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE pet_packs
        SET owner_actor_id=?, status='active',
            source_type=CASE WHEN built_in=1 THEN 'legacy_private' ELSE 'user_import' END,
            deleted_at=''
        """,
        (DEFAULT_OWNER_ACTOR_ID,),
    )
    selected_pack = settings.get("admin_pet_pack_id") or ""
    if not selected_pack or not conn.execute(
        "SELECT 1 FROM pet_packs WHERE id=? AND status='active'",
        (selected_pack,),
    ).fetchone():
        row = conn.execute(
            "SELECT id FROM pet_packs WHERE status='active' ORDER BY created_at LIMIT 1",
        ).fetchone()
        selected_pack = str(row[0]) if row else ""
    conn.execute(
        """
        INSERT INTO settings(key,value,updated_at) VALUES('admin_pet_pack_id',?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """,
        (selected_pack, now),
    )
    conn.execute(
        """
        INSERT INTO assistant_instances(
            id,owner_actor_id,display_name,status,active_persona_version_id,
            active_appearance_pack_id,active_voice_pack_id,created_at,updated_at,archived_at
        ) VALUES(?,?,?,'active',?,?,NULL,?,?, '')
        """,
        (
            assistant_id,
            DEFAULT_OWNER_ACTOR_ID,
            settings["display_name"],
            persona_version_id,
            selected_pack or None,
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,0,?)",
        (IDENTITY_FEATURE_FLAG, now),
    )
    conn.execute(
        """
        INSERT INTO assistant_instance_events(
            assistant_id,event_type,actor_type,channel,detail_json,created_at
        ) VALUES(?,'legacy_identity_migrated','system','migration',?,?)
        """,
        (
            assistant_id,
            json.dumps(
                {
                    "appearance_registered": bool(selected_pack),
                    "identity_fields": 4,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            now,
        ),
    )


def inspect_identity_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_tables = sorted(set(IDENTITY_TABLE_COLUMNS) - tables)
    missing_columns: dict[str, list[str]] = {}
    for table, required in IDENTITY_TABLE_COLUMNS.items():
        if table not in tables:
            continue
        present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - present)
        if missing:
            missing_columns[table] = missing
    pet_columns = (
        {str(row[1]) for row in conn.execute("PRAGMA table_info(pet_packs)")}
        if "pet_packs" in tables
        else set()
    )
    missing_pet_columns = sorted(set(PET_OWNERSHIP_COLUMNS) - pet_columns)
    missing_indexes = sorted(set(IDENTITY_REQUIRED_INDEXES) - indexes)
    foreign_key_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    active_count = (
        int(conn.execute("SELECT count(*) FROM assistant_instances WHERE status='active'").fetchone()[0])
        if "assistant_instances" in tables
        else 0
    )
    ok = not (
        missing_tables
        or missing_columns
        or missing_pet_columns
        or missing_indexes
        or foreign_key_errors
        or active_count != 1
    )
    return {
        "ok": ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_pet_columns": missing_pet_columns,
        "missing_indexes": missing_indexes,
        "foreign_key_error_count": len(foreign_key_errors),
        "active_assistant_count": active_count,
    }


def require_identity_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_identity_schema(conn)
    if not audit["ok"]:
        raise MigrationDriftError(
            "assistant_identity_schema_drift:"
            + json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
    return audit


IDENTITY_MIGRATION_CONTRACT = {
    "tables": {key: list(value) for key, value in IDENTITY_TABLE_COLUMNS.items()},
    "pet_columns": PET_OWNERSHIP_COLUMNS,
    "indexes": list(IDENTITY_REQUIRED_INDEXES),
    "owner": DEFAULT_OWNER_ACTOR_ID,
    "flag": IDENTITY_FEATURE_FLAG,
    "migration": "legacy_identity_to_user_owned_resources",
}
IDENTITY_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(
        IDENTITY_MIGRATION_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
).hexdigest()


__all__ = [
    "DEFAULT_OWNER_ACTOR_ID",
    "IDENTITY_FEATURE_FLAG",
    "IDENTITY_MIGRATION_CHECKSUM",
    "apply_assistant_identity_v2",
    "identity_source_preflight",
    "inspect_identity_schema",
    "require_identity_schema",
]
