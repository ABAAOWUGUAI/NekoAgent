#!/usr/bin/env python3
"""Recover Continuity projections after committed cross-database interruptions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from bridge_migrations import utc_now


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def reconcile_continuity_state(
    kernel,
    task_connect,
    *,
    now: datetime | None = None,
    planning_timeout_seconds: int = 300,
    limit: int = 200,
) -> dict:
    """Settle replaced deliveries and turns interrupted before dispatch settled."""

    bounded_limit = max(1, min(int(limit), 1000))
    current = now or datetime.now(timezone.utc)
    cutoff = _timestamp(current - timedelta(seconds=max(60, int(planning_timeout_seconds))))
    try:
        with kernel._connect() as conn:
            waiting = conn.execute(
                """
                SELECT delivery_id FROM continuity_turns
                WHERE status='waiting_delivery' AND delivery_id<>''
                ORDER BY updated_at LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
    except sqlite3.Error:
        return {"superseded": 0, "interrupted": 0}

    delivery_ids = [str(row[0]) for row in waiting]
    superseded_ids: list[str] = []
    if delivery_ids:
        placeholders = ",".join("?" for _ in delivery_ids)
        try:
            with task_connect() as conn:
                superseded_ids = [
                    str(row[0])
                    for row in conn.execute(
                        f"""
                        SELECT id FROM delivery_outbox
                        WHERE id IN ({placeholders}) AND superseded_by<>''
                        """,
                        delivery_ids,
                    ).fetchall()
                ]
        except sqlite3.Error:
            superseded_ids = []

    for delivery_id in superseded_ids:
        kernel.settle_delivery(delivery_id, "superseded", "delivery_superseded")

    interrupted = 0
    try:
        with kernel._connect() as conn:
            stale = conn.execute(
                """
                SELECT id FROM continuity_turns
                WHERE status='planning' AND updated_at<=?
                  AND task_id='' AND delivery_id=''
                ORDER BY updated_at LIMIT ?
                """,
                (cutoff, bounded_limit),
            ).fetchall()
            settled_at = utc_now()
            for row in stale:
                turn_id = str(row[0])
                changed = conn.execute(
                    """
                    UPDATE continuity_turns
                    SET status='failed',error_kind='dispatch_interrupted',
                        updated_at=?,completed_at=?
                    WHERE id=? AND status='planning' AND task_id='' AND delivery_id=''
                    """,
                    (settled_at, settled_at, turn_id),
                )
                if int(changed.rowcount or 0) != 1:
                    continue
                kernel._event(
                    conn,
                    turn_id,
                    "dispatch_reconciled",
                    "failed",
                    {"error_kind": "dispatch_interrupted"},
                    key="dispatch-reconciled:interrupted",
                )
                interrupted += 1
    except sqlite3.Error:
        interrupted = 0
    return {"superseded": len(superseded_ids), "interrupted": interrupted}


__all__ = ["reconcile_continuity_state"]
