#!/usr/bin/env python3
"""Durable Automation object references, clarifications, and action plans."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from typing import Mapping

from bridge_automation_contracts import DEFAULT_OUTPUT_CONTRACT, normalize_output_contract
from bridge_automation_schema import ensure_automation_tables
from bridge_migrations import utc_now


OPEN_STATUSES = {"waiting_clarification", "ready", "executing"}
_TASK_HINTS = ("定时任务", "定时计划", "这条任务", "这个任务", "该任务", "这个")
_UPDATE_HINTS = ("修改", "改成", "改为", "调整", "更新", "改一下", "改一改", "改改", "格式")
_RUN_HINTS = ("马上触发", "立即触发", "现在触发", "触发一次", "马上执行", "立即执行", "执行一次", "跑一次")
_PURPOSE_HINTS = ("中文简介", "中文说明", "用途", "做什么", "是干什么", "功能说明")
_SCOPE_CURRENT_HINTS = ("只针对这个", "只改这个", "就这个", "当前这个", "这一个")


def _thread_ref(actor_id: str, inbound: Mapping[str, object] | None) -> str:
    payload = dict(inbound or {})
    session = str(payload.get("session") or "").strip()
    return session[:300] or f"qq:private:{str(actor_id or '').strip()[:80]}"


def automation_conversation_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name='automation_conversation_contract_v1'",
        ).fetchone()
        return bool(row and int(row[0]))
    except sqlite3.Error:
        return False


def _contract_changes(message: str) -> dict:
    text = str(message or "").strip()
    if "中文" in text and any(hint in text for hint in _PURPOSE_HINTS):
        return {
            "output_contract": normalize_output_contract(DEFAULT_OUTPUT_CONTRACT),
            "legacy_parameters": {
                "output_language": "zh-CN",
                "delivery_format": "conversation",
            },
        }
    return {}


def _initial_plan(message: str) -> dict | None:
    text = str(message or "").strip()
    if not text or not any(hint in text for hint in _TASK_HINTS):
        return None
    wants_update = any(hint in text for hint in _UPDATE_HINTS)
    wants_run = any(hint in text for hint in _RUN_HINTS)
    if not wants_update or not wants_run:
        return None
    changes = _contract_changes(text)
    update_status = "ready" if changes else "waiting_clarification"
    return {
        "schema_version": 1,
        "scope": "current_automation_job",
        "status": update_status,
        "actions": [
            {
                "id": "update",
                "type": "automation.schedule.update",
                "depends_on": [],
                "status": update_status,
                "changes": changes,
            },
            {
                "id": "run_now",
                "type": "automation.schedule.run_now",
                "depends_on": ["update"],
                "status": "blocked_by_dependency",
            },
        ],
        "clarification_key": "output_contract_required" if not changes else "",
    }


def _apply_clarification(plan: dict, message: str) -> tuple[dict, bool]:
    text = str(message or "").strip()
    changes = _contract_changes(text)
    if not changes:
        return plan, bool(any(hint in text for hint in _SCOPE_CURRENT_HINTS))
    result = json.loads(json.dumps(plan, ensure_ascii=False))
    for action in result.get("actions") or []:
        if action.get("id") == "update":
            action["changes"] = changes
            action["status"] = "ready"
    result["status"] = "ready"
    result["clarification_key"] = ""
    return result, True


def load_open_action_plan(conn: sqlite3.Connection, *, actor_id: str, thread_ref: str) -> dict | None:
    ensure_automation_tables(conn)
    row = conn.execute(
        """SELECT * FROM automation_action_plans
           WHERE actor_id=? AND thread_ref=?
             AND status IN ('waiting_clarification','ready','executing')
           ORDER BY updated_at DESC LIMIT 1""",
        (str(actor_id or "")[:80], str(thread_ref or "")[:300]),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["plan"] = json.loads(str(result.get("plan_json") or "{}"))
    except json.JSONDecodeError:
        result["plan"] = {}
    return result


def _save_plan(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    thread_ref: str,
    source_message_id: str,
    target_job_id: str,
    target_revision: int,
    plan: dict,
    existing_id: str = "",
) -> dict:
    plan_id = existing_id or "automation-plan-" + uuid.uuid4().hex
    now = utc_now()
    status = str(plan.get("status") or "waiting_clarification")
    conn.execute(
        """INSERT INTO automation_action_plans(
               id,actor_id,thread_ref,source_message_id,target_job_id,target_revision,
               status,plan_json,clarification_key,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               source_message_id=excluded.source_message_id,
               target_job_id=excluded.target_job_id,
               target_revision=excluded.target_revision,
               status=excluded.status,
               plan_json=excluded.plan_json,
               clarification_key=excluded.clarification_key,
               updated_at=excluded.updated_at""",
        (
            plan_id, str(actor_id or "")[:80], str(thread_ref or "")[:300],
            str(source_message_id or "")[:300], (str(target_job_id)[:80] if target_job_id else None),
            int(target_revision or 0), status,
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            str(plan.get("clarification_key") or "")[:80], now, now,
        ),
    )
    return dict(conn.execute("SELECT * FROM automation_action_plans WHERE id=?", (plan_id,)).fetchone())


def finish_action_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    status: str,
    receipts: list[dict],
) -> None:
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError("automation_action_plan_terminal_status_invalid")
    now = utc_now()
    conn.execute(
        """UPDATE automation_action_plans
           SET status=?,receipts_json=?,updated_at=?,completed_at=? WHERE id=?""",
        (
            status,
            json.dumps(receipts, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now, now, str(plan_id or ""),
        ),
    )


def plan_automation_conversation(
    connect: Callable[[], sqlite3.Connection],
    *,
    actor_id: str,
    message: str,
    inbound_context: Mapping[str, object] | None,
    resolve_target: Callable[[str, Mapping[str, object]], dict],
) -> dict | None:
    inbound = dict(inbound_context or {})
    thread_ref = _thread_ref(actor_id, inbound)
    with connect() as conn:
        if not automation_conversation_enabled(conn):
            return None
        open_row = load_open_action_plan(conn, actor_id=actor_id, thread_ref=thread_ref)
        if open_row:
            plan, relevant = _apply_clarification(dict(open_row.get("plan") or {}), message)
            if not relevant:
                return None
            target_job_id = str(open_row.get("target_job_id") or "")
            target_revision = int(open_row.get("target_revision") or 0)
            if not target_job_id:
                target = dict(resolve_target(actor_id, inbound) or {})
                if target.get("status") == "resolved":
                    target_job_id = str(target.get("job_id") or "")
                    target_revision = int(target.get("revision") or 0)
                    if any(item.get("id") == "update" and item.get("status") == "waiting_clarification" for item in plan.get("actions") or []):
                        plan["clarification_key"] = "output_contract_required"
                else:
                    plan["status"] = "waiting_clarification"
                    plan["clarification_key"] = "target_required"
            saved = _save_plan(
                conn,
                actor_id=actor_id,
                thread_ref=thread_ref,
                source_message_id=str(inbound.get("_external_message_id") or ""),
                target_job_id=target_job_id,
                target_revision=target_revision,
                plan=plan,
                existing_id=str(open_row.get("id") or ""),
            )
            conn.commit()
            return {"record": saved, "plan": plan, "resumed": True}
        plan = _initial_plan(message)
        if plan is None:
            return None
        target = dict(resolve_target(actor_id, inbound) or {})
        if target.get("status") != "resolved":
            plan["status"] = "waiting_clarification"
            plan["clarification_key"] = "target_required"
        saved = _save_plan(
            conn,
            actor_id=actor_id,
            thread_ref=thread_ref,
            source_message_id=str(inbound.get("_external_message_id") or ""),
            target_job_id=str(target.get("job_id") or ""),
            target_revision=int(target.get("revision") or 0),
            plan=plan,
        )
        conn.commit()
        return {"record": saved, "plan": plan, "resumed": False, "target": target}


def clarification_reply(plan: Mapping[str, object], *, target_resolved: bool) -> str:
    key = str(plan.get("clarification_key") or "")
    if key == "target_required" or not target_resolved:
        return "我知道你要先修改再立即检验，但还不能唯一确定是哪条定时任务。请引用那条任务结果，或说明任务名称。"
    return (
        "我已锁定你引用的这条定时任务，并保留了“修改成功后立即执行一次”的后续动作。"
        "还差一个会改变结果的条件：请说明这条任务希望怎样展示，例如“每个项目增加一句中文用途简介”。"
    )


__all__ = [
    "automation_conversation_enabled",
    "clarification_reply",
    "finish_action_plan",
    "load_open_action_plan",
    "plan_automation_conversation",
]
