#!/usr/bin/env python3
"""Public, data-minimized Continuity Kernel projection."""

from __future__ import annotations

import sqlite3

from bridge_assistant_identity import current_assistant
from bridge_continuity_kernel import ContinuityKernel


def continuity_summary(conn: sqlite3.Connection, *, limit: int = 12) -> dict:
    bounded = max(1, min(int(limit or 12), 100))
    if not ContinuityKernel._enabled(conn):
        return {"feature_enabled": False, "counts": {}, "recent_exceptions": []}
    assistant = current_assistant(conn)
    if not assistant:
        return {"feature_enabled": True, "counts": {}, "recent_exceptions": []}
    counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT status,count(*) AS count FROM continuity_turns
            WHERE assistant_id=? GROUP BY status
            """,
            (assistant["id"],),
        )
    }
    exceptions = [
        {
            "turn_id": str(row["id"]),
            "status": str(row["status"]),
            "action_type": str(row["action_type"]),
            "error_kind": str(row["error_kind"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in conn.execute(
            """
            SELECT id,status,action_type,error_kind,updated_at
            FROM continuity_turns
            WHERE assistant_id=? AND status IN ('failed','blocked')
            ORDER BY updated_at DESC LIMIT ?
            """,
            (assistant["id"], bounded),
        )
    ]
    return {
        "feature_enabled": True,
        "counts": counts,
        "recent_exceptions": exceptions,
    }


__all__ = ["continuity_summary"]
