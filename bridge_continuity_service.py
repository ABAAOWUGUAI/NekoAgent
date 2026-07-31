#!/usr/bin/env python3
"""Governed memory-candidate lifecycle for Assistant Continuity."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Callable

from bridge_conversation_memory import add_memory, resolve_thread
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


def create_candidates_from_plan(
    conn: sqlite3.Connection,
    *,
    legacy_user_id: str,
    message: str,
    interaction_plan: dict | None,
    source: str = "",
    group: dict | None = None,
) -> list[dict]:
    """Persist planner proposals as pending records, never as active memory."""

    text = " ".join(str(message or "").split()).strip()
    proposals = list((interaction_plan or {}).get("memory_candidates") or [])
    if not text or not proposals or any(hint in text.lower() for hint in SENSITIVE_HINTS):
        return []
    assistant_id, owner_actor_id = _active_assistant(conn)
    thread = resolve_thread(conn, legacy_user_id, source=source)
    if group:
        scope_type = "qq_group"
        group_id = str(group.get("group_id") or "").removeprefix("group:")
        scope_id = str(thread.get("external_thread_ref") or (f"group:{group_id}" if group_id else ""))
        subject_actor_ref = str(group.get("sender_id") or thread.get("subject_actor_ref") or "")
    else:
        scope_type = "thread"
        scope_id = str(thread["id"])
        subject_actor_ref = str(thread.get("subject_actor_ref") or legacy_user_id)
    if not scope_id:
        return []

    now = utc_now()
    created = []
    confidence = max(0.0, min(float((interaction_plan or {}).get("confidence") or 0.68), 1.0))
    for proposal in proposals[:20]:
        kind = _candidate_kind(text, proposal.get("kind"))
        existing_memory = conn.execute(
            """SELECT id FROM memory_records
            WHERE status='active' AND scope_type=? AND scope_id=? AND kind=? AND lower(content)=lower(?)
            ORDER BY updated_at DESC LIMIT 1""",
            (scope_type, scope_id, kind, text),
        ).fetchone()
        duplicate = existing_memory or conn.execute(
            """SELECT id FROM memory_candidates
            WHERE assistant_id=? AND scope_type=? AND scope_id=? AND kind=? AND lower(content)=lower(?)
              AND status IN ('pending','accepted','merged')
            ORDER BY updated_at DESC LIMIT 1""",
            (assistant_id, scope_type, scope_id, kind, text),
        ).fetchone()
        conflict = conn.execute(
            """SELECT id FROM memory_records
            WHERE status='active' AND scope_type=? AND scope_id=? AND kind=? AND lower(content)<>lower(?)
            ORDER BY updated_at DESC LIMIT 1""",
            (scope_type, scope_id, kind, text),
        ).fetchone()
        candidate_id = "memory-candidate-" + uuid.uuid4().hex
        status = "merged" if duplicate else "pending"
        conn.execute(
            """INSERT INTO memory_candidates(
                id,assistant_id,owner_actor_id,subject_actor_ref,scope_type,scope_id,
                kind,content,confidence,consent_basis,source_thread_id,source_message_id,
                status,duplicate_of,conflict_with,reviewed_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id, assistant_id, owner_actor_id, subject_actor_ref, scope_type, scope_id,
                kind, text, confidence,
                "requires_user_confirmation" if proposal.get("requires_consent", True) else "planner_proposal",
                str(thread["id"]), "", status, str(duplicate[0]) if duplicate else "",
                str(conflict[0]) if conflict and not duplicate else "", "", now, now,
            ),
        )
        created.append(_row(conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone()))
    conn.commit()
    return created


def capture_plan_candidate_metadata(
    db_connect: Callable[[], sqlite3.Connection],
    *,
    explicit_memories: list[str],
    legacy_user_id: str,
    message: str,
    interaction_plan: dict | None,
    source: str = "",
    group: dict | None = None,
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
        elif scope_type == "thread":
            thread = conn.execute(
                "SELECT channel_type,external_thread_ref FROM conversation_threads WHERE id=?",
                (candidate["source_thread_id"],),
            ).fetchone()
            if not thread:
                raise ValueError("memory_candidate_thread_not_found")
            legacy_user_id = str(thread["external_thread_ref"])
            request_source = str(thread["channel_type"])
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
        )
        superseded_id = str(payload.get("supersedes_memory_id") or candidate["conflict_with"] or "").strip()
        if superseded_id:
            conn.execute(
                "UPDATE memory_records SET status='paused',updated_at=? WHERE id=? AND assistant_id=? AND status='active'",
                (utc_now(), superseded_id, assistant_id),
            )
    now = utc_now()
    conn.execute(
        "UPDATE memory_candidates SET status=?,reviewed_by=?,updated_at=? WHERE id=?",
        (decision, actor, now, candidate_id),
    )
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
    "capture_plan_candidate_metadata",
    "create_candidates_from_plan",
    "list_memory_candidates",
    "review_memory_candidate",
    "expire_stale_memories",
]
