#!/usr/bin/env python3
"""AC-3 delivery settlement and downstream projection updates."""

from __future__ import annotations

from bridge_automation import mark_proactive_delivery, settle_automation_dispatch
from bridge_delivery_operations import delivery_task_id
from bridge_meme_social import mark_meme_delivery
from bridge_qq_participation_shadow import confirm_group_delivery
from bridge_social_opportunity import record_delivery_feedback


def _payload(delivery: dict | None) -> dict:
    if isinstance(delivery, dict) and isinstance(delivery.get("payload"), dict):
        return delivery["payload"]
    return {}


def settle_ack(
    outbox,
    delivery_id: str,
    lease_token: str,
    *,
    platform_message_id: str,
    assistant_db_connect,
    set_task_delivery,
    record_conversation,
) -> dict | None:
    delivery = outbox.ack(delivery_id, lease_token, platform_message_id=platform_message_id)
    task_id = delivery_task_id(delivery)
    if task_id:
        set_task_delivery(task_id, "sent")
    payload = _payload(delivery)
    if payload.get("kind") == "proactive_chat":
        with assistant_db_connect() as conn:
            event = mark_proactive_delivery(conn, delivery_id)
        if event and event.get("message"):
            record_conversation(
                str(event.get("user_id") or "default"),
                "assistant",
                str(event.get("message") or ""),
            )
    elif payload.get("kind") == "automation_reminder":
        with assistant_db_connect() as conn:
            settle_automation_dispatch(conn, delivery_id=delivery_id, status="completed")
    elif payload.get("kind") == "assistant_reply" and payload.get("group_id"):
        with assistant_db_connect() as conn:
            confirm_group_delivery(conn, delivery)
    selection_id = str(payload.get("selection_id") or "").strip()
    if selection_id:
        with assistant_db_connect() as conn:
            mark_meme_delivery(conn, selection_id, status="sent")
    from bridge_continuity_kernel import settle_delivery_link

    settle_delivery_link(assistant_db_connect, delivery_id, "confirmed")
    return delivery


def settle_retry(
    outbox,
    delivery_id: str,
    lease_token: str,
    *,
    error: str,
    delay_seconds: float,
    known_not_sent: bool,
    assistant_db_connect,
    set_task_delivery,
    pending_status: str,
) -> dict | None:
    delivery = outbox.retry(
        delivery_id,
        lease_token,
        error=error,
        delay_seconds=delay_seconds,
        known_not_sent=known_not_sent,
    )
    task_id = delivery_task_id(delivery)
    if task_id:
        set_task_delivery(task_id, "failed" if delivery and delivery.get("dead_letter") else pending_status, error)
    payload = _payload(delivery)
    if payload.get("kind") == "proactive_chat" and delivery and delivery.get("dead_letter"):
        with assistant_db_connect() as conn:
            mark_proactive_delivery(conn, delivery_id, error=error or "delivery_dead_letter")
    elif payload.get("kind") == "automation_reminder" and delivery and delivery.get("dead_letter"):
        with assistant_db_connect() as conn:
            settle_automation_dispatch(
                conn,
                delivery_id=delivery_id,
                status="failed",
                error=error or "delivery_dead_letter",
            )
    elif payload.get("kind") == "assistant_reply" and delivery and delivery.get("dead_letter"):
        with assistant_db_connect() as conn:
            record_delivery_feedback(conn, delivery, "delivery_failed")
    if delivery and delivery.get("dead_letter"):
        from bridge_continuity_kernel import settle_delivery_link

        settle_delivery_link(assistant_db_connect, delivery_id, "dead_letter", error)
    return delivery


def settle_ambiguous(
    outbox,
    delivery_id: str,
    lease_token: str,
    *,
    error: str,
    assistant_db_connect,
) -> dict | None:
    delivery = outbox.mark_ambiguous(delivery_id, lease_token, error=error)
    with assistant_db_connect() as conn:
        record_delivery_feedback(conn, delivery, "ambiguous")
    selection_id = str(_payload(delivery).get("selection_id") or "").strip()
    if selection_id:
        with assistant_db_connect() as conn:
            mark_meme_delivery(conn, selection_id, status="failed", error="delivery_ambiguous")
    from bridge_continuity_kernel import settle_delivery_link

    settle_delivery_link(assistant_db_connect, delivery_id, "ambiguous", error)
    return delivery


__all__ = ["settle_ack", "settle_ambiguous", "settle_retry"]
