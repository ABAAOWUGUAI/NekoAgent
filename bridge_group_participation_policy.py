#!/usr/bin/env python3
"""Deterministic AC-4 guardrails around model-based group engagement."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from bridge_conversation_participation_contract import GroupParticipationMode, group_mode_from_legacy
from bridge_conversation_participation_engine import deterministic_participation_enabled
from bridge_conversation_participation import participation_shadow_enabled
from bridge_group_participation_schema import (
    GROUP_PARTICIPATION_BUDGET_TABLE,
    NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG,
    require_group_participation_schema,
)
from bridge_delivery_continuity import unified_delivery_enabled
from bridge_migrations import utc_now


POLICY_VERSION = "group-social-action-plan-v2"


def group_participation_confidence_floor(policy: dict) -> float:
    """Map participation strength to a stable model-evidence floor."""

    try:
        strength = max(
            0.0,
            min(float(policy.get("reply_probability") or 0.2), 1.0),
        )
    except (TypeError, ValueError):
        strength = 0.2
    return max(0.35, min(0.65 - 0.30 * strength, 0.65))


def group_active_topic_window_seconds(policy: dict) -> int:
    """Bound busy-topic deferral using the existing participation-strength control."""

    try:
        strength = max(0.0, min(float(policy.get("reply_probability") or 0.2), 1.0))
    except (TypeError, ValueError):
        strength = 0.2
    return max(30, min(90, int(round(90 - 60 * strength))))


def natural_group_participation_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG,),
        ).fetchone()
    except (sqlite3.Error, AttributeError):
        return False
    return bool(row and int(row[0]))


def natural_group_cutover_plan(conn: sqlite3.Connection) -> dict:
    schema = require_group_participation_schema(conn)
    payload = {
        "feature": NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG,
        "feature_enabled": natural_group_participation_enabled(conn),
        "deterministic_enabled": deterministic_participation_enabled(conn),
        "unified_delivery_enabled": unified_delivery_enabled(conn),
        "shadow_enabled": participation_shadow_enabled(conn),
        "policy_version": POLICY_VERSION,
        "reversible": True,
        "schema_ok": bool(schema["ok"]),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {**payload, "ok": bool(schema["ok"]), "plan_checksum": hashlib.sha256(canonical.encode()).hexdigest()}


def set_natural_group_participation_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    plan = natural_group_cutover_plan(conn)
    if plan["plan_checksum"] != expect_plan_checksum:
        raise ValueError("stale_natural_group_participation_plan")
    if enabled and not plan["deterministic_enabled"]:
        raise ValueError("deterministic_participation_required")
    if enabled and not plan["unified_delivery_enabled"]:
        raise ValueError("unified_delivery_required")
    if enabled and not plan["shadow_enabled"]:
        raise ValueError("participation_shadow_required")
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (NATURAL_GROUP_PARTICIPATION_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return natural_group_cutover_plan(conn)


def _parse_time(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _day_key(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def observe_group_message(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    created_at: str,
    burst_window_seconds: int,
) -> dict:
    """Track only timing/count metadata; no message body is copied here."""

    now = _parse_time(created_at) or datetime.now(timezone.utc)
    row = conn.execute(
        f"SELECT * FROM {GROUP_PARTICIPATION_BUDGET_TABLE} WHERE group_id=?",
        (str(group_id or "").strip(),),
    ).fetchone()
    previous = _parse_time(row["burst_started_at"] if row else "") if row else None
    previous_message = _parse_time(row["last_message_at"] if row else "") if row else None
    previous_gap = (now - previous_message).total_seconds() if previous_message is not None else -1.0
    if previous is None or (now - previous).total_seconds() > max(5, int(burst_window_seconds or 12)):
        started = now
        count = 1
    else:
        started = previous
        count = int(row["burst_message_count"] or 0) + 1
    day = _day_key(now)
    conn.execute(
        f"""
        INSERT INTO {GROUP_PARTICIPATION_BUDGET_TABLE}(
            group_id,day_key,daily_reply_count,burst_started_at,burst_message_count,
            last_message_at,last_reply_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(group_id) DO UPDATE SET
            day_key=excluded.day_key,
            burst_started_at=excluded.burst_started_at,
            burst_message_count=excluded.burst_message_count,
            last_message_at=excluded.last_message_at,
            updated_at=excluded.updated_at
        """,
        (str(group_id or "").strip(), day, int(row["daily_reply_count"] or 0) if row else 0,
         started.isoformat(), count, now.isoformat(),
         str(row["last_reply_at"] or "") if row else "", utc_now()),
    )
    return {
        "day_key": day,
        "burst_message_count": count,
        "burst_started_at": started.isoformat(),
        "previous_message_gap_seconds": previous_gap,
    }


def record_group_reply(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    replied_at: str = "",
    count_towards_budget: bool = True,
) -> dict:
    """Record an ACKed group reply without spending ambient budget on direct turns.

    A direct ``@`` / quote reply must still update the last-reply time so an
    immediate unsolicited follow-up cannot cut in. It is not an *active* group
    intervention and therefore must not exhaust the ambient daily allowance.
    """
    now = _parse_time(replied_at) or datetime.now(timezone.utc)
    row = conn.execute(
        f"SELECT daily_reply_count,day_key FROM {GROUP_PARTICIPATION_BUDGET_TABLE} WHERE group_id=?",
        (str(group_id or "").strip(),),
    ).fetchone()
    count = int(row["daily_reply_count"] or 0) if row and str(row["day_key"] or "") == _day_key(now) else 0
    if count_towards_budget:
        count += 1
    conn.execute(
        f"""
        INSERT INTO {GROUP_PARTICIPATION_BUDGET_TABLE}(
            group_id,day_key,daily_reply_count,burst_started_at,burst_message_count,
            last_message_at,last_reply_at,updated_at
        ) VALUES(?,?,?,'',0,'',?,?)
        ON CONFLICT(group_id) DO UPDATE SET
            day_key=excluded.day_key,
            daily_reply_count=excluded.daily_reply_count,
            last_reply_at=excluded.last_reply_at,
            updated_at=excluded.updated_at
        """,
        (str(group_id or "").strip(), _day_key(now), count, now.isoformat(), utc_now()),
    )
    return {
        "day_key": _day_key(now),
        "daily_reply_count": count,
        "counted_towards_budget": bool(count_towards_budget),
    }


def natural_group_preflight(
    conn: sqlite3.Connection,
    *,
    policy: dict,
    group_id: str,
    current: dict,
    observation: dict | None = None,
    conversation_frame: dict | None = None,
    candidate_kind: str = "ambient",
    now: datetime | None = None,
) -> dict | None:
    """Return a deterministic silent decision for natural-participation work.

    ``group_final_action_gate`` is intentionally the shared primitive.  Natural
    participation is one caller of it; a same-member continuation must not
    become an unguarded second path merely because it was admitted by the
    deterministic conversation state machine.
    """

    if not natural_group_participation_enabled(conn):
        return None
    if group_mode_from_legacy(policy) is not GroupParticipationMode.NATURAL_PARTICIPATION:
        return None
    return group_final_action_gate(
        conn,
        policy=policy,
        group_id=group_id,
        current=current,
        observation=observation,
        conversation_frame=conversation_frame,
        candidate_kind=candidate_kind,
        now=now,
    )


def group_final_action_gate(
    conn: sqlite3.Connection,
    *,
    policy: dict,
    group_id: str,
    current: dict,
    observation: dict | None = None,
    conversation_frame: dict | None = None,
    candidate_kind: str = "ambient",
    directed: bool = False,
    now: datetime | None = None,
) -> dict | None:
    """Apply one server-owned rhythm gate to every uninvited group action.

    Explicit ``@`` and reply-to-assistant turns remain direct conversations.
    All other actions, including a continuation after the assistant has asked a
    question, must pass the same no-content timing, density and budget checks.
    This function does not decide conversational value; it only fail-closes a
    candidate that the interaction layer already proposed.
    """

    if directed or bool(current.get("is_mention")) or bool(current.get("reply_to_assistant")):
        return None
    current_time = now or datetime.now(timezone.utc)
    is_continuation = str(candidate_kind or "") == "continuation"
    frame = conversation_frame or {}
    if is_continuation:
        try:
            max_auto_continuations = max(
                1,
                min(int(policy.get("max_auto_continuations") or 2), 3),
            )
        except (TypeError, ValueError):
            max_auto_continuations = 2
        try:
            continuation_turns = max(0, int(frame.get("continuation_assistant_turns") or 0))
        except (TypeError, ValueError):
            continuation_turns = 0
        if continuation_turns >= max_auto_continuations:
            return {
                "should_reply": False,
                "reason": "auto_continuation_limit",
                "policy_version": POLICY_VERSION,
                "candidate_kind": "continuation",
            }
    row = conn.execute(
        f"SELECT * FROM {GROUP_PARTICIPATION_BUDGET_TABLE} WHERE group_id=?",
        (str(group_id or "").strip(),),
    ).fetchone()
    if row:
        observed = observation or {}
        gap = float(observed.get("previous_message_gap_seconds", -1) or -1)
        quiet_gap_raw = policy.get("quiet_gap_seconds")
        quiet_gap = max(0, int(8 if quiet_gap_raw in {None, ""} else quiet_gap_raw))
        if quiet_gap and 0 <= gap < quiet_gap:
            return {
                "should_reply": False,
                "reason": "continuation_min_gap" if is_continuation else "quiet_gap",
                "policy_version": POLICY_VERSION,
                "candidate_kind": candidate_kind,
            }
        day = _day_key(current_time)
        daily = int(row["daily_reply_count"] or 0) if str(row["day_key"] or "") == day else 0
        budget_raw = policy.get("daily_reply_budget")
        budget = max(0, int(20 if budget_raw in {None, ""} else budget_raw))
        if budget and daily >= budget:
            return {"should_reply": False, "reason": "daily_reply_budget", "policy_version": POLICY_VERSION}
        burst_max = max(2, int(policy.get("burst_max_messages") or 6))
        burst_count = int(row["burst_message_count"] or 0)
        if burst_count >= burst_max:
            return {"should_reply": False, "reason": "burst_coalescing", "policy_version": POLICY_VERSION}
        last_reply = _parse_time(row["last_reply_at"])
        cooldown_raw = policy.get("cooldown_seconds")
        cooldown = max(15, int(180 if cooldown_raw in {None, ""} else cooldown_raw))
        if is_continuation and last_reply is not None and quiet_gap:
            if (current_time - last_reply).total_seconds() < quiet_gap:
                return {
                    "should_reply": False,
                    "reason": "continuation_min_gap",
                    "policy_version": POLICY_VERSION,
                    "candidate_kind": "continuation",
                }
        if (
            last_reply is not None
            and (current_time - last_reply).total_seconds() < cooldown
            and not is_continuation
        ):
            return {"should_reply": False, "reason": "cooldown", "policy_version": POLICY_VERSION}
    return None


__all__ = [
    "POLICY_VERSION",
    "group_active_topic_window_seconds",
    "group_participation_confidence_floor",
    "group_final_action_gate",
    "natural_group_cutover_plan",
    "natural_group_participation_enabled",
    "natural_group_preflight",
    "observe_group_message",
    "record_group_reply",
    "set_natural_group_participation_feature",
]
