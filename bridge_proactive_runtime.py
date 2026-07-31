#!/usr/bin/env python3
"""Runtime orchestration for proactive social policy evaluation."""

from __future__ import annotations

from typing import Any

from bridge_social_start import reconcile_stale_start_opportunities


def process_proactive_policies(services: dict[str, Any]) -> None:
    connect = services["_assistant_db_connect"]
    with connect() as conn:
        if not services["social_proactive_globally_enabled"](conn):
            return
        if hasattr(conn, "execute"):
            reconcile_stale_start_opportunities(conn)
        services["reconcile_owner_proactive_policy"](conn)
        services["reconcile_group_proactive_policies"](conn)
        policies = services["claim_due_proactive_policies"](conn, limit=3)
    for policy in policies:
        try:
            decision = services["_generate_proactive_decision"](policy)
            with connect() as conn:
                event = services["record_proactive_decision"](
                    conn,
                    policy,
                    decision,
                )
            if event.get("action") != "send" or event.get("action_staged"):
                continue
            user_id = str(policy.get("user_id") or "")
            is_group = (
                str(policy.get("policy_kind") or "") == "group_social"
                and user_id.startswith("group:")
            )
            target_id = user_id[6:] if is_group else user_id
            delivery = services["_phase2_outbox"]().enqueue(
                dedupe_key=f"qq:proactive:{event['id']}",
                channel="qq",
                destination=str(policy.get("send_session") or target_id),
                payload={
                    "kind": "proactive_chat",
                    "proactive_event_id": event["id"],
                    "user_id": target_id,
                    "send_session": str(policy.get("send_session") or ""),
                    "content": event["message"],
                    "scope": "group" if is_group else "private",
                    "group_id": target_id if is_group else "",
                },
                max_attempts=100,
                thread_ref=f"qq:{'group' if is_group else 'private'}:{target_id}",
                delivery_class="social",
            )
            with connect() as conn:
                services["attach_proactive_delivery"](
                    conn,
                    event["id"],
                    str(delivery.get("id") or ""),
                )
        except Exception as exc:
            with connect() as conn:
                services["record_proactive_failure"](conn, policy, str(exc))


__all__ = ["process_proactive_policies"]
