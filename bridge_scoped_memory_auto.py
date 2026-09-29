"""Conservative, source-bound proposals for QQ long-term memory.

This module never decides that a quoted claim is externally true.  It only
recognizes direct, low-risk statements that are safe to attribute to speakers.
"""

from __future__ import annotations

import re
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping


_UNCERTAIN = re.compile(r"可能|也许|大概|听说|据说|如果|假如|好像|估计|\?|？")
_SENSITIVE = re.compile(
    r"密码|口令|验证码|密钥|api\s*key|token|cookie|身份证|银行卡|住址|地址|"
    r"病史|诊断|药物|收入|工资|负债|宗教|政治立场|性取向|private\s*key|"
    r"手机号|电话号码|邮箱|出生日期|年龄|患病|疾病|治疗|借款",
    re.IGNORECASE,
)
_SECRET_SHAPES = re.compile(r"(?:(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dXx](?!\d)|(?<!\d)\d{15}(?!\d)|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")
_REPORTED = re.compile(r"朋友|同事|别人|他[们]?说|她[们]?说|原文|引用|[“”‘’\"']")
_SELF_PREFERENCE = re.compile(r"^我(?P<stance>不喜欢|更喜欢|喜欢|偏好)(?P<topic>[^，。！？!?；;]{2,40})[。！!]?$" )
_SELF_ADDRESS = re.compile(r"^(?:以后)?叫我([^，。！？!?；;]{2,20})[。！!]?$" )
_SELF_BOUNDARY = re.compile(r"^请不要(?:再)?(?:和我)?(?:聊|提)(?P<topic>[^，。！？!?；;]{2,30})[。！!]?$" )
_SELF_PROJECT = re.compile(r"^我(?:一直|长期)在参与(?P<topic>[^，。！？!?；;]{2,30})项目[。！!]?$" )
_EVENT = re.compile(r"(?P<object>[\u4e00-\u9fffA-Za-z0-9]{2,24}?)(?P<result>办完|完成|结束|搞定)了?[。！!]?$" )
_CONVENTION = re.compile(r"^以后(?:本群|群里)(?P<rule>只聊|不聊|优先聊)(?P<topic>[^，。！？!?；;]{2,30})[。！!]?$" )
_TEMPORAL_PREFIX = re.compile(r"^(?:对[，,]|是啊[，,]|没错[，,])?(?:昨晚|昨天|前天|今天|上周|本周)?(?:群里|我们)?(?:把)?")
_TIME_MARKER = re.compile(r"昨晚|昨天|前天|今天|上周|本周")
_QQ_ID = re.compile(r"[1-9][0-9]{4,19}")


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def self_claim(text: str, *, actor: str, scope: str) -> dict | None:
    """Accept only a direct low-risk self-preference or preferred address."""

    content = _clean(text)
    correction = content.startswith("我之前说错了，")
    if correction:
        content = content.removeprefix("我之前说错了，")
    if not actor or not scope or len(content) > 60 or _UNCERTAIN.search(content):
        return None
    if _SENSITIVE.search(content) or _SECRET_SHAPES.search(content) or _REPORTED.search(content):
        return None
    match = _SELF_PREFERENCE.fullmatch(content)
    address = _SELF_ADDRESS.fullmatch(content)
    boundary = _SELF_BOUNDARY.fullmatch(content)
    project = _SELF_PROJECT.fullmatch(content)
    kind = "preference" if match or boundary else "fact" if address or project else ""
    if not kind:
        return None
    topic = match.group("topic") if match else boundary.group("topic") if boundary else project.group("topic") if project else "preferred_address"
    return {"kind": kind, "content": content, "subject": actor, "scope": scope,
            "topic_key": topic, "correction": correction}


def _completed_event(text: str) -> tuple[str, str] | None:
    content = _clean(text)
    if len(content) > 80 or _UNCERTAIN.search(content) or _SENSITIVE.search(content) or _SECRET_SHAPES.search(content):
        return None
    content = _TEMPORAL_PREFIX.sub("", content)
    match = _EVENT.fullmatch(content)
    if not match:
        return None
    return match.group("object"), match.group("result")


def shared_claim(first: Mapping, second: Mapping) -> dict | None:
    """Two different human authors must directly confirm one completed event."""

    group = _clean(first.get("group_id"))
    first_id, second_id = _clean(first.get("id")), _clean(second.get("id"))
    first_actor, second_actor = _clean(first.get("actor")), _clean(second.get("actor"))
    if not group or group != _clean(second.get("group_id")) or not first_id or not second_id:
        return None
    if not first_actor or not second_actor or first_actor == second_actor:
        return None
    first_text, second_text = str(first.get("text") or ""), str(second.get("text") or "")
    a, b = _completed_event(first_text), _completed_event(second_text)
    first_time = _TIME_MARKER.search(first_text)
    second_time = _TIME_MARKER.search(second_text)
    if first_time and second_time and first_time.group() != second_time.group():
        return None
    convention_a = _CONVENTION.fullmatch(first_text)
    convention_b = _CONVENTION.fullmatch(re.sub(r"^(?:同意[，,]|对[，,])", "", second_text))
    if a is not None and a == b:
        kind, content, topic = "episode", f"群内成员确认{a[0]}已{a[1]}", a[0]
    elif convention_a and convention_b and convention_a.groupdict() == convention_b.groupdict():
        kind, content, topic = "instruction", "群成员约定：" + first_text, convention_a.group("topic")
    else:
        return None
    reply = _clean(second.get("reply_to"))
    linked = bool(reply) and reply in {first_id, _clean(first.get("external_id")), first_id.removeprefix("group:")}
    # Without an explicit reply edge, only an identical event + result + time
    # in the same short window counts. A mere "yes" can never supply evidence.
    if not linked:
        first_created_at = _clean(first.get("created_at"))
        second_created_at = _clean(second.get("created_at"))
        if not first_created_at or not second_created_at:
            return None
        try:
            delta = datetime.fromisoformat(second_created_at.replace("Z", "+00:00")) - datetime.fromisoformat(first_created_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if not timedelta(0) < delta <= timedelta(minutes=10):
            return None
        if not first_time or not second_time or first_time.group() != second_time.group():
            return None
    return {
        "kind": kind,
        "content": content,
        "scope": f"group:{group}",
        "subject": "",
        "evidence_ids": (first_id, second_id),
        "topic_key": topic,
    }


SCOPED_MEMORY_MIGRATION_CHECKSUM = hashlib.sha256(b"scoped_memory_auto_v1:fixed-cutoff,state,evidence,outcomes").hexdigest()


def apply_scoped_memory_auto_v1(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE scoped_memory_scan_state(
          channel TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL DEFAULT 0,
          scan_cutoff TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE scoped_memory_evidence(
          memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
          source_key TEXT NOT NULL, scope_id TEXT NOT NULL, actor_ref TEXT NOT NULL,
          snippet TEXT NOT NULL, source_hash TEXT NOT NULL, topic_key TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(memory_id,source_key)
        );
        CREATE INDEX idx_scoped_memory_evidence_scope
        ON scoped_memory_evidence(scope_id,topic_key,actor_ref);
        CREATE TABLE scoped_memory_scan_outcomes(
          source_key TEXT PRIMARY KEY, reason TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX idx_scoped_memory_scan_outcomes_time
        ON scoped_memory_scan_outcomes(created_at);
    """)


def _table_ready(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_memory_evidence'").fetchone())


def _source_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _outcome(conn: sqlite3.Connection, key: str, reason: str) -> None:
    from bridge_migrations import utc_now
    conn.execute("""INSERT INTO scoped_memory_scan_outcomes(source_key,reason,created_at)
      VALUES(?,?,?) ON CONFLICT(source_key) DO UPDATE SET reason=excluded.reason,
      created_at=excluded.created_at""",(key,reason,utc_now()))


def _memory_for_topic(conn: sqlite3.Connection, *, scope: str, actor: str, topic: str) -> list[sqlite3.Row]:
    return conn.execute("""SELECT DISTINCT m.id,m.content,m.status FROM memory_records m
      JOIN scoped_memory_evidence e ON e.memory_id=m.id
      WHERE e.scope_id=? AND m.subject_actor_ref=? AND e.topic_key=?
        AND m.status IN ('active','paused')""",
      (scope, actor, topic)).fetchall()


def _persist_claim(conn: sqlite3.Connection, claim: Mapping, sources: list[Mapping]) -> str:
    """Persist only source-verified snippets; never turn a chat claim into task truth."""
    from bridge_conversation_memory import add_memory, update_memory
    from bridge_migrations import utc_now

    scope = str(claim["scope"])
    actor = str(claim["subject"])
    topic = str(claim["topic_key"])
    for old in _memory_for_topic(conn, scope=scope, actor=actor, topic=topic):
        if str(old["content"]) == str(claim["content"]):
            return "existing"
        if not claim.get("correction") and actor:
            updated = conn.execute("SELECT updated_at FROM memory_records WHERE id=?",(old["id"],)).fetchone()
            update_memory(conn,str(old["id"]),{"status":"paused","expected_updated_at":str(updated[0])})
            return "conflict_paused"
        # A group-wide replacement has already supplied two independent
        # witnesses; it is not a single-speaker conflict.
        updated = conn.execute("SELECT updated_at FROM memory_records WHERE id=?",(old["id"],)).fetchone()
        update_memory(conn,str(old["id"]),{"status":"paused","expected_updated_at":str(updated[0])})
    kind = str(claim["kind"])
    is_group = scope.startswith("group:")
    memory = add_memory(conn, scope if is_group else scope.removeprefix("private:"),
      str(claim["content"]), kind=kind, source="qq_auto_source_verified", score=6,
      request_source="qq_group" if is_group else "qq_private", scope_type="qq_group" if is_group else "thread",
      sensitivity="normal" if is_group else "private", consent_basis="auto_source_verified",
      subject_actor_ref=actor)
    memory_id = str(memory["id"])
    for source in sources:
        body = str(source["text"])
        snippet = str(source.get("snippet") or body)
        if not snippet or snippet not in body or len(snippet) > 180:
            raise ValueError("source_span_invalid")
        conn.execute("""INSERT OR IGNORE INTO scoped_memory_evidence(
          memory_id,source_key,scope_id,actor_ref,snippet,source_hash,topic_key,created_at)
          VALUES(?,?,?,?,?,?,?,?)""", (memory_id,str(source["id"]),scope,
          str(source["actor"]),snippet,_source_hash(body),topic,utc_now()))
    return memory_id


def _group_source(row: Mapping) -> dict:
    metadata = {}
    try:
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
    except (ValueError, TypeError):
        pass
    if not isinstance(metadata, dict):
        metadata = {}
    return {"id": "group:" + str(row["id"]), "group_id": str(row["group_id"]),
       "actor": str(row["sender_id"]), "text": str(row["content"]),
       "created_at": str(row["created_at"]),
       "external_id": str(row.get("external_message_id") or ""),
       "reply_to": str(metadata.get("reply_to_external_message_id")
                         or metadata.get("reply_to_group_message_id") or "")}


def process_scoped_memory_pass(conn: sqlite3.Connection, *, limit: int = 40,
                               cutoff: str | None = None) -> dict:
    conn.execute("SAVEPOINT scoped_memory_pass")
    try:
        result = _process_scoped_memory_pass(conn,limit=limit,cutoff=cutoff)
    except Exception:
        conn.execute("ROLLBACK TO scoped_memory_pass")
        conn.execute("RELEASE scoped_memory_pass")
        raise
    conn.execute("RELEASE scoped_memory_pass")
    return result


def _process_scoped_memory_pass(conn: sqlite3.Connection, *, limit: int = 40,
                                cutoff: str | None = None) -> dict:
    """Bounded, reply-independent scan of still-readable authorized QQ sources.

    The initial pass may cover up to 24h. It never reads backups or redacted
    bodies; future passes continue from durable rowid watermarks.
    """
    from bridge_qq_access_service import check_qq_access, check_private_chat_access
    from bridge_migrations import utc_now

    if not _table_ready(conn):
        return {"processed": 0, "applied": 0, "reason": "schema_missing"}
    initial_cutoff = cutoff or (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    processed = applied = 0
    access_cache: dict[tuple[str, str, str], bool] = {}

    def admitted(channel: str, actor: str, group_id: str = "") -> bool:
        key = (channel, actor, group_id)
        if key not in access_cache:
            if channel == "group":
                verdict = check_qq_access(conn,{"sender_id":actor,"event_type":"group",
                    "group_id":group_id,"requested_action":"group_message"})
            else:
                verdict = check_private_chat_access(conn,actor,requested_action="chat")
            access_cache[key] = bool(verdict.get("allowed"))
        return access_cache[key]
    for channel in ("group", "private"):
        state = conn.execute("SELECT last_rowid,scan_cutoff FROM scoped_memory_scan_state WHERE channel=?", (channel,)).fetchone()
        if state is None:
            conn.execute("INSERT INTO scoped_memory_scan_state(channel,last_rowid,scan_cutoff,updated_at) VALUES(?,?,?,?)",
                (channel,0,initial_cutoff,utc_now()))
        scan_cutoff = str(state[1]) if state else initial_cutoff
        cursor = int(state[0]) if state else 0
        if channel == "group":
            rows = conn.execute("""SELECT g.* FROM group_messages g
              JOIN group_policies p ON p.group_id=g.group_id AND p.enabled=1
              WHERE g.id>? AND g.created_at>=? ORDER BY g.id LIMIT ?""", (cursor,scan_cutoff,limit)).fetchall()
        else:
            rows = conn.execute("""SELECT c.rowid AS source_rowid,c.*,t.external_thread_ref
              FROM conversation_messages c JOIN conversation_threads t ON t.id=c.thread_id
              WHERE c.rowid>? AND c.created_at>=? AND c.role='user'
                AND t.channel_type='qq_private' AND t.status='active'
                AND t.assistant_id=(SELECT id FROM assistant_instances WHERE status='active' ORDER BY created_at LIMIT 1)
              ORDER BY c.rowid LIMIT ?""", (cursor,scan_cutoff,limit)).fetchall()
        for raw in rows:
            row = dict(raw)
            cursor = int(row["id"] if channel == "group" else row["source_rowid"])
            source_key = ("group:" if channel == "group" else "private:") + str(row["id"])
            processed += 1
            body = str(row.get("content") or "")
            if (not body or row.get("body_redacted_at") or row.get("retention_class") == "metadata_only"
                or (row.get("expires_at") and str(row["expires_at"]) <= utc_now())):
                _outcome(conn,source_key,"source_unreadable")
                continue
            actor = str(row.get("sender_id") if channel == "group" else row.get("actor_ref") or "")
            group_id = str(row.get("group_id") or "")
            # The persisted row ID is the durable source ID. Some accepted QQ
            # group events lack an adapter-supplied external ID; rejecting
            # them would silently discard most still-readable source bodies.
            if (not _QQ_ID.fullmatch(actor)
                or (channel == "group" and not _QQ_ID.fullmatch(group_id))
                or (channel == "private" and actor != str(row.get("external_thread_ref") or ""))):
                _outcome(conn,source_key,"source_identity_invalid")
                continue
            if not admitted(channel,actor,group_id):
                _outcome(conn,source_key,"qq_admission_denied")
                continue
            scope = "group:" + group_id if channel == "group" else "private:" + actor
            claim = self_claim(body,actor=actor,scope=scope)
            reason = "not_stable_self_claim"
            if claim:
                source = {"id": ("group:" + str(row["id"])) if channel == "group" else str(row["id"]),
                          "actor":actor,"text":body,"snippet":claim["content"]}
                result = _persist_claim(conn,claim,[source])
                applied += result not in {"conflict_paused", "existing"}
                reason = "conflict_paused" if result == "conflict_paused" else "duplicate_source" if result == "existing" else "self_claim_applied"
            if channel == "group":
                second = _group_source(row)
                earlier = conn.execute("""SELECT * FROM group_messages WHERE group_id=? AND id<?
                  AND sender_id<>? AND sender_id<>'' AND content<>'' AND body_redacted_at=''
                  AND (expires_at='' OR expires_at>?) ORDER BY id DESC LIMIT 20""",
                  (group_id,row["id"],actor,utc_now())).fetchall()
                for prior in earlier:
                    first = _group_source(dict(prior))
                    if not _QQ_ID.fullmatch(first["actor"]):
                        continue
                    if not admitted("group",first["actor"],group_id):
                        continue
                    shared = shared_claim(first,second)
                    if shared:
                        result = _persist_claim(conn,shared,[first,second])
                        applied += result not in {"conflict_paused", "existing"}
                        reason = "shared_claim_applied" if result not in {"conflict_paused", "existing"} else "shared_claim_duplicate"
                        break
            _outcome(conn,source_key,reason)
        if rows:
            conn.execute("""UPDATE scoped_memory_scan_state SET last_rowid=?,updated_at=?
              WHERE channel=?""",(cursor,utc_now(),channel))
    conn.execute("""DELETE FROM scoped_memory_scan_outcomes WHERE source_key IN (
      SELECT source_key FROM scoped_memory_scan_outcomes WHERE created_at<?
      ORDER BY created_at LIMIT 100)""",
      ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),))
    return {"processed":processed,"applied":applied}


def _management_scope(conn: sqlite3.Connection, channel: str, subject_id: str) -> tuple[str, str]:
    channel = str(channel or "").strip()
    subject_id = str(subject_id or "").strip()
    if channel not in {"group", "private"} or not subject_id or len(subject_id) > 80:
        raise ValueError("qq_memory_scope_invalid")
    if channel == "group":
        row = conn.execute("SELECT enabled FROM group_policies WHERE group_id=?",(subject_id,)).fetchone()
        if not row or not int(row[0]):
            raise PermissionError("qq_memory_scope_forbidden")
        scope_type, scope_id = "qq_group", "group:" + subject_id
    else:
        from bridge_qq_access_service import check_private_chat_access
        if not check_private_chat_access(conn,subject_id,requested_action="chat").get("allowed"):
            raise PermissionError("qq_memory_scope_forbidden")
        row = conn.execute("""SELECT id FROM conversation_threads WHERE channel_type='qq_private'
          AND external_thread_ref=? AND status='active' ORDER BY updated_at DESC LIMIT 1""",
          (subject_id,)).fetchone()
        scope_type, scope_id = "thread", str(row[0]) if row else ""
    return scope_type, scope_id


def list_scoped_memory_management(conn: sqlite3.Connection, channel: str,
                                  subject_id: str) -> list[dict]:
    """Admin-only caller; records are restricted to the current Assistant."""
    scope_type, scope_id = _management_scope(conn,channel,subject_id)
    if not scope_id:
        return []
    from bridge_conversation_memory import _active_assistant
    assistant_id, owner_actor_id = _active_assistant(conn)
    rows = conn.execute("""SELECT id,kind,content,status,source,subject_actor_ref,source_message_id,
      created_at,updated_at FROM memory_records WHERE assistant_id=? AND owner_actor_id=?
      AND scope_type=? AND scope_id=? AND status<>'deleted'
      ORDER BY updated_at DESC,id LIMIT 80""",
      (assistant_id,owner_actor_id,scope_type,scope_id)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        evidence = conn.execute("""SELECT source_key,actor_ref,snippet FROM scoped_memory_evidence
          WHERE memory_id=? ORDER BY source_key LIMIT 2""",(item["id"],)).fetchall()
        item["evidence"] = [dict(value) for value in evidence]
        if not item["evidence"] and item.get("source_message_id"):
            source = conn.execute("""SELECT id,actor_ref,content FROM conversation_messages
              WHERE id=? AND role='user' AND content<>'' AND body_redacted_at=''
              AND (expires_at='' OR expires_at>?) LIMIT 1""",
              (item["source_message_id"],datetime.now(timezone.utc).isoformat())).fetchone()
            if source and str(item["content"]) in str(source["content"]):
                item["evidence"] = [{"source_key":str(source["id"]),
                    "actor_ref":str(source["actor_ref"]),"snippet":str(item["content"])}]
        item.pop("source_message_id",None)
        item["provenance"] = ("主人添加" if item["source"] in {"owner_added","owner_correction","manual","qq-manual"}
          else "聊天来源" if item["evidence"] else "既有记忆·来源待核对")
        result.append(item)
    return result


def mutate_scoped_memory_management(conn: sqlite3.Connection, channel: str,
                                    subject_id: str, payload: Mapping) -> dict:
    """Versioned owner create/correct/delete within one admitted QQ scope."""
    from bridge_conversation_memory import add_memory, delete_memory, update_memory, _active_assistant
    scope_type, scope_id = _management_scope(conn,channel,subject_id)
    action = str(payload.get("action") or "")
    if action not in {"add","correct","delete"}:
        raise ValueError("qq_memory_action_invalid")
    assistant_id, owner_actor_id = _active_assistant(conn)
    old = None
    if action != "add":
        old = conn.execute("""SELECT id,updated_at FROM memory_records WHERE id=?
          AND assistant_id=? AND owner_actor_id=? AND scope_type=? AND scope_id=?
          AND status<>'deleted'""",(str(payload.get("id") or ""),assistant_id,
          owner_actor_id,scope_type,scope_id)).fetchone()
        if not old:
            raise ValueError("qq_memory_not_found")
        if str(payload.get("expected_updated_at") or "") != str(old["updated_at"]):
            raise ValueError("memory_version_conflict")
    if action == "delete":
        delete_memory(conn,str(old["id"]),expected_updated_at=str(old["updated_at"]))
        return {"deleted":True,"id":str(old["id"])}
    content = _clean(payload.get("content"))
    if not content or len(content) > 180 or _SENSITIVE.search(content) or _SECRET_SHAPES.search(content):
        raise ValueError("qq_memory_content_invalid")
    if old:
        old_content = conn.execute("SELECT content FROM memory_records WHERE id=?",(old["id"],)).fetchone()[0]
        if content == str(old_content):
            raise ValueError("qq_memory_content_unchanged")
    duplicate = conn.execute("""SELECT 1 FROM memory_records WHERE assistant_id=?
      AND owner_actor_id=? AND scope_type=? AND scope_id=? AND status='active'
      AND content=? LIMIT 1""",(assistant_id,owner_actor_id,scope_type,scope_id,content)).fetchone()
    if duplicate:
        raise ValueError("qq_memory_already_exists")
    if action == "correct":
        update_memory(conn,str(old["id"]),{"status":"paused",
          "expected_updated_at":str(old["updated_at"])})
    created = add_memory(conn, "group:" + subject_id if channel == "group" else subject_id,
      content,kind="fact",source="owner_correction" if action == "correct" else "owner_added",
      request_source="qq_group" if channel == "group" else "qq_private",
      scope_type=scope_type,sensitivity="normal" if channel == "group" else "private",
      consent_basis="owner_added",score=7)
    return {"memory":created,"supersedes":str(old["id"]) if old else ""}
