#!/usr/bin/env python3
"""Privacy-safe participation explanation projection for the admin console."""

from __future__ import annotations

import sqlite3

from bridge_conversation_participation_routing_schema import DETERMINISTIC_PARTICIPATION_FEATURE_FLAG
from bridge_conversation_participation_schema import PARTICIPATION_SHADOW_FEATURE_FLAG


def participation_diagnostics(connect, *, limit: int = 12) -> dict:
    empty = {"shadow_enabled": False, "deterministic_enabled": False, "decisions": []}
    try:
        with connect() as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "engagement_decisions" not in tables or "conversation_events" not in tables:
                return empty
            flags = {}
            if "assistant_feature_flags" in tables:
                flags = {
                    str(row[0]): bool(int(row[1]))
                    for row in conn.execute(
                        "SELECT name,enabled FROM assistant_feature_flags WHERE name IN (?,?)",
                        (PARTICIPATION_SHADOW_FEATURE_FLAG, DETERMINISTIC_PARTICIPATION_FEATURE_FLAG),
                    ).fetchall()
                }
            rows = conn.execute(
                """
                SELECT d.id,d.candidate_kind,d.action,d.reason_code,d.policy_version,
                       d.model_role,d.model_id,d.confidence,d.created_at,
                       e.channel_type,e.conversation_scope,e.message_kind,
                       e.reply_to_assistant,e.attachment_count,e.text_length
                FROM engagement_decisions d
                JOIN conversation_events e ON e.id=d.event_id
                ORDER BY d.created_at DESC,d.id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 50)),),
            ).fetchall()
            return {
                "shadow_enabled": flags.get(PARTICIPATION_SHADOW_FEATURE_FLAG, False),
                "deterministic_enabled": flags.get(DETERMINISTIC_PARTICIPATION_FEATURE_FLAG, False),
                "decisions": [dict(row) for row in rows],
            }
    except (AttributeError, TypeError, sqlite3.Error, ValueError):
        return empty


__all__ = ["participation_diagnostics"]
