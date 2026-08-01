#!/usr/bin/env python3
"""Recover Continuity projections after committed cross-database interruptions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
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
        return {
            "superseded": 0,
            "interrupted": 0,
            "plans_settled": 0,
            "empty_skills_settled": 0,
        }

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

    plans_settled = empty_skills_settled = 0
    plan_statuses = {
        "succeeded": "completed",
        "failed": "failed",
        "blocked": "failed",
        "cancelled": "cancelled",
    }
    try:
        with kernel._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.id,t.status,t.plan_id,t.skill_plan_id,
                       p.status,s.status,s.selected_json
                FROM continuity_turns AS t
                LEFT JOIN interaction_plans AS p ON p.id=t.plan_id
                LEFT JOIN continuity_skill_plans AS s ON s.id=t.skill_plan_id
                WHERE t.status IN ('succeeded','failed','blocked','cancelled')
                  AND (
                    p.status IN ('planned','dispatched')
                    OR s.status='selected'
                  )
                ORDER BY t.updated_at DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            settled_at = utc_now()
            for row in rows:
                turn_id = str(row[0])
                plan_changed = skill_changed = False
                target_plan_status = plan_statuses.get(str(row[1]), "")
                if row[2] and target_plan_status and str(row[4] or "") in {"planned", "dispatched"}:
                    changed = conn.execute(
                        """
                        UPDATE interaction_plans SET status=?,updated_at=?
                        WHERE id=? AND status IN ('planned','dispatched')
                        """,
                        (target_plan_status, settled_at, str(row[2])),
                    )
                    plan_changed = int(changed.rowcount or 0) == 1
                    plans_settled += int(plan_changed)
                selected = []
                if row[3] and str(row[5] or "") == "selected":
                    try:
                        parsed = json.loads(str(row[6] or "[]"))
                        selected = parsed if isinstance(parsed, list) else ["invalid"]
                    except json.JSONDecodeError:
                        selected = ["invalid"]
                if row[3] and str(row[5] or "") == "selected" and not selected:
                    changed = conn.execute(
                        """
                        UPDATE continuity_skill_plans
                        SET status='not_applied',updated_at=?,completed_at=?
                        WHERE id=? AND status='selected'
                        """,
                        (settled_at, settled_at, str(row[3])),
                    )
                    skill_changed = int(changed.rowcount or 0) == 1
                    empty_skills_settled += int(skill_changed)
                if plan_changed or skill_changed:
                    kernel._event(
                        conn,
                        turn_id,
                        "projection_reconciled",
                        str(row[1]),
                        {
                            "interaction_plan": target_plan_status if plan_changed else "",
                            "skill_plan": "not_applied" if skill_changed else "",
                        },
                        key="projection-reconciled:plan-skill-v1",
                    )
    except (sqlite3.Error, ValueError, TypeError):
        plans_settled = empty_skills_settled = 0
    return {
        "superseded": len(superseded_ids),
        "interrupted": interrupted,
        "plans_settled": plans_settled,
        "empty_skills_settled": empty_skills_settled,
    }


__all__ = ["reconcile_continuity_state"]
