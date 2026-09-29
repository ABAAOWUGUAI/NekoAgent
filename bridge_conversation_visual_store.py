#!/usr/bin/env python3
"""Hash-bound persistence for bounded conversation visual observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from bridge_conversation_visual_schema import (
    require_conversation_visual_observation_schema,
)
from bridge_migrations import utc_now
from bridge_visual_context import parse_visual_observation


class ConversationVisualObservationError(RuntimeError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cycle_binding(conn: sqlite3.Connection, cycle_id: object) -> tuple[str, str, str, str]:
    token = str(cycle_id or "").strip()
    if not token:
        raise ConversationVisualObservationError("conversation_visual_cycle_id_missing")
    row = conn.execute(
        """
        SELECT id,thread_id,assistant_id,source_set_hash
        FROM conversation_response_cycles WHERE id=?
        """,
        (token,),
    ).fetchone()
    if row is None:
        raise ConversationVisualObservationError("conversation_visual_cycle_missing")
    binding = tuple(str(value or "").strip() for value in row[:4])
    if not all(binding):
        raise ConversationVisualObservationError("conversation_visual_cycle_binding_invalid")
    return binding


def _ready_observation(value: object) -> dict:
    parsed = parse_visual_observation(value)
    if not isinstance(parsed, Mapping) or parsed.get("status") != "ready":
        raise ConversationVisualObservationError("conversation_visual_observation_invalid")
    return dict(parsed)


def _validated_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row | tuple,
    *,
    expected_binding: tuple[str, str, str, str],
) -> dict:
    cycle_id, thread_id, assistant_id, source_set_hash = expected_binding
    stored_binding = tuple(str(row[index] or "") for index in range(4))
    if stored_binding != (cycle_id, thread_id, assistant_id, source_set_hash):
        raise ConversationVisualObservationError("conversation_visual_observation_binding_drift")
    if int(row[4]) != 1 or str(row[5]) != "ready":
        raise ConversationVisualObservationError("conversation_visual_observation_schema_invalid")
    body = str(row[6] or "")
    if not body or _sha256(body) != str(row[7] or ""):
        raise ConversationVisualObservationError("conversation_visual_observation_hash_mismatch")
    try:
        decoded = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConversationVisualObservationError(
            "conversation_visual_observation_json_invalid",
        ) from exc
    observation = _ready_observation(decoded)
    if _canonical(observation) != body:
        raise ConversationVisualObservationError("conversation_visual_observation_not_canonical")
    return observation


def record_cycle_visual_observation(
    conn: sqlite3.Connection,
    *,
    cycle_id: object,
    observation: object,
) -> dict:
    """Insert one immutable visual fact, or verify an idempotent replay."""

    require_conversation_visual_observation_schema(conn)
    binding = _cycle_binding(conn, cycle_id)
    normalized = _ready_observation(observation)
    body = _canonical(normalized)
    digest = _sha256(body)
    conn.execute(
        """
        INSERT OR IGNORE INTO conversation_visual_observations(
            response_cycle_id,thread_id,assistant_id,source_set_hash,
            schema_version,status,observation_json,observation_sha256,observed_at
        ) VALUES(?,?,?,?,1,'ready',?,?,?)
        """,
        (*binding, body, digest, utc_now()),
    )
    row = conn.execute(
        """
        SELECT response_cycle_id,thread_id,assistant_id,source_set_hash,
               schema_version,status,observation_json,observation_sha256,observed_at
        FROM conversation_visual_observations WHERE response_cycle_id=?
        """,
        (binding[0],),
    ).fetchone()
    if row is None:
        raise ConversationVisualObservationError("conversation_visual_observation_write_missing")
    stored = _validated_row(conn, row, expected_binding=binding)
    if stored != normalized:
        raise ConversationVisualObservationError("conversation_visual_observation_conflict")
    return stored


def load_cycle_visual_observation(
    conn: sqlite3.Connection,
    cycle_id: object,
) -> dict | None:
    """Load one observation and fail closed on binding, schema, or hash drift."""

    require_conversation_visual_observation_schema(conn)
    binding = _cycle_binding(conn, cycle_id)
    row = conn.execute(
        """
        SELECT response_cycle_id,thread_id,assistant_id,source_set_hash,
               schema_version,status,observation_json,observation_sha256,observed_at
        FROM conversation_visual_observations WHERE response_cycle_id=?
        """,
        (binding[0],),
    ).fetchone()
    if row is None:
        return None
    return _validated_row(conn, row, expected_binding=binding)


__all__ = [
    "ConversationVisualObservationError",
    "load_cycle_visual_observation",
    "record_cycle_visual_observation",
]
