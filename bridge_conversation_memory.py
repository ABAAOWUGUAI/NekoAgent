#!/usr/bin/env python3
"""Conversation Thread and scoped Memory service with legacy compatibility."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Mapping

from bridge_assistant_identity_schema import DEFAULT_OWNER_ACTOR_ID
from bridge_conversation_memory_schema import MEMORY_SCOPE_FEATURE_FLAG
from bridge_migrations import utc_after, utc_now


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _has_v2(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_records'",
        ).fetchone(),
    )


def memory_scope_feature_enabled(conn: sqlite3.Connection) -> bool:
    if not _has_v2(conn):
        return False
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (MEMORY_SCOPE_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def _active_assistant(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT id,owner_actor_id FROM assistant_instances
        WHERE status='active' ORDER BY created_at LIMIT 1
        """,
    ).fetchone()
    if not row:
        raise ValueError("active_assistant_missing")
    return str(row[0]), str(row[1])


def _channel_type(source: str, legacy_user_id: str) -> str:
    source = str(source or "").strip().lower()
    key = str(legacy_user_id or "").strip()
    if source in {"admin", "web", "web-console"} or key in {"admin", "web", "web-console"}:
        return "web"
    if source in {"qq_group", "group"} or key.startswith("group:"):
        return "qq_group"
    if source in {"qq", "qq_private", "private"} or key.isdigit():
        return "qq_private"
    return "legacy_unknown"


def _thread_ref(channel_type: str, legacy_user_id: str) -> str:
    key = str(legacy_user_id or "").strip()
    if channel_type == "web" and key in {"", "default", "admin", "web-console"}:
        return "owner-web"
    return key or "default"


def resolve_thread(
    conn: sqlite3.Connection,
    legacy_user_id: str,
    *,
    source: str = "",
    project_id: str | None = None,
) -> dict:
    """Resolve or create a server-owned Thread; clients never choose its owner."""

    assistant_id, owner_actor_id = _active_assistant(conn)
    channel_type = _channel_type(source, legacy_user_id)
    external_ref = _thread_ref(channel_type, legacy_user_id)
    row = conn.execute(
        """
        SELECT * FROM conversation_threads
        WHERE assistant_id=? AND channel_type=? AND external_thread_ref=?
        """,
        (assistant_id, channel_type, external_ref),
    ).fetchone()
    if row:
        columns = [str(item[0]) for item in conn.execute(
            "SELECT * FROM conversation_threads LIMIT 0",
        ).description or ()]
        return dict(zip(columns, tuple(row)))
    now = utc_now()
    thread_id = "thread-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO conversation_threads(
            id,owner_actor_id,assistant_id,channel_type,external_thread_ref,
            subject_actor_ref,project_id,status,legacy_user_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,'active',?,?,?)
        """,
        (
            thread_id, owner_actor_id, assistant_id, channel_type, external_ref,
            external_ref, project_id, str(legacy_user_id or "").strip(), now, now,
        ),
    )
    return _rows(
        conn.execute("SELECT * FROM conversation_threads WHERE id=?", (thread_id,)),
    )[0]


def _legacy_memory_public(row: sqlite3.Row | tuple, columns: tuple[str, ...]) -> dict:
    item = dict(zip(columns, tuple(row)))
    return {
        "id": item["id"],
        "user_id": item["user_id"],
        "kind": item["kind"],
        "content": item["content"],
        "source": item["source"],
        "score": item["score"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "last_used_at": item["last_used_at"],
        "scope_type": "legacy",
        "scope_label": "旧版兼容作用域",
        "sensitivity": "private",
        "status": "active",
    }


def _scope_label(item: Mapping[str, object]) -> str:
    labels = {
        "thread": "仅当前对话",
        "qq_group": "仅当前群聊",
        "project": "仅当前项目",
        "assistant_private": "仅当前助手",
        "owner_private": "本人私有",
        "global_preference": "本人全局偏好",
        "sensitive_private": "敏感·仅来源对话",
    }
    return labels.get(str(item.get("scope_type") or ""), "未知作用域")


def _memory_public(item: Mapping[str, object]) -> dict:
    return {
        "id": item["id"],
        "kind": item["kind"],
        "content": item["content"],
        "source": item["source"],
        "score": int(item.get("score") or 0),
        "scope_type": item["scope_type"],
        "scope_label": _scope_label(item),
        "sensitivity": item["sensitivity"],
        "consent_basis": item["consent_basis"],
        "status": item["status"],
        "expires_at": item.get("expires_at"),
        "last_used_at": item.get("last_used_at"),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def _keyword_set(text: str) -> set[str]:
    lowered = str(text or "").lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        words.add(chunk)
        for index in range(max(0, len(chunk) - 1)):
            words.add(chunk[index:index + 2])
    return words


def _rank_memories(records: list[dict], query: str, limit: int) -> list[dict]:
    query_words = _keyword_set(query)
    ranked: list[tuple[int, dict]] = []
    for item in records:
        overlap = len(query_words & _keyword_set(str(item.get("content") or "")))
        substring = 2 if query.strip() and query.strip() in str(item.get("content") or "") else 0
        always_relevant = item.get("kind") == "instruction" or (
            (
                item.get("scope_type") == "global_preference"
                or item.get("user_id") == "global"
            )
            and int(item.get("score") or 0) >= 9
        )
        if not always_relevant and overlap == 0 and substring == 0:
            continue
        ranked.append((overlap * 3 + substring + int(item.get("score") or 0), item))
    ranked.sort(
        key=lambda pair: (pair[0], str(pair[1].get("updated_at") or "")),
        reverse=True,
    )
    return [item for _, item in ranked[:limit]]


def record_conversation(
    conn: sqlite3.Connection,
    legacy_user_id: str,
    role: str,
    content: str,
    *,
    source: str = "",
    project_id: str | None = None,
) -> str | None:
    content = str(content or "").strip()
    if not content:
        return None
    legacy_key = str(legacy_user_id or "default").strip()
    created_at = utc_now()
    cursor = conn.execute(
        "INSERT INTO conversations(user_id,role,content,created_at) VALUES(?,?,?,?)",
        (legacy_key, str(role or ""), content[-6000:], created_at),
    )
    if not _has_v2(conn):
        return None
    thread = resolve_thread(conn, legacy_key, source=source, project_id=project_id)
    message_id = "message-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO conversation_messages(
            id,thread_id,role,content,source_type,legacy_conversation_id,created_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            message_id, thread["id"], str(role or ""), content[-6000:],
            thread["channel_type"], int(cursor.lastrowid), created_at,
        ),
    )
    conn.execute(
        "UPDATE conversation_threads SET updated_at=? WHERE id=?",
        (created_at, thread["id"]),
    )
    return message_id


def conversation_history(
    conn: sqlite3.Connection,
    legacy_user_id: str,
    *,
    source: str = "",
    limit: int = 20,
) -> list[dict]:
    limit = max(1, min(int(limit or 20), 30))
    if not memory_scope_feature_enabled(conn):
        return list(reversed(_rows(conn.execute(
            """
            SELECT role,content,created_at FROM conversations
            WHERE user_id=? ORDER BY id DESC LIMIT ?
            """,
            (str(legacy_user_id or "default").strip(), limit),
        ))))
    thread = resolve_thread(conn, legacy_user_id, source=source)
    rows = _rows(conn.execute(
        """
        SELECT role,content,created_at FROM conversation_messages
        WHERE thread_id=? ORDER BY created_at DESC,id DESC LIMIT ?
        """,
        (thread["id"], limit),
    ))
    return list(reversed(rows))


def add_memory(
    conn: sqlite3.Connection,
    legacy_user_id: str,
    content: str,
    *,
    kind: str = "fact",
    source: str = "manual",
    score: int = 5,
    request_source: str = "",
    scope_type: str = "",
    sensitivity: str = "private",
    consent_basis: str = "explicit",
    project_id: str | None = None,
) -> dict:
    legacy_key = str(legacy_user_id or "default").strip()
    content = " ".join(str(content or "").split())
    if not content:
        raise ValueError("memory_content_required")
    v2_enabled = _has_v2(conn)
    thread = None
    assistant_id = owner_actor_id = scope_id = ""
    if v2_enabled:
        thread = resolve_thread(
            conn, legacy_key, source=request_source or source, project_id=project_id,
        )
        assistant_id, owner_actor_id = _active_assistant(conn)
        channel_type = str(thread["channel_type"])
        allowed_scopes = {
            "thread", "qq_group", "project", "assistant_private",
            "owner_private", "global_preference", "sensitive_private",
        }
        if not scope_type:
            if sensitivity == "sensitive":
                scope_type = "sensitive_private"
            elif channel_type == "qq_group":
                scope_type = "qq_group"
            elif channel_type == "web":
                scope_type = "owner_private"
            else:
                scope_type = "thread"
        if scope_type not in allowed_scopes:
            raise ValueError("invalid_memory_scope")
        if channel_type == "qq_group" and scope_type != "qq_group":
            raise ValueError("group_memory_scope_must_match_group")
        if scope_type == "qq_group":
            scope_id = thread["external_thread_ref"]
        elif scope_type == "project":
            if not project_id:
                raise ValueError("project_scope_requires_project")
            scope_id = project_id
        elif scope_type in {"owner_private", "global_preference"}:
            scope_id = owner_actor_id
        elif scope_type == "assistant_private":
            scope_id = assistant_id
        else:
            scope_id = thread["id"]
    identity_seed = (
        f"{owner_actor_id}\0{assistant_id if scope_type != 'global_preference' else '*'}\0"
        f"{scope_type}\0{scope_id}\0" if v2_enabled else f"{legacy_key}\0"
    )
    digest = hashlib.sha1(
        f"{identity_seed}{kind}\0{content}".encode("utf-8"),
    ).hexdigest()[:12]
    legacy_id = f"mem_{digest}"
    now = utc_now()
    conn.execute(
        """
        INSERT INTO memories(
            id,user_id,kind,content,source,score,deleted,
            created_at,updated_at,last_used_at
        ) VALUES(?,?,?,?,?,?,0,?,?,NULL)
        ON CONFLICT(id) DO UPDATE SET
            deleted=0,score=max(memories.score,excluded.score),
            source=excluded.source,updated_at=excluded.updated_at
        """,
        (legacy_id, legacy_key, kind, content, source, int(score), now, now),
    )
    if not v2_enabled:
        columns = tuple(item[0] for item in conn.execute(
            "SELECT * FROM memories LIMIT 0",
        ).description or ())
        row = conn.execute("SELECT * FROM memories WHERE id=?", (legacy_id,)).fetchone()
        return _legacy_memory_public(row, columns)

    record_id = "memory-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO memory_records(
            id,owner_actor_id,assistant_id,subject_actor_ref,scope_type,scope_id,
            kind,content,source,score,sensitivity,consent_basis,source_thread_id,
            source_message_id,expires_at,last_used_at,status,legacy_memory_id,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,'active',?,?,?)
        ON CONFLICT(legacy_memory_id) DO UPDATE SET
            kind=excluded.kind,content=excluded.content,source=excluded.source,
            score=max(memory_records.score,excluded.score),
            sensitivity=excluded.sensitivity,consent_basis=excluded.consent_basis,
            status='active',updated_at=excluded.updated_at
        """,
        (
            record_id, owner_actor_id,
            None if scope_type == "global_preference" else assistant_id,
            thread["subject_actor_ref"], scope_type, scope_id, kind, content,
            source, int(score), sensitivity, consent_basis, thread["id"],
            legacy_id, now, now,
        ),
    )
    row = _rows(
        conn.execute("SELECT * FROM memory_records WHERE legacy_memory_id=?", (legacy_id,)),
    )[0]
    return _memory_public(row)


def _visible_clause(
    thread: Mapping[str, object],
    *,
    purpose: str,
    project_id: str | None,
    owner_bound: bool,
) -> tuple[str, list[object]]:
    channel = str(thread["channel_type"])
    if channel == "qq_group":
        return (
            "scope_type='qq_group' AND scope_id=?",
            [thread["external_thread_ref"]],
        )
    clauses = ["(scope_type='thread' AND scope_id=?)"]
    params: list[object] = [thread["id"]]
    if purpose != "proactive":
        clauses.append("(scope_type='sensitive_private' AND scope_id=?)")
        params.append(thread["id"])
    clauses.append("(scope_type='assistant_private' AND scope_id=?)")
    params.append(thread["assistant_id"])
    if channel == "web" or owner_bound:
        clauses.extend(
            (
                "(scope_type='owner_private' AND scope_id=?)",
                "(scope_type='global_preference' AND scope_id=?)",
            ),
        )
        params.extend([thread["owner_actor_id"], thread["owner_actor_id"]])
    if project_id:
        clauses.append("(scope_type='project' AND scope_id=?)")
        params.append(project_id)
    return "(" + " OR ".join(clauses) + ")", params


def list_memories(
    conn: sqlite3.Connection,
    legacy_user_id: str = "default",
    *,
    request_source: str = "",
    query: str = "",
    limit: int = 20,
    purpose: str = "chat",
    project_id: str | None = None,
    owner_management: bool = False,
    owner_bound: bool = False,
) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    if not memory_scope_feature_enabled(conn):
        params: list[object] = [str(legacy_user_id or "default").strip()]
        where = "deleted=0 AND user_id IN (?,'global')"
        if query:
            where += " AND source NOT LIKE 'smoke%'"
        params.append(80 if query else limit)
        cursor = conn.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        )
        columns = tuple(item[0] for item in cursor.description or ())
        records = [_legacy_memory_public(row, columns) for row in cursor.fetchall()]
        if query:
            selected = _rank_memories(records, query, limit)
            now = utc_now()
            conn.executemany(
                "UPDATE memories SET last_used_at=? WHERE id=?",
                [(now, item["id"]) for item in selected],
            )
            return selected
        return records

    assistant_id, owner_actor_id = _active_assistant(conn)
    params = [owner_actor_id]
    where = [
        "owner_actor_id=?",
        "status='active'",
        "(expires_at IS NULL OR expires_at='' OR expires_at>?)",
        "(assistant_id IS NULL OR assistant_id=?)",
    ]
    params.extend([utc_now(), assistant_id])
    if not owner_management:
        thread = resolve_thread(
            conn,
            legacy_user_id,
            source=request_source,
            project_id=project_id,
        )
        visibility, visibility_params = _visible_clause(
            thread,
            purpose=purpose,
            project_id=project_id,
            owner_bound=owner_bound,
        )
        where.append(visibility)
        params.extend(visibility_params)
    params.append(80 if query else limit)
    records = _rows(conn.execute(
        f"""
        SELECT * FROM memory_records
        WHERE {' AND '.join(where)}
        ORDER BY score DESC,updated_at DESC LIMIT ?
        """,
        tuple(params),
    ))
    if query:
        records = _rank_memories(records, query, limit)
    now = utc_now()
    if records and query:
        conn.executemany(
            "UPDATE memory_records SET last_used_at=? WHERE id=?",
            [(now, item["id"]) for item in records],
        )
        legacy_ids = [item["legacy_memory_id"] for item in records if item.get("legacy_memory_id")]
        conn.executemany(
            "UPDATE memories SET last_used_at=? WHERE id=?",
            [(now, item) for item in legacy_ids],
        )
    return [_memory_public(item) for item in records]


def delete_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    expected_updated_at: str = "",
) -> bool:
    memory_id = str(memory_id or "").strip()
    if not memory_id:
        return False
    if _has_v2(conn):
        row = conn.execute(
            """
            SELECT id,legacy_memory_id,updated_at FROM memory_records
            WHERE (id=? OR legacy_memory_id=?) AND owner_actor_id=?
            """,
            (memory_id, memory_id, DEFAULT_OWNER_ACTOR_ID),
        ).fetchone()
        if row:
            if expected_updated_at and str(row[2]) != expected_updated_at:
                raise ValueError("memory_version_conflict")
            now = utc_after(str(row[2]))
            conn.execute(
                "UPDATE memory_records SET status='deleted',updated_at=? WHERE id=?",
                (now, row[0]),
            )
            if row[1]:
                conn.execute(
                    "UPDATE memories SET deleted=1,updated_at=? WHERE id=?",
                    (now, row[1]),
                )
            return True
    cursor = conn.execute(
        "UPDATE memories SET deleted=1,updated_at=? WHERE id=?",
        (utc_now(), memory_id),
    )
    return cursor.rowcount > 0


def update_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    payload: Mapping[str, object],
) -> dict:
    if not memory_scope_feature_enabled(conn):
        raise ValueError("memory_scope_v2_disabled")
    expected = str(payload.get("expected_updated_at") or "").strip()
    if not expected:
        raise ValueError("memory_version_required")
    row = _rows(conn.execute(
        """
        SELECT * FROM memory_records
        WHERE id=? AND owner_actor_id=?
        """,
        (str(memory_id or "").strip(), DEFAULT_OWNER_ACTOR_ID),
    ))
    if not row:
        raise ValueError("memory_not_found")
    current = row[0]
    if str(current["updated_at"]) != expected:
        raise ValueError("memory_version_conflict")
    allowed = {"status", "expires_at", "sensitivity", "expected_updated_at"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unsupported_memory_fields:" + ",".join(unknown))
    status = str(payload.get("status") or current["status"])
    sensitivity = str(payload.get("sensitivity") or current["sensitivity"])
    if status not in {"active", "paused", "deleted"}:
        raise ValueError("invalid_memory_status")
    if sensitivity not in {"normal", "private", "sensitive"}:
        raise ValueError("invalid_memory_sensitivity")
    expires_at = (
        str(payload.get("expires_at") or "").strip()
        if "expires_at" in payload
        else current["expires_at"]
    )
    now = utc_after(str(current["updated_at"]))
    conn.execute(
        """
        UPDATE memory_records
        SET status=?,sensitivity=?,expires_at=?,updated_at=?
        WHERE id=?
        """,
        (status, sensitivity, expires_at or None, now, current["id"]),
    )
    if current.get("legacy_memory_id"):
        conn.execute(
            "UPDATE memories SET deleted=?,updated_at=? WHERE id=?",
            (1 if status == "deleted" else 0, now, current["legacy_memory_id"]),
        )
    return _memory_public(_rows(
        conn.execute("SELECT * FROM memory_records WHERE id=?", (current["id"],)),
    )[0])


def list_threads(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    assistant_id, owner_actor_id = _active_assistant(conn)
    records = _rows(conn.execute(
        """
        SELECT id,channel_type,project_id,status,created_at,updated_at
        FROM conversation_threads
        WHERE owner_actor_id=? AND assistant_id=?
        ORDER BY updated_at DESC LIMIT ?
        """,
        (owner_actor_id, assistant_id, max(1, min(int(limit or 50), 100))),
    ))
    for item in records:
        item["channel_label"] = {
            "web": "Web 私人对话",
            "qq_private": "QQ 私聊",
            "qq_group": "QQ 群聊",
            "legacy_unknown": "旧版待确认",
        }.get(str(item["channel_type"]), "未知")
    return records


def thread_messages(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    limit: int = 50,
) -> list[dict]:
    assistant_id, owner_actor_id = _active_assistant(conn)
    thread = conn.execute(
        """
        SELECT id FROM conversation_threads
        WHERE id=? AND assistant_id=? AND owner_actor_id=?
        """,
        (str(thread_id or "").strip(), assistant_id, owner_actor_id),
    ).fetchone()
    if not thread:
        raise ValueError("conversation_thread_not_found")
    return list(reversed(_rows(conn.execute(
        """
        SELECT role,content,source_type,created_at
        FROM conversation_messages WHERE thread_id=?
        ORDER BY created_at DESC,id DESC LIMIT ?
        """,
        (thread[0], max(1, min(int(limit or 50), 100))),
    ))))


def memory_scope_catalog() -> list[dict]:
    return [
        {"id": "owner_private", "label": "本人私有", "cross_channel": True},
        {"id": "thread", "label": "仅当前对话", "cross_channel": False},
        {"id": "qq_group", "label": "仅当前群聊", "cross_channel": False},
        {"id": "project", "label": "仅当前项目", "cross_channel": False},
        {"id": "assistant_private", "label": "仅当前助手", "cross_channel": True},
        {"id": "sensitive_private", "label": "敏感·仅来源对话", "cross_channel": False},
        {"id": "global_preference", "label": "本人全局偏好", "cross_channel": True},
    ]


def conversation_memory_shadow_compare(conn: sqlite3.Connection) -> dict:
    if not _has_v2(conn):
        return {"ok": False, "mismatches": ["schema"], "checked": 0}
    mismatches: list[str] = []
    legacy_conversations = int(conn.execute("SELECT count(*) FROM conversations").fetchone()[0])
    mapped_conversations = int(
        conn.execute(
            "SELECT count(*) FROM conversation_messages WHERE legacy_conversation_id IS NOT NULL",
        ).fetchone()[0],
    )
    if legacy_conversations != mapped_conversations:
        mismatches.append("conversation_count")
    legacy_memories = int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
    mapped_memories = int(
        conn.execute(
            "SELECT count(*) FROM memory_records WHERE legacy_memory_id IS NOT NULL",
        ).fetchone()[0],
    )
    if legacy_memories != mapped_memories:
        mismatches.append("memory_count")
    conversation_mismatch = int(conn.execute(
        """
        SELECT count(*) FROM conversations c
        LEFT JOIN conversation_messages m ON m.legacy_conversation_id=c.id
        WHERE m.id IS NULL OR m.role<>c.role OR m.content<>c.content OR m.created_at<>c.created_at
        """,
    ).fetchone()[0])
    if conversation_mismatch:
        mismatches.append("conversation_content")
    memory_mismatch = int(conn.execute(
        """
        SELECT count(*) FROM memories l
        LEFT JOIN memory_records m ON m.legacy_memory_id=l.id
        WHERE m.id IS NULL OR m.kind<>l.kind OR m.content<>l.content
           OR m.source<>l.source OR m.score<>l.score
           OR (m.status='deleted')<>l.deleted
        """,
    ).fetchone()[0])
    if memory_mismatch:
        mismatches.append("memory_content")
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "checked": legacy_conversations + legacy_memories,
        "legacy_conversations": legacy_conversations,
        "mapped_conversations": mapped_conversations,
        "legacy_memories": legacy_memories,
        "mapped_memories": mapped_memories,
        "feature_enabled": memory_scope_feature_enabled(conn),
    }


def conversation_memory_cutover_plan(conn: sqlite3.Connection) -> dict:
    shadow = conversation_memory_shadow_compare(conn)
    payload = {
        "feature_enabled": bool(shadow.get("feature_enabled")),
        "mismatches": shadow["mismatches"],
        "checked": shadow["checked"],
        "legacy_conversations": shadow.get("legacy_conversations", 0),
        "legacy_memories": shadow.get("legacy_memories", 0),
    }
    checksum = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    return {"ok": shadow["ok"], **payload, "plan_checksum": checksum}


def set_memory_scope_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    plan = conversation_memory_cutover_plan(conn)
    if expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_memory_scope_cutover_plan")
    if enabled and not plan["ok"]:
        raise ValueError("memory_scope_shadow_compare_failed")
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (MEMORY_SCOPE_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return conversation_memory_cutover_plan(conn)


__all__ = [
    "add_memory",
    "conversation_history",
    "conversation_memory_cutover_plan",
    "conversation_memory_shadow_compare",
    "delete_memory",
    "list_memories",
    "list_threads",
    "memory_scope_catalog",
    "memory_scope_feature_enabled",
    "record_conversation",
    "resolve_thread",
    "set_memory_scope_feature",
    "thread_messages",
    "update_memory",
]
