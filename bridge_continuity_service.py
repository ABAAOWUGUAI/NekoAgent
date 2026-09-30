#!/usr/bin/env python3
"""Governed memory-candidate lifecycle for Assistant Continuity."""

from __future__ import annotations

import sqlite3
import re
import hashlib
import uuid
from typing import Callable

from bridge_conversation_memory import add_memory, record_conversation, resolve_thread
from bridge_migrations import utc_now


SENSITIVE_HINTS = (
    "密码",
    "口令",
    "验证码",
    "密钥",
    "api key",
    "token",
    "cookie",
    "身份证",
    "银行卡",
    "private key",
    "secret",
)
SENSITIVE_PRIVATE_RE = re.compile(
    r"(?:抑郁|焦虑症|诊断|药物|病史|收入|负债|银行卡|住址|家庭地址|政治立场|宗教|性取向|身份证)"
)
REPORTED_SPEECH_RE = re.compile(r"(?:朋友|同事|他|她|别人|原文|引用).{0,8}(?:说|写|表示)|[“‘\"]")


def _row(row: sqlite3.Row | None) -> dict | None:
    return {key: row[key] for key in row.keys()} if row else None


def _active_assistant(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute(
        "SELECT id,owner_actor_id FROM assistant_instances "
        "WHERE status='active' ORDER BY created_at LIMIT 1",
    ).fetchone()
    if not row:
        raise ValueError("active_assistant_missing")
    return str(row[0]), str(row[1])


def _candidate_kind(message: str, raw_kind: str) -> str:
    raw_kind = str(raw_kind or "").strip().lower()
    if raw_kind in {"preference", "fact", "project", "profile", "instruction"}:
        return raw_kind
    if any(word in message for word in ("喜欢", "不喜欢", "希望", "偏好", "以后", "习惯")):
        return "preference"
    return "fact"


def _private_preference_object(content: str) -> tuple[str, str]:
    match = re.fullmatch(r"我(不喜欢|喜欢|更喜欢|偏好)([^，。！？!?；;]{2,40})", content.strip())
    if not match:
        return "", ""
    return ("negative" if match.group(1) == "不喜欢" else "positive", match.group(2).strip())


def _auto_private_preference(content: str, proposal: dict, *, message: str, start: int) -> bool:
    sentiment, subject = _private_preference_object(content)
    return (
        str(proposal.get("kind") or "") == "preference"
        and bool(sentiment and subject)
        and len(content) <= 60
        and not any(hint in content.lower() for hint in SENSITIVE_HINTS)
        and not SENSITIVE_PRIVATE_RE.search(content)
        and not REPORTED_SPEECH_RE.search(message[max(0, start - 40):start])
    )


def _unique_group_source(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    actor_ref: str,
    external_message_id: str,
) -> dict | None:
    """Return one exact human source or fail closed on missing/ambiguous provenance."""

    rows = conn.execute(
        """SELECT id,external_message_id,actor_ref,content FROM conversation_messages
           WHERE thread_id=? AND role='user' AND external_message_id=? AND actor_ref=?
           ORDER BY created_at,id LIMIT 2""",
        (thread_id, external_message_id, actor_ref),
    ).fetchall()
    if len(rows) != 1:
        return None
    return _row(rows[0])


def _unique_private_source(
    conn: sqlite3.Connection, *, thread_id: str, actor_ref: str,
    external_message_id: str,
) -> dict | None:
    """Thread is user-bound; accept legacy blank actor but never another actor."""
    rows = conn.execute(
        """SELECT id,external_message_id,actor_ref,content FROM conversation_messages
           WHERE thread_id=? AND role='user' AND external_message_id=?
             AND actor_ref IN ('',?) ORDER BY created_at,id LIMIT 2""",
        (thread_id, external_message_id, actor_ref),
    ).fetchall()
    return _row(rows[0]) if len(rows) == 1 else None


def create_candidates_from_plan(
    conn: sqlite3.Connection,
    *,
    legacy_user_id: str,
    message: str,
    interaction_plan: dict | None,
    source: str = "",
    group: dict | None = None,
    source_external_message_id: str = "",
    source_actor_ref: str = "",
) -> list[dict]:
    """Use exact private spans; only clear low-risk self-preferences auto-apply."""

    text = " ".join(str(message or "").split()).strip()
    proposals = list((interaction_plan or {}).get("memory_candidates") or [])
    if not text or not proposals or any(hint in text.lower() for hint in SENSITIVE_HINTS):
        return []
    assistant_id, owner_actor_id = _active_assistant(conn)
    thread = resolve_thread(conn, legacy_user_id, source=source)
    source_message_id = ""
    if group:
        scope_type = "qq_group"
        group_id = str(group.get("group_id") or "").removeprefix("group:")
        scope_id = str(thread.get("external_thread_ref") or (f"group:{group_id}" if group_id else ""))
        subject_actor_ref = str(group.get("sender_id") or "").strip()[:300]
        external_message_id = str(
            group.get("source_external_message_id") or group.get("message_id") or ""
        ).strip()[:300]
        if not group_id or not subject_actor_ref or not external_message_id:
            return []
        source_row = _unique_group_source(
            conn,
            thread_id=str(thread["id"]),
            actor_ref=subject_actor_ref,
            external_message_id=external_message_id,
        )
        if source_row is None:
            return []
        if " ".join(str(source_row.get("content") or "").split()).strip() != text:
            return []
        source_message_id = str(source_row["id"])
    else:
        scope_type = "thread"
        scope_id = str(thread["id"])
        subject_actor_ref = str(thread.get("subject_actor_ref") or legacy_user_id)
        external_message_id = str(source_external_message_id or "").strip()[:300]
        subject_actor_ref = str(source_actor_ref or legacy_user_id).strip()[:300]
        if source not in {"qq", "qq_private", "private"} or not external_message_id or not subject_actor_ref:
            return []
        source_row = _unique_private_source(
            conn, thread_id=str(thread["id"]), actor_ref=subject_actor_ref,
            external_message_id=external_message_id,
        )
        if source_row is None or str(source_row.get("content") or "").strip() != str(message).strip():
            return []
        source_message_id = str(source_row["id"])
    if not scope_id:
        return []

    actor_clause = " AND subject_actor_ref=?" if scope_type == "qq_group" else ""
    actor_params: tuple[object, ...] = (subject_actor_ref,) if actor_clause else ()
    now = utc_now()
    created = []
    confidence = max(0.0, min(float((interaction_plan or {}).get("confidence") or 0.68), 1.0))
    for proposal in proposals[:20]:
        if not isinstance(proposal, dict):
            continue
        content = text
        start = 0
        if scope_type == "thread":
            start, end = proposal.get("start"), proposal.get("end")
            if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(message):
                raise ValueError("source_span_invalid")
            content = str(message)[start:end].strip()
            if not content or len(content) > 180:
                raise ValueError("source_span_invalid")
        kind = _candidate_kind(content, proposal.get("kind"))
        if source_message_id:
            same_source = conn.execute(
                """SELECT * FROM memory_candidates
                   WHERE assistant_id=? AND scope_type=? AND scope_id=?
                     AND subject_actor_ref=? AND source_message_id=? AND kind=?
                     AND lower(content)=lower(?)
                     AND status IN ('pending','accepted','merged')
                   ORDER BY updated_at DESC LIMIT 1""",
                (assistant_id, scope_type, scope_id, subject_actor_ref, source_message_id, kind, content),
            ).fetchone()
            if same_source:
                created.append(_row(same_source))
                continue
        existing_memory = conn.execute(
            """SELECT id FROM memory_records
            WHERE status='active' AND scope_type=? AND scope_id=? AND kind=? AND lower(content)=lower(?)"""
            + actor_clause + " ORDER BY updated_at DESC LIMIT 1",
            (scope_type, scope_id, kind, content, *actor_params),
        ).fetchone()
        duplicate = existing_memory or conn.execute(
            """SELECT id FROM memory_candidates
            WHERE assistant_id=? AND scope_type=? AND scope_id=? AND kind=? AND lower(content)=lower(?)
              AND status IN ('pending','accepted','merged')"""
            + actor_clause + " ORDER BY updated_at DESC LIMIT 1",
            (assistant_id, scope_type, scope_id, kind, content, *actor_params),
        ).fetchone()
        possible_conflicts = conn.execute(
            """SELECT id,content FROM memory_records
            WHERE status='active' AND scope_type=? AND scope_id=? AND kind=? AND lower(content)<>lower(?)"""
            + actor_clause + " ORDER BY updated_at DESC LIMIT 20",
            (scope_type, scope_id, kind, content, *actor_params),
        ).fetchall()
        conflict = None
        for old in possible_conflicts:
            if scope_type != "thread" or kind != "preference":
                conflict = old
                break
            old_sentiment, old_subject = _private_preference_object(str(old["content"]))
            new_sentiment, new_subject = _private_preference_object(content)
            if not old_subject or not new_subject or (old_subject == new_subject and old_sentiment != new_sentiment):
                conflict = old
                break
        auto_apply = (
            scope_type == "thread" and not duplicate and not conflict
            and _auto_private_preference(content, proposal, message=str(message), start=start)
        )
        candidate_id = (
            "memory-candidate-" + hashlib.sha256(
                f"{assistant_id}\0{scope_id}\0{source_message_id}\0{kind}\0{content}".encode("utf-8")
            ).hexdigest()[:32]
            if scope_type == "thread" else "memory-candidate-" + uuid.uuid4().hex
        )
        # Distinct private sources remain independently reviewable even when
        # they describe the same preference. Only a replay of one source merges.
        status = "merged" if duplicate and scope_type != "thread" else "accepted" if auto_apply else "pending"
        conn.execute(
            """INSERT OR IGNORE INTO memory_candidates(
                id,assistant_id,owner_actor_id,subject_actor_ref,scope_type,scope_id,
                kind,content,confidence,consent_basis,source_thread_id,source_message_id,
                status,duplicate_of,conflict_with,reviewed_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id, assistant_id, owner_actor_id, subject_actor_ref, scope_type, scope_id,
                kind, content, confidence,
                "auto_low_risk_source" if auto_apply else "requires_user_confirmation",
                str(thread["id"]), source_message_id, status, str(duplicate[0]) if duplicate else "",
                str(conflict[0]) if conflict and not duplicate else "", "auto_low_risk" if auto_apply else "", now, now,
            ),
        )
        if auto_apply:
            add_memory(
                conn, legacy_user_id, content, kind=kind, source="plan_source_span",
                score=7, request_source=source, scope_type="thread",
                sensitivity="private", consent_basis="auto_low_risk_source",
                subject_actor_ref=subject_actor_ref,
                source_external_message_id=external_message_id,
            )
        if scope_type == "thread" and kind == "preference" and not conflict:
            conn.execute("SAVEPOINT private_topic_learning")
            try:
                from bridge_learning_service import capture_private_topic_signal
                capture_private_topic_signal(conn, candidate_id)
                conn.execute("RELEASE private_topic_learning")
            except (sqlite3.Error, ValueError, TypeError):
                conn.execute("ROLLBACK TO private_topic_learning")
                conn.execute("RELEASE private_topic_learning")
        created.append(_row(conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone()))
    conn.commit()
    return created


def capture_group_single_plan_memory_candidates(
    db_connect: Callable[[], sqlite3.Connection],
    *,
    explicit_memories: list[str],
    legacy_user_id: str,
    message: str,
    interaction_plan: dict | None,
    source: str = "qq_group",
    group: dict | None = None,
) -> list[dict]:
    """Record one exact accepted group source and its pending proposals.

    This is shared by direct and ambient SinglePlan.  It never writes a source
    mirror unless the parsed plan contains an allowed proposal, and it returns
    only safe candidate metadata.
    """

    proposals = list((interaction_plan or {}).get("memory_candidates") or [])
    group_data = dict(group or {})
    group_id = str(group_data.get("group_id") or "").removeprefix("group:")
    actor_ref = str(group_data.get("sender_id") or "").strip()[:300]
    external_message_id = str(
        group_data.get("source_external_message_id") or group_data.get("message_id") or ""
    ).strip()[:300]
    if explicit_memories or not proposals or not group_id or not actor_ref or not external_message_id:
        return []
    # Synthetic worker fallback IDs never prove an external inbound source.
    if external_message_id.startswith("natural:"):
        return []
    try:
        with db_connect() as conn:
            record_conversation(
                conn,
                legacy_user_id,
                "user",
                message,
                source=source or "qq_group",
                external_message_id=external_message_id,
                actor_ref=actor_ref,
            )
            created = create_candidates_from_plan(
                conn,
                legacy_user_id=legacy_user_id,
                message=message,
                interaction_plan=interaction_plan,
                source=source or "qq_group",
                group={
                    **group_data,
                    "group_id": group_id,
                    "sender_id": actor_ref,
                    "source_external_message_id": external_message_id,
                },
            )
    except (sqlite3.Error, ValueError, TypeError):
        return []
    return [
        {
            "id": item.get("id"),
            "status": item.get("status"),
            "scope_type": item.get("scope_type"),
            "kind": item.get("kind"),
        }
        for item in created
    ]


def capture_plan_candidate_metadata(
    db_connect: Callable[[], sqlite3.Connection],
    *,
    explicit_memories: list[str],
    legacy_user_id: str,
    message: str,
    interaction_plan: dict | None,
    source: str = "",
    group: dict | None = None,
    source_external_message_id: str = "",
    source_actor_ref: str = "",
) -> list[dict]:
    """Capture planner candidates safely and return only non-sensitive metadata."""

    if explicit_memories:
        return []
    try:
        with db_connect() as conn:
            created = create_candidates_from_plan(
                conn,
                legacy_user_id=legacy_user_id,
                message=message,
                interaction_plan=interaction_plan,
                source=source,
                group=group,
                source_external_message_id=source_external_message_id,
                source_actor_ref=source_actor_ref,
            )
    except (sqlite3.Error, ValueError, TypeError):
        return []
    return [
        {
            "id": item.get("id"),
            "status": item.get("status"),
            "scope_type": item.get("scope_type"),
            "kind": item.get("kind"),
        }
        for item in created
    ]


def list_memory_candidates(
    conn: sqlite3.Connection,
    *,
    status: str = "pending",
    limit: int = 100,
) -> list[dict]:
    assistant_id, _ = _active_assistant(conn)
    if status not in {"", "pending", "accepted", "rejected", "merged"}:
        raise ValueError("memory_candidate_status_invalid")
    params: list[object] = [assistant_id]
    where = "assistant_id=?"
    if status:
        where += " AND status=?"
        params.append(status)
    params.append(max(1, min(int(limit), 200)))
    rows = conn.execute(
        f"SELECT * FROM memory_candidates WHERE {where} ORDER BY updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [_row(row) for row in rows]


def review_memory_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    payload: dict,
    *,
    actor: str = "admin",
) -> dict:
    assistant_id, _ = _active_assistant(conn)
    candidate = conn.execute(
        "SELECT * FROM memory_candidates WHERE id=? AND assistant_id=?",
        (candidate_id, assistant_id),
    ).fetchone()
    if not candidate:
        raise ValueError("memory_candidate_not_found")
    if str(candidate["status"]) != "pending":
        raise ValueError("memory_candidate_already_reviewed")
    decision = str(payload.get("status") or "").strip()
    if decision not in {"accepted", "rejected"}:
        raise ValueError("memory_candidate_review_invalid")
    if decision == "accepted" and candidate["conflict_with"] and not payload.get("confirm_conflict"):
        raise ValueError("memory_candidate_conflict_review_required")
    memory = None
    if decision == "accepted":
        scope_type = str(candidate["scope_type"])
        if scope_type == "qq_group":
            legacy_user_id = str(candidate["scope_id"])
            request_source = "group"
            # Query by durable message ID first because review must not trust a
            # client-provided actor or external source.
            source_rows = conn.execute(
                """SELECT m.id,m.external_message_id,m.actor_ref,
                          t.channel_type,t.external_thread_ref
                   FROM conversation_messages AS m
                   JOIN conversation_threads AS t ON t.id=m.thread_id
                   WHERE m.id=? AND m.thread_id=? AND m.role='user' AND m.actor_ref=?
                     AND trim(m.external_message_id)<>'' LIMIT 2""",
                (
                    str(candidate["source_message_id"] or ""),
                    str(candidate["source_thread_id"] or ""),
                    str(candidate["subject_actor_ref"] or "").strip()[:300],
                ),
            ).fetchall()
            if (
                len(source_rows) != 1
                or str(source_rows[0]["channel_type"] or "") != "qq_group"
                or str(source_rows[0]["external_thread_ref"] or "")
                    != str(candidate["scope_id"] or "")
            ):
                raise ValueError("memory_candidate_group_source_required")
            source_external_message_id = str(source_rows[0]["external_message_id"] or "").strip()[:300]
            if not source_external_message_id:
                raise ValueError("memory_candidate_group_source_required")
        elif scope_type == "thread":
            thread = conn.execute(
                "SELECT channel_type,external_thread_ref FROM conversation_threads WHERE id=?",
                (candidate["source_thread_id"],),
            ).fetchone()
            if not thread:
                raise ValueError("memory_candidate_thread_not_found")
            legacy_user_id = str(thread["external_thread_ref"])
            request_source = str(thread["channel_type"])
            private_source_rows = conn.execute(
                """SELECT external_message_id,actor_ref,content FROM conversation_messages
                   WHERE id=? AND thread_id=? AND role='user' LIMIT 2""",
                (str(candidate["source_message_id"] or ""), str(candidate["source_thread_id"] or "")),
            ).fetchall()
            private_source = private_source_rows[0] if len(private_source_rows) == 1 else None
            if candidate["source_message_id"] and (
                not private_source
                or str(private_source["actor_ref"] or "") not in {"", str(candidate["subject_actor_ref"] or "")}
                or str(candidate["content"] or "") not in str(private_source["content"] or "")
            ):
                raise ValueError("memory_candidate_private_source_invalid")
            source_external_message_id = (
                str(private_source["external_message_id"] or "") if private_source else ""
            )
        else:
            raise ValueError("memory_candidate_scope_invalid")
        memory = add_memory(
            conn,
            legacy_user_id,
            str(payload.get("content") or candidate["content"]),
            kind=str(payload.get("kind") or candidate["kind"]),
            source="continuity_review",
            score=max(5, min(round(float(candidate["confidence"]) * 10), 10)),
            request_source=request_source,
            scope_type=scope_type,
            consent_basis="user_confirmed",
            subject_actor_ref=(
                str(candidate["subject_actor_ref"] or "").strip()[:300]
                if scope_type in {"qq_group", "thread"} else ""
            ),
            source_external_message_id=source_external_message_id,
        )
        superseded_id = str(payload.get("supersedes_memory_id") or candidate["conflict_with"] or "").strip()
        if superseded_id:
            old_source = conn.execute(
                "SELECT source_message_id FROM memory_records WHERE id=? AND assistant_id=?",
                (superseded_id, assistant_id),
            ).fetchone()
            conn.execute(
                "UPDATE memory_records SET status='paused',updated_at=? WHERE id=? AND assistant_id=? AND status='active'",
                (utc_now(), superseded_id, assistant_id),
            )
            if old_source and old_source[0]:
                from bridge_learning_service import revoke_private_topic_applications_for_source
                revoke_private_topic_applications_for_source(conn, str(old_source[0]))
    now = utc_now()
    conn.execute(
        "UPDATE memory_candidates SET status=?,reviewed_by=?,updated_at=? WHERE id=?",
        (decision, actor, now, candidate_id),
    )
    if decision == "rejected" and candidate["source_message_id"]:
        from bridge_learning_service import revoke_private_topic_applications_for_source
        revoke_private_topic_applications_for_source(conn, str(candidate["source_message_id"]))
    conn.commit()
    return {
        "candidate": _row(conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone()),
        "memory": memory,
    }


def expire_stale_memories(conn: sqlite3.Connection, *, now: str | None = None) -> int:
    """Pause expired memories without deleting provenance or user history."""

    cutoff = str(now or utc_now())
    result = conn.execute(
        """UPDATE memory_records SET status='paused',updated_at=?
           WHERE status='active' AND expires_at IS NOT NULL AND expires_at<>'' AND expires_at<=?""",
        (cutoff, cutoff),
    )
    conn.commit()
    return int(result.rowcount or 0)


__all__ = [
    "capture_group_single_plan_memory_candidates",
    "capture_plan_candidate_metadata",
    "create_candidates_from_plan",
    "list_memory_candidates",
    "review_memory_candidate",
    "expire_stale_memories",
]
