#!/usr/bin/env python3
"""Claim durable deliveries and resolve channel destinations."""

from __future__ import annotations


def claim_deliveries(
    outbox,
    lease_owner: str,
    *,
    wait_seconds: float,
    lease_seconds: float,
    limit: int,
    channel: str,
    sessions: dict[str, str],
    policy_filter=None,
) -> list[dict]:
    deliveries = outbox.claim_or_wait(
        lease_owner,
        wait_seconds=max(0.0, min(float(wait_seconds), 30.0)),
        lease_seconds=max(5.0, min(float(lease_seconds), 300.0)),
        limit=max(1, min(int(limit), 20)),
        channel=channel or None,
    )
    ready: list[dict] = []
    for delivery in deliveries:
        payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
        user_id = str(payload.get("user_id") or "").strip()
        session = sessions.get(user_id) or str(payload.get("send_session") or "").strip()
        if channel == "qq" and session:
            delivery.update(recipient_id=user_id, destination=session)
            payload["send_session"] = session
            delivery["payload"] = payload
        if channel == "qq" and not session:
            outbox.retry(
                str(delivery.get("id") or ""),
                str(delivery.get("lease_token") or ""),
                error="qq_session_unavailable",
                delay_seconds=300,
            )
            continue
        ready.append(delivery)
    return policy_filter(ready) if policy_filter is not None else ready


__all__ = ["claim_deliveries"]
