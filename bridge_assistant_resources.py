#!/usr/bin/env python3
"""Assistant, VoicePack and AppearancePack resource lifecycle operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Mapping

from bridge_assistant_identity import (
    _record_event,
    _write_setting,
    current_assistant,
    identity_feature_enabled,
    identity_resources,
    list_assistants,
)
from bridge_assistant_identity_schema import DEFAULT_OWNER_ACTOR_ID
from bridge_migrations import utc_now
from bridge_network_policy_schema import ensure_network_policy_for_assistant
from bridge_voice_pack_tuning import normalize_piper_synthesis, piper_tuning_presets


def create_assistant(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    *,
    owner_actor_id: str = DEFAULT_OWNER_ACTOR_ID,
    actor_type: str = "owner",
    channel: str = "web",
) -> dict:
    if not identity_feature_enabled(conn):
        raise ValueError("assistant_identity_v2_disabled")
    display_name = str(payload.get("display_name") or "").strip()
    persona_text = str(payload.get("persona") or "").strip()
    speaking_style = str(payload.get("style") or "").strip()
    relationship = str(payload.get("relationship") or "朋友").strip()
    if not display_name or len(display_name) > 80 or not persona_text or not speaking_style:
        raise ValueError("invalid_assistant_identity")
    now = utc_now()
    persona_pack_id = "persona-" + uuid.uuid4().hex
    persona_version_id = "persona-version-" + uuid.uuid4().hex
    assistant_id = "assistant-" + uuid.uuid4().hex
    active_exists = bool(
        conn.execute(
            "SELECT 1 FROM assistant_instances WHERE owner_actor_id=? AND status='active'",
            (owner_actor_id,),
        ).fetchone(),
    )
    status = "archived" if active_exists else "active"
    boundaries_json = "{}"
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
        INSERT INTO persona_packs(
            id,owner_actor_id,name,status,source_type,created_at,updated_at,archived_at
        ) VALUES(?,?,?,'active','user_created',?,?, '')
        """,
        (persona_pack_id, owner_actor_id, f"{display_name} 人格", now, now),
    )
    conn.execute(
        """
        INSERT INTO persona_versions(
            id,persona_pack_id,version,persona_text,speaking_style,relationship_label,
            behavior_boundaries_json,snapshot_hash,created_at
        ) VALUES(?,?,1,?,?,?,?,?,?)
        """,
        (
            persona_version_id,
            persona_pack_id,
            persona_text,
            speaking_style,
            relationship,
            boundaries_json,
            hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest(),
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO assistant_instances(
            id,owner_actor_id,display_name,status,active_persona_version_id,
            active_appearance_pack_id,active_voice_pack_id,created_at,updated_at,archived_at
        ) VALUES(?,?,?,?,?,NULL,NULL,?,?,?)
        """,
        (
            assistant_id,
            owner_actor_id,
            display_name,
            status,
            persona_version_id,
            now,
            now,
            now if status == "archived" else "",
        ),
    )
    network_policy_available = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='assistant_network_policies'
        """,
    ).fetchone()
    if network_policy_available:
        ensure_network_policy_for_assistant(conn, assistant_id, now=now)
    _record_event(
        conn,
        assistant_id,
        "assistant_created",
        {"initial_status": status, "inherits_private_state": False},
        actor_type=actor_type,
        channel=channel,
    )
    return next(
        item
        for item in list_assistants(conn, owner_actor_id)
        if item["id"] == assistant_id
    )


def activate_assistant(
    conn: sqlite3.Connection,
    assistant_id: str,
    *,
    actor_type: str = "owner",
    channel: str = "web",
) -> dict:
    if not identity_feature_enabled(conn):
        raise ValueError("assistant_identity_v2_disabled")
    target = conn.execute(
        "SELECT owner_actor_id,status FROM assistant_instances WHERE id=?",
        (assistant_id,),
    ).fetchone()
    if not target:
        raise ValueError("assistant_not_found")
    owner_actor_id = str(target[0])
    now = utc_now()
    current = current_assistant(conn, owner_actor_id)
    if current and current["id"] == assistant_id:
        return current
    if current:
        conn.execute(
            "UPDATE assistant_instances SET status='archived',archived_at=?,updated_at=? WHERE id=?",
            (now, now, current["id"]),
        )
        _record_event(
            conn,
            current["id"],
            "assistant_archived",
            {"replacement_assistant_id": assistant_id},
            actor_type=actor_type,
            channel=channel,
        )
    conn.execute(
        "UPDATE assistant_instances SET status='active',archived_at='',updated_at=? WHERE id=?",
        (now, assistant_id),
    )
    activated = current_assistant(conn, owner_actor_id)
    if activated is None:
        raise ValueError("assistant_activation_failed")
    for key, value in {
        "display_name": activated["display_name"],
        "persona": activated["persona"]["persona"],
        "style": activated["persona"]["style"],
        "relationship": activated["persona"]["relationship"],
        "admin_pet_pack_id": str((activated.get("appearance") or {}).get("id") or ""),
    }.items():
        _write_setting(conn, key, value)
    if not activated.get("appearance"):
        _write_setting(conn, "admin_pet_enabled", "0")
    _record_event(
        conn,
        assistant_id,
        "assistant_activated",
        {"previous_assistant_id": (current or {}).get("id") or ""},
        actor_type=actor_type,
        channel=channel,
    )
    return activated


def archive_assistant(
    conn: sqlite3.Connection,
    assistant_id: str,
    *,
    replacement_assistant_id: str = "",
    actor_type: str = "owner",
    channel: str = "web",
) -> dict:
    if not identity_feature_enabled(conn):
        raise ValueError("assistant_identity_v2_disabled")
    row = conn.execute(
        "SELECT owner_actor_id,status FROM assistant_instances WHERE id=?",
        (assistant_id,),
    ).fetchone()
    if not row:
        raise ValueError("assistant_not_found")
    if str(row[1]) == "active":
        if not replacement_assistant_id:
            raise ValueError("active_assistant_replacement_required")
        if replacement_assistant_id == assistant_id:
            raise ValueError("replacement_assistant_required")
        replacement = conn.execute(
            "SELECT owner_actor_id FROM assistant_instances WHERE id=?",
            (replacement_assistant_id,),
        ).fetchone()
        if not replacement or str(replacement[0]) != str(row[0]):
            raise ValueError("replacement_assistant_not_found")
        return activate_assistant(
            conn,
            replacement_assistant_id,
            actor_type=actor_type,
            channel=channel,
        )
    now = utc_now()
    conn.execute(
        "UPDATE assistant_instances SET status='archived',archived_at=?,updated_at=? WHERE id=?",
        (now, now, assistant_id),
    )
    _record_event(
        conn,
        assistant_id,
        "assistant_archived",
        {"replacement_assistant_id": ""},
        actor_type=actor_type,
        channel=channel,
    )
    return next(
        item
        for item in list_assistants(conn, str(row[0]))
        if item["id"] == assistant_id
    )


def create_voice_pack(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    owner_actor_id: str = DEFAULT_OWNER_ACTOR_ID,
) -> dict:
    if not identity_feature_enabled(conn):
        raise ValueError("assistant_identity_v2_disabled")
    name = str(payload.get("name") or "").strip()
    config = payload.get("config") or {}
    if not name or len(name) > 80 or not isinstance(config, dict):
        raise ValueError("invalid_voice_pack")
    pack_id = "voice-" + uuid.uuid4().hex
    now = utc_now()
    conn.execute(
        """
        INSERT INTO voice_packs(
            id,owner_actor_id,name,status,source_type,config_json,created_at,updated_at,archived_at
        ) VALUES(?,?,?,'active','user_created',?,?,?, '')
        """,
        (
            pack_id,
            owner_actor_id,
            name,
            json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now,
            now,
        ),
    )
    return next(
        item
        for item in identity_resources(conn, owner_actor_id)["voices"]
        if item["id"] == pack_id
    )


def archive_voice_pack(conn: sqlite3.Connection, pack_id: str) -> dict:
    if not identity_feature_enabled(conn):
        raise ValueError("assistant_identity_v2_disabled")
    bound = conn.execute(
        "SELECT id FROM assistant_instances WHERE active_voice_pack_id=?",
        (pack_id,),
    ).fetchone()
    if bound:
        raise ValueError("voice_pack_in_use")
    row = conn.execute(
        "SELECT owner_actor_id FROM voice_packs WHERE id=?",
        (pack_id,),
    ).fetchone()
    if not row:
        raise ValueError("voice_pack_not_found")
    now = utc_now()
    conn.execute(
        "UPDATE voice_packs SET status='archived',archived_at=?,updated_at=? WHERE id=?",
        (now, now, pack_id),
    )
    return next(
        item
        for item in identity_resources(conn, str(row[0]))["voices"]
        if item["id"] == pack_id
    )


def active_voice_pack_tuning(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT a.id,v.id,v.name,v.status,v.config_json,v.updated_at
        FROM assistant_instances a
        LEFT JOIN voice_packs v ON v.id=a.active_voice_pack_id
        WHERE a.status='active' ORDER BY a.updated_at DESC,a.id LIMIT 1
        """,
    ).fetchone()
    if not row or not row[1] or str(row[3]) != "active":
        raise ValueError("voice_pack_not_bound")
    try:
        config = json.loads(str(row[4] or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("voice_pack_config_invalid") from exc
    if not isinstance(config, dict) or str(config.get("engine") or "") != "piper":
        raise ValueError("voice_pack_engine_tuning_unsupported")
    return {
        "assistant_id": str(row[0]),
        "voice_pack_id": str(row[1]),
        "voice_pack_name": str(row[2]),
        "engine": "piper",
        "model": str(config.get("model") or ""),
        "synthesis": normalize_piper_synthesis(config.get("synthesis")),
        "presets": piper_tuning_presets(),
        "updated_at": str(row[5]),
    }


def update_active_voice_pack_tuning(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
) -> dict:
    current = active_voice_pack_tuning(conn)
    expected = str(payload.get("expected_updated_at") or "").strip()
    if not expected:
        raise ValueError("voice_pack_version_required")
    if expected != current["updated_at"]:
        raise ValueError("voice_pack_version_conflict")
    row = conn.execute(
        "SELECT config_json FROM voice_packs WHERE id=? AND status='active'",
        (current["voice_pack_id"],),
    ).fetchone()
    if not row:
        raise ValueError("voice_pack_not_found")
    try:
        config = json.loads(str(row[0] or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("voice_pack_config_invalid") from exc
    synthesis = normalize_piper_synthesis(payload.get("synthesis"))
    config["synthesis"] = synthesis
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE voice_packs SET config_json=?,updated_at=?
        WHERE id=? AND status='active' AND updated_at=?
        """,
        (
            json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now,
            current["voice_pack_id"],
            expected,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("voice_pack_version_conflict")
    _record_event(
        conn,
        current["assistant_id"],
        "voice_pack_tuning_updated",
        {
            "voice_pack_id": current["voice_pack_id"],
            "engine": "piper",
            "preset": synthesis["preset"],
            "emotion_variation": synthesis["emotion_variation"],
        },
        actor_type="owner",
        channel="web",
    )
    return active_voice_pack_tuning(conn)


def appearance_pack_binding(conn: sqlite3.Connection, pack_id: str) -> list[str]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_instances'",
    ).fetchone()
    if not table:
        return []
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT id FROM assistant_instances WHERE active_appearance_pack_id=?",
            (pack_id,),
        ).fetchall()
    ]


def replace_or_unbind_appearance(
    conn: sqlite3.Connection,
    pack_id: str,
    *,
    replacement_pack_id: str = "",
    unbind: bool = False,
) -> None:
    bindings = appearance_pack_binding(conn, pack_id)
    if not bindings:
        return
    if not replacement_pack_id and not unbind:
        raise ValueError("appearance_pack_in_use")
    replacement: str | None = None
    if replacement_pack_id:
        row = conn.execute(
            "SELECT owner_actor_id,status FROM pet_packs WHERE id=?",
            (replacement_pack_id,),
        ).fetchone()
        if not row or str(row[1]) != "active":
            raise ValueError("replacement_appearance_pack_not_found")
        replacement = replacement_pack_id
    now = utc_now()
    for assistant_id in bindings:
        conn.execute(
            "UPDATE assistant_instances SET active_appearance_pack_id=?,updated_at=? WHERE id=?",
            (replacement, now, assistant_id),
        )
        _record_event(
            conn,
            assistant_id,
            "appearance_rebound" if replacement else "appearance_unbound",
            {"old_pack_id": pack_id, "new_pack_id": replacement or ""},
        )
    _write_setting(conn, "admin_pet_pack_id", replacement or "")
    if replacement is None:
        _write_setting(conn, "admin_pet_enabled", "0")


__all__ = [
    "activate_assistant",
    "active_voice_pack_tuning",
    "appearance_pack_binding",
    "archive_assistant",
    "archive_voice_pack",
    "create_assistant",
    "create_voice_pack",
    "replace_or_unbind_appearance",
    "update_active_voice_pack_tuning",
]
