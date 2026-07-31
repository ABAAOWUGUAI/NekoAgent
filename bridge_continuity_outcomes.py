#!/usr/bin/env python3
"""Outcome projections for Continuity Kernel task, Skill and delivery state."""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from bridge_capability_registry import record_skill_outcomes
from bridge_learning_service import learning_feature_enabled, record_learning_signal
from bridge_migrations import utc_now


FINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}


def _clip(value: object, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _settle_skill_outcome(kernel, conn, turn_id: str, succeeded: bool, now: str) -> None:
    plan = conn.execute(
        "SELECT * FROM continuity_skill_plans WHERE turn_id=?",
        (turn_id,),
    ).fetchone()
    if not plan:
        return
    skill_ids = [
        _clip(item.get("id"), 120)
        for item in json.loads(str(plan["selected_json"] or "[]"))
        if isinstance(item, Mapping)
    ]
    outcome = "succeeded" if succeeded else "failed"
    inserted = kernel._event(
        conn,
        turn_id,
        "skill_outcome",
        outcome,
        {"skill_count": len(skill_ids)},
        key=f"skill-outcome:{outcome}",
    )
    if inserted and skill_ids:
        record_skill_outcomes(conn, skill_ids, succeeded=succeeded, occurred_at=now)
    conn.execute(
        """
        UPDATE continuity_skill_plans
        SET status=?,updated_at=?,completed_at=?
        WHERE turn_id=? AND status NOT IN ('not_applied','missing_capability','unavailable')
        """,
        (outcome, now, now, turn_id),
    )


def observe_task(
    kernel,
    task: Mapping[str, object],
    *,
    projection: Mapping[str, object] | None = None,
    delivery: Mapping[str, object] | None = None,
) -> None:
    task_id = _clip(task.get("id"))
    if not task_id:
        return
    projection = projection if isinstance(projection, Mapping) else {}
    delivery = delivery if isinstance(delivery, Mapping) else {}
    try:
        with kernel._connect() as conn:
            turn = conn.execute(
                "SELECT * FROM continuity_turns WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if not turn:
                return
            task_status = _clip(task.get("status"), 40)
            final = task_status in FINAL_TASK_STATUSES
            succeeded = task_status == "succeeded" and bool(task.get("ok", True))
            delivery_id = _clip(delivery.get("id"))
            status = (
                "waiting_delivery" if final and succeeded and delivery_id
                else "succeeded" if final and succeeded
                else "cancelled" if task_status == "cancelled"
                else "failed" if final
                else "running"
            )
            error = _clip(task.get("error_kind") or task.get("error"))
            now = utc_now()
            conn.execute(
                """
                UPDATE continuity_turns SET goal_id=?,run_id=?,capability_id=?,
                    status=?,delivery_id=CASE WHEN ?<>'' THEN ? ELSE delivery_id END,
                    error_kind=?,updated_at=?,completed_at=?
                WHERE id=?
                """,
                (
                    _clip(task.get("goal_id") or projection.get("goal_id")),
                    _clip(task.get("run_id") or projection.get("run_id")),
                    _clip(task.get("capability_id")),
                    status,
                    delivery_id,
                    delivery_id,
                    error,
                    now,
                    now if final and not delivery_id else "",
                    turn["id"],
                ),
            )
            kernel._event(
                conn,
                str(turn["id"]),
                "task_observed",
                status,
                {"task_status": task_status},
                key=f"task:{task_id}:{task_status}",
            )
            if final:
                _settle_skill_outcome(kernel, conn, str(turn["id"]), succeeded, now)
                if not succeeded and learning_feature_enabled(conn):
                    record_learning_signal(
                        conn,
                        actor_ref=str(turn["actor_ref"]),
                        channel_type=str(turn["channel_type"]),
                        thread_id=str(turn["thread_ref"]),
                        source_message_id=str(turn["trace_ref"]),
                        signal_type="execution_outcome",
                        domain="reliability",
                        payload={"status": status, "error_kind": _clip(error, 80)},
                        confidence=1.0,
                        consent_basis="system_observation",
                        idempotency_key=f"continuity-task:{task_id}:{status}",
                    )
    except (sqlite3.Error, ValueError):
        return


def settle_delivery(kernel, delivery_id: str, outcome: str, error_kind: str = "") -> None:
    delivery_id = _clip(delivery_id)
    if not delivery_id:
        return
    try:
        with kernel._connect() as conn:
            turn = conn.execute(
                "SELECT * FROM continuity_turns WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if not turn:
                return
            previous = str(turn["status"])
            if outcome == "confirmed":
                status = "succeeded" if previous == "waiting_delivery" else previous
            elif outcome == "ambiguous":
                status = "blocked"
            elif outcome == "dead_letter":
                status = "failed"
            elif outcome == "superseded":
                status = "cancelled" if previous == "waiting_delivery" else previous
            else:
                status = previous
            previous_error = _clip(turn["error_kind"])
            if error_kind:
                settled_error = _clip(error_kind)
            elif outcome == "confirmed" and previous == "waiting_delivery":
                settled_error = ""
            elif outcome == "ambiguous":
                settled_error = "delivery_ambiguous"
            elif outcome == "dead_letter":
                settled_error = "delivery_dead_letter"
            elif outcome == "superseded" and previous == "waiting_delivery":
                settled_error = "delivery_superseded"
            else:
                settled_error = previous_error
            now = utc_now()
            conn.execute(
                """
                UPDATE continuity_turns
                SET status=?,error_kind=?,updated_at=?,completed_at=?
                WHERE id=?
                """,
                (
                    status,
                    settled_error,
                    now,
                    now if status in {"succeeded", "failed", "blocked", "cancelled"} else "",
                    turn["id"],
                ),
            )
            kernel._event(
                conn,
                str(turn["id"]),
                "delivery_settled",
                outcome,
                {"delivery_id": delivery_id},
                key=f"delivery-settled:{delivery_id}:{outcome}",
            )
            if outcome in {"dead_letter", "ambiguous"} and learning_feature_enabled(conn):
                record_learning_signal(
                    conn,
                    actor_ref=str(turn["actor_ref"]),
                    channel_type=str(turn["channel_type"]),
                    thread_id=str(turn["thread_ref"]),
                    source_message_id=str(turn["trace_ref"]),
                    signal_type="delivery_outcome",
                    domain="reliability",
                    payload={"outcome": outcome, "error_kind": _clip(error_kind, 80)},
                    confidence=1.0,
                    consent_basis="system_observation",
                    idempotency_key=f"continuity-delivery:{delivery_id}:{outcome}",
                )
    except sqlite3.Error:
        return


__all__ = ["observe_task", "settle_delivery"]
