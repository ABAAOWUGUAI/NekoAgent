#!/usr/bin/env python3
"""Runtime dispatcher for Gate C3 Assistant-DB action commands."""

from __future__ import annotations

from bridge_automation import attach_proactive_delivery, record_proactive_failure
from bridge_reliability_service import (
    mark_action_linked,
    mark_action_retry,
    pending_actions,
    reliability_enabled,
)


def drain_action_outbox(connect, delivery_outbox, *, limit: int = 10) -> dict:
    with connect() as conn:
        if not reliability_enabled(conn):
            return {"enabled": False, "linked": 0, "failed": 0}
        actions = pending_actions(conn, limit=limit)
    linked = failed = 0
    for action in actions:
        payload = action.get("payload") or {}
        try:
            delivery = delivery_outbox.enqueue(
                dedupe_key=str(action.get("dedupe_key") or ""),
                channel=str(payload.get("channel") or "qq"),
                destination=str(payload.get("destination") or ""),
                payload=dict(payload.get("payload") or {}),
                max_attempts=int(payload.get("max_attempts") or 100),
                thread_ref=str(payload.get("thread_ref") or ""),
                delivery_class=str(payload.get("delivery_class") or "operational"),
            )
            delivery_id = str(delivery.get("id") or "")
            if not delivery_id:
                raise RuntimeError("delivery_outbox_identity_missing")
            with connect() as conn:
                mark_action_linked(conn, str(action["id"]), delivery_id)
                if action.get("kind") == "proactive_delivery":
                    attach_proactive_delivery(conn, str(action["aggregate_id"]), delivery_id)
            linked += 1
        except Exception as exc:
            with connect() as conn:
                saved = mark_action_retry(conn, str(action["id"]), str(exc))
                if (
                    str(saved.get("status") or "") == "failed"
                    and str(action.get("kind") or "") == "proactive_delivery"
                    and str(action.get("aggregate_type") or "") == "proactive_event"
                ):
                    event = conn.execute(
                        "SELECT * FROM proactive_events WHERE id=?",
                        (str(action.get("aggregate_id") or ""),),
                    ).fetchone()
                    if event:
                        event = dict(event)
                        policy = conn.execute(
                            "SELECT * FROM proactive_policies WHERE user_id=?",
                            (str(event.get("user_id") or ""),),
                        ).fetchone()
                        record_proactive_failure(
                            conn,
                            dict(policy) if policy else {
                                "user_id": str(event.get("user_id") or ""),
                                "assistant_id": str(event.get("assistant_id") or ""),
                                "policy_kind": str(event.get("policy_kind") or ""),
                                "policy_version": int(event.get("policy_version") or 0),
                            },
                            str(exc),
                            event_id=str(event.get("id") or ""),
                        )
            failed += 1
    return {"enabled": True, "linked": linked, "failed": failed}


__all__ = ["drain_action_outbox"]
