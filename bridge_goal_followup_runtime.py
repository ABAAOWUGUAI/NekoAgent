#!/usr/bin/env python3
"""Bridge-facing orchestration helpers for scope-safe Goal follow-up."""

from __future__ import annotations

import sqlite3
from typing import Callable

from bridge_goal_followup import followup_resolution_reply, resolve_goal_followup


def followup_scope(source: str, user_id: str, delivery_recipient_id: str) -> tuple[str, str]:
    recipient = str(delivery_recipient_id or "")
    channel = "qq_group" if recipient.startswith("group:") else source
    return channel, recipient or str(user_id)


def followup_history(
    inbound_context: dict | None,
    load_history: Callable[..., list[dict]],
    conversation_ref: str,
    channel: str,
    limit: int,
) -> list[dict]:
    override = (inbound_context or {}).get("history")
    if isinstance(override, list):
        return list(override)
    return load_history(conversation_ref, limit, source=channel)


def load_goal_followup(
    connect: Callable[[], sqlite3.Connection],
    *,
    actor_id: str,
    channel: str,
    conversation_ref: str,
    message: str,
    recent_context: list[dict],
) -> dict | None:
    try:
        with connect() as conn:
            return resolve_goal_followup(
                conn,
                actor_id=actor_id,
                channel=channel,
                conversation_ref=conversation_ref,
                message=message,
                recent_context=recent_context,
            )
    except sqlite3.Error:
        return None


def unresolved_followup_result(
    target: dict | None,
    *,
    message: str,
    conversation_ref: str,
    channel: str,
    record_conversation: Callable[..., object],
) -> dict | None:
    if not target or target.get("resolution") == "resolved":
        return None
    reply = followup_resolution_reply(target)
    record_conversation(conversation_ref, "user", message, source=channel)
    record_conversation(conversation_ref, "assistant", reply, source=channel)
    return {
        "ok": True,
        "dispatch": "goal_followup_clarification",
        "reply": reply,
        "continuity_resolution": target,
    }


__all__ = [
    "followup_history",
    "followup_scope",
    "load_goal_followup",
    "unresolved_followup_result",
]
