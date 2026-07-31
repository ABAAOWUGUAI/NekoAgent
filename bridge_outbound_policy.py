#!/usr/bin/env python3
"""Unified AC-5 policy checks for proactive and operational deliveries.

Interactive replies are deliberately outside this policy layer. A delivery is
policy-controlled only when its payload is a social initiative
(``proactive_chat``) or declares an explicit ``notification_category``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bridge_relationship_service import get_notification_policy, get_social_proactive_policy

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
    return payload.get("kind") == "proactive_chat" or bool(
        str(payload.get("notification_category") or "").strip(),
    )


def _allow(kind: str = "interactive") -> dict:
    return {"action": "allow", "reason": "policy_not_applicable", "policy_kind": kind}


def _social_decision(conn: sqlite3.Connection, delivery: dict, current: datetime) -> dict:
    payload = _payload(delivery)
    user_id = str(payload.get("user_id") or "").strip()
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
    if int(policy.get("consecutive_unanswered") or 0) >= int(policy.get("unanswered_limit") or 2):
        return {"action": "cancel", "reason": "social_unanswered_limit", "policy_kind": "social"}
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
    latest_user = conn.execute(
        "SELECT MAX(created_at) FROM conversations WHERE user_id=? AND role='user'",
        (user_id,),
    ).fetchone()[0]
    if latest_user and _utc(latest_user) > _utc(event.get("decision_at")):
        return {"action": "cancel", "reason": "user_became_active", "policy_kind": "social"}
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


def evaluate_delivery_policy(conn: sqlite3.Connection, delivery: dict, *, now=None) -> dict:
    payload = _payload(delivery)
    current = _utc(now)
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
    user_id = str(payload.get("user_id") or "").strip()
    reason = str(decision.get("reason") or "outbound_policy_blocked")[:300]
    # A quiet-hours deferral is not a failed or blocked proactive event.  Keep
    # the event sendable so the next eligible evaluation can retry it after the
    # returned ``available_at`` timestamp.  Only a terminal policy cancellation
    # should poison the event itself.
    if event_id and decision.get("action") == "cancel":
        conn.execute(
            "UPDATE proactive_events SET blocked_reason=?,error=? WHERE id=? AND delivered_at=''",
            (reason, f"policy_{phase}:{reason}"[:1000], event_id),
        )
    if user_id:
        state = "quiet" if decision.get("action") == "defer" else "suppressed"
        conn.execute(
            "UPDATE proactive_policies SET state=?,state_reason=?,lease_until='',updated_at=? WHERE user_id=?",
            (state, reason, _timestamp(), user_id),
        )


def _evaluate(connect, delivery: dict, *, now=None) -> dict:
    if not is_policy_controlled(delivery):
        return _allow()
    try:
        with connect() as conn:
            return evaluate_delivery_policy(conn, delivery, now=now)
    except (sqlite3.Error, ValueError, RuntimeError):
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
