#!/usr/bin/env python3
"""Unified AC-5 policy checks for proactive and operational deliveries.

Interactive replies are deliberately outside this policy layer. A delivery is
policy-controlled only when its payload is a social initiative
(``proactive_chat``) or declares an explicit ``notification_category``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bridge_group_participation_windows import ambient_participation_window_decision
from bridge_group_state import group_gate
import re
from bridge_group_topic_delivery import evaluate_topic_context, evaluate_topic_delivery
from bridge_relationship_service import get_notification_policy, get_social_proactive_policy
from bridge_proactive_messaging_policy import policy_gate_if_present
from bridge_social_start import owner_social_start_preflight

UTC = timezone.utc
TRUE_VALUES = {"1", "true", "yes", "on"}
CRITICAL_NOTIFICATION_CATEGORIES = {"security", "resource"}


class DeliveryPolicyBlockedError(RuntimeError):
    """Raised after a claimed delivery has been safely cancelled or deferred."""

    def __init__(self, decision: dict):
        self.decision = dict(decision)
        self.reason = str(decision.get("reason") or "outbound_policy_blocked")
        self.action = str(decision.get("action") or "cancel")
        super().__init__(f"delivery_policy_{self.action}:{self.reason}")


def _utc(value=None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value=None) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _payload(delivery: dict) -> dict:
    value = delivery.get("payload")
    return value if isinstance(value, dict) else {}


def _parse_clock(value: object, default: str) -> tuple[int, int]:
    text = str(value or default).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        hour, minute = (23, 30) if default == "23:30" else (9, 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        hour, minute = (23, 30) if default == "23:30" else (9, 0)
    return hour, minute


def _zone(value: object):
    try:
        return ZoneInfo(str(value or "Asia/Shanghai").strip())
    except ZoneInfoNotFoundError:
        name = str(value or "Asia/Shanghai").strip()
        if name in {"UTC", "Etc/UTC"}:
            return UTC
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        return UTC


def _quiet_end(current: datetime, policy: dict, *, timezone_name: str) -> datetime | None:
    zone = _zone(timezone_name)
    local = current.astimezone(zone)
    start_hour, start_minute = _parse_clock(policy.get("quiet_start"), "23:30")
    end_hour, end_minute = _parse_clock(policy.get("quiet_end"), "09:00")
    start_value = start_hour * 60 + start_minute
    end_value = end_hour * 60 + end_minute
    minute = local.hour * 60 + local.minute
    if start_value == end_value:
        return None
    inside = (
        start_value <= minute < end_value
        if start_value < end_value
        else minute >= start_value or minute < end_value
    )
    if not inside:
        return None
    target = local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if start_value >= end_value and minute >= start_value:
        target += timedelta(days=1)
    return target.astimezone(UTC)


def social_proactive_globally_enabled(settings_or_conn) -> bool:
    if isinstance(settings_or_conn, dict):
        return _truthy(settings_or_conn.get("proactive_enabled"))
    row = settings_or_conn.execute(
        "SELECT value FROM settings WHERE key='proactive_enabled'",
    ).fetchone()
    return bool(row and _truthy(row[0]))


def is_policy_controlled(delivery: dict) -> bool:
    payload = _payload(delivery)
    return bool(_delivery_group(delivery)) or bool(payload.get("ambient_participation")) or str(delivery.get("delivery_class") or "") == "topic_social" or payload.get("kind") == "proactive_chat" or bool(
        str(payload.get("notification_category") or "").strip(),
    )


def _delivery_group(delivery):
    payload = _payload(delivery)
    session = str(payload.get("send_session") or "")
    if ":FriendMessage:" in session:
        return ""
    match = re.search(r":GroupMessage:([1-9][0-9]*)$", session)
    return match.group(1) if match else str(payload.get("group_id") or "")


def _social_policy_user_id(payload: dict) -> str:
    user_id = str(payload.get("user_id") or "").strip()
    group_id = str(payload.get("group_id") or "").strip()
    session = str(payload.get("send_session") or "").strip()
    is_group = str(payload.get("scope") or "").strip() == "group" or bool(group_id) or ":GroupMessage:" in session
    if not is_group:
        return user_id
    target = group_id or user_id
    if target.startswith("group:"):
        target = target[6:]
    return f"group:{target}" if target else ""


def _allow(kind: str = "interactive") -> dict:
    return {"action": "allow", "reason": "policy_not_applicable", "policy_kind": kind}


def _social_decision(conn: sqlite3.Connection, delivery: dict, current: datetime) -> dict:
    payload = _payload(delivery)
    user_id = _social_policy_user_id(payload)
    event_id = str(payload.get("proactive_event_id") or "").strip()
    if not social_proactive_globally_enabled(conn):
        return {"action": "cancel", "reason": "global_social_proactive_disabled", "policy_kind": "social"}
    if not user_id:
        return {"action": "cancel", "reason": "social_user_missing", "policy_kind": "social"}
    policy = get_social_proactive_policy(conn, user_id=user_id)
    if not policy.get("enabled"):
        return {"action": "cancel", "reason": "social_policy_disabled", "policy_kind": "social"}
    if not policy.get("authorized"):
        return {"action": "cancel", "reason": "social_policy_not_authorized", "policy_kind": "social"}
    if user_id.startswith("group:"):
        group_id = user_id[6:]
        try:
            group_policy = conn.execute(
                """SELECT enabled,participation_mode,session
                   FROM group_policies WHERE group_id=?""",
                (group_id,),
            ).fetchone()
        except sqlite3.Error:
            group_policy = None
        if not group_policy or not bool(group_policy[0]) or str(group_policy[1] or "") != "natural_participation" or not str(group_policy[2] or "").strip():
            return {"action": "cancel", "reason": "group_participation_ineligible", "policy_kind": "social"}
        if str(group_policy[2]).strip() != str(payload.get("send_session") or "").strip():
            return {"action": "cancel", "reason": "group_session_stale", "policy_kind": "social"}
    event = (
        conn.execute("SELECT * FROM proactive_events WHERE id=?", (event_id,)).fetchone()
        if event_id else None
    )
    if event is None:
        return {"action": "cancel", "reason": "proactive_event_missing", "policy_kind": "social"}
    event = dict(event)
    if event.get("delivered_at"):
        return {"action": "cancel", "reason": "proactive_event_already_delivered", "policy_kind": "social"}
    if event.get("error") or event.get("blocked_reason"):
        return {"action": "cancel", "reason": "proactive_event_not_sendable", "policy_kind": "social"}
    if str(event.get("user_id") or "") != user_id:
        return {"action": "cancel", "reason": "proactive_event_subject_stale", "policy_kind": "social"}
    event_assistant = str(event.get("assistant_id") or "").strip()
    event_kind = str(event.get("policy_kind") or policy.get("policy_kind") or "social").strip()
    if event_assistant and event_assistant != str(policy.get("assistant_id") or "").strip():
        return {"action": "cancel", "reason": "assistant_policy_stale", "policy_kind": "social"}
    try:
        event_version = int(event.get("policy_version") or 0)
        policy_version = int(policy.get("policy_version") or 0)
    except (TypeError, ValueError):
        event_version = policy_version = -1
    if event_version > 0 and policy_version > 0 and event_version != policy_version:
        return {"action": "cancel", "reason": "social_policy_stale", "policy_kind": "social"}
    if event_kind != str(policy.get("policy_kind") or event_kind).strip():
        return {"action": "cancel", "reason": "social_policy_kind_stale", "policy_kind": "social"}
    # A social event may remain queued while its per-target messaging policy
    # is changed.  Re-read that exact private scope immediately before the
    # current authorization/preflight gate so an old queued event cannot send
    # after its contact has been switched off or moved to review.
    if not user_id.startswith("group:"):
        # Use the same feature-gated final mode check as the scheduler.  This
        # retains the pre-v2 legacy social path until its policy feature is
        # explicitly enabled, then prevents any queued event from bypassing a
        # current explicit auto/off/review policy.
        messaging_gate = policy_gate_if_present(conn, user_id)
        # No policy row means this delivery belongs to the pre-policy legacy
        # path.  Preserve that path; once a specific or inherited policy exists,
        # its current mode becomes a hard final delivery boundary.
        message_policy = dict((messaging_gate or {}).get("policy") or {})
        if messaging_gate is not None and str(message_policy.get("id") or ""):
            if not messaging_gate.get("allowed"):
                gate_reason = str(messaging_gate.get("reason") or "disabled")[:120]
                reason = "messaging_policy_disabled" if gate_reason == "policy_disabled" else f"messaging_policy_{gate_reason}"
                return {
                    "action": "cancel",
                    "reason": reason,
                    "policy_kind": "social",
                }
            if not messaging_gate.get("send_allowed"):
                return {
                    "action": "cancel",
                    "reason": "messaging_policy_requires_review",
                    "policy_kind": "social",
                }
    preflight = owner_social_start_preflight(conn, {
        "user_id": user_id,
        "assistant_id": event_assistant or str(policy.get("assistant_id") or ""),
        "policy_kind": event_kind,
        "enabled": policy.get("enabled"),
        "authorized": policy.get("authorized"),
        "send_session": payload.get("send_session"),
    })
    if not preflight.get("allowed"):
        return {
            "action": "cancel",
            "reason": str(preflight.get("reason") or "social_start_preflight_denied")[:180],
            "policy_kind": "social",
        }
    if user_id.startswith("group:"):
        latest_user = conn.execute(
            "SELECT MAX(created_at) FROM group_messages WHERE group_id=? AND sender_id<>'bot'",
            (user_id[6:],),
        ).fetchone()[0]
    else:
        latest_user = conn.execute(
            "SELECT MAX(created_at) FROM conversations WHERE user_id=? AND role='user'",
            (user_id,),
        ).fetchone()[0]
        policy_activity = str(policy.get("last_user_at") or "").strip()
        if policy_activity and (not latest_user or _utc(policy_activity) > _utc(latest_user)):
            latest_user = policy_activity
    if latest_user and _utc(latest_user) > _utc(event.get("decision_at")):
        return {"action": "cancel", "reason": "user_became_active", "policy_kind": "social"}
    if int(policy.get("consecutive_unanswered") or 0) >= int(policy.get("unanswered_limit") or 2):
        return {"action": "cancel", "reason": "social_unanswered_limit", "policy_kind": "social"}
    quiet_until = _quiet_end(
        current,
        policy,
        timezone_name=str(policy.get("timezone") or "Asia/Shanghai"),
    )
    if quiet_until:
        return {
            "action": "defer", "reason": "social_quiet_hours", "policy_kind": "social",
            "available_at": _timestamp(quiet_until),
        }
    return {"action": "allow", "reason": "social_policy_passed", "policy_kind": "social"}


def _notification_decision(conn: sqlite3.Connection, delivery: dict, current: datetime) -> dict:
    payload = _payload(delivery)
    category = str(payload.get("notification_category") or "").strip()
    user_id = str(payload.get("actor_id") or payload.get("user_id") or "").strip()
    if not category:
        return _allow()
    if not user_id:
        return {"action": "cancel", "reason": "notification_user_missing", "policy_kind": "operational"}
    policy = get_notification_policy(conn, user_id=user_id)
    if category not in set(policy.get("enabled_categories") or []):
        return {
            "action": "cancel", "reason": f"notification_category_disabled:{category}"[:180],
            "policy_kind": "operational", "category": category,
        }
    quiet_until = _quiet_end(current, policy, timezone_name=str(payload.get("timezone") or "Asia/Shanghai"))
    critical = category in CRITICAL_NOTIFICATION_CATEGORIES and bool(payload.get("critical"))
    if quiet_until and not (critical and policy.get("critical_bypass_quiet")):
        return {
            "action": "defer", "reason": "notification_quiet_hours", "policy_kind": "operational",
            "category": category, "available_at": _timestamp(quiet_until),
        }
    return {
        "action": "allow", "reason": "notification_policy_passed", "policy_kind": "operational",
        "category": category,
        "critical_bypass": bool(quiet_until and critical and policy.get("critical_bypass_quiet")),
    }


def _ambient_decision(conn: sqlite3.Connection, delivery: dict, current: datetime) -> dict:
    """Fail closed for optional group participation at every Outbox boundary."""

    payload = _payload(delivery)
    group_id = str(payload.get("group_id") or "").strip()
    try:
        policy_version = int(payload.get("policy_version") or 0)
    except (TypeError, ValueError):
        policy_version = 0
    if (
        not group_id
        or not payload.get("quality_receipt_id")
        or not payload.get("finalized_at")
        or not payload.get("freshness_ref")
        or policy_version < 1
    ):
        return {"action": "cancel", "reason": "ambient_delivery_metadata_invalid", "policy_kind": "ambient"}
    freshness = evaluate_topic_context(
        conn,
        group_id=group_id,
        context=payload.get("freshness_ref"),
        now=current,
        require_feature=False,
    )
    if freshness.get("action") != "allow":
        return {**freshness, "policy_kind": "ambient"}
    window = ambient_participation_window_decision(conn, group_id, now=current)
    return {**window, "policy_kind": "ambient"}


def evaluate_delivery_policy(conn: sqlite3.Connection, delivery: dict, *, now=None) -> dict:
    payload = _payload(delivery)
    current = _utc(now)
    group = _delivery_group(delivery)
    if group:
        work = bool(delivery.get("task_id") or payload.get("task_id"))
        origin = payload.get("group_generation_started_at", delivery.get("created_at"))
        gate = group_gate(conn, group, now=current, created_at=None if work else origin)
        if not gate["allowed"]:
            result = {"action": "defer" if work else "cancel", "reason": gate["reason"], "policy_kind": "group_availability"}
            if work:
                result["available_at"] = _timestamp(current + timedelta(minutes=1))
            return result
    if payload.get("ambient_participation"):
        return _ambient_decision(conn, delivery, current)
    if str(delivery.get("delivery_class") or "") == "topic_social":
        return evaluate_topic_delivery(conn, delivery, now=current)
    if payload.get("kind") == "proactive_chat":
        return _social_decision(conn, delivery, current)
    if str(payload.get("notification_category") or "").strip():
        return _notification_decision(conn, delivery, current)
    return _allow()


def _record_policy_outcome(conn: sqlite3.Connection, delivery: dict, decision: dict, phase: str) -> None:
    payload = _payload(delivery)
    if payload.get("kind") != "proactive_chat" or decision.get("action") == "allow":
        return
    event_id = str(payload.get("proactive_event_id") or "").strip()
    user_id = _social_policy_user_id(payload)
    reason = str(decision.get("reason") or "outbound_policy_blocked")[:300]
    event_row = conn.execute(
        "SELECT * FROM proactive_events WHERE id=? AND user_id=?",
        (event_id, user_id),
    ).fetchone() if event_id and user_id else None
    event = dict(event_row) if event_row else {}
    # A quiet-hours deferral is not a failed or blocked proactive event.  Keep
    # the event sendable so the next eligible evaluation can retry it after the
    # returned ``available_at`` timestamp.  Only a terminal policy cancellation
    # should poison the event itself.
    if event_id and decision.get("action") == "cancel":
        conn.execute(
            """UPDATE proactive_events SET blocked_reason=?,error=?
               WHERE id=? AND user_id=? AND delivered_at=''""",
            (reason, f"policy_{phase}:{reason}"[:1000], event_id, user_id),
        )
        if event:
            try:
                evidence = json.loads(str(event.get("evidence_snapshot_json") or "{}"))
            except json.JSONDecodeError:
                evidence = {}
            plan_id = str(evidence.get("interaction_plan_id") or "").strip()[:80] if isinstance(evidence, dict) else ""
            if plan_id and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_plans'",
            ).fetchone():
                conn.execute(
                    """UPDATE interaction_plans SET status='cancelled',updated_at=?
                       WHERE id=? AND status IN ('planned','dispatched')""",
                    (_timestamp(), plan_id),
                )
    if event:
        policy_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(proactive_policies)")
        }
        clauses = ["user_id=?"]
        params: list[object] = [user_id]
        for field, expected in (
            ("assistant_id", str(event.get("assistant_id") or "").strip()[:80]),
            ("policy_kind", str(event.get("policy_kind") or "").strip()[:40]),
            ("policy_version", int(event.get("policy_version") or 0)),
        ):
            if field in policy_columns and expected:
                clauses.append(f"{field}=?")
                params.append(expected)
        state = "quiet" if decision.get("action") == "defer" else "suppressed"
        # Bind the state projection to the event generation in the UPDATE
        # itself.  A read-then-user-only write allowed an Assistant/policy
        # switch during quiet-hour deferral to receive the stale event state.
        conn.execute(
            f"""UPDATE proactive_policies
                   SET state=?,state_reason=?,lease_until='',updated_at=?
                 WHERE {' AND '.join(clauses)}""",
            (state, reason, _timestamp(), *params),
        )


def _evaluate(connect, delivery: dict, *, now=None) -> dict:
    if not is_policy_controlled(delivery):
        return _allow()
    try:
        with connect() as conn:
            return evaluate_delivery_policy(conn, delivery, now=now)
    except (sqlite3.Error, ValueError, RuntimeError):
        if _payload(delivery).get("ambient_participation"):
            return {"action": "cancel", "reason": "ambient_window_policy_unavailable", "policy_kind": "ambient"}
        return {
            "action": "defer", "reason": "outbound_policy_unavailable", "policy_kind": "unknown",
            "available_at": _timestamp(_utc(now) + timedelta(minutes=5)),
        }


def _settle(outbox, delivery: dict, decision: dict) -> dict | None:
    delivery_id = str(delivery.get("id") or "")
    lease_token = str(delivery.get("lease_token") or "")
    if decision.get("action") == "defer":
        return outbox.defer_claim(
            delivery_id, lease_token, reason=str(decision.get("reason") or "policy_defer"),
            available_at=decision.get("available_at"),
        )
    return outbox.cancel_claim(
        delivery_id, lease_token, reason=str(decision.get("reason") or "policy_cancel"),
    )


def filter_claimed_deliveries(outbox, deliveries: list[dict], connect, *, now=None) -> list[dict]:
    ready: list[dict] = []
    for delivery in deliveries:
        decision = _evaluate(connect, delivery, now=now)
        if decision.get("action") == "allow":
            ready.append(delivery)
            continue
        _settle(outbox, delivery, decision)
        try:
            with connect() as conn:
                _record_policy_outcome(conn, delivery, decision, "claim")
        except sqlite3.Error:
            pass
    return ready


def begin_delivery_with_policy(outbox, delivery_id: str, lease_token: str, connect, *, now=None):
    delivery = outbox.get_delivery(delivery_id)
    if delivery is None:
        return None
    decision = _evaluate(connect, delivery, now=now)
    if decision.get("action") != "allow":
        _settle(outbox, delivery, decision)
        try:
            with connect() as conn:
                _record_policy_outcome(conn, delivery, decision, "send_start")
        except sqlite3.Error:
            pass
        raise DeliveryPolicyBlockedError(decision)
    return outbox.begin_send(delivery_id, lease_token)


__all__ = [
    "DeliveryPolicyBlockedError", "begin_delivery_with_policy", "evaluate_delivery_policy",
    "filter_claimed_deliveries", "is_policy_controlled", "social_proactive_globally_enabled",
]
