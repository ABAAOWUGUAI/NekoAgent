#!/usr/bin/env python3
"""Narrow, versioned updates for shipped Character template defaults."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone


OLD_PRIVATE_GREETING_EXAMPLE = {
    "scenario": "私聊招呼",
    "intent": "自然接住用户，不用客服式开场",
    "preferred_style": "早呀。今天想随便聊聊，还是有件事要我一起弄？",
    "avoid_style": "您好，请问有什么可以帮助您？",
}
NEW_PRIVATE_GREETING_EXAMPLE = {
    "scenario": "私聊招呼",
    "intent": "自然接住用户，顺着当下话题自然聊天，不主动把聊天分成聊天或办事",
    "preferred_style": "自然回应用户当下的话题；只有语境明确需要时再追问，不把招呼改写成客服式需求分流。",
    "avoid_style": "您好，请问有什么可以帮助您？",
}
PRIVATE_GREETING_DEFAULT_MIGRATION_CHECKSUM = "9e91cae1f66703fcf061c99c2c65af245be429b37627c4c21ec63cbf722ab53a"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _updated_boundaries(value: object) -> dict | None:
    try:
        boundaries = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(boundaries, dict):
        return None
    contract = boundaries.get("voice_contract_v1")
    if not isinstance(contract, dict):
        return None
    examples = contract.get("examples")
    if not isinstance(examples, list) or OLD_PRIVATE_GREETING_EXAMPLE not in examples:
        return None
    updated_examples = [
        dict(NEW_PRIVATE_GREETING_EXAMPLE) if item == OLD_PRIVATE_GREETING_EXAMPLE else item
        for item in examples
    ]
    updated_contract = {**contract, "examples": updated_examples}
    return {**boundaries, "voice_contract_v1": updated_contract}


def apply_private_greeting_default_v1(conn: sqlite3.Connection) -> int:
    """Fork only exact old shipped examples; never overwrite a custom Persona."""

    rows = conn.execute(
        """
        SELECT ai.id, ai.active_persona_version_id,
               pv.persona_pack_id, pv.persona_text, pv.speaking_style,
               pv.relationship_label, pv.behavior_boundaries_json,
               pp.source_type
        FROM assistant_instances AS ai
        JOIN persona_versions AS pv ON pv.id = ai.active_persona_version_id
        JOIN persona_packs AS pp ON pp.id = pv.persona_pack_id
        WHERE ai.status='active'
        ORDER BY ai.id ASC
        """,
    ).fetchall()
    changed = 0
    replacement_ids: dict[str, str] = {}
    now = _now()
    for row in rows:
        assistant_id = str(row[0])
        source_id = str(row[1])
        # A user-imported Persona can deliberately retain this sentence. Its
        # durable pack provenance means that a textual match alone must not
        # cause a migration-owned replacement.
        if str(row[7]) not in {"legacy_settings", "legacy_private", "built_in"}:
            continue
        updated_boundaries = _updated_boundaries(row[6])
        if updated_boundaries is None:
            continue
        replacement_id = replacement_ids.get(source_id)
        if replacement_id is None:
            boundaries_json = json.dumps(updated_boundaries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            next_version = int(conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM persona_versions WHERE persona_pack_id=?",
                (row[2],),
            ).fetchone()[0])
            replacement_id = f"persona-default-greeting-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO persona_versions(
                    id, persona_pack_id, version, persona_text, speaking_style,
                    relationship_label, behavior_boundaries_json, snapshot_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    replacement_id, row[2], next_version, row[3], row[4], row[5], boundaries_json,
                    _snapshot_hash(str(row[3]), str(row[4]), str(row[5]), boundaries_json), now,
                ),
            )
            replacement_ids[source_id] = replacement_id
        conn.execute(
            "UPDATE assistant_instances SET active_persona_version_id=?, updated_at=? WHERE id=?",
            (replacement_id, now, assistant_id),
        )
        conn.execute(
            """
            INSERT INTO assistant_instance_events(id, assistant_id, event_type, actor_type, channel, detail_json, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex, assistant_id, "persona_default_greeting_updated", "system", "migration",
                json.dumps({"matched_default": True}, separators=(",", ":")), now,
            ),
        )
        changed += 1
    return changed


__all__ = [
    "NEW_PRIVATE_GREETING_EXAMPLE",
    "OLD_PRIVATE_GREETING_EXAMPLE",
    "PRIVATE_GREETING_DEFAULT_MIGRATION_CHECKSUM",
    "apply_private_greeting_default_v1",
]
