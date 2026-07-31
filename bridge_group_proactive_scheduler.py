#!/usr/bin/env python3
"""Channel-subject projection for the bounded proactive scheduler.

This module keeps QQ Owner/group discovery and group-aware query construction
out of the generic automation scheduler. Messaging policy remains the
authorization source; ``proactive_policies`` only stores scheduling state.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from bridge_automation_schema import ensure_automation_tables
from bridge_migrations import utc_now
from bridge_proactive_messaging_policy import policy_gate_if_present

GROUP_DORMANT_AFTER_MINUTES = 72 * 60
GROUP_DORMANT_RECHECK_MINUTES = 24 * 60


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _parse_timestamp(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def dormant_group_next_check(
    *,
    last_user: datetime | None,
    current: datetime,
) -> datetime | None:
    if not last_user:
        return None
    if current - last_user < timedelta(minutes=GROUP_DORMANT_AFTER_MINUTES):
        return None
    return current + timedelta(minutes=GROUP_DORMANT_RECHECK_MINUTES)


def _wake_group_policy_from_activity(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    group_id: str,
) -> None:
    policy = conn.execute(
        """
        SELECT last_user_at,min_silence_minutes
        FROM proactive_policies WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()
    latest = conn.execute(
        """
        SELECT MAX(created_at) FROM group_messages
        WHERE group_id=? AND sender_id<>'bot'
        """,
        (group_id,),
    ).fetchone()
    latest_at = _parse_timestamp(latest[0] if latest else "")
    saved_at = _parse_timestamp(policy[0] if policy else "")
    if not policy or not latest_at or (saved_at and latest_at <= saved_at):
        return
    next_check = latest_at + timedelta(minutes=max(15, int(policy[1] or 120)))
    conn.execute(
        """
        UPDATE proactive_policies
        SET last_user_at=?,next_check_at=?,state='waiting_silence',
            state_reason='new_group_activity',consecutive_unanswered=0,updated_at=?
        WHERE user_id=?
        """,
        (latest_at.isoformat(), next_check.isoformat(), utc_now(), user_id),
    )


def _sync_messaging_limits(
    conn: sqlite3.Connection,
    user_id: str,
    messaging_policy: dict,
) -> None:
    conn.execute(
        """
        UPDATE proactive_policies
        SET quiet_start=?,quiet_end=?,daily_limit=?,weekly_limit=?,
            unanswered_limit=?,updated_at=?
        WHERE user_id=?
        """,
        (
            _clip(messaging_policy.get("quiet_start") or "23:00", 5),
            _clip(messaging_policy.get("quiet_end") or "08:00", 5),
            max(1, int(messaging_policy.get("daily_limit") or 1)),
            max(1, int(messaging_policy.get("weekly_limit") or 1)),
            max(1, int(messaging_policy.get("unanswered_limit") or 1)),
            utc_now(),
            user_id,
        ),
    )


def persist_subject_metadata(
    conn: sqlite3.Connection,
    *,
    payload: dict,
    existing: dict,
    user_id: str,
) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(proactive_policies)").fetchall()
    }
    if not {"assistant_id", "policy_kind"}.issubset(columns):
        return
    policy_kind = _clip(
        payload.get("policy_kind") or existing.get("policy_kind") or "social",
        40,
    )
    if policy_kind not in {"social", "group_social"}:
        raise ValueError("invalid_proactive_policy_kind")
    conn.execute(
        """
        UPDATE proactive_policies
        SET assistant_id=?,policy_kind=?
        WHERE user_id=?
        """,
        (
            _clip(payload.get("assistant_id") or existing.get("assistant_id"), 80),
            policy_kind,
            user_id,
        ),
    )


def proactive_due_query(conn: sqlite3.Connection) -> str:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    }
    if not {"group_policies", "group_messages"}.issubset(tables):
        return """SELECT p.*, COALESCE(s.session, '') AS send_session,
                  COALESCE((SELECT MAX(created_at) FROM conversations c
                            WHERE c.user_id=p.user_id AND c.role='user'), '') AS observed_user_at
           FROM proactive_policies p LEFT JOIN qq_sessions s ON s.user_id=p.user_id
           WHERE p.enabled=1 AND p.authorized=1 AND p.next_check_at<=?
             AND (p.lease_until='' OR p.lease_until<=?)
           ORDER BY p.next_check_at ASC LIMIT 30"""
    return """SELECT p.*,
                  CASE
                    WHEN p.policy_kind='group_social' THEN COALESCE(gp.session,'')
                    ELSE COALESCE(s.session,'')
                  END AS send_session,
                  CASE
                    WHEN p.policy_kind='group_social' THEN COALESCE(
                      (SELECT MAX(gm.created_at) FROM group_messages gm
                       WHERE gm.group_id=substr(p.user_id,7) AND gm.sender_id<>'bot'),'')
                    ELSE COALESCE(
                      (SELECT MAX(created_at) FROM conversations c
                       WHERE c.user_id=p.user_id AND c.role='user'),'')
                  END AS observed_user_at
           FROM proactive_policies p
           LEFT JOIN qq_sessions s ON s.user_id=p.user_id
           LEFT JOIN group_policies gp
             ON p.policy_kind='group_social'
            AND p.user_id=('group:' || gp.group_id)
           WHERE p.enabled=1 AND p.authorized=1 AND p.next_check_at<=?
             AND (p.lease_until='' OR p.lease_until<=?)
           ORDER BY p.next_check_at ASC LIMIT 30"""


def reconcile_group_proactive_policies(
    conn: sqlite3.Connection,
    *,
    upsert_policy: Callable[[sqlite3.Connection, dict], dict],
) -> int:
    """Create scheduler state only for groups already allowed by policy."""

    ensure_automation_tables(conn)
    required = {"group_policies", "proactive_messaging_policies", "assistant_instances"}
    available = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    }
    if not required.issubset(available):
        return 0
    assistant = conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
    ).fetchone()
    if not assistant:
        return 0
    created = 0
    groups = conn.execute(
        """
        SELECT group_id FROM group_policies
        WHERE enabled=1 AND participation_mode='natural_participation'
          AND session<>''
        """,
    ).fetchall()
    for row in groups:
        group_id = _clip(row[0], 80)
        policy_key = f"group:{group_id}"
        if not group_id:
            continue
        gate = policy_gate_if_present(conn, policy_key)
        if not gate or not gate.get("allowed"):
            continue
        messaging_policy = gate["policy"]
        if conn.execute(
            "SELECT 1 FROM proactive_policies WHERE user_id=?",
            (policy_key,),
        ).fetchone():
            _sync_messaging_limits(conn, policy_key, messaging_policy)
            _wake_group_policy_from_activity(
                conn,
                user_id=policy_key,
                group_id=group_id,
            )
            continue
        upsert_policy(
            conn,
            {
                "user_id": policy_key,
                "assistant_id": str(assistant[0]),
                "policy_kind": "group_social",
                "enabled": True,
                "authorized": True,
                "min_silence_minutes": 120,
                "min_gap_minutes": 720,
                "quiet_start": messaging_policy["quiet_start"],
                "quiet_end": messaging_policy["quiet_end"],
                "daily_limit": messaging_policy["daily_limit"],
                "weekly_limit": messaging_policy["weekly_limit"],
                "unanswered_limit": messaging_policy["unanswered_limit"],
                "evaluation_interval_minutes": 120,
                "topic_cooldown_minutes": 1440,
                "schedule_jitter_minutes": 30,
                "initiative_mode": "balanced",
                "allowed_intents": ["share", "check_in"],
                "topic_notes": "群内共同兴趣、最近讨论的自然延续、轻松且不要求回应的开放话题",
            },
        )
        created += 1
    return created


def reconcile_owner_proactive_policy(
    conn: sqlite3.Connection,
    *,
    upsert_policy: Callable[[sqlite3.Connection, dict], dict],
) -> int:
    """Create Owner scheduling state for the active QQ super-admin session."""

    ensure_automation_tables(conn)
    required = {
        "assistant_instances",
        "proactive_messaging_policies",
        "qq_identities",
        "qq_role_assignments",
        "qq_sessions",
    }
    available = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    }
    if not required.issubset(available):
        return 0
    assistant = conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
    ).fetchone()
    owner = conn.execute(
        """
        SELECT i.qq_id
        FROM qq_identities i
        JOIN qq_role_assignments r ON r.identity_id=i.id
        JOIN qq_sessions s ON s.user_id=i.qq_id
        WHERE i.status='active' AND r.role='super_admin' AND r.enabled=1
          AND s.session<>''
        ORDER BY i.updated_at DESC
        LIMIT 1
        """,
    ).fetchone()
    if not assistant or not owner:
        return 0
    user_id = _clip(owner[0], 80)
    gate = policy_gate_if_present(conn, user_id)
    if not user_id or not gate or not gate.get("allowed"):
        return 0
    messaging_policy = gate["policy"]
    if conn.execute(
        "SELECT 1 FROM proactive_policies WHERE user_id=?",
        (user_id,),
    ).fetchone():
        _sync_messaging_limits(conn, user_id, messaging_policy)
        return 0
    upsert_policy(
        conn,
        {
            "user_id": user_id,
            "assistant_id": str(assistant[0]),
            "policy_kind": "social",
            "enabled": True,
            "authorized": True,
            "min_silence_minutes": 180,
            "min_gap_minutes": 360,
            "quiet_start": messaging_policy["quiet_start"],
            "quiet_end": messaging_policy["quiet_end"],
            "daily_limit": messaging_policy["daily_limit"],
            "weekly_limit": messaging_policy["weekly_limit"],
            "unanswered_limit": messaging_policy["unanswered_limit"],
            "evaluation_interval_minutes": 60,
            "topic_cooldown_minutes": 1440,
            "schedule_jitter_minutes": 20,
            "initiative_mode": "balanced",
            "allowed_intents": ["follow_up", "share", "check_in", "celebrate"],
            "topic_notes": "自然延续最近对话、轻量分享共同兴趣、适度关心近况；没有真实切入点就保持安静",
        },
    )
    return 1


__all__ = [
    "dormant_group_next_check",
    "persist_subject_metadata",
    "proactive_due_query",
    "reconcile_group_proactive_policies",
    "reconcile_owner_proactive_policy",
]
