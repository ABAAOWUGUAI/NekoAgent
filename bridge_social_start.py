#!/usr/bin/env python3
"""Evidence assembly and fail-closed adjudication for social topic starts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_group_context_frame import normalize_group_visible_text
from bridge_knowledge_service import search_published
from bridge_social_opportunity import (
    add_topic_candidate,
    create_opportunity,
    decide_opportunity,
)


def _clip(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _load(value: object, fallback):
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
    ).fetchone()
    if not row:
        raise ValueError("active_assistant_missing")
    return str(row[0])


def _group_relationship_projection(
    conn: sqlite3.Connection,
    *,
    group_id: str,
) -> dict:
    """Derive bounded group familiarity without copying member-private state."""

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN sender_id<>'bot' THEN 1 ELSE 0 END) AS member_messages,
            SUM(CASE WHEN sender_id='bot' THEN 1 ELSE 0 END) AS assistant_turns,
            COUNT(DISTINCT CASE WHEN sender_id<>'bot' THEN sender_id END) AS participants,
            MAX(created_at) AS last_activity_at
        FROM group_messages
        WHERE group_id=? AND created_at>=?
        """,
        (group_id, cutoff),
    ).fetchone()
    item = dict(row) if row else {}
    member_messages = int(item.get("member_messages") or 0)
    assistant_turns = int(item.get("assistant_turns") or 0)
    participants = int(item.get("participants") or 0)
    if member_messages >= 30 and assistant_turns >= 3:
        familiarity = "established"
    elif member_messages >= 10 and assistant_turns >= 1:
        familiarity = "familiar"
    else:
        familiarity = "new"
    if member_messages >= 50 or participants >= 5:
        style = "lively"
    elif member_messages >= 10:
        style = "conversational"
    else:
        style = "natural"
    return {
        "preferred_address": "",
        "interaction_style": style,
        "familiarity_context": familiarity,
        "blocked_topics": [],
        "version": 0,
        "projection": "group_participation_30d",
        "member_messages": member_messages,
        "assistant_turns": assistant_turns,
        "participant_count": participants,
        "last_activity_at": str(item.get("last_activity_at") or ""),
    }


def prepare_start_opportunity(
    conn: sqlite3.Connection,
    policy: dict,
    *,
    history: list[dict],
    memories: list[dict],
) -> dict:
    assistant_id = _clip(policy.get("assistant_id"), 80) or _active_assistant_id(conn)
    user_id = _clip(policy.get("user_id"), 80)
    if not user_id:
        raise ValueError("social_start_user_required")
    is_group = (
        _clip(policy.get("policy_kind"), 40) == "group_social"
        and user_id.startswith("group:")
        and bool(user_id[6:])
    )
    subject_type = "qq_group" if is_group else "private_user"
    subject_id = user_id[6:] if is_group else user_id
    thread_id = f"qq:group:{subject_id}" if is_group else f"qq:private:{subject_id}"
    relationship_row = None if is_group else conn.execute(
        """SELECT * FROM relationship_states
           WHERE assistant_id=? AND user_id=? AND scope_type='private_user'
           ORDER BY updated_at DESC LIMIT 1""",
        (assistant_id, user_id),
    ).fetchone()
    relationship = (
        _group_relationship_projection(conn, group_id=subject_id)
        if is_group
        else (dict(relationship_row) if relationship_row else {})
    )
    allowed_topics = _load(relationship.get("allowed_topics_json"), [])
    blocked_topics = [_clip(item, 120) for item in _load(relationship.get("blocked_topics_json"), [])]
    seed = f"{assistant_id}|{subject_type}|{subject_id}|{policy.get('next_check_at') or ''}|{policy.get('policy_version') or 1}"
    opportunity_id = "start-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    opportunity = create_opportunity(
        conn,
        assistant_id=assistant_id,
        kind="start",
        subject_type=subject_type,
        subject_id=subject_id,
        thread_id=thread_id,
        trigger_type="authorized_social_timer",
        trigger_ref=str(policy.get("next_check_at") or ""),
        policy_snapshot={
            "policy_version": int(policy.get("policy_version") or 1),
            "initiative_mode": str(policy.get("initiative_mode") or "balanced"),
            "allowed_intents": [item for item in str(policy.get("allowed_intents") or "").split(",") if item],
            "consecutive_unanswered": int(policy.get("consecutive_unanswered") or 0),
        },
        relationship_version=int(relationship.get("version") or 0),
        opportunity_id=opportunity_id,
    )

    def blocked(text: str) -> bool:
        return any(topic and topic in text for topic in blocked_topics)

    candidates = []
    recent_user = next(
        (item for item in reversed(history) if item.get("role") != "assistant" and _clip(item.get("content"), 800)),
        None,
    )
    if recent_user:
        summary = _clip(
            normalize_group_visible_text(recent_user.get("content"))
            if is_group
            else recent_user.get("content"),
            300,
        )
        if not blocked(summary):
            candidates.append(add_topic_candidate(conn, opportunity["id"], {
                "source_type": "conversation",
                "source_id": _clip(recent_user.get("id") or recent_user.get("created_at") or "recent", 240),
                "scope_type": subject_type, "scope_id": subject_id, "summary": summary,
                "freshness": _clip(recent_user.get("created_at"), 80),
                "why_relevant": (
                    "最近群聊中仍有可自然延续或重新提起的共同话题"
                    if is_group
                    else "最近对话中仍有可自然延续的用户话题"
                ),
                "risk": "low",
            }))
    for memory in ([] if is_group else memories[:4]):
        summary = _clip(memory.get("content"), 300)
        if not summary or blocked(summary) or str(memory.get("sensitivity") or "") == "sensitive":
            continue
        candidates.append(add_topic_candidate(conn, opportunity["id"], {
            "source_type": "memory", "source_id": _clip(memory.get("id"), 240),
            "scope_type": "private_user", "scope_id": user_id, "summary": summary,
            "freshness": _clip(memory.get("updated_at") or memory.get("created_at"), 80),
            "why_relevant": "当前私聊作用域可见且非敏感的长期记忆", "risk": "medium",
        }))
    query = _clip(recent_user.get("content"), 240) if recent_user else ""
    if query:
        for item in search_published(
            conn,
            query,
            channel="group" if is_group else "private",
            limit=3,
        ):
            summary = _clip(item.get("summary") or item.get("title") or item.get("content"), 300)
            if not summary or blocked(summary):
                continue
            candidates.append(add_topic_candidate(conn, opportunity["id"], {
                "source_type": "knowledge", "source_id": _clip(item.get("id"), 240),
                "scope_type": subject_type, "scope_id": subject_id, "summary": summary,
                "freshness": _clip(item.get("effective_freshness") or item.get("updated_at"), 80),
                "why_relevant": "已发布且受众允许私聊使用的知识", "risk": "low",
            }))
    for index, topic in enumerate([] if is_group else allowed_topics[:3]):
        summary = _clip(topic, 300)
        if summary and not blocked(summary):
            candidates.append(add_topic_candidate(conn, opportunity["id"], {
                "source_type": "relationship", "source_id": f"relationship:{relationship.get('id') or user_id}:{index}",
                "scope_type": "private_user", "scope_id": user_id, "summary": summary,
                "freshness": _clip(relationship.get("updated_at"), 80),
                "why_relevant": "管理员在当前关系作用域明确允许的话题方向", "risk": "medium",
            }))
    topic_notes = _clip(policy.get("topic_notes"), 300)
    if topic_notes and not blocked(topic_notes):
        candidates.append(add_topic_candidate(conn, opportunity["id"], {
            "source_type": "relationship",
            "source_id": f"proactive-policy:{subject_type}:{subject_id}",
            "scope_type": subject_type,
            "scope_id": subject_id,
            "summary": topic_notes,
            "freshness": "" if is_group else _clip(policy.get("updated_at"), 80),
            "why_relevant": (
                "当前群主动策略允许的话题方向；这是方向约束，不是新鲜话题事实"
                if is_group
                else "当前主动社交策略明确允许的话题方向"
            ),
            "risk": "medium",
        }))
    return {
        "opportunity": opportunity,
        "candidates": candidates,
        "relationship": {
            "preferred_address": relationship.get("preferred_address") or "",
            "interaction_style": relationship.get("interaction_style") or "natural",
            "familiarity_context": relationship.get("familiarity_context") or "new",
            "blocked_topics": blocked_topics,
            "version": int(relationship.get("version") or 0),
            **(
                {
                    "projection": relationship.get("projection") or "",
                    "member_messages": int(relationship.get("member_messages") or 0),
                    "assistant_turns": int(relationship.get("assistant_turns") or 0),
                    "participant_count": int(relationship.get("participant_count") or 0),
                    "last_activity_at": relationship.get("last_activity_at") or "",
                }
                if is_group
                else {}
            ),
        },
        "subject_type": subject_type,
        "subject_id": subject_id,
    }


def finalize_start_decision(conn: sqlite3.Connection, prepared: dict, model_value: dict) -> dict:
    opportunity_id = prepared["opportunity"]["id"]
    requested_send = str(model_value.get("action") or "").lower() == "send"
    if not prepared["candidates"]:
        requested_send = False
        model_value = {**model_value, "reason": "no_authorized_topic_candidate"}
    if not requested_send:
        social = decide_opportunity(conn, opportunity_id, {
            "action": "silent", "reason_code": model_value.get("reason") or "default_silent",
            "confidence": model_value.get("confidence", 1.0),
        })
        return {
            "action": "skip", "intent": "silence", "reason": social["reason_code"],
            "message": "", "topic_key": "", "next_check_minutes": model_value.get("next_check_minutes", 60),
            **social,
        }
    social = decide_opportunity(conn, opportunity_id, {
        "action": "reply", "reason_code": model_value.get("reason") or "authorized_topic_now",
        "why_now": model_value.get("why_now"), "topic_candidate_id": model_value.get("topic_candidate_id"),
        "approach": model_value.get("approach"), "confidence": model_value.get("confidence", 0.5),
        "meme_intent": model_value.get("meme_intent"),
    })
    return {
        "action": "send", "intent": _clip(model_value.get("intent"), 40),
        "reason": social["reason_code"], "message": _clip(model_value.get("message"), 600),
        "topic_key": social["topic_candidate_id"],
        "next_check_minutes": model_value.get("next_check_minutes", 60),
        **social,
    }


def finalize_start_failure(
    conn: sqlite3.Connection,
    prepared: dict,
    error_kind: str,
) -> dict:
    """Close a fail-closed start opportunity when no model decision exists."""

    return decide_opportunity(
        conn,
        prepared["opportunity"]["id"],
        {
            "action": "silent",
            "reason_code": f"decision_unavailable:{_clip(error_kind, 80)}",
            "confidence": 0,
        },
    )


def reconcile_stale_start_opportunities(
    conn: sqlite3.Connection,
    *,
    max_age_minutes: int = 30,
) -> int:
    """Close opportunities orphaned by a crash or an older failed runtime."""

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=max(5, min(int(max_age_minutes or 30), 1440)))
    ).isoformat()
    rows = conn.execute(
        """
        SELECT id FROM social_opportunities
        WHERE kind='start' AND status='open' AND created_at<=?
        ORDER BY created_at
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        decide_opportunity(
            conn,
            str(row[0]),
            {
                "action": "silent",
                "reason_code": "decision_unavailable:stale_open",
                "confidence": 0,
            },
        )
    return len(rows)


__all__ = [
    "finalize_start_decision",
    "finalize_start_failure",
    "prepare_start_opportunity",
    "reconcile_stale_start_opportunities",
]
