#!/usr/bin/env python3
"""Assistant Instance, Persona, Appearance and Voice ownership service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Mapping

from bridge_assistant_identity_schema import (
    DEFAULT_OWNER_ACTOR_ID,
    IDENTITY_FEATURE_FLAG,
    require_identity_schema,
)
from bridge_migrations import utc_after, utc_now


IDENTITY_SETTING_KEYS = {"display_name", "persona", "style", "relationship"}


def _rows_as_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _write_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """,
        (key, value, utc_now()),
    )


def _record_event(
    conn: sqlite3.Connection,
    assistant_id: str,
    event_type: str,
    detail: Mapping[str, object] | None = None,
    *,
    actor_type: str = "owner",
    channel: str = "web",
) -> None:
    conn.execute(
        """
        INSERT INTO assistant_instance_events(
            assistant_id,event_type,actor_type,channel,detail_json,created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            assistant_id,
            event_type,
            actor_type,
            channel,
            json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            utc_now(),
        ),
    )


def identity_feature_enabled(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_feature_flags'",
    ).fetchone()
    if not table:
        return False
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (IDENTITY_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def _assistant_query() -> str:
    return """
        SELECT
            a.id,a.owner_actor_id,a.display_name,a.status,
            a.active_persona_version_id,a.active_appearance_pack_id,a.active_voice_pack_id,
            a.created_at,a.updated_at,a.archived_at,
            pv.persona_pack_id,pv.version AS persona_version,pv.persona_text,
            pv.speaking_style,pv.relationship_label,pv.behavior_boundaries_json,
            pv.snapshot_hash,pv.created_at AS persona_version_updated_at,
            pp.name AS persona_pack_name,pp.status AS persona_pack_status,
            ap.name AS appearance_name,ap.author AS appearance_author,
            ap.license AS appearance_license,ap.source_type AS appearance_source_type,
            ap.status AS appearance_status,ap.built_in AS appearance_built_in,
            vp.name AS voice_name,vp.status AS voice_status,vp.source_type AS voice_source_type
        FROM assistant_instances a
        JOIN persona_versions pv ON pv.id=a.active_persona_version_id
        JOIN persona_packs pp ON pp.id=pv.persona_pack_id
        LEFT JOIN pet_packs ap ON ap.id=a.active_appearance_pack_id
        LEFT JOIN voice_packs vp ON vp.id=a.active_voice_pack_id
    """


def _public_assistant(row: dict) -> dict:
    try:
        boundaries = json.loads(str(row.get("behavior_boundaries_json") or "{}"))
    except json.JSONDecodeError:
        boundaries = {}
    appearance = None
    if row.get("active_appearance_pack_id"):
        appearance = {
            "id": row["active_appearance_pack_id"],
            "name": row.get("appearance_name") or "",
            "author": row.get("appearance_author") or "",
            "license": row.get("appearance_license") or "",
            "source_type": row.get("appearance_source_type") or "",
            "status": row.get("appearance_status") or "",
            "preinstalled_source": bool(row.get("appearance_built_in")),
        }
    voice = None
    if row.get("active_voice_pack_id"):
        voice = {
            "id": row["active_voice_pack_id"],
            "name": row.get("voice_name") or "",
            "source_type": row.get("voice_source_type") or "",
            "status": row.get("voice_status") or "",
        }
    return {
        "id": row["id"],
        "owner_actor_id": row["owner_actor_id"],
        "display_name": row["display_name"],
        "status": row["status"],
        "persona": {
            "pack_id": row["persona_pack_id"],
            "pack_name": row["persona_pack_name"],
            "pack_status": row["persona_pack_status"],
            "version_id": row["active_persona_version_id"],
            "version": int(row["persona_version"]),
            "persona": row["persona_text"],
            "style": row["speaking_style"],
            "relationship": row["relationship_label"],
            "behavior_boundaries": boundaries,
            "snapshot_hash": row["snapshot_hash"],
            "updated_at": row["persona_version_updated_at"],
        },
        "appearance": appearance,
        "voice": voice,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
    }


def list_assistants(conn: sqlite3.Connection, owner_actor_id: str = DEFAULT_OWNER_ACTOR_ID) -> list[dict]:
    require_identity_schema(conn)
    cursor = conn.execute(
        _assistant_query() + " WHERE a.owner_actor_id=? ORDER BY a.status='active' DESC,a.created_at",
        (owner_actor_id,),
    )
    return [_public_assistant(row) for row in _rows_as_dicts(cursor)]


def current_assistant(conn: sqlite3.Connection, owner_actor_id: str = DEFAULT_OWNER_ACTOR_ID) -> dict | None:
    require_identity_schema(conn)
    cursor = conn.execute(
        _assistant_query() + " WHERE a.owner_actor_id=? AND a.status='active' LIMIT 1",
        (owner_actor_id,),
    )
    rows = _rows_as_dicts(cursor)
    return _public_assistant(rows[0]) if rows else None


def identity_resources(conn: sqlite3.Connection, owner_actor_id: str = DEFAULT_OWNER_ACTOR_ID) -> dict:
    require_identity_schema(conn)
    personas = _rows_as_dicts(
        conn.execute(
            """
            SELECT p.id,p.name,p.status,p.source_type,p.created_at,p.updated_at,p.archived_at,
                   count(v.id) AS version_count
            FROM persona_packs p
            LEFT JOIN persona_versions v ON v.persona_pack_id=p.id
            WHERE p.owner_actor_id=?
            GROUP BY p.id ORDER BY p.status='active' DESC,p.created_at
            """,
            (owner_actor_id,),
        ),
    )
    appearances = _rows_as_dicts(
        conn.execute(
            """
            SELECT id,name,author,license,status,source_type,built_in,created_at,updated_at,deleted_at
            FROM pet_packs WHERE owner_actor_id=? ORDER BY status='active' DESC,created_at
            """,
            (owner_actor_id,),
        ),
    )
    for item in appearances:
        item["preinstalled_source"] = bool(item.pop("built_in"))
        item["deletable"] = item["status"] == "active"
    voices = _rows_as_dicts(
        conn.execute(
            """
            SELECT id,name,status,source_type,config_json,created_at,updated_at,archived_at
            FROM voice_packs WHERE owner_actor_id=? ORDER BY status='active' DESC,created_at
            """,
            (owner_actor_id,),
        ),
    )
    for item in voices:
        try:
            item["config"] = json.loads(str(item.pop("config_json") or "{}"))
        except json.JSONDecodeError:
            item["config"] = {}
    return {"personas": personas, "appearances": appearances, "voices": voices}


def identity_shadow_compare(conn: sqlite3.Connection) -> dict:
    require_identity_schema(conn)
    current = current_assistant(conn)
    if current is None:
        return {"ok": False, "mismatches": ["active_assistant"], "checked_fields": 5}
    rows = conn.execute(
        """
        SELECT key,value FROM settings
        WHERE key IN ('display_name','persona','style','relationship','admin_pet_pack_id')
        """,
    ).fetchall()
    legacy = {str(row[0]): str(row[1] or "") for row in rows}
    expected = {
        "display_name": str(current["display_name"]),
        "persona": str(current["persona"]["persona"]),
        "style": str(current["persona"]["style"]),
        "relationship": str(current["persona"]["relationship"]),
        "admin_pet_pack_id": str((current.get("appearance") or {}).get("id") or ""),
    }
    mismatches = sorted(key for key, value in expected.items() if legacy.get(key, "") != value)
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "checked_fields": len(expected),
        "assistant_id": current["id"],
        "feature_enabled": identity_feature_enabled(conn),
    }


def identity_cutover_plan(conn: sqlite3.Connection) -> dict:
    shadow = identity_shadow_compare(conn)
    payload = {
        "assistant_id": shadow.get("assistant_id") or "",
        "feature_enabled": bool(shadow.get("feature_enabled")),
        "mismatches": shadow["mismatches"],
        "checked_fields": shadow["checked_fields"],
    }
    checksum = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    return {"ok": shadow["ok"], **payload, "plan_checksum": checksum}


def set_identity_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
    actor_type: str = "operator",
    channel: str = "cli",
) -> dict:
    plan = identity_cutover_plan(conn)
    if expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_identity_cutover_plan")
    if enabled and not plan["ok"]:
        raise ValueError("identity_shadow_compare_failed")
    now = utc_now()
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (IDENTITY_FEATURE_FLAG, 1 if enabled else 0, now),
    )
    assistant = current_assistant(conn)
    if assistant:
        _record_event(
            conn,
            assistant["id"],
            "identity_feature_enabled" if enabled else "identity_feature_disabled",
            {"shadow_checked_fields": plan["checked_fields"]},
            actor_type=actor_type,
            channel=channel,
        )
    return identity_cutover_plan(conn)


def identity_overlay_settings(conn: sqlite3.Connection, legacy_settings: dict) -> dict:
    if not identity_feature_enabled(conn):
        return dict(legacy_settings)
    shadow = identity_shadow_compare(conn)
    if not shadow["ok"]:
        raise ValueError("identity_shadow_compare_failed")
    current = current_assistant(conn)
    if current is None:
        raise ValueError("active_assistant_missing")
    result = dict(legacy_settings)
    from bridge_persona_runtime import resolve_voice_contract

    voice_contract, contract_resolution = resolve_voice_contract(
        current["persona"].get("behavior_boundaries"),
    )
    result.update(
        {
            "assistant_id": current["id"],
            "display_name": current["display_name"],
            "persona": current["persona"]["persona"],
            "style": current["persona"]["style"],
            "relationship": current["persona"]["relationship"],
            "voice_contract": voice_contract,
            "persona_version_id": current["persona"]["version_id"],
            "persona_version": current["persona"]["version"],
            "persona_updated_at": current["persona"]["updated_at"],
            "persona_contract_source": contract_resolution["source"],
            "persona_contract_error": contract_resolution["compile_error"],
            "assistant_identity_source": "assistant_identity_v2",
        },
    )
    return result


def _normalize_identity_patch(payload: Mapping[str, object]) -> dict:
    allowed = {
        "display_name", "persona", "style", "relationship",
        "appearance_pack_id", "voice_pack_id", "behavior_boundaries", "expected_updated_at",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unsupported_assistant_fields:" + ",".join(unknown))
    clean: dict[str, object] = {}
    for key in ("display_name", "persona", "style", "relationship"):
        if key in payload:
            value = str(payload.get(key) or "").strip()
            limit = 80 if key in {"display_name", "relationship"} else 4000
            if not value or len(value) > limit:
                raise ValueError(f"invalid_{key}")
            clean[key] = value
    for key in ("appearance_pack_id", "voice_pack_id"):
        if key in payload:
            clean[key] = str(payload.get(key) or "").strip() or None
    if "behavior_boundaries" in payload:
        boundaries = payload.get("behavior_boundaries")
        if not isinstance(boundaries, Mapping):
            raise ValueError("invalid_behavior_boundaries")
        encoded = json.dumps(
            dict(boundaries), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > 32768:
            raise ValueError("invalid_behavior_boundaries")
        clean["behavior_boundaries"] = json.loads(encoded)
    if not clean:
        raise ValueError("assistant_patch_required")
    return clean


def update_assistant(
    conn: sqlite3.Connection,
    assistant_id: str,
    payload: Mapping[str, object],
    *,
    actor_type: str = "owner",
    channel: str = "web",
) -> dict:
    if not identity_feature_enabled(conn):
        raise ValueError("assistant_identity_v2_disabled")
    expected_updated_at = str(payload.get("expected_updated_at") or "").strip()
    patch = _normalize_identity_patch(payload)
    current = current_assistant(conn)
    if current is None or current["id"] != assistant_id:
        raise ValueError("assistant_not_found")
    if expected_updated_at and expected_updated_at != current["updated_at"]:
        raise ValueError("assistant_version_conflict")
    now = utc_after(str(current["updated_at"]))
    changes: list[str] = []
    display_name = str(patch.get("display_name") or current["display_name"])
    persona_text = str(patch.get("persona") or current["persona"]["persona"])
    speaking_style = str(patch.get("style") or current["persona"]["style"])
    relationship = str(patch.get("relationship") or current["persona"]["relationship"])
    persona_changed = any(
        key in patch for key in ("persona", "style", "relationship", "behavior_boundaries")
    )
    persona_version_id = current["persona"]["version_id"]
    if persona_changed:
        next_version = int(
            conn.execute(
                "SELECT coalesce(max(version),0)+1 FROM persona_versions WHERE persona_pack_id=?",
                (current["persona"]["pack_id"],),
            ).fetchone()[0],
        )
        persona_version_id = "persona-version-" + uuid.uuid4().hex
        boundaries_json = json.dumps(
            patch.get("behavior_boundaries", current["persona"].get("behavior_boundaries") or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_payload = json.dumps(
            {
                "persona_text": persona_text,
                "speaking_style": speaking_style,
                "relationship_label": relationship,
                "behavior_boundaries_json": boundaries_json,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """
            INSERT INTO persona_versions(
                id,persona_pack_id,version,persona_text,speaking_style,
                relationship_label,behavior_boundaries_json,snapshot_hash,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                persona_version_id,
                current["persona"]["pack_id"],
                next_version,
                persona_text,
                speaking_style,
                relationship,
                boundaries_json,
                hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest(),
                now,
            ),
        )
        changes.append("persona_version")
    appearance_id = (
        patch["appearance_pack_id"]
        if "appearance_pack_id" in patch
        else (current.get("appearance") or {}).get("id")
    )
    if appearance_id is not None:
        row = conn.execute(
            "SELECT owner_actor_id,status FROM pet_packs WHERE id=?",
            (appearance_id,),
        ).fetchone()
        if not row or str(row[0]) != current["owner_actor_id"] or str(row[1]) != "active":
            raise ValueError("appearance_pack_not_found")
    voice_id = (
        patch["voice_pack_id"]
        if "voice_pack_id" in patch
        else (current.get("voice") or {}).get("id")
    )
    if voice_id is not None:
        row = conn.execute(
            "SELECT owner_actor_id,status FROM voice_packs WHERE id=?",
            (voice_id,),
        ).fetchone()
        if not row or str(row[0]) != current["owner_actor_id"] or str(row[1]) != "active":
            raise ValueError("voice_pack_not_found")
    if display_name != current["display_name"]:
        changes.append("display_name")
    if appearance_id != (current.get("appearance") or {}).get("id"):
        changes.append("appearance")
    if voice_id != (current.get("voice") or {}).get("id"):
        changes.append("voice")
    conn.execute(
        """
        UPDATE assistant_instances
        SET display_name=?,active_persona_version_id=?,active_appearance_pack_id=?,
            active_voice_pack_id=?,updated_at=?
        WHERE id=? AND status='active'
        """,
        (display_name, persona_version_id, appearance_id, voice_id, now, assistant_id),
    )
    for key, value in {
        "display_name": display_name,
        "persona": persona_text,
        "style": speaking_style,
        "relationship": relationship,
        "admin_pet_pack_id": str(appearance_id or ""),
    }.items():
        _write_setting(conn, key, value)
    if appearance_id is None:
        _write_setting(conn, "admin_pet_enabled", "0")
    _record_event(
        conn,
        assistant_id,
        "assistant_updated",
        {"changed_fields": sorted(changes)},
        actor_type=actor_type,
        channel=channel,
    )
    updated = current_assistant(conn)
    if updated is None:
        raise ValueError("active_assistant_missing")
    return updated


def write_identity_settings(conn: sqlite3.Connection, payload: Mapping[str, object]) -> bool:
    """Double-write legacy identity fields when the Gate 2 read path is enabled."""

    patch = {key: payload[key] for key in IDENTITY_SETTING_KEYS if key in payload}
    if not patch or not identity_feature_enabled(conn):
        return False
    assistant = current_assistant(conn)
    if assistant is None:
        raise ValueError("active_assistant_missing")
    update_assistant(
        conn,
        assistant["id"],
        patch,
        channel="web-settings",
    )
    return True


__all__ = [
    "IDENTITY_SETTING_KEYS",
    "current_assistant",
    "identity_cutover_plan",
    "identity_feature_enabled",
    "identity_overlay_settings",
    "identity_resources",
    "identity_shadow_compare",
    "list_assistants",
    "set_identity_feature",
    "update_assistant",
    "write_identity_settings",
]
