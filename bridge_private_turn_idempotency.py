#!/usr/bin/env python3
"""Durable aliases for source IDs joined into one private logical turn.

The plugin's short collector is intentionally process-local. This module
stores only a non-primary QQ source-ID to primary-receipt relationship, so a
post-restart redelivery cannot turn consumed media into a second reply. It
never stores message text or media bytes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable

from bridge_inbound_idempotency import (
    InboundConflictError,
    InboundIdempotencyUnavailableError,
    InboundOutcomeUnknownError,
    InboundProcessingError,
    InboundRequestValidationError,
    MESSAGE_ID_RE,
)
from bridge_migrations import utc_now


PRIVATE_TURN_MEMBER_TABLE = "qq_private_turn_members"
PRIVATE_TURN_MEMBER_MAX_SOURCES = 16
PRIVATE_TURN_MEMBER_COLUMNS = (
    "platform_message_id", "primary_message_id", "actor_id", "conversation_ref", "created_at",
)
PRIVATE_TURN_MEMBER_INDEXES = ("idx_qq_private_turn_members_primary",)


def _checksum() -> str:
    payload = json.dumps(
        {
            "table": PRIVATE_TURN_MEMBER_TABLE,
            "columns": list(PRIVATE_TURN_MEMBER_COLUMNS),
            "indexes": list(PRIVATE_TURN_MEMBER_INDEXES),
            "max_sources": PRIVATE_TURN_MEMBER_MAX_SOURCES,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


PRIVATE_TURN_MEMBER_MIGRATION_CHECKSUM = _checksum()


def apply_private_turn_member_dedupe_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE {PRIVATE_TURN_MEMBER_TABLE} (
            platform_message_id TEXT PRIMARY KEY,
            primary_message_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            conversation_ref TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "CREATE INDEX idx_qq_private_turn_members_primary "
        f"ON {PRIVATE_TURN_MEMBER_TABLE}(primary_message_id,actor_id,conversation_ref)",
    )


def inspect_private_turn_member_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    columns = (
        {str(row[1]) for row in conn.execute(f"PRAGMA table_info({PRIVATE_TURN_MEMBER_TABLE})")}
        if PRIVATE_TURN_MEMBER_TABLE in tables else set()
    )
    missing_columns = sorted(set(PRIVATE_TURN_MEMBER_COLUMNS) - columns)
    missing_indexes = sorted(set(PRIVATE_TURN_MEMBER_INDEXES) - indexes)
    return {
        "ok": PRIVATE_TURN_MEMBER_TABLE in tables and not missing_columns and not missing_indexes,
        "contract_checksum": PRIVATE_TURN_MEMBER_MIGRATION_CHECKSUM,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
    }


def require_private_turn_member_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_private_turn_member_schema(conn)
    if not audit["ok"]:
        raise InboundIdempotencyUnavailableError("private_turn_member_idempotency_unavailable")
    return audit


def _message_id(value: object, *, error: str) -> str:
    normalized = str(value or "").strip()
    if not MESSAGE_ID_RE.fullmatch(normalized):
        raise InboundRequestValidationError(error)
    return normalized


def _members(primary_message_id: object, source_message_ids: object) -> tuple[str, list[str]]:
    primary = _message_id(primary_message_id, error="qq_private_turn_primary_message_invalid")
    if not isinstance(source_message_ids, list) or not source_message_ids:
        raise InboundRequestValidationError("qq_private_turn_sources_required")
    if len(source_message_ids) > PRIVATE_TURN_MEMBER_MAX_SOURCES:
        raise InboundRequestValidationError("qq_private_turn_sources_limit")
    members = [_message_id(item, error="qq_private_turn_source_message_invalid") for item in source_message_ids]
    if members[0] != primary:
        raise InboundRequestValidationError("qq_private_turn_primary_message_mismatch")
    if len(set(members)) != len(members):
        raise InboundRequestValidationError("qq_private_turn_source_message_duplicate")
    return primary, members


def register_private_turn_member_aliases(
    connect: Callable[[], sqlite3.Connection],
    *,
    primary_message_id: object,
    source_message_ids: object,
    actor_id: object,
    conversation_ref: object,
) -> None:
    """Atomically bind joined source IDs to their primary inbound receipt."""

    primary, members = _members(primary_message_id, source_message_ids)
    actor = str(actor_id or "").strip()
    conversation = str(conversation_ref or "").strip()
    if not actor or not conversation:
        raise InboundRequestValidationError("qq_private_turn_identity_required")
    aliases = [member for member in members if member != primary]
    if not aliases:
        return
    now = utc_now()
    with connect() as conn:
        require_private_turn_member_schema(conn)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        for member in aliases:
            existing = conn.execute(
                f"""SELECT primary_message_id,actor_id,conversation_ref
                    FROM {PRIVATE_TURN_MEMBER_TABLE} WHERE platform_message_id=?""",
                (member,),
            ).fetchone()
            if existing:
                if tuple(existing) != (primary, actor, conversation):
                    raise InboundConflictError("qq_private_turn_member_conflict")
                continue
            conn.execute(
                f"""INSERT INTO {PRIVATE_TURN_MEMBER_TABLE}(
                       platform_message_id,primary_message_id,actor_id,conversation_ref,created_at
                   ) VALUES(?,?,?,?,?)""",
                (member, primary, actor, conversation, now),
            )


def replay_private_turn_member(
    connect: Callable[[], sqlite3.Connection],
    platform_message_id: object,
    actor_id: object,
    conversation_ref: object,
) -> dict | None:
    """Return a completed primary result for a joined source replay.

    ``None`` means this source was not a joined member. A member with no
    completed primary outcome is fail-closed rather than re-executed.
    """

    member = _message_id(platform_message_id, error="qq_message_id_invalid")
    actor = str(actor_id or "").strip()
    conversation = str(conversation_ref or "").strip()
    if not actor or not conversation:
        return None
    with connect() as conn:
        require_private_turn_member_schema(conn)
        alias = conn.execute(
            f"""SELECT primary_message_id FROM {PRIVATE_TURN_MEMBER_TABLE}
                WHERE platform_message_id=? AND actor_id=? AND conversation_ref=?""",
            (member, actor, conversation),
        ).fetchone()
        if not alias:
            return None
        primary = str(alias[0])
        receipt = conn.execute(
            """SELECT status,response_json FROM qq_inbound_receipts
               WHERE platform_message_id=? AND actor_id=? AND conversation_ref=?""",
            (primary, actor, conversation),
        ).fetchone()
        if not receipt or str(receipt[0]) == "processing":
            raise InboundProcessingError("qq_private_turn_processing")
        if str(receipt[0]) != "completed":
            raise InboundOutcomeUnknownError("qq_private_turn_outcome_unknown")
        try:
            result = json.loads(str(receipt[1] or ""))
        except (TypeError, ValueError) as exc:
            raise InboundOutcomeUnknownError("qq_private_turn_outcome_unknown") from exc
        if not isinstance(result, dict):
            raise InboundOutcomeUnknownError("qq_private_turn_outcome_unknown")
        return result


__all__ = [
    "PRIVATE_TURN_MEMBER_MIGRATION_CHECKSUM",
    "PRIVATE_TURN_MEMBER_MAX_SOURCES",
    "apply_private_turn_member_dedupe_v1",
    "inspect_private_turn_member_schema",
    "register_private_turn_member_aliases",
    "replay_private_turn_member",
    "require_private_turn_member_schema",
]
