#!/usr/bin/env python3
"""Runtime orchestration for proactive social policy evaluation."""

from __future__ import annotations

import time
from typing import Any

from bridge_social_start import reconcile_stale_start_opportunities


def _trace_elapsed(phase: str, started: float, *, status: str = "ok") -> None:
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if elapsed_ms >= 250 or status != "ok":
        print(
            f"proactive_stage phase={phase} elapsed_ms={elapsed_ms} status={status}",
            flush=True,
        )


def _proactive_stage(phase: str, work):
    started = time.monotonic()
    status = "ok"
    try:
        return work()
    except Exception:
        status = "error"
        raise
    finally:
        _trace_elapsed(phase, started, status=status)


def process_proactive_policies(services: dict[str, Any]) -> None:
    connect = services["_assistant_db_connect"]
    with connect() as conn:
        if not services["social_proactive_globally_enabled"](conn):
            return
        if hasattr(conn, "execute"):
            _proactive_stage("stale_reconcile", lambda: reconcile_stale_start_opportunities(conn))
        _proactive_stage("owner_reconcile", lambda: services["reconcile_owner_proactive_policy"](conn))
        _proactive_stage("group_reconcile", lambda: services["reconcile_group_proactive_policies"](conn))
        policies = _proactive_stage(
            "due_claim", lambda: services["claim_due_proactive_policies"](conn, limit=3),
        )
        commit_started = time.monotonic()
    _trace_elapsed("policy_commit", commit_started)
    for policy in policies:
        event = None
        try:
            decision = _proactive_stage(
                "model_decision", lambda: services["_generate_proactive_decision"](policy),
            )
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
                services["record_proactive_failure"](
                    conn,
                    policy,
                    str(exc),
                    event_id=str((event or {}).get("id") or ""),
                )


__all__ = ["process_proactive_policies"]
