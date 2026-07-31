#!/usr/bin/env python3
"""Persistent control plane joining routing, Skill, work and delivery outcomes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from functools import wraps
from typing import Callable, Mapping

from bridge_action_registry import action_definition
from bridge_assistant_identity import current_assistant
from bridge_continuity_kernel_schema import CONTINUITY_KERNEL_FEATURE_FLAG
from bridge_learning_service import learning_feature_enabled, record_learning_signal
from bridge_migrations import utc_now


FINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
FAILURE_DISPATCHES = {
    "automation_denied", "automation_failed", "network_policy_blocked",
    "blocked", "error", "unavailable",
}
DISPATCH_ACTIONS = {
    "chat": "respond",
    "goal_feedback": "respond",
    "approval_required": "start_task",
    "task": "start_task",
    "task_append": "continue_task",
    "automation_create": "automation.schedule.create",
    "automation_update": "automation.schedule.update",
    "automation_disable": "automation.schedule.disable",
    "automation_run_now": "automation.schedule.run_now",
    "network_policy": "invoke_capability",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _clip(value: object, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _row(row: sqlite3.Row | tuple | None) -> dict:
    return dict(row) if row is not None else {}


def _selected_skill_ids(skill_plan: Mapping[str, object] | None) -> list[str]:
    plan = skill_plan if isinstance(skill_plan, Mapping) else {}
    return sorted({
        _clip(item.get("id"), 120)
        for item in plan.get("selected_skills", [])
        if isinstance(item, Mapping) and _clip(item.get("id"), 120)
    })


class ContinuityKernel:
    """Fail-open observability around an already authorized dispatch."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    @staticmethod
    def _enabled(conn: sqlite3.Connection) -> bool:
        try:
            row = conn.execute(
                "SELECT enabled FROM assistant_feature_flags WHERE name=?",
                (CONTINUITY_KERNEL_FEATURE_FLAG,),
            ).fetchone()
            return bool(row and int(row[0]))
        except sqlite3.Error:
            return False

    def set_feature(self, enabled: bool) -> dict:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO assistant_feature_flags(name,enabled,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE
                SET enabled=excluded.enabled,updated_at=excluded.updated_at
                """,
                (CONTINUITY_KERNEL_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
            )
        return {"name": CONTINUITY_KERNEL_FEATURE_FLAG, "enabled": bool(enabled)}

    @staticmethod
    def _thread(kwargs: Mapping[str, object]) -> tuple[str, str]:
        inbound = kwargs.get("inbound_context")
        inbound = inbound if isinstance(inbound, Mapping) else {}
        group_id = _clip(inbound.get("group_id"))
        actor = _clip(
            inbound.get("sender_id")
            or kwargs.get("delivery_recipient_id")
            or kwargs.get("user_id")
            or "default",
        )
        if group_id:
            return actor, f"qq:group:{group_id}"
        source = _clip(kwargs.get("source"), 40)
        if source == "admin":
            return actor, f"admin:{actor}"
        return actor, f"qq:private:{actor}"

    def _event(
        self,
        conn: sqlite3.Connection,
        turn_id: str,
        event_type: str,
        outcome: str = "",
        detail: Mapping[str, object] | None = None,
        *,
        key: str = "",
    ) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO continuity_events(
                id,turn_id,event_type,outcome,detail_json,idempotency_key,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "continuity-event-" + uuid.uuid4().hex,
                turn_id,
                _clip(event_type, 80),
                _clip(outcome, 80),
                _json(dict(detail or {})),
                _clip(key, 180),
                utc_now(),
            ),
        )
        return int(cursor.rowcount or 0) > 0

    def begin_turn(self, kwargs: Mapping[str, object]) -> str:
        try:
            with self._connect() as conn:
                if not self._enabled(conn):
                    return ""
                assistant = current_assistant(conn)
                if not assistant:
                    return ""
                actor, thread = self._thread(kwargs)
                inbound = kwargs.get("inbound_context")
                inbound = inbound if isinstance(inbound, Mapping) else {}
                parent_turn_id = _clip(inbound.get("_continuity_turn_id"))
                if parent_turn_id:
                    parent = conn.execute(
                        """
                        SELECT id FROM continuity_turns
                        WHERE id=? AND assistant_id=? AND actor_ref=? AND thread_ref=?
                        """,
                        (parent_turn_id, assistant["id"], actor, thread),
                    ).fetchone()
                    if parent:
                        return str(parent[0])
                trace = _clip(kwargs.get("trace_id") or inbound.get("trace_id"))
                external = _clip(inbound.get("_external_message_id"))
                stable_source = trace or external
                key = _digest(
                    f"{assistant['id']}\0{thread}\0{stable_source}"
                ) if stable_source else ""
                if key:
                    existing = conn.execute(
                        """
                        SELECT id FROM continuity_turns
                        WHERE assistant_id=? AND idempotency_key=?
                        """,
                        (assistant["id"], key),
                    ).fetchone()
                    if existing:
                        return str(existing[0])
                now = utc_now()
                turn_id = "continuity-turn-" + uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO continuity_turns(
                        id,assistant_id,actor_ref,channel_type,thread_ref,trace_ref,
                        idempotency_key,message_digest,status,started_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        turn_id,
                        assistant["id"],
                        actor,
                        "admin" if _clip(kwargs.get("source"), 40) == "admin"
                        else "qq_group" if thread.startswith("qq:group:") else "qq_private",
                        thread,
                        trace,
                        key,
                        _digest(kwargs.get("message")),
                        "planning",
                        now,
                        now,
                    ),
                )
                self._event(
                    conn,
                    turn_id,
                    "turn_started",
                    "planning",
                    {"channel_type": "admin" if thread.startswith("admin:") else "qq"},
                    key="turn-started",
                )
                return turn_id
        except (sqlite3.Error, ValueError):
            return ""

    @staticmethod
    def _plan(result: Mapping[str, object]) -> tuple[dict, dict, dict]:
        decision = result.get("mode_decision")
        decision = dict(decision) if isinstance(decision, Mapping) else {}
        plan = result.get("interaction_plan") or decision.get("interaction_plan")
        plan = dict(plan) if isinstance(plan, Mapping) else {}
        skill = decision.get("skill_plan")
        skill = dict(skill) if isinstance(skill, Mapping) else {}
        return decision, plan, skill

    @staticmethod
    def _action(result: Mapping[str, object], plan: Mapping[str, object]) -> tuple[str, str]:
        actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
        action = next((dict(item) for item in actions if isinstance(item, Mapping)), {})
        action_type = _clip(action.get("action_type") or action.get("type"), 100)
        dispatch = _clip(result.get("dispatch"), 100)
        if not action_type:
            action_type = DISPATCH_ACTIONS.get(dispatch, "respond")
        try:
            definition = action_definition(action_type)
        except KeyError:
            action_type = "invoke_capability" if result.get("capability_id") else "respond"
            definition = action_definition(action_type)
        capability = _clip(
            result.get("capability_id")
            or action.get("capability_id")
            or definition.capability_id,
            160,
        )
        return action_type, capability

    def _persist_skill_plan(
        self,
        conn: sqlite3.Connection,
        turn_id: str,
        assistant_id: str,
        skill: Mapping[str, object],
        *,
        applied: bool,
    ) -> str:
        selected = [
            {
                "id": _clip(item.get("id"), 120),
                "name": _clip(item.get("name"), 160),
                "source_digest": _clip(item.get("source_digest"), 128),
            }
            for item in skill.get("selected_skills", [])
            if isinstance(item, Mapping) and _clip(item.get("id"), 120)
        ]
        required = sorted({_clip(item, 160) for item in skill.get("required_capabilities", []) if _clip(item, 160)})
        missing = sorted({_clip(item, 160) for item in skill.get("missing_capabilities", []) if _clip(item, 160)})
        source_status = _clip(skill.get("status"), 40)
        status = (
            "missing_capability" if missing or source_status == "missing_capability"
            else "unavailable" if source_status == "unavailable"
            else "applied" if selected and applied
            else "not_applied" if selected
            else "selected"
        )
        payload = {"selected": selected, "required": required, "missing": missing}
        plan_hash = _digest(_json(payload))
        plan_id = "continuity-skill-plan-" + uuid.uuid4().hex
        now = utc_now()
        conn.execute(
            """
            INSERT INTO continuity_skill_plans(
                id,turn_id,assistant_id,status,selected_json,
                required_capabilities_json,missing_capabilities_json,
                plan_hash,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(turn_id) DO UPDATE SET
                status=excluded.status,selected_json=excluded.selected_json,
                required_capabilities_json=excluded.required_capabilities_json,
                missing_capabilities_json=excluded.missing_capabilities_json,
                plan_hash=excluded.plan_hash,updated_at=excluded.updated_at
            """,
            (
                plan_id,
                turn_id,
                assistant_id,
                status,
                _json(selected),
                _json(required),
                _json(missing),
                plan_hash,
                now,
                now,
                now if status in {"not_applied", "missing_capability", "unavailable"} else "",
            ),
        )
        row = conn.execute(
            "SELECT id FROM continuity_skill_plans WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        return str(row[0]) if row else ""

    @staticmethod
    def _status(result: Mapping[str, object]) -> tuple[str, str]:
        dispatch = _clip(result.get("dispatch"), 100)
        error = _clip(result.get("error_kind") or result.get("error"), 160)
        if result.get("capability_limited"):
            return "blocked", _clip(result.get("reason"), 160) or "capability_limited"
        if dispatch in FAILURE_DISPATCHES:
            return "blocked", error or dispatch
        if result.get("ok") is False:
            return "failed", error or "dispatch_failed"
        if dispatch == "approval_required":
            return "waiting_approval", ""
        if dispatch in {"task", "task_append"}:
            return "running", ""
        return "succeeded", ""

    def settle_turn(self, turn_id: str, result: Mapping[str, object]) -> None:
        if not turn_id:
            return
        try:
            with self._connect() as conn:
                turn = conn.execute(
                    "SELECT * FROM continuity_turns WHERE id=?",
                    (turn_id,),
                ).fetchone()
                if not turn:
                    return
                decision, plan, skill = self._plan(result)
                action_type, capability = self._action(result, plan)
                status, error = self._status(result)
                task = result.get("task")
                task = task if isinstance(task, Mapping) else {}
                plan_record = result.get("interaction_plan_record")
                plan_record = plan_record if isinstance(plan_record, Mapping) else {}
                skill_id = self._persist_skill_plan(
                    conn,
                    turn_id,
                    str(turn["assistant_id"]),
                    skill,
                    applied=status in {"running", "waiting_approval"},
                )
                now = utc_now()
                conn.execute(
                    """
                    UPDATE continuity_turns SET
                        plan_id=?,skill_plan_id=?,primary_intent=?,summary_mode=?,
                        action_type=?,capability_id=?,dispatch=?,status=?,
                        goal_id=?,run_id=?,task_id=?,error_kind=?,updated_at=?,
                        completed_at=?
                    WHERE id=?
                    """,
                    (
                        _clip(plan_record.get("id") or plan.get("id"), 160),
                        skill_id,
                        _clip(result.get("intent") or decision.get("intent") or plan.get("primary_intent"), 80),
                        _clip(result.get("mode") or decision.get("mode"), 40),
                        action_type,
                        capability,
                        _clip(result.get("dispatch"), 100),
                        status,
                        _clip(result.get("goal_id") or task.get("goal_id"), 160),
                        _clip(result.get("run_id") or task.get("run_id"), 160),
                        _clip(task.get("id") or result.get("task_id"), 160),
                        error,
                        now,
                        now if status in {"succeeded", "failed", "blocked", "cancelled"} else "",
                        turn_id,
                    ),
                )
                self._event(
                    conn,
                    turn_id,
                    "dispatch_settled",
                    status,
                    {"action_type": action_type, "capability_id": capability},
                    key="dispatch-settled",
                )
                self._goal_feedback_signal(conn, turn, result, turn_id)
        except (sqlite3.Error, ValueError, TypeError):
            return

    def _goal_feedback_signal(
        self,
        conn: sqlite3.Connection,
        turn: sqlite3.Row,
        result: Mapping[str, object],
        turn_id: str,
    ) -> None:
        if _clip(result.get("dispatch")) != "goal_feedback" or not learning_feature_enabled(conn):
            return
        feedback = result.get("feedback")
        feedback = feedback if isinstance(feedback, Mapping) else {}
        outcome = _clip(feedback.get("feedback_type") or feedback.get("kind") or "received", 40)
        record_learning_signal(
            conn,
            actor_ref=str(turn["actor_ref"]),
            channel_type=str(turn["channel_type"]),
            thread_id=str(turn["thread_ref"]),
            source_message_id=str(turn["trace_ref"]),
            signal_type="goal_outcome_feedback",
            domain="execution",
            payload={"outcome": outcome, "goal_id": _clip(result.get("goal_id"), 160)},
            confidence=1.0,
            consent_basis="explicit_user_feedback",
            idempotency_key=f"continuity-goal-feedback:{turn_id}",
        )

    def execute_turn(self, kwargs: Mapping[str, object], operation):
        """Run an admitted interaction inside one reusable Continuity Turn.

        Callers can pass a server-created ``_continuity_turn_id`` in
        ``inbound_context`` when a nested route (for example a group control
        action) continues the same inbound interaction.  The id is accepted
        only when its assistant, actor and thread all match the current call.
        """
        turn_id = self.begin_turn(kwargs)
        try:
            result = operation(turn_id)
        except Exception as exc:
            self.settle_turn(
                turn_id,
                {"ok": False, "dispatch": "error", "error_kind": type(exc).__name__},
            )
            raise
        if isinstance(result, dict):
            self.settle_turn(turn_id, result)
            if turn_id:
                result["continuity_turn_id"] = turn_id
        return result

    def wrap_dispatch(self, operation):
        @wraps(operation)
        def wrapped(*args, **kwargs):
            return self.execute_turn(
                kwargs,
                lambda _turn_id: operation(*args, **kwargs),
            )

        return wrapped

    def bind_delivery(self, result: Mapping[str, object]) -> None:
        turn_id = _clip(result.get("continuity_turn_id"), 160)
        delivery = result.get("delivery")
        delivery = delivery if isinstance(delivery, Mapping) else {}
        delivery_id = _clip(delivery.get("id"), 160)
        if not turn_id or not delivery_id:
            return
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT status FROM continuity_turns WHERE id=?",
                    (turn_id,),
                ).fetchone()
                if not row:
                    return
                status = str(row[0])
                next_status = "waiting_delivery" if status == "succeeded" else status
                conn.execute(
                    """
                    UPDATE continuity_turns
                    SET delivery_id=?,status=?,updated_at=?,
                        completed_at=CASE WHEN ?='waiting_delivery' THEN '' ELSE completed_at END
                    WHERE id=?
                    """,
                    (delivery_id, next_status, utc_now(), next_status, turn_id),
                )
                self._event(
                    conn,
                    turn_id,
                    "delivery_bound",
                    next_status,
                    {"delivery_id": delivery_id},
                    key=f"delivery-bound:{delivery_id}",
                )
        except sqlite3.Error:
            return

    def observe_task(
        self,
        task: Mapping[str, object],
        projection: Mapping[str, object] | None = None,
        delivery: Mapping[str, object] | None = None,
    ) -> None:
        from bridge_continuity_outcomes import observe_task

        observe_task(self, task, projection=projection, delivery=delivery)

    def settle_delivery(self, delivery_id: str, outcome: str, error_kind: str = "") -> None:
        from bridge_continuity_outcomes import settle_delivery

        settle_delivery(self, delivery_id, outcome, error_kind)


def settle_delivery_link(
    assistant_db_connect: Callable[[], sqlite3.Connection],
    delivery_id: str,
    outcome: str,
    error_kind: str = "",
) -> None:
    ContinuityKernel(assistant_db_connect).settle_delivery(delivery_id, outcome, error_kind)


__all__ = [
    "ContinuityKernel",
    "settle_delivery_link",
]
