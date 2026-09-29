#!/usr/bin/env python3
"""Bounded, policy-aware product reads for retained QQ conversations.

This is deliberately a read model over the existing group message store.  It
does not create a second copy of QQ bodies, and it does not pretend that a
delivery acknowledgement proves the client rendered a message.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Callable


_MAX_LIMIT = 80
_MAX_SUMMARY_WINDOW_HOURS = 24 * 7


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit(value: object, *, default: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError("qq_conversation_limit_invalid") from exc
    if not 1 <= parsed <= _MAX_LIMIT:
        raise ValueError("qq_conversation_limit_invalid")
    return parsed


def _summary_window_hours(value: object) -> int:
    try:
        parsed = int(value if value is not None else _MAX_SUMMARY_WINDOW_HOURS)
    except (TypeError, ValueError) as exc:
        raise ValueError("qq_conversation_window_invalid") from exc
    if not 1 <= parsed <= _MAX_SUMMARY_WINDOW_HOURS:
        raise ValueError("qq_conversation_window_invalid")
    return parsed


def _window_start(value: str, hours: int) -> str:
    raw = str(value or "").strip().replace("Z", "+00:00")
    try:
        current = datetime.fromisoformat(raw)
    except ValueError:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - timedelta(hours=hours)).isoformat()


def _cursor_encode(timestamp: object, identifier: object) -> str:
    raw = json.dumps([str(timestamp or ""), str(identifier or "")], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_decode(value: object) -> tuple[str, str] | None:
    cursor = str(value or "").strip()
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        timestamp, identifier = json.loads(
            base64.urlsafe_b64decode((cursor + padding).encode("ascii")).decode("utf-8"),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("qq_conversation_cursor_invalid") from exc
    if not isinstance(timestamp, str) or not timestamp or not isinstance(identifier, str) or not identifier:
        raise ValueError("qq_conversation_cursor_invalid")
    return timestamp, identifier


def _row_dict(row: sqlite3.Row | tuple | None) -> dict:
    if row is None:
        return {}
    return dict(row) if isinstance(row, sqlite3.Row) else {}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _group_id(conversation_ref: object) -> str:
    reference = str(conversation_ref or "").strip()
    prefix, separator, group_id = reference.partition(":")
    if prefix != "group" or not separator or not group_id or len(group_id) > 80:
        raise ValueError("qq_conversation_not_available")
    return group_id


def _private_thread_id(conversation_ref: object) -> str:
    reference = str(conversation_ref or "").strip()
    prefix, separator, thread_id = reference.partition(":")
    if prefix != "private" or not separator or not thread_id or len(thread_id) > 160:
        raise ValueError("qq_conversation_not_available")
    return thread_id


def _require_private_read_contract(conn: sqlite3.Connection) -> None:
    """Validate every privacy/retention input before reading a private body.

    The admin surface is a projection over the existing Conversation store.  A
    partially migrated database must therefore fail closed rather than silently
    treating a missing retention or authorization column as permissive.
    """

    required = {
        "assistant_instances": {"id", "status", "updated_at"},
        "assistant_feature_flags": {"name", "enabled"},
        "qq_channel_settings": {
            "channel_id", "assistant_id", "channel_enabled", "access_mode",
            "private_chat_enabled",
        },
        "qq_access_entries": {"subject_type", "subject_id", "enabled", "remark"},
        "qq_identities": {"id", "qq_id", "display_name", "status"},
        "qq_role_assignments": {"identity_id", "role", "enabled"},
        "conversation_threads": {
            "id", "assistant_id", "channel_type", "external_thread_ref",
            "subject_actor_ref", "status", "updated_at",
        },
        "conversation_messages": {
            "id", "thread_id", "role", "content", "source_type", "created_at",
            "retention_class", "expires_at", "body_redacted_at",
            "delivery_projection_state",
        },
    }
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not set(required).issubset(tables):
        raise ValueError("qq_conversation_not_available")
    if any(not columns.issubset(_columns(conn, table)) for table, columns in required.items()):
        raise ValueError("qq_conversation_not_available")


def _active_private_assistant(conn: sqlite3.Connection) -> str:
    _require_private_read_contract(conn)
    feature = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name='qq_access_control_v2'",
    ).fetchone()
    assistant = conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
    ).fetchone()
    settings = conn.execute(
        """SELECT assistant_id,channel_enabled,access_mode,private_chat_enabled
           FROM qq_channel_settings WHERE channel_id='qq-main'""",
    ).fetchone()
    if (
        not feature or not int(feature[0] or 0) or not assistant or not settings
        or str(settings[0] or "") != str(assistant[0] or "")
        or not int(settings[1] or 0) or str(settings[2] or "") not in {"allowlist", "admin_only"}
        or not int(settings[3] or 0)
    ):
        raise ValueError("qq_private_read_disabled")
    return str(assistant[0])


def _private_authorization_where(subject_sql: str) -> str:
    """Read-only SQL equivalent of QQ private access authorization.

    The runtime access checker also updates ``last_seen_at``.  Admin reads must
    not create that side effect, so this predicate mirrors its two authorization
    branches with EXISTS clauses instead of invoking the mutating checker.
    """

    return f"""
      AND (
        EXISTS(
          SELECT 1
          FROM qq_identities auth_i
          JOIN qq_role_assignments auth_r ON auth_r.identity_id=auth_i.id
          WHERE auth_i.qq_id={subject_sql} AND auth_i.status='active'
            AND auth_r.enabled=1 AND auth_r.role IN ('super_admin','admin')
        )
        OR (
          (SELECT access_mode FROM qq_channel_settings WHERE channel_id='qq-main')='allowlist'
          AND EXISTS(
            SELECT 1 FROM qq_access_entries auth_a
            WHERE auth_a.subject_type='private_user'
              AND auth_a.subject_id={subject_sql} AND auth_a.enabled=1
          )
        )
      )
    """


def _private_label_sql(subject_sql: str) -> str:
    return f"""COALESCE(
        NULLIF((SELECT label_a.remark FROM qq_access_entries label_a
                WHERE label_a.subject_type='private_user'
                  AND label_a.subject_id={subject_sql} AND label_a.enabled=1
                LIMIT 1),''),
        NULLIF((SELECT label_i.display_name FROM qq_identities label_i
                WHERE label_i.qq_id={subject_sql} AND label_i.status='active'
                LIMIT 1),''),
        {subject_sql}
    )"""


def private_subject_read_allowed(conn: sqlite3.Connection, user_id: str) -> bool:
    """Return the current private-chat authorization without touching identity state."""

    subject = str(user_id or "").strip()
    if not subject:
        return False
    try:
        _active_private_assistant(conn)
    except (sqlite3.Error, ValueError):
        return False
    admin = conn.execute(
        """SELECT 1
           FROM qq_identities i
           JOIN qq_role_assignments r ON r.identity_id=i.id
           WHERE i.qq_id=? AND i.status='active' AND r.enabled=1
             AND r.role IN ('super_admin','admin') LIMIT 1""",
        (subject,),
    ).fetchone()
    if admin:
        return True
    mode = conn.execute(
        "SELECT access_mode FROM qq_channel_settings WHERE channel_id='qq-main'",
    ).fetchone()
    if not mode or str(mode[0] or "") != "allowlist":
        return False
    return bool(conn.execute(
        """SELECT 1 FROM qq_access_entries
           WHERE subject_type='private_user' AND subject_id=? AND enabled=1 LIMIT 1""",
        (subject,),
    ).fetchone())


def _available_private(conn: sqlite3.Connection, thread_id: str) -> dict:
    assistant_id = _active_private_assistant(conn)
    row = conn.execute(
        f"""SELECT t.id,t.external_thread_ref,t.subject_actor_ref,
                   {_private_label_sql('t.subject_actor_ref')} AS label
            FROM conversation_threads t
            WHERE t.id=? AND t.assistant_id=? AND t.channel_type='qq_private'
              AND t.status='active' AND t.external_thread_ref=t.subject_actor_ref
              {_private_authorization_where('t.subject_actor_ref')}
            LIMIT 1""",
        (thread_id, assistant_id),
    ).fetchone()
    item = _row_dict(row)
    if not item:
        raise ValueError("qq_conversation_not_available")
    return item


def _private_retention_where(*, now: str, alias: str = "m") -> tuple[str, list[object]]:
    return (
        f" AND {alias}.content<>''"
        f" AND {alias}.retention_class<>'metadata_only'"
        f" AND ({alias}.expires_at='' OR {alias}.expires_at>?)"
        f" AND {alias}.body_redacted_at=''"
        f" AND ({alias}.role<>'assistant' OR {alias}.delivery_projection_state IN ('not_applicable','channel_acked'))",
        [now],
    )


def _available_group(conn: sqlite3.Connection, group_id: str) -> dict:
    row = conn.execute(
        "SELECT group_id,group_name,participation_mode,enabled FROM group_policies WHERE group_id=?",
        (group_id,),
    ).fetchone()
    item = _row_dict(row)
    if not item or not bool(int(item.get("enabled") or 0)):
        raise ValueError("qq_conversation_not_available")
    return item


def _retention_where(conn: sqlite3.Connection, *, now: str, alias: str = "m") -> tuple[str, list[object]]:
    columns = _columns(conn, "group_messages")
    if {"retention_class", "expires_at"}.issubset(columns):
        return (
            f" AND {alias}.retention_class<>'metadata_only'"
            f" AND ({alias}.expires_at='' OR {alias}.expires_at>?)",
            [now],
        )
    return "", []


def _delivery_projection(delivery: dict) -> dict:
    if not delivery:
        return {"id": "", "status": "not_available", "client_projection": "unverified", "proof_ref": ""}
    certainty = str(delivery.get("delivery_certainty") or delivery.get("certainty") or "").strip()
    acknowledged = bool(str(delivery.get("acked_at") or "").strip()) or certainty == "confirmed"
    proof_ref = str(delivery.get("client_projection_proof_ref") or "").strip()
    projected = (
        str(delivery.get("client_projection_status") or "").strip() == "client_projected"
        and bool(proof_ref)
    )
    if projected:
        status, projection = "client_projected", "verified"
    elif acknowledged:
        status, projection = "application_ack", "unverified"
    else:
        status, projection = "delivery_pending", "unverified"
    return {
        "id": str(delivery.get("id") or ""),
        "status": status,
        "client_projection": projection,
        "proof_ref": proof_ref if projected else "",
    }


def _product_conversation_ref(value: object) -> str:
    reference = str(value or "").strip()
    if reference.startswith("qq:group:"):
        return "group:" + reference[len("qq:group:"):]
    if reference.startswith("qq:private:"):
        return "private:" + reference[len("qq:private:"):]
    return reference


class QqConversationReadService:
    """Read only authorized, retained QQ histories and evidence summaries."""

    def __init__(
        self,
        assistant_connect: Callable[[], sqlite3.Connection],
        *,
        delivery_reader: Callable[[int], list[dict]],
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self._assistant_connect = assistant_connect
        self._delivery_reader = delivery_reader
        self._now = now

    def index(self, *, kind: str, limit: int = 20, cursor: str = "") -> dict:
        if str(kind or "").strip() not in {"group", "private"}:
            raise ValueError("qq_conversation_kind_invalid")
        safe_limit = _limit(limit, default=20)
        if str(kind).strip() == "private":
            after = _cursor_decode(cursor)
            try:
                with self._assistant_connect() as conn:
                    assistant_id = _active_private_assistant(conn)
                    retention_sql, retention_params = _private_retention_where(now=self._now())
                    query = f"""
                        WITH authorized_subjects(subject_id) AS (
                            SELECT i.qq_id
                            FROM qq_identities i
                            JOIN qq_role_assignments r ON r.identity_id=i.id
                            WHERE i.status='active' AND r.enabled=1
                              AND r.role IN ('super_admin','admin')
                            UNION
                            SELECT a.subject_id
                            FROM qq_access_entries a
                            WHERE a.subject_type='private_user' AND a.enabled=1
                              AND (SELECT access_mode FROM qq_channel_settings
                                   WHERE channel_id='qq-main')='allowlist'
                        )
                        SELECT s.subject_id,
                               {_private_label_sql('s.subject_id')} AS label,
                               t.id AS thread_id,
                               MAX(m.created_at) AS latest_at,
                               COUNT(m.id) AS message_count,
                               COALESCE(MAX(m.created_at),t.updated_at,'1970-01-01T00:00:00+00:00') AS sort_at
                        FROM authorized_subjects s
                        LEFT JOIN conversation_threads t ON t.id=(
                            SELECT t2.id FROM conversation_threads t2
                            WHERE t2.assistant_id=? AND t2.channel_type='qq_private'
                              AND t2.status='active'
                              AND t2.external_thread_ref=s.subject_id
                              AND t2.subject_actor_ref=s.subject_id
                            ORDER BY t2.updated_at DESC,t2.id DESC LIMIT 1
                        )
                        LEFT JOIN conversation_messages m ON m.thread_id=t.id{retention_sql}
                        GROUP BY s.subject_id,label,t.id,t.updated_at
                    """
                    params: list[object] = [assistant_id, *retention_params]
                    if after:
                        query = f"SELECT * FROM ({query}) WHERE (sort_at < ? OR (sort_at = ? AND subject_id < ?))"
                        params.extend((after[0], after[0], after[1]))
                    query += " ORDER BY sort_at DESC,subject_id DESC LIMIT ?"
                    params.append(safe_limit + 1)
                    rows = [_row_dict(row) for row in conn.execute(query, tuple(params)).fetchall()]
            except ValueError as exc:
                if str(exc) == "qq_private_read_disabled":
                    return {"items": [], "next_cursor": "", "has_more": False}
                raise
            except sqlite3.Error as exc:
                raise ValueError("qq_conversation_not_available") from exc
            selected = rows[:safe_limit]
            next_cursor = ""
            if len(rows) > safe_limit and selected:
                last = selected[-1]
                next_cursor = _cursor_encode(last.get("sort_at"), last.get("subject_id"))
            return {
                "items": [{
                    "conversation_ref": f"private:{row['thread_id']}" if row.get("thread_id") else "",
                    "kind": "private",
                    "subject_id": str(row.get("subject_id") or ""),
                    "label": str(row.get("label") or row.get("subject_id") or "QQ 用户"),
                    "latest_at": str(row.get("latest_at") or ""),
                    "message_count": int(row.get("message_count") or 0),
                } for row in selected],
                "next_cursor": next_cursor,
                "has_more": bool(next_cursor),
            }
        after = _cursor_decode(cursor)
        try:
            with self._assistant_connect() as conn:
                now = self._now()
                retention_sql, retention_params = _retention_where(conn, now=now)
                query = f"""
                    SELECT p.group_id,p.group_name,p.participation_mode,
                           MAX(m.created_at) AS latest_at,COUNT(m.id) AS message_count
                    FROM group_policies p
                    JOIN group_messages m ON m.group_id=p.group_id
                      AND m.content<>''{retention_sql}
                    WHERE p.enabled=1
                    GROUP BY p.group_id,p.group_name,p.participation_mode
                """
                params: list[object] = list(retention_params)
                if after:
                    query = f"SELECT * FROM ({query}) WHERE (latest_at < ? OR (latest_at = ? AND group_id < ?))"
                    params.extend((after[0], after[0], after[1]))
                order_group_id = "group_id" if after else "p.group_id"
                query += f" ORDER BY latest_at DESC,{order_group_id} DESC LIMIT ?"
                params.append(safe_limit + 1)
                rows = [_row_dict(row) for row in conn.execute(query, tuple(params)).fetchall()]
        except ValueError:
            raise
        except sqlite3.Error as exc:
            raise ValueError("qq_conversation_read_failed") from exc
        selected = rows[:safe_limit]
        next_cursor = ""
        if len(rows) > safe_limit and selected:
            last = selected[-1]
            next_cursor = _cursor_encode(last.get("latest_at"), last.get("group_id"))
        return {
            "items": [{
                "conversation_ref": f"group:{row['group_id']}",
                "kind": "group",
                "label": str(row.get("group_name") or row["group_id"]),
                "participation_mode": str(row.get("participation_mode") or ""),
                "latest_at": str(row.get("latest_at") or ""),
                "message_count": int(row.get("message_count") or 0),
            } for row in selected],
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
        }

    def timeline(self, conversation_ref: str, *, limit: int = 40, cursor: str = "") -> dict:
        if str(conversation_ref or "").strip().startswith("private:"):
            thread_id = _private_thread_id(conversation_ref)
            safe_limit = _limit(limit, default=40)
            after = _cursor_decode(cursor)
            try:
                with self._assistant_connect() as conn:
                    thread = _available_private(conn, thread_id)
                    retention_sql, retention_params = _private_retention_where(now=self._now())
                    clauses = ["m.thread_id=?"]
                    params: list[object] = [thread_id, *retention_params]
                    if retention_sql:
                        clauses.append(retention_sql.removeprefix(" AND "))
                    if after:
                        clauses.append("(m.created_at < ? OR (m.created_at = ? AND m.id < ?))")
                        params.extend((after[0], after[0], after[1]))
                    params.append(safe_limit + 1)
                    rows = [_row_dict(row) for row in conn.execute(
                        f"""SELECT m.id,m.role,m.content,m.created_at
                            FROM conversation_messages m
                            WHERE {' AND '.join(clauses)}
                            ORDER BY m.created_at DESC,m.id DESC LIMIT ?""",
                        tuple(params),
                    ).fetchall()]
            except ValueError as exc:
                if str(exc) == "qq_private_read_disabled":
                    raise ValueError("qq_conversation_not_available") from exc
                raise
            except sqlite3.Error as exc:
                raise ValueError("qq_conversation_not_available") from exc
            selected = rows[:safe_limit]
            next_cursor = ""
            if len(rows) > safe_limit and selected:
                last = selected[-1]
                next_cursor = _cursor_encode(last.get("created_at"), last.get("id"))
            subject_id = str(thread.get("subject_actor_ref") or "")
            return {
                "items": [{
                    "message_ref": f"private-message:{row['id']}",
                    "event_ref": f"private-message:{row['id']}",
                    "conversation_ref": f"private:{thread_id}",
                    "channel_type": "qq_private",
                    "actor_ref": "" if str(row.get("role") or "") == "assistant" else subject_id,
                    "actor_label": "助手" if str(row.get("role") or "") == "assistant" else str(thread.get("label") or subject_id or "QQ 用户"),
                    "actor_kind": "assistant" if str(row.get("role") or "") == "assistant" else "member",
                    "content": str(row.get("content") or ""),
                    "is_mention": False,
                    "assistant_replied": False,
                    "created_at": str(row.get("created_at") or ""),
                } for row in selected],
                "next_cursor": next_cursor,
                "has_more": bool(next_cursor),
            }
        group_id = _group_id(conversation_ref)
        safe_limit = _limit(limit, default=40)
        after = _cursor_decode(cursor)
        try:
            with self._assistant_connect() as conn:
                _available_group(conn, group_id)
                retention_sql, retention_params = _retention_where(conn, now=self._now())
                clauses = ["m.group_id=?", "m.content<>''"]
                params: list[object] = [group_id, *retention_params]
                if retention_sql:
                    clauses.append(retention_sql.removeprefix(" AND "))
                if after:
                    clauses.append("(m.created_at < ? OR (m.created_at = ? AND m.id < ?))")
                    params.extend((after[0], after[0], after[1]))
                params.append(safe_limit + 1)
                rows = [_row_dict(row) for row in conn.execute(
                    f"""
                    SELECT m.id,m.sender_id,m.sender_name,m.content,m.is_mention,m.replied,m.created_at,
                           m.engagement_decision_id
                    FROM group_messages m WHERE {' AND '.join(clauses)}
                    ORDER BY m.created_at DESC,m.id DESC LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()]
        except ValueError:
            raise
        except sqlite3.Error as exc:
            raise ValueError("qq_conversation_read_failed") from exc
        selected = rows[:safe_limit]
        next_cursor = ""
        if len(rows) > safe_limit and selected:
            last = selected[-1]
            next_cursor = _cursor_encode(last.get("created_at"), last.get("id"))
        return {
            # The product conversation surface is an activity feed: newest
            # retained event first.  Pagination uses the last (oldest) item as
            # its cursor, so callers append later pages rather than reversing
            # either page in the UI.
            "items": [{
                # A decision can describe more than one message. UI identity
                # is the immutable message row, not the diagnostic decision.
                "message_ref": f"group-message:{row['id']}",
                "event_ref": (
                    f"decision:{row['engagement_decision_id']}"
                    if str(row.get("engagement_decision_id") or "")
                    else f"group-message:{row['id']}"
                ),
                "conversation_ref": f"group:{group_id}",
                "channel_type": "qq_group",
                "actor_ref": str(row.get("sender_id") or ""),
                "actor_label": str(row.get("sender_name") or row.get("sender_id") or "未知成员"),
                "actor_kind": "assistant" if str(row.get("sender_id") or "") == "bot" else "member",
                "content": str(row.get("content") or ""),
                "is_mention": bool(int(row.get("is_mention") or 0)),
                "assistant_replied": bool(int(row.get("replied") or 0)),
                "created_at": str(row.get("created_at") or ""),
            } for row in selected],
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
        }

    def summary(self, conversation_ref: str, *, window_hours: int = _MAX_SUMMARY_WINDOW_HOURS) -> dict:
        """Return a body-free participation and delivery summary for one group."""

        group_id = _group_id(conversation_ref)
        safe_hours = _summary_window_hours(window_hours)
        try:
            with self._assistant_connect() as conn:
                _available_group(conn, group_id)
                now = self._now()
                since = _window_start(now, safe_hours)
                retention_sql, retention_params = _retention_where(conn, now=now)
                clauses = ["m.group_id=?", "m.content<>''", "m.created_at>=?"]
                params: list[object] = [group_id, since, *retention_params]
                if retention_sql:
                    clauses.append(retention_sql.removeprefix(" AND "))
                message_row = _row_dict(conn.execute(
                    f"""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN m.sender_id='bot' THEN 1 ELSE 0 END) AS assistant,
                           SUM(CASE WHEN m.sender_id<>'bot' THEN 1 ELSE 0 END) AS member,
                           SUM(CASE WHEN m.is_mention=1 THEN 1 ELSE 0 END) AS mentions
                    FROM group_messages m WHERE {' AND '.join(clauses)}
                    """,
                    tuple(params),
                ).fetchone())
                quality = {
                    "replied": 0, "rewritten": 0, "silent_policy": 0, "blocked_truth": 0,
                }
                delivery = {
                    "application_ack": 0, "client_projected": 0, "pending": 0,
                }
                if _columns(conn, "qq_quality_receipts"):
                    rows = conn.execute(
                        """
                        SELECT outcome,persona_verdict,rewrite_or_block_code,delivery_status
                        FROM qq_quality_receipts
                        WHERE conversation_ref=? AND created_at>=?
                        """,
                        (f"group:{group_id}", since),
                    ).fetchall()
                    for row in rows:
                        item = _row_dict(row)
                        outcome = str(item.get("outcome") or "")
                        if outcome == "replied":
                            quality["replied"] += 1
                            if str(item.get("persona_verdict") or "") == "rewritten_passed" or str(item.get("rewrite_or_block_code") or ""):
                                quality["rewritten"] += 1
                        elif outcome == "silent":
                            quality["silent_policy"] += 1
                        elif outcome == "blocked":
                            quality["blocked_truth"] += 1
                        delivery_state = str(item.get("delivery_status") or "")
                        if delivery_state == "application_ack":
                            delivery["application_ack"] += 1
                        elif delivery_state == "client_projected":
                            delivery["client_projected"] += 1
                        elif delivery_state == "delivery_pending":
                            delivery["pending"] += 1
        except ValueError:
            raise
        except sqlite3.Error as exc:
            raise ValueError("qq_conversation_read_failed") from exc
        return {
            "conversation_ref": f"group:{group_id}",
            "window_hours": safe_hours,
            "messages": {
                "total": int(message_row.get("total") or 0),
                "assistant": int(message_row.get("assistant") or 0),
                "member": int(message_row.get("member") or 0),
                "mentions": int(message_row.get("mentions") or 0),
            },
            "quality": quality,
            "delivery": delivery,
        }

    def event_inspector(self, event_ref: str) -> dict:
        requested_ref = str(event_ref or "").strip()
        if not requested_ref or len(requested_ref) > 180:
            raise ValueError("qq_event_not_available")
        try:
            with self._assistant_connect() as conn:
                decision = {}
                event_ref_out = requested_ref
                if requested_ref.startswith("group-message:"):
                    message_id = requested_ref[len("group-message:"):].strip()
                    if not message_id.isdecimal():
                        raise ValueError("qq_event_not_available")
                    message = _row_dict(conn.execute(
                        "SELECT id,group_id,engagement_decision_id,created_at FROM group_messages WHERE id=?",
                        (int(message_id),),
                    ).fetchone())
                    if not message:
                        raise ValueError("qq_event_not_available")
                    group_id = str(message.get("group_id") or "")
                    _available_group(conn, group_id)
                    event = {
                        "id": requested_ref,
                        "channel_type": "qq_group",
                        "external_thread_ref": f"group:{group_id}",
                        "created_at": str(message.get("created_at") or ""),
                    }
                    decision_id = str(message.get("engagement_decision_id") or "")
                    if decision_id:
                        decision = _row_dict(conn.execute(
                            "SELECT id,event_id,action,reason_code FROM engagement_decisions WHERE id=?",
                            (decision_id,),
                        ).fetchone())
                elif requested_ref.startswith("quality:"):
                    receipt_id = requested_ref[len("quality:"):].strip()
                    receipt = _row_dict(conn.execute(
                        "SELECT decision_id FROM qq_quality_receipts WHERE id=?",
                        (receipt_id,),
                    ).fetchone())
                    decision_id = str(receipt.get("decision_id") or "")
                    if not decision_id:
                        raise ValueError("qq_event_not_available")
                    decision = _row_dict(conn.execute(
                        "SELECT id,event_id,action,reason_code FROM engagement_decisions WHERE id=?",
                        (decision_id,),
                    ).fetchone())
                    event_id = str(decision.get("event_id") or "")
                    event_ref_out = event_id
                    row = conn.execute(
                        "SELECT id,channel_type,external_thread_ref,created_at FROM conversation_events WHERE id=?",
                        (event_id,),
                    ).fetchone()
                    event = _row_dict(row)
                elif requested_ref.startswith("decision:"):
                    decision_id = requested_ref[len("decision:"):].strip()
                    decision = _row_dict(conn.execute(
                        "SELECT id,event_id,action,reason_code FROM engagement_decisions WHERE id=?",
                        (decision_id,),
                    ).fetchone())
                    event_id = str(decision.get("event_id") or "")
                    event_ref_out = event_id
                    row = conn.execute(
                        "SELECT id,channel_type,external_thread_ref,created_at FROM conversation_events WHERE id=?",
                        (event_id,),
                    ).fetchone()
                    event = _row_dict(row)
                else:
                    event_id = requested_ref
                    row = conn.execute(
                        "SELECT id,channel_type,external_thread_ref,created_at FROM conversation_events WHERE id=?",
                        (event_id,),
                    ).fetchone()
                    event = _row_dict(row)
                if not event or str(event.get("channel_type") or "") not in {"qq_group", "qq_private"}:
                    raise ValueError("qq_event_not_available")
                if str(event.get("channel_type") or "") != "qq_group":
                    raise ValueError("qq_conversation_not_available")
                _available_group(conn, _group_id(_product_conversation_ref(event.get("external_thread_ref"))))
                if not decision:
                    decision = _row_dict(conn.execute(
                        "SELECT id,event_id,action,reason_code FROM engagement_decisions WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
                        (str(event.get("id") or ""),),
                    ).fetchone())
                quality = None
                if decision:
                    try:
                        from bridge_qq_quality_receipt import quality_receipt_for_decision

                        quality = quality_receipt_for_decision(
                            conn,
                            decision_id=str(decision.get("id") or ""),
                        )
                    except sqlite3.Error:
                        quality = None
        except ValueError:
            raise
        except sqlite3.Error as exc:
            raise ValueError("qq_conversation_read_failed") from exc
        deliveries = []
        if decision:
            try:
                deliveries = [
                    item for item in self._delivery_reader(_MAX_LIMIT)
                    if str(item.get("channel") or "") == "qq"
                    and str(item.get("engagement_decision_id") or "") == str(decision.get("id") or "")
                ]
            except Exception as exc:
                raise ValueError("qq_delivery_read_failed") from exc
        deliveries.sort(key=lambda item: str(item.get("updated_at") or item.get("acked_at") or ""), reverse=True)
        return {
            "event_ref": event_ref_out,
            "conversation_ref": _product_conversation_ref(event.get("external_thread_ref")),
            "channel_type": str(event.get("channel_type") or ""),
            "created_at": str(event.get("created_at") or ""),
            "decision": {
                "id": str(decision.get("id") or ""),
                "action": str(decision.get("action") or ""),
                "reason_code": str(decision.get("reason_code") or "not_recorded"),
            },
            "delivery": _delivery_projection(deliveries[0] if deliveries else {}),
            "quality": quality or {"status": "not_recorded"},
        }


__all__ = ["QqConversationReadService"]
