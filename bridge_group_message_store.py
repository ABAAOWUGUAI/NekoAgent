#!/usr/bin/env python3
"""Persistence and retention boundary for QQ group conversation context."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from bridge_conversation_participation import participation_shadow_enabled
from bridge_group_context_frame import DEFAULT_GROUP_CONTEXT_LIMIT, normalize_group_context_limit


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def record_group_message(conn: sqlite3.Connection, payload: dict) -> dict:
    group_id = _clip(payload.get("group_id"), 80)
    source_content = _clip(payload.get("message") or payload.get("content"), 4000)
    if not group_id or not source_content:
        raise ValueError("group_message_required")
    now = _utc_now()
    if participation_shadow_enabled(conn):
        retention_class = _clip(payload.get("retention_class") or "conversation", 40)
        if retention_class not in {"metadata_only", "transient", "conversation", "governed"}:
            raise ValueError("group_message_retention_invalid")
        content = "" if retention_class == "metadata_only" else source_content
        conn.execute(
            """
            INSERT INTO group_messages(
                group_id,sender_id,sender_name,content,is_mention,
                decision,decision_reason,replied,created_at,
                external_message_id,content_sha256,content_length,
                retention_class,expires_at,body_redacted_at,
                engagement_decision_id,metadata_json
            ) VALUES(?,?,?,?,?,'','',0,?,?,?,?,?,?,?,?,?)
            """,
            (
                group_id,
                _clip(payload.get("sender_id"), 80),
                _clip(payload.get("sender_name"), 120),
                content,
                1 if _truthy(payload.get("is_mention")) else 0,
                now,
                _clip(payload.get("external_message_id"), 300),
                hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
                len(source_content),
                retention_class,
                _clip(payload.get("expires_at"), 80),
                now if retention_class == "metadata_only" else "",
                _clip(payload.get("engagement_decision_id"), 160),
                json.dumps(
                    dict(payload.get("metadata") or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO group_messages(
                group_id,sender_id,sender_name,content,is_mention,
                decision,decision_reason,replied,created_at
            ) VALUES(?,?,?,?,?,'','',0,?)
            """,
            (
                group_id,
                _clip(payload.get("sender_id"), 80),
                _clip(payload.get("sender_name"), 120),
                source_content,
                1 if _truthy(payload.get("is_mention")) else 0,
                now,
            ),
        )
    message_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        UPDATE group_policies
        SET session=CASE WHEN ?<>'' THEN ? ELSE session END,
            group_name=CASE WHEN ?<>'' THEN ? ELSE group_name END,
            message_count=message_count+1,updated_at=?
        WHERE group_id=?
        """,
        (
            _clip(payload.get("session"), 300),
            _clip(payload.get("session"), 300),
            _clip(payload.get("group_name"), 120),
            _clip(payload.get("group_name"), 120),
            now,
            group_id,
        ),
    )
    row = conn.execute("SELECT * FROM group_messages WHERE id=?", (message_id,)).fetchone()
    return dict(row)


def group_context(
    conn: sqlite3.Connection,
    group_id: str,
    limit: int = DEFAULT_GROUP_CONTEXT_LIMIT,
) -> list[dict]:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(group_messages)")}
    if "retention_class" in columns and participation_shadow_enabled(conn):
        now = _utc_now()
        conn.execute(
            """
            UPDATE group_messages SET content='',body_redacted_at=?
            WHERE retention_class='transient' AND content<>''
              AND expires_at<>'' AND expires_at<=?
            """,
            (now, now),
        )
        rows = conn.execute(
            """
            SELECT id,sender_id,sender_name,content,is_mention,replied,created_at,
                   retention_class,expires_at
            FROM group_messages
            WHERE group_id=? AND content<>'' AND retention_class<>'metadata_only'
              AND (expires_at='' OR expires_at>?)
            ORDER BY id DESC LIMIT ?
            """,
            (str(group_id or "").strip(), now, normalize_group_context_limit(limit)),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    rows = conn.execute(
        """
        SELECT id,sender_id,sender_name,content,is_mention,replied,created_at
        FROM group_messages WHERE group_id=? ORDER BY id DESC LIMIT ?
        """,
        (str(group_id or "").strip(), normalize_group_context_limit(limit)),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def group_recent_turn_metadata(
    conn: sqlite3.Connection,
    group_id: str,
    limit: int = 8,
) -> list[dict]:
    """Return chronological turn rhythm without retaining or exposing bodies.

    Retention may remove transient message bodies from ``group_context``.  That
    must not make older assistant messages look like the most recent turns for
    cooldown and turn-density guards, so rhythm intentionally reads only
    metadata from the real latest group rows.
    """

    safe_limit = max(1, min(int(limit or 8), 80))
    rows = conn.execute(
        """
        SELECT sender_id,is_mention,replied,created_at
        FROM group_messages
        WHERE group_id=?
        ORDER BY id DESC LIMIT ?
        """,
        (str(group_id or "").strip(), safe_limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


__all__ = ["group_context", "record_group_message"]
