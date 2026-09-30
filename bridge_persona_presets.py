#!/usr/bin/env python3
"""Scoped, versioned Persona draft presets.

Presets are intentionally separate from ``persona_versions``: applying one
returns a normalized draft for the Console, while only the existing Persona
workspace save path may create a runtime version.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping

from bridge_assistant_audit import record_security_audit
from bridge_migrations import MigrationDriftError, utc_after, utc_now
from bridge_persona_runtime import _assistant_or_error, normalize_voice_contract


PERSONA_PRESET_TABLE = "persona_presets"
PERSONA_PRESET_COLUMNS = frozenset({
    "id", "assistant_id", "name", "description", "draft_json",
    "created_at", "updated_at", "archived_at",
})
PERSONA_PRESET_INDEX = "idx_persona_presets_active"
PERSONA_PRESET_DDL = (
    """
    CREATE TABLE persona_presets (
        id TEXT PRIMARY KEY,
        assistant_id TEXT NOT NULL REFERENCES assistant_instances(id),
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        draft_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX idx_persona_presets_active ON persona_presets(assistant_id,archived_at,updated_at DESC)",
)
PERSONA_PRESET_MIGRATION_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in PERSONA_PRESET_DDL).encode("utf-8"),
).hexdigest()

_DRAFT_FIELDS = frozenset({
    "display_name", "relationship", "persona", "style", "voice_contract",
})
_TOP_LEVEL_FIELDS = frozenset({"name", "description", "draft"})


def apply_persona_presets_v1(conn: sqlite3.Connection) -> None:
    for statement in PERSONA_PRESET_DDL:
        conn.execute(statement)


def require_persona_presets_schema(conn: sqlite3.Connection) -> dict:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (PERSONA_PRESET_TABLE,),
    ).fetchone()
    columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({PERSONA_PRESET_TABLE})")
    } if table else set()
    index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (PERSONA_PRESET_INDEX,),
    ).fetchone()
    if columns != PERSONA_PRESET_COLUMNS or not index:
        raise MigrationDriftError("persona_presets_schema_drift")
    return {"ok": True, "table": PERSONA_PRESET_TABLE, "index": PERSONA_PRESET_INDEX}


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_persona_preset_{field}")
    normalized = " ".join(value.split())
    if "\x00" in value or len(normalized) > limit or (required and not normalized):
        raise ValueError(f"invalid_persona_preset_{field}")
    return normalized


def _preset_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("persona_preset_id_required")
    identifier = value.strip()
    if not identifier or len(identifier) > 100:
        raise ValueError("persona_preset_id_required")
    return identifier


def _normalize_draft(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("persona_preset_draft_required")
    unknown = sorted(set(value) - _DRAFT_FIELDS)
    missing = sorted(_DRAFT_FIELDS - set(value))
    if unknown:
        raise ValueError("unsupported_persona_preset_draft_fields:" + ",".join(unknown))
    if missing:
        raise ValueError("persona_preset_draft_fields_required:" + ",".join(missing))
    return {
        "display_name": _text(value.get("display_name"), "display_name", 80, required=True),
        "relationship": _text(value.get("relationship"), "relationship", 80, required=True),
        "persona": _text(value.get("persona"), "persona", 4000, required=True),
        "style": _text(value.get("style"), "style", 4000, required=True),
        "voice_contract": normalize_voice_contract(value.get("voice_contract")),
    }


def _normalized_create_payload(payload: object) -> tuple[str, str, dict]:
    if not isinstance(payload, Mapping):
        raise ValueError("persona_preset_payload_required")
    unknown = sorted(set(payload) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise ValueError("unsupported_persona_preset_fields:" + ",".join(unknown))
    return (
        _text(payload.get("name"), "name", 80, required=True),
        _text(payload.get("description", ""), "description", 400),
        _normalize_draft(payload.get("draft")),
    )


def _encode_draft(draft: Mapping[str, object]) -> str:
    return json.dumps(dict(draft), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_draft(value: object) -> dict:
    try:
        decoded = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise MigrationDriftError("persona_preset_draft_corrupt") from exc
    try:
        return _normalize_draft(decoded)
    except ValueError as exc:
        raise MigrationDriftError("persona_preset_draft_corrupt") from exc


def _public(row: sqlite3.Row | Mapping[str, object]) -> dict:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"] or ""),
        "draft": _decode_draft(row["draft_json"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "archived_at": str(row["archived_at"] or ""),
    }


def _active_row(conn: sqlite3.Connection, assistant_id: str, preset_id: object) -> sqlite3.Row:
    identifier = _preset_id(preset_id)
    row = conn.execute(
        """
        SELECT id,assistant_id,name,description,draft_json,created_at,updated_at,archived_at
        FROM persona_presets
        WHERE id=? AND assistant_id=? AND archived_at=''
        """,
        (identifier, assistant_id),
    ).fetchone()
    if row is None:
        raise ValueError("persona_preset_not_found")
    return row


def _audit(conn: sqlite3.Connection, event_type: str, assistant_id: str, preset_id: str, draft: Mapping[str, object] | None = None) -> None:
    detail = {"assistant_id": assistant_id, "preset_id": preset_id}
    if draft is not None:
        detail["draft_hash"] = hashlib.sha256(_encode_draft(draft).encode("utf-8")).hexdigest()
    record_security_audit(conn, event_type, "success", detail=detail)


def list_persona_presets(conn: sqlite3.Connection, *, include_archived: bool = False) -> dict:
    require_persona_presets_schema(conn)
    assistant = _assistant_or_error(conn)
    where = "assistant_id=?" if include_archived else "assistant_id=? AND archived_at=''"
    rows = conn.execute(
        f"""
        SELECT id,assistant_id,name,description,draft_json,created_at,updated_at,archived_at
        FROM persona_presets WHERE {where}
        ORDER BY archived_at='',updated_at DESC,id
        """,
        (assistant["id"],),
    ).fetchall()
    return {"items": [_public(row) for row in rows]}


def create_persona_preset(conn: sqlite3.Connection, payload: object) -> dict:
    require_persona_presets_schema(conn)
    assistant = _assistant_or_error(conn)
    name, description, draft = _normalized_create_payload(payload)
    now = utc_now()
    preset_id = "persona-preset-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO persona_presets(id,assistant_id,name,description,draft_json,created_at,updated_at,archived_at)
        VALUES(?,?,?,?,?,?,?, '')
        """,
        (preset_id, assistant["id"], name, description, _encode_draft(draft), now, now),
    )
    row = _active_row(conn, assistant["id"], preset_id)
    _audit(conn, "persona_preset_created", assistant["id"], preset_id, draft)
    return _public(row)


def update_persona_preset(conn: sqlite3.Connection, payload: object) -> dict:
    if not isinstance(payload, Mapping):
        raise ValueError("persona_preset_payload_required")
    allowed = _TOP_LEVEL_FIELDS | {"id", "expected_updated_at"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unsupported_persona_preset_fields:" + ",".join(unknown))
    expected = _text(payload.get("expected_updated_at"), "expected_updated_at", 80, required=True)
    assistant = _assistant_or_error(conn)
    current = _active_row(conn, assistant["id"], payload.get("id"))
    if str(current["updated_at"]) != expected:
        raise ValueError("persona_preset_version_conflict")
    name, description, draft = _normalized_create_payload({key: payload.get(key) for key in _TOP_LEVEL_FIELDS})
    now = utc_after(expected)
    updated = conn.execute(
        """
        UPDATE persona_presets
        SET name=?,description=?,draft_json=?,updated_at=?
        WHERE id=? AND assistant_id=? AND archived_at='' AND updated_at=?
        """,
        (name, description, _encode_draft(draft), now, str(current["id"]), assistant["id"], expected),
    )
    if updated.rowcount != 1:
        raise ValueError("persona_preset_version_conflict")
    row = _active_row(conn, assistant["id"], current["id"])
    _audit(conn, "persona_preset_updated", assistant["id"], str(current["id"]), draft)
    return _public(row)


def archive_persona_preset(conn: sqlite3.Connection, payload: object) -> dict:
    if not isinstance(payload, Mapping) or set(payload) - {"id", "expected_updated_at"}:
        raise ValueError("unsupported_persona_preset_fields")
    expected = _text(payload.get("expected_updated_at"), "expected_updated_at", 80, required=True)
    assistant = _assistant_or_error(conn)
    current = _active_row(conn, assistant["id"], payload.get("id"))
    if str(current["updated_at"]) != expected:
        raise ValueError("persona_preset_version_conflict")
    now = utc_after(expected)
    archived = conn.execute(
        """
        UPDATE persona_presets SET archived_at=?,updated_at=?
        WHERE id=? AND assistant_id=? AND archived_at='' AND updated_at=?
        """,
        (now, now, str(current["id"]), assistant["id"], expected),
    )
    if archived.rowcount != 1:
        raise ValueError("persona_preset_version_conflict")
    _audit(conn, "persona_preset_archived", assistant["id"], str(current["id"]))
    return {"id": str(current["id"]), "archived": True, "archived_at": now}


def apply_persona_preset(conn: sqlite3.Connection, preset_id: object) -> dict:
    """Return a draft only; no Persona version or runtime setting is written."""

    require_persona_presets_schema(conn)
    assistant = _assistant_or_error(conn)
    row = _active_row(conn, assistant["id"], preset_id)
    return {"applied": False, "preset": _public(row), "draft": _decode_draft(row["draft_json"])}


__all__ = [
    "PERSONA_PRESET_MIGRATION_CHECKSUM",
    "apply_persona_preset",
    "apply_persona_presets_v1",
    "archive_persona_preset",
    "create_persona_preset",
    "list_persona_presets",
    "require_persona_presets_schema",
    "update_persona_preset",
]
