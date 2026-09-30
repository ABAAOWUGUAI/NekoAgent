#!/usr/bin/env python3
"""Durable ownership and model-call fences for group responses.

The ledger stores identifiers and hashes only.  Conversation text, generated
text, media descriptors and visual observations stay in their existing
governed/transient paths.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping

from bridge_delivery_continuity import logical_response_id
from bridge_group_response_commitment_schema import (
    require_group_response_commitment_schema,
)
from bridge_migrations import utc_now


_HASH = re.compile(r"[0-9a-f]{64}")
_OWNER_KINDS = {"direct", "ambient"}
_PHASE_COLUMNS = {"vision": "vision_started", "plan": "plan_started"}
_STATES = {"owned", "silent", "planned", "committed", "held"}
_TRANSITIONS = {
    "owned": {"silent", "planned", "committed", "held"},
    "planned": {"silent", "committed", "held"},
    "silent": set(),
    "committed": set(),
    "held": set(),
}


def _required(value: object, error: str, *, limit: int = 300) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(error)
    return result[:limit]


def _hash(value: object, error: str) -> str:
    result = str(value or "").strip().lower()
    if not _HASH.fullmatch(result):
        raise ValueError(error)
    return result


def _owner(value: object) -> str:
    result = str(value or "").strip().lower()
    if result not in _OWNER_KINDS:
        raise ValueError("group_response_owner_kind_invalid")
    return result


def _row(row: sqlite3.Row | tuple | None, columns: list[str] | None = None) -> dict | None:
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    if columns is None:
        raise ValueError("group_response_row_shape_invalid")
    return dict(zip(columns, row))


def _select_one(conn: sqlite3.Connection, where: str, value: str) -> dict | None:
    cursor = conn.execute(
        f"SELECT * FROM group_response_commitments WHERE {where}=?",
        (value,),
    )
    columns = [str(item[0]) for item in cursor.description or ()]
    return _row(cursor.fetchone(), columns)


def load_group_response_commitment(
    conn: sqlite3.Connection,
    commitment_id: str,
) -> dict | None:
    require_group_response_commitment_schema(conn)
    return _select_one(
        conn,
        "id",
        _required(commitment_id, "group_response_commitment_id_required", limit=120),
    )


def load_group_response_commitment_for_event(
    conn: sqlite3.Connection,
    event_id: str,
) -> dict | None:
    require_group_response_commitment_schema(conn)
    return _select_one(
        conn,
        "event_id",
        _required(event_id, "group_response_event_required", limit=160),
    )


def claim_group_response_commitment(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    assistant_id: str,
    group_id: str,
    source_message_id: str,
    source_set_hash: str,
    situation_revision: str,
    owner_kind: str,
) -> dict:
    require_group_response_commitment_schema(conn)
    event = _required(event_id, "group_response_event_required", limit=160)
    assistant = _required(assistant_id, "group_response_assistant_required", limit=160)
    group = _required(group_id, "group_response_group_required", limit=160)
    source = _required(source_message_id, "group_response_source_required", limit=300)
    source_hash = _hash(source_set_hash, "group_response_source_set_hash_invalid")
    revision = _hash(situation_revision, "group_response_situation_revision_invalid")
    owner = _owner(owner_kind)
    commitment_id = "grc_" + hashlib.sha256(event.encode("utf-8")).hexdigest()[:32]
    response_id = logical_response_id(
        channel="qq",
        thread_ref=f"qq:group:{group}",
        source_message_id=source,
        response_kind="group_response_commitment",
    )
    now = utc_now()
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO group_response_commitments(
                id,event_id,assistant_id,group_id,source_message_id,
                source_set_hash,situation_revision,owner_kind,
                logical_response_id,outbox_dedupe_key,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                commitment_id, event, assistant, group, source,
                source_hash, revision, owner, response_id,
                f"qq:response:{response_id}", now, now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("group_response_event_missing") from exc
    stored = load_group_response_commitment_for_event(conn, event)
    if stored is None:
        raise ValueError("group_response_commitment_claim_failed")
    expected = {
        "event_id": event,
        "assistant_id": assistant,
        "group_id": group,
        "source_message_id": source,
        "source_set_hash": source_hash,
        "situation_revision": revision,
    }
    if any(str(stored.get(key) or "") != value for key, value in expected.items()):
        raise ValueError("group_response_commitment_binding_conflict")
    return {**stored, "acquired": bool(cursor.rowcount)}


def advance_group_response_situation(
    conn: sqlite3.Connection,
    *,
    commitment_id: str,
    owner_kind: str,
    source_set_hash: str,
    situation_revision: str,
) -> dict:
    commitment = _required(
        commitment_id,
        "group_response_commitment_id_required",
        limit=120,
    )
    owner = _owner(owner_kind)
    source_hash = _hash(source_set_hash, "group_response_source_set_hash_invalid")
    revision = _hash(situation_revision, "group_response_situation_revision_invalid")
    current = load_group_response_commitment(conn, commitment)
    if current is None:
        raise ValueError("group_response_commitment_missing")
    if str(current.get("owner_kind") or "") != owner:
        raise ValueError("group_response_commitment_owner_lost")
    if str(current.get("source_set_hash") or "") != source_hash:
        raise ValueError("group_response_commitment_binding_conflict")
    if int(current.get("plan_started") or 0):
        raise ValueError("group_response_situation_frozen")
    conn.execute(
        """
        UPDATE group_response_commitments
        SET situation_revision=?,updated_at=?
        WHERE id=? AND owner_kind=? AND source_set_hash=? AND plan_started=0
        """,
        (revision, utc_now(), commitment, owner, source_hash),
    )
    advanced = load_group_response_commitment(conn, commitment)
    if advanced is None or str(advanced.get("situation_revision") or "") != revision:
        raise ValueError("group_response_situation_advance_failed")
    return advanced


def begin_group_response_phase(
    conn: sqlite3.Connection,
    *,
    commitment_id: str,
    owner_kind: str,
    phase: str,
) -> bool:
    commitment = _required(
        commitment_id,
        "group_response_commitment_id_required",
        limit=120,
    )
    owner = _owner(owner_kind)
    phase_name = str(phase or "").strip().lower()
    column = _PHASE_COLUMNS.get(phase_name)
    if column is None:
        raise ValueError("group_response_phase_invalid")
    require_group_response_commitment_schema(conn)
    cursor = conn.execute(
        f"""
        UPDATE group_response_commitments
        SET {column}=1,updated_at=?
        WHERE id=? AND owner_kind=? AND {column}=0
          AND state IN ('owned','planned')
        """,
        (utc_now(), commitment, owner),
    )
    if cursor.rowcount:
        return True
    current = load_group_response_commitment(conn, commitment)
    if current is None:
        raise ValueError("group_response_commitment_missing")
    if str(current.get("owner_kind") or "") != owner:
        return False
    return False


def settle_group_response_commitment(
    conn: sqlite3.Connection,
    *,
    commitment_id: str,
    owner_kind: str,
    state: str,
    delivery_id: str = "",
) -> dict:
    commitment = _required(
        commitment_id,
        "group_response_commitment_id_required",
        limit=120,
    )
    owner = _owner(owner_kind)
    target = str(state or "").strip().lower()
    if target not in _STATES or target == "owned":
        raise ValueError("group_response_state_invalid")
    delivery = str(delivery_id or "").strip()[:160]
    if target == "committed" and not delivery:
        raise ValueError("group_response_delivery_required")
    current = load_group_response_commitment(conn, commitment)
    if current is None:
        raise ValueError("group_response_commitment_missing")
    if str(current.get("owner_kind") or "") != owner:
        raise ValueError("group_response_commitment_owner_lost")
    current_state = str(current.get("state") or "")
    if current_state == target:
        if target == "committed" and str(current.get("delivery_id") or "") != delivery:
            raise ValueError("group_response_delivery_conflict")
        return current
    if target not in _TRANSITIONS.get(current_state, set()):
        raise ValueError("group_response_state_transition_invalid")
    conn.execute(
        """
        UPDATE group_response_commitments
        SET state=?,delivery_id=?,updated_at=?
        WHERE id=? AND owner_kind=? AND state=?
        """,
        (target, delivery if target == "committed" else "", utc_now(), commitment, owner, current_state),
    )
    updated = load_group_response_commitment(conn, commitment)
    if updated is None or str(updated.get("state") or "") != target:
        raise ValueError("group_response_settlement_failed")
    return updated


def group_response_identity(commitment: Mapping[str, object]) -> dict[str, object]:
    required = {
        "id": "group_response_commitment_id_required",
        "event_id": "group_response_event_required",
        "assistant_id": "group_response_assistant_required",
        "group_id": "group_response_group_required",
        "source_message_id": "group_response_source_required",
        "source_set_hash": "group_response_source_set_hash_invalid",
        "situation_revision": "group_response_situation_revision_invalid",
        "owner_kind": "group_response_owner_kind_invalid",
        "logical_response_id": "group_response_logical_id_required",
        "outbox_dedupe_key": "group_response_dedupe_required",
    }
    values = {
        key: _required(commitment.get(key), error, limit=300)
        for key, error in required.items()
    }
    _hash(values["source_set_hash"], "group_response_source_set_hash_invalid")
    _hash(values["situation_revision"], "group_response_situation_revision_invalid")
    _owner(values["owner_kind"])
    if values["outbox_dedupe_key"] != f"qq:response:{values['logical_response_id']}":
        raise ValueError("group_response_dedupe_invalid")
    return {
        "commitment_id": values["id"],
        "event_id": values["event_id"],
        "assistant_id": values["assistant_id"],
        "group_id": values["group_id"],
        "source_message_id": values["source_message_id"],
        "source_set_hash": values["source_set_hash"],
        "situation_revision": values["situation_revision"],
        "owner_kind": values["owner_kind"],
        "logical_response_id": values["logical_response_id"],
        "outbox_dedupe_key": values["outbox_dedupe_key"],
    }


__all__ = [
    "advance_group_response_situation",
    "begin_group_response_phase",
    "claim_group_response_commitment",
    "group_response_identity",
    "load_group_response_commitment",
    "load_group_response_commitment_for_event",
    "settle_group_response_commitment",
]
