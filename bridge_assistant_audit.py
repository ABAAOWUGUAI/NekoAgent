#!/usr/bin/env python3
"""Sanitized Assistant security audit persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from bridge_migrations import utc_now


def record_security_audit(
    conn: sqlite3.Connection,
    event_type: str,
    outcome: str,
    *,
    actor_type: str = "admin",
    channel: str = "web",
    client_ip: str = "",
    detail: Mapping[str, object] | None = None,
) -> int:
    """Persist a sanitized security event; callers must never include secrets."""

    event_type = str(event_type or "").strip()[:80]
    outcome = str(outcome or "").strip()[:40]
    if not event_type or not outcome:
        raise ValueError("security_audit_event_required")
    cursor = conn.execute(
        """
        INSERT INTO security_audit_events(
            event_type, outcome, actor_type, channel, client_ip, detail_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            outcome,
            str(actor_type or "admin")[:40],
            str(channel or "web")[:40],
            str(client_ip or "")[:80],
            json.dumps(
                dict(detail or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def audit(
    connect,
    event_type: str,
    outcome: str,
    client_ip: str = "",
    detail: Mapping[str, object] | None = None,
) -> bool:
    """Record an admin event without allowing audit I/O to block logout."""

    try:
        with connect() as conn:
            record_security_audit(
                conn,
                event_type,
                outcome,
                client_ip=client_ip,
                detail=detail,
            )
        return True
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(
            f"security_audit_failed event={event_type} error={type(exc).__name__}",
            flush=True,
        )
        return False


__all__ = ["audit", "record_security_audit"]
