#!/usr/bin/env python3
"""Conversation Thread and scoped Memory service with legacy compatibility."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import uuid
from typing import Mapping

from bridge_assistant_identity_schema import DEFAULT_OWNER_ACTOR_ID
from bridge_conversation_memory_schema import MEMORY_SCOPE_FEATURE_FLAG
from bridge_migrations import utc_after, utc_now
from bridge_inbound_context import current_inbound_exchange_context
from bridge_group_context_frame import canonical_group_reply_message_id, group_source_message_id


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _has_v2(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_records'",
        ).fetchone(),
    )


def _has_continuous_private_fields(conn: sqlite3.Connection) -> bool:
    if not _has_v2(conn):
        return False
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(conversation_messages)")
    }
    return {
        "inbound_sequence",
        "response_cycle_id",
        "logical_response_id",
        "delivery_id",
        "delivery_projection_state",
    }.issubset(columns)


def _continuous_private_enabled(conn: sqlite3.Connection) -> bool:
    if not _has_continuous_private_fields(conn):
        return False
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name='continuous_private_conversation_v1'",
    ).fetchone()
    return bool(row and int(row[0]))


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


def build_group_memory_retrieval_context(
    current: dict, history: list[dict], decision_messages: list[dict],
) -> dict:
    """Project only human turns actually rendered in the bounded prompt.

    Same actor / recent timestamp is not a topic edge. Only explicit reply
    edges may expand the query. External IDs are resolved inside the group DB.
    """
    try:
        metadata = json.loads(str(current.get("metadata_json") or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    reply = str(current.get("reply_to_external_message_id")
                or metadata.get("reply_to_external_message_id") or "").strip()
    visible = []
    for item in history[-100:]:
        actor = str(item.get("sender_id") or "")
        body = str(item.get("content") or "").strip()
        if not actor or actor == "bot" or item.get("role") == "assistant" or not body:
            continue
        marker = f"[消息#{item.get('id')}]"
        if any(
            p.get("role") == "user"
            and marker in str(p.get("content") or "").partition("] ")[0] + "]"
            and str(p.get("content") or "").partition("] ")[2].startswith(body)
            for p in decision_messages
        ):
            visible.append({"external_message_id": group_source_message_id(item),
                            "sender_id": actor, "content": body})
    return {"current_actor_ref": str(current.get("sender_id") or ""),
            "reply_to_external_message_id": reply, "visible_messages": visible}


def _group_context_memories(
    conn: sqlite3.Connection, thread: dict, where: list[str], params: list,
    query: str, context: Mapping[str, object], limit: int,
) -> list[dict]:
    """Internal evidence projection; no model call or memory-content writes."""
    actor = str(context.get("current_actor_ref") or "").strip()[:300]
    reply = canonical_group_reply_message_id(context.get("reply_to_external_message_id"))
    # Resolve only explicit, same-thread human references; never trust body text
    # or an inferred frame topic_root_id as an identity/source join.
    refs: list[str] = []
    topics: list[str] = []
    for _ in range(3):
        if not reply:
            break
        aliases = [reply, "napcat:" + reply] if re.fullmatch(r"-?\d+", reply) else [reply]
        matches = conn.execute(
            "SELECT id,content,reply_to_external_message_id FROM conversation_messages "
            "WHERE thread_id=? AND role='user' AND trim(content)<>'' AND external_message_id IN ("
            + ",".join("?" for _ in aliases) + ") LIMIT 2", (thread["id"], *aliases),
        ).fetchall()
        row = matches[0] if len(matches) == 1 else None
        if not row or row[0] in refs:
            break
        refs.append(str(row[0]))
        topics.append(str(row[1] or ""))
        reply = canonical_group_reply_message_id(row[2])
    # Small generic-word guard, not a growing intent classifier. Subject and
    # source constraints do most of the work; score cannot bypass relevance.
    generic = {"不是", "这个", "那个", "自己", "什么", "怎么", "就是", "没有",
               "这样", "那样", "我们", "你们", "他们", "现在", "之前", "还是"}
    words = sorted(_keyword_set(str(query)[:400] + " " + " ".join(topics)[:400]) - generic,
                   key=lambda word: (-len(word), word))[:48]
    topic_sql = " OR ".join("instr(lower(m.content),?)>0" for _ in words) or "0"
    ref_sql = "m.source_message_id IN (" + ",".join("?" for _ in refs) + ")" if refs else "0"
    cursor = conn.execute(
        f"""SELECT m.*,s.content AS evidence_content,s.external_message_id AS evidence_external_id
        FROM (SELECT * FROM memory_records WHERE {' AND '.join(where)}) AS m
        JOIN conversation_messages AS s ON s.id=m.source_message_id
        WHERE s.thread_id=? AND m.source_thread_id=? AND s.role='user' AND trim(s.content)<>''
          AND s.actor_ref=m.subject_actor_ref AND m.subject_actor_ref<>''
          AND m.sensitivity<>'sensitive'
          AND ((m.subject_actor_ref=? AND ?<>'' AND ({topic_sql})) OR ({ref_sql}))
        ORDER BY CASE WHEN ({ref_sql}) THEN 1 ELSE 0 END DESC,m.score DESC,m.updated_at DESC,m.id ASC""",
        (*params, thread["id"], thread["id"], actor, actor, *words, *refs, *refs),
    )
    columns = [c[0] for c in cursor.description]
    visible = list(context.get("visible_messages") or [])[:100]
    result: list[dict] = []
    budget = 600  # UTF-8 byte ceiling, deliberately tighter than 600 model tokens.
    for row in cursor:
        item = dict(zip(columns, row))
        source_body = " ".join(str(item["evidence_content"] or "").split())
        fact = " ".join(str(item["content"] or "").split())
        # A source ID alone does not prove a multi-source summary is covered.
        covered = source_body and fact in source_body and any(
            isinstance(v, dict)
            and canonical_group_reply_message_id(v.get("external_message_id"))
                == canonical_group_reply_message_id(item["evidence_external_id"])
            and str(v.get("sender_id") or "") == item["subject_actor_ref"]
            and source_body in " ".join(str(v.get("content") or "").split())
            for v in visible
        )
        if covered:
            continue
        # Use the same pseudonymous member marker as the existing group prompt.
        group_id = str(thread["external_thread_ref"]).removeprefix("group:")
        digest = hashlib.sha256(f"{group_id}\0{item['subject_actor_ref']}".encode()).hexdigest()[:12]
        content = f"[历史事实，成员#{digest}，非当前状态] {fact}"
        cost = len(("- " + content + "\n").encode("utf-8"))
        if not fact or cost > budget:
            continue  # Do not truncate a fact and change its meaning.
        public = _memory_public(item)
        public["content"] = content
        public["retrieval_evidence"] = {
            "subject_actor_ref": item["subject_actor_ref"],
            "source_message_id": item["source_message_id"],
            "matched_by": "explicit_source" if item["source_message_id"] in refs else "actor_topic",
        }
        now = utc_now()
        conn.execute("UPDATE memory_records SET last_used_at=? WHERE id=?", (now, item["id"]))
        if item.get("legacy_memory_id"):
            conn.execute("UPDATE memories SET last_used_at=? WHERE id=?", (now, item["legacy_memory_id"]))
        result.append(public)
        budget -= cost
        if len(result) >= min(limit, 3):
            break
    # Source-verified automatic QQ memories keep only their short evidence
    # snippets after the ordinary chat body expires. The evidence table is
    # populated only while the original authorized source is still readable.
    if len(result) < min(limit, 3) and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_memory_evidence'"
    ).fetchone():
        auto_rows = conn.execute(
            """SELECT m.*,e.topic_key,e.snippet,e.source_key,
                      count(DISTINCT e.actor_ref) AS witness_count
               FROM memory_records m JOIN scoped_memory_evidence e ON e.memory_id=m.id
               WHERE m.owner_actor_id=? AND m.assistant_id=? AND m.status='active'
                 AND (m.expires_at IS NULL OR m.expires_at='' OR m.expires_at>?)
                 AND m.scope_type='qq_group' AND m.scope_id=?
                 AND m.source='qq_auto_source_verified' AND m.sensitivity<>'sensitive'
                 AND (m.subject_actor_ref=? OR m.subject_actor_ref='')
               GROUP BY m.id ORDER BY m.score DESC,m.updated_at DESC LIMIT 40""",
            (thread["owner_actor_id"], thread["assistant_id"], utc_now(),
             thread["external_thread_ref"], actor),
        ).fetchall()
        query_words = _keyword_set(str(query)[:400] + " " + " ".join(topics)[:400]) - generic
        for row in auto_rows:
            item = dict(row)
            if not item["subject_actor_ref"] and int(item["witness_count"]) < 2:
                continue
            fact = str(item["content"] or "").strip()
            if not query_words.intersection(_keyword_set(fact + " " + str(item["topic_key"]))):
                continue
            label = "历史群聊，至少两名成员确认的说法，非任务状态" if not item["subject_actor_ref"] else "历史成员自述，非外部核实事实"
            content = f"[{label}] {fact}"
            cost = len(("- " + content + "\n").encode("utf-8"))
            if not fact or cost > budget:
                continue
            public = _memory_public(item)
            public["content"] = content
            public["retrieval_evidence"] = {"source_key": item["source_key"],
                "witness_count": int(item["witness_count"]), "matched_by": "scoped_topic"}
            conn.execute("UPDATE memory_records SET last_used_at=? WHERE id=?", (utc_now(),item["id"]))
            result.append(public)
            budget -= cost
            if len(result) >= min(limit, 3):
                break
    # Owner-added group notes are a separate provenance, never represented as
    # corroborated member statements or task-system completion facts.
    if len(result) < min(limit, 3):
        owner_rows = conn.execute("""SELECT m.* FROM memory_records m WHERE
          m.owner_actor_id=? AND m.assistant_id=? AND m.status='active'
          AND (m.expires_at IS NULL OR m.expires_at='' OR m.expires_at>?)
          AND m.scope_type='qq_group' AND m.scope_id=?
          AND m.source IN ('owner_added','owner_correction')
          ORDER BY m.score DESC,m.updated_at DESC LIMIT 20""",
          (thread["owner_actor_id"],thread["assistant_id"],utc_now(),thread["external_thread_ref"])).fetchall()
        query_words = _keyword_set(str(query)[:400] + " " + " ".join(topics)[:400]) - generic
        for row in owner_rows:
            item = dict(row)
            fact = str(item["content"] or "")
            if not query_words.intersection(_keyword_set(fact)):
                continue
            content = f"[主人添加的本群记忆，非任务状态] {fact}"
            cost = len(("- " + content + "\n").encode("utf-8"))
            if cost > budget:
                continue
            public = _memory_public(item)
            public["content"] = content
            result.append(public)
            budget -= cost
            if len(result) >= min(limit, 3):
                break
    return result


def record_conversation(
    conn: sqlite3.Connection,
    legacy_user_id: str,
    role: str,
    content: str,
    *,
    source: str = "",
    project_id: str | None = None,
    external_message_id: str = "",
    reply_to_external_message_id: str = "",
    directed_to_assistant: bool = False,
    message_kind: str = "text",
    source_type_override: str = "",
    metadata: Mapping[str, object] | None = None,
    thread_id: str | None = None,
    actor_ref: str = "",
) -> str | None:
    content = str(content or "").strip()
    if not content:
        return None
    legacy_key = str(legacy_user_id or "default").strip()
    inbound = current_inbound_exchange_context()
    external_message_id = external_message_id or str(inbound.get("_external_message_id") or "")
    actor_ref = str(
        actor_ref
        or inbound.get("sender_id")
        or inbound.get("_qq_actor_id")
        or inbound.get("user_id")
        or ""
    ).strip()[:300]
    message_kind = message_kind if message_kind != "text" else str(inbound.get("message_kind") or "text")
    source_type_override = source_type_override or str(inbound.get("source_type_override") or "")
    created_at = utc_now()
    v2_enabled = _has_v2(conn)
    thread = None
    if v2_enabled:
        resolved_thread_id = str(thread_id or "").strip()
        if resolved_thread_id:
            row = conn.execute(
                "SELECT * FROM conversation_threads WHERE id=?",
                (resolved_thread_id,),
            ).fetchone()
            if row is None:
                raise ValueError("conversation_thread_missing")
            thread = dict(zip([str(item[0]) for item in conn.execute(
                "SELECT * FROM conversation_threads LIMIT 0",
            ).description or ()], tuple(row)))
        else:
            thread = resolve_thread(
                conn, legacy_key, source=source, project_id=project_id,
            )
    continuous_fields = _has_continuous_private_fields(conn)
    response_cycle_id = str(inbound.get("_response_cycle_id") or "").strip()
    if thread and str(role or "") == "user" and str(external_message_id or "").strip():
        existing = conn.execute(
            """
            SELECT id FROM conversation_messages
            WHERE thread_id=? AND role='user' AND external_message_id=?
            ORDER BY created_at,id LIMIT 1
            """,
            (thread["id"], str(external_message_id)[:300]),
        ).fetchone()
        if existing:
            if actor_ref and "actor_ref" in {
                str(row[1]) for row in conn.execute("PRAGMA table_info(conversation_messages)")
            }:
                conn.execute(
                    "UPDATE conversation_messages SET actor_ref=? WHERE id=? AND actor_ref=''",
                    (actor_ref, str(existing[0])),
                )
            return str(existing[0])
    if thread and continuous_fields and str(role or "") == "assistant" and response_cycle_id:
        existing = conn.execute(
            """
            SELECT id FROM conversation_messages
            WHERE thread_id=? AND role='assistant' AND response_cycle_id=?
            ORDER BY created_at,id LIMIT 1
            """,
            (thread["id"], response_cycle_id),
        ).fetchone()
        if existing:
            return str(existing[0])
    cursor = conn.execute(
        "INSERT INTO conversations(user_id,role,content,created_at) VALUES(?,?,?,?)",
        (legacy_key, str(role or ""), content[-6000:], created_at),
    )
    if not v2_enabled:
        return None
    source_type = str(source_type_override or "").strip()
    if source_type and source_type not in {"qq_voice_transcript"}:
        raise ValueError("conversation_source_type_override_invalid")
    source_type = source_type or str(thread["channel_type"])
    message_id = "message-" + uuid.uuid4().hex
    values = (
        message_id, thread["id"], str(role or ""), content[-6000:],
        source_type, int(cursor.lastrowid), created_at,
        str(external_message_id or "")[:300],
        str(reply_to_external_message_id or "")[:300],
        1 if directed_to_assistant else 0,
        str(message_kind or "text")[:40],
        json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:4000],
    )
    if continuous_fields:
        conn.execute(
            """
            INSERT INTO conversation_messages(
                id,thread_id,role,content,source_type,legacy_conversation_id,created_at,
                external_message_id,reply_to_external_message_id,directed_to_assistant,
                message_kind,metadata_json,response_cycle_id,delivery_projection_state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                *values,
                response_cycle_id or None,
                "pending_outbox" if str(role or "") == "assistant" and response_cycle_id else "not_applicable",
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO conversation_messages(
                id,thread_id,role,content,source_type,legacy_conversation_id,created_at,
                external_message_id,reply_to_external_message_id,directed_to_assistant,
                message_kind,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )
    conn.execute(
        "UPDATE conversation_threads SET updated_at=? WHERE id=?",
        (created_at, thread["id"]),
    )
    if (
        actor_ref
        and str(role or "") == "user"
        and "actor_ref" in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(conversation_messages)")
        }
    ):
        conn.execute(
            "UPDATE conversation_messages SET actor_ref=? WHERE id=?",
            (actor_ref, message_id),
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
    if not memory_scope_feature_enabled(conn) and not _continuous_private_enabled(conn):
        return list(reversed(_rows(conn.execute(
            """
            SELECT role,content,created_at FROM conversations
            WHERE user_id=? ORDER BY id DESC LIMIT ?
            """,
            (str(legacy_user_id or "default").strip(), limit),
        ))))
    thread = resolve_thread(conn, legacy_user_id, source=source)
    if _has_continuous_private_fields(conn):
        rows = _rows(conn.execute(
            """
            SELECT role,content,created_at,metadata_json,external_message_id
            FROM conversation_messages
            WHERE thread_id=?
              AND (role<>'assistant' OR delivery_projection_state IN ('not_applicable','channel_acked'))
            ORDER BY created_at DESC,id DESC LIMIT ?
            """,
            (thread["id"], limit),
        ))
    else:
        rows = _rows(conn.execute(
            """
            SELECT role,content,created_at,metadata_json,external_message_id
            FROM conversation_messages
            WHERE thread_id=? ORDER BY created_at DESC,id DESC LIMIT ?
            """,
            (thread["id"], limit),
        ))
    result = []
    for row in reversed(rows):
        item = dict(row)
        try:
            metadata = json.loads(str(item.pop("metadata_json", "") or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        replay = metadata.get("provider_cache_replay_v3") if isinstance(metadata, dict) else None
        if item.get("role") == "user" and isinstance(replay, str) and replay:
            item["provider_cache_replay"] = replay
        result.append(item)
    return result


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
    subject_actor_ref: str = "",
    source_external_message_id: str = "",
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
    group_identity_actor = (
        str(subject_actor_ref or "").strip()[:300]
        if v2_enabled and scope_type == "qq_group" else ""
    )
    identity_seed = (
        f"{owner_actor_id}\0{assistant_id if scope_type != 'global_preference' else '*'}\0"
        f"{scope_type}\0{scope_id}\0{group_identity_actor}\0"
        if v2_enabled and scope_type == "qq_group" else
        (f"{owner_actor_id}\0{assistant_id if scope_type != 'global_preference' else '*'}\0"
         f"{scope_type}\0{scope_id}\0" if v2_enabled else f"{legacy_key}\0")
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

    memory_subject_actor_ref = str(thread["subject_actor_ref"] or "")
    source_message_id = None
    if scope_type == "qq_group" or (
        channel_type == "qq_private" and scope_type == "thread"
        and str(source_external_message_id or "").strip()
    ):
        memory_subject_actor_ref = (
            group_identity_actor if scope_type == "qq_group"
            else str(subject_actor_ref or thread["subject_actor_ref"] or "").strip()[:300]
        )
        source_external_message_id = str(source_external_message_id or "").strip()[:300]
        if memory_subject_actor_ref and source_external_message_id:
            actor_clause = (
                "actor_ref=?" if scope_type == "qq_group"
                else "actor_ref IN (?, '')"
            )
            source_rows = conn.execute(
                f"""
                SELECT id FROM conversation_messages
                WHERE thread_id=? AND role='user' AND external_message_id=?
                  AND {actor_clause}
                ORDER BY created_at,id LIMIT 2
                """,
                (thread["id"], source_external_message_id, memory_subject_actor_ref),
            ).fetchall()
            if len(source_rows) == 1:
                source_message_id = str(source_rows[0][0])

    record_id = "memory-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO memory_records(
            id,owner_actor_id,assistant_id,subject_actor_ref,scope_type,scope_id,
            kind,content,source,score,sensitivity,consent_basis,source_thread_id,
            source_message_id,expires_at,last_used_at,status,legacy_memory_id,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'active',?,?,?)
        ON CONFLICT(legacy_memory_id) DO UPDATE SET
            kind=excluded.kind,content=excluded.content,source=excluded.source,
            score=max(memory_records.score,excluded.score),
            sensitivity=excluded.sensitivity,consent_basis=excluded.consent_basis,
            subject_actor_ref=CASE
                WHEN excluded.subject_actor_ref<>'' THEN excluded.subject_actor_ref
                ELSE memory_records.subject_actor_ref END,
            source_message_id=COALESCE(excluded.source_message_id,memory_records.source_message_id),
            status='active',updated_at=excluded.updated_at
        """,
        (
            record_id, owner_actor_id,
            None if scope_type == "global_preference" else assistant_id,
            memory_subject_actor_ref, scope_type, scope_id, kind, content,
            source, int(score), sensitivity, consent_basis, thread["id"],
            source_message_id, legacy_id, now, now,
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
    retrieval_context: Mapping[str, object] | None = None,
) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    if not memory_scope_feature_enabled(conn):
        if retrieval_context is not None:
            return []  # Legacy records cannot establish member/source boundaries.
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
        if retrieval_context is not None and thread["channel_type"] == "qq_group":
            return _group_context_memories(conn, thread, where, params, query, retrieval_context, limit)
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
            SELECT id,legacy_memory_id,updated_at,source_message_id FROM memory_records
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
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='scoped_memory_evidence'").fetchone():
                conn.execute("DELETE FROM scoped_memory_evidence WHERE memory_id=?", (row[0],))
            if row[3]:
                from bridge_learning_service import revoke_private_topic_applications_for_source
                revoke_private_topic_applications_for_source(conn, str(row[3]))
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
    allowed = {"status", "expires_at", "sensitivity", "content", "expected_updated_at"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unsupported_memory_fields:" + ",".join(unknown))
    if "content" in payload:
        if set(payload) != {"content", "expected_updated_at"}:
            raise ValueError("memory_correction_fields_conflict")
        content = " ".join(str(payload.get("content") or "").split())
        if not content or len(content) > 1000:
            raise ValueError("memory_correction_content_invalid")
        if content == str(current["content"]):
            raise ValueError("memory_correction_content_unchanged")
        if current["status"] != "active" or current["scope_type"] not in {
            "owner_private", "project", "assistant_private", "global_preference",
        } or current["sensitivity"] == "sensitive":
            raise ValueError("memory_correction_scope_unsupported")
        assistant_id, owner_actor_id = _active_assistant(conn)
        if current["owner_actor_id"] != owner_actor_id or (
            current["scope_type"] != "global_preference"
            and current["assistant_id"] != assistant_id
        ):
            raise ValueError("memory_not_found")
        conn.execute("SAVEPOINT owner_memory_correction")
        try:
            duplicate = conn.execute(
                """SELECT 1 FROM memory_records
                   WHERE owner_actor_id=? AND assistant_id IS ? AND scope_type=?
                     AND scope_id IS ? AND kind=? AND content=? AND status='active'
                     AND id<>? LIMIT 1""",
                (owner_actor_id, current["assistant_id"], current["scope_type"],
                 current["scope_id"], current["kind"], content, current["id"]),
            ).fetchone()
            if duplicate:
                raise ValueError("memory_correction_duplicate")
            update_memory(conn, str(current["id"]), {
                "status": "paused", "expected_updated_at": expected,
            })
            replacement = add_memory(
                conn, "web-console", content, kind=str(current["kind"]),
                source="owner_correction", score=int(current["score"]),
                request_source="admin", scope_type=str(current["scope_type"]),
                sensitivity=str(current["sensitivity"]), consent_basis="explicit",
                project_id=(str(current["scope_id"])
                            if current["scope_type"] == "project" else None),
            )
            replacement["supersedes_id"] = str(current["id"])
            conn.execute("RELEASE owner_memory_correction")
            return replacement
        except Exception:
            conn.execute("ROLLBACK TO owner_memory_correction")
            conn.execute("RELEASE owner_memory_correction")
            raise
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
    changed = conn.execute(
        """
        UPDATE memory_records
        SET status=?,sensitivity=?,expires_at=?,updated_at=?
        WHERE id=? AND updated_at=?
        """,
        (status, sensitivity, expires_at or None, now, current["id"], expected),
    )
    if changed.rowcount != 1:
        raise ValueError("memory_version_conflict")
    if status == "deleted" and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='scoped_memory_evidence'"
    ).fetchone():
        conn.execute("DELETE FROM scoped_memory_evidence WHERE memory_id=?", (current["id"],))
    if status != "active" and current.get("source_message_id"):
        from bridge_learning_service import revoke_private_topic_applications_for_source
        revoke_private_topic_applications_for_source(conn, str(current["source_message_id"]))
    if current.get("legacy_memory_id"):
        conn.execute(
            "UPDATE memories SET deleted=?,updated_at=? WHERE id=?",
            (1 if status != "active" else 0, now, current["legacy_memory_id"]),
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


_B2_CHANNEL_TYPES = frozenset({"web", "qq_private", "qq_group"})


def _page_limit(value: int, *, maximum: int = 80) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("conversation_limit_invalid") from exc
    if parsed < 1 or parsed > maximum:
        raise ValueError("conversation_limit_invalid")
    return parsed


def _encode_page_cursor(timestamp: object, identifier: object) -> str:
    payload = json.dumps([str(timestamp or ""), str(identifier or "")], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_page_cursor(cursor: str) -> tuple[str, str] | None:
    value = str(cursor or "").strip()
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
        timestamp, identifier = json.loads(decoded)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("conversation_cursor_invalid") from exc
    if not isinstance(timestamp, str) or not isinstance(identifier, str) or not timestamp or not identifier:
        raise ValueError("conversation_cursor_invalid")
    return timestamp, identifier


def _channel_label(channel_type: str) -> str:
    return {
        "web": "Web 私人对话",
        "qq_private": "QQ 私聊",
        "qq_group": "QQ 群聊",
    }.get(channel_type, "未知")


def list_threads_page(
    conn: sqlite3.Connection,
    *,
    channel_type: str,
    limit: int = 20,
    cursor: str = "",
) -> dict:
    """Read one channel-specific thread page without leaking legacy references."""

    channel_type = str(channel_type or "").strip()
    if channel_type not in _B2_CHANNEL_TYPES:
        raise ValueError("conversation_channel_type_invalid")
    safe_limit = _page_limit(limit)
    assistant_id, owner_actor_id = _active_assistant(conn)
    clauses = ["owner_actor_id=?", "assistant_id=?", "channel_type=?"]
    params: list[object] = [owner_actor_id, assistant_id, channel_type]
    after = _decode_page_cursor(cursor)
    if after:
        clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
        params.extend((after[0], after[0], after[1]))
    params.append(safe_limit + 1)
    rows = _rows(conn.execute(
        f"""
        SELECT id,channel_type,project_id,status,created_at,updated_at
        FROM conversation_threads
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC,id DESC LIMIT ?
        """,
        tuple(params),
    ))
    selected = rows[:safe_limit]
    next_cursor = ""
    if len(rows) > safe_limit and selected:
        last = selected[-1]
        next_cursor = _encode_page_cursor(last.get("updated_at"), last.get("id"))
    return {
        "items": [{
            "id": str(row["id"]),
            "channel_type": str(row["channel_type"]),
            "channel_label": _channel_label(str(row["channel_type"])),
            "status": str(row["status"]),
            "project_id": str(row.get("project_id") or ""),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        } for row in selected],
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


def thread_messages_page(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    required_channel_type: str,
    limit: int = 40,
    cursor: str = "",
) -> dict:
    """Read a selected thread only after enforcing its channel type."""

    required_channel_type = str(required_channel_type or "").strip()
    if required_channel_type not in _B2_CHANNEL_TYPES:
        raise ValueError("conversation_channel_type_invalid")
    safe_limit = _page_limit(limit)
    assistant_id, owner_actor_id = _active_assistant(conn)
    thread = conn.execute(
        """
        SELECT id,channel_type FROM conversation_threads
        WHERE id=? AND assistant_id=? AND owner_actor_id=?
        """,
        (str(thread_id or "").strip(), assistant_id, owner_actor_id),
    ).fetchone()
    if not thread:
        raise ValueError("conversation_thread_not_found")
    if str(thread[1]) != required_channel_type:
        raise ValueError("conversation_channel_mismatch")
    clauses = ["thread_id=?"]
    params: list[object] = [str(thread[0])]
    after = _decode_page_cursor(cursor)
    if after:
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        params.extend((after[0], after[0], after[1]))
    params.append(safe_limit + 1)
    rows = _rows(conn.execute(
        f"""
        SELECT id,role,content,source_type,created_at
        FROM conversation_messages
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC,id DESC LIMIT ?
        """,
        tuple(params),
    ))
    selected = rows[:safe_limit]
    next_cursor = ""
    if len(rows) > safe_limit and selected:
        last = selected[-1]
        next_cursor = _encode_page_cursor(last.get("created_at"), last.get("id"))
    return {
        "items": list(reversed([{
            "id": str(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"]),
            "source_type": str(row["source_type"]),
            "created_at": str(row["created_at"]),
        } for row in selected])),
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


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
    "list_threads_page",
    "memory_scope_catalog",
    "memory_scope_feature_enabled",
    "record_conversation",
    "resolve_thread",
    "set_memory_scope_feature",
    "thread_messages",
    "thread_messages_page",
    "update_memory",
]
