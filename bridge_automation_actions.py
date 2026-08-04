#!/usr/bin/env python3
"""Deterministic natural-language automation actions for private assistant chat.

The model may help execute a scheduled agent job later, but it never parses the
schedule, grants authority, or writes the durable job.  This module owns that
control-plane boundary and auto-enables complete, low-risk Owner requests.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from bridge_action_registry import action_definition
from bridge_automation import DEFAULT_TIMEZONE, ensure_automation_tables, upsert_automation_job
from bridge_automation_contracts import normalize_output_contract, output_contract_hash
from bridge_automation_conversation import (
    clarification_reply,
    finish_action_plan,
    plan_automation_conversation,
)
from bridge_automation_disable import execute_automation_disable
from bridge_qq_access_service import check_qq_access


_SCHEDULE_HINTS = (
    "定时任务", "定时计划", "定时提醒", "每天", "每日", "每晚", "每早",
    "每周", "每隔", "定期", "按时", "到点",
)
_CREATE_HINTS = ("做一个", "创建", "新建", "设置", "安排", "帮我", "要求", "提醒", "推送")
_UPDATE_HINTS = (
    "修改", "改成", "改为", "调整", "更新", "改一下", "改一改", "改改", "换一下",
)
_READABLE_FORMAT_HINTS = (
    "看不懂", "看不明白", "不能理解", "不好理解", "太乱", "清楚一点", "易懂",
    "好读", "聊天记录", "聊天式", "对话式", "简洁", "直观",
)
_DISABLE_HINTS = ("删除", "取消", "停用", "停止", "停掉", "关掉", "不要了", "不再需要")
_RUN_NOW_HINTS = (
    "强制触发", "立即触发", "现在触发", "马上触发",
    "立即执行", "现在执行", "马上执行", "执行一次", "跑一次", "触发一次",
)
_TASK_REFERENCE_HINTS = ("定时任务", "定时计划", "任务", "计划", "这条", "这个", "该")
_HIGH_RISK_HINTS = (
    "删除", "清空", "重启", "关机", "部署", "上线", "改权限", "开放端口",
    "转账", "付款", "购买", "下单", "发邮件", "发消息给", "群发", "提交代码",
)
_AGENT_HINTS = (
    "github", "榜单", "热门", "搜索", "查询", "统计", "汇总", "报告", "新闻",
    "天气", "价格", "生成", "整理", "推送",
)
_CLOCK_RE = re.compile(
    r"(?:(?:每天|每日|每晚|每早)\s*)?"
    r"(?:(凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
    r"([01]?\d|2[0-3])\s*(?:[:：点时])\s*([0-5]?\d)?\s*分?",
)
_LEADING_SCHEDULE_RE = re.compile(
    r"^.*?(?:定时任务|定时计划|定时提醒)\s*[，,：:]?\s*(?:要求)?\s*",
)
_DAILY_CLAUSE_RE = re.compile(
    r"(?:每天|每日|每晚|每早)\s*"
    r"(?:(?:凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
    r"(?:[01]?\d|2[0-3])\s*(?:[:：点时])\s*(?:[0-5]?\d)?\s*分?",
)


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _is_schedule_request(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return bool(lowered) and any(hint in lowered for hint in _SCHEDULE_HINTS) and (
        any(hint in lowered for hint in _CREATE_HINTS)
        or any(hint in lowered for hint in _UPDATE_HINTS)
        or any(hint in lowered for hint in ("每天", "每日", "每晚", "每早", "每周", "每隔"))
    )


def _is_run_now_request(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return (
        bool(lowered)
        and any(hint in lowered for hint in _RUN_NOW_HINTS)
        and any(hint in lowered for hint in _TASK_REFERENCE_HINTS)
    )


def _is_disable_request(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return (
        bool(lowered)
        and any(hint in lowered for hint in _DISABLE_HINTS)
        and any(hint in lowered for hint in _TASK_REFERENCE_HINTS)
    )


def _history_text(history: list[dict] | None) -> str:
    return "\n".join(
        str(item.get("content") or "")
        for item in (history or [])[-8:]
        if isinstance(item, dict)
    )


def _daily_time(text: str) -> str:
    normalized = str(text or "").replace("\u70b9\u949f", "\u70b9")
    match = _CLOCK_RE.search(normalized)
    if not match:
        return ""
    period, raw_hour, raw_minute = match.groups()
    hour = int(raw_hour)
    minute = int(raw_minute or 0)
    if period in {"下午", "傍晚", "晚上"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _instruction(text: str) -> str:
    value = _LEADING_SCHEDULE_RE.sub("", str(text or "").strip(), count=1)
    value = re.sub(r"^(?:\u5462|\u5440|\u5427)\s*[,，:：]?\s*", "", value, count=1)
    value = value.replace("\u70b9\u949f", "\u70b9")
    value = _DAILY_CLAUSE_RE.sub("", value, count=1)
    value = re.sub(r"^(?:请|麻烦|帮我|给我|要求)\s*", "", value)
    value = re.sub(r"^(?:给我)?推送\s*", "获取并整理", value)
    value = re.sub(r"[，,。；;\s]+$", "", value).strip(" ，,：:")
    return _clip(value, 4000)


def _automation_parameters(instruction: str) -> dict:
    """Extract stable business constraints without asking the model to infer them."""

    text = str(instruction or "")
    parameters: dict[str, object] = {}
    count = re.search(r"(?:\u6bcf\u5929|\u6bcf\u65e5|\u524d|\u53d6|\u8981|\u5171)\s*(\d+)\s*(?:\u6761|\u4e2a|\u9879)", text)
    if count:
        parameters["item_limit"] = max(1, min(int(count.group(1)), 100))
    lowered = text.lower()
    if "github" in lowered:
        parameters["source"] = "github"
    if "AI" in text.upper() or "aiagent" in lowered or "ai agent" in lowered:
        parameters["topic"] = "ai_agent"
    if any(token in text for token in ("\u4e0d\u5141\u8bb8\u51fa\u73b0\u91cd\u590d", "\u4e0d\u8981\u91cd\u590d", "\u53bb\u91cd")):
        parameters["dedupe_policy"] = "job_history"
    if any(token in text for token in ("\u804a\u5929\u8bb0\u5f55", "\u804a\u5929\u65b9\u5f0f", "\u53d1\u9001\u6d88\u606f")):
        parameters["delivery_format"] = "conversation"
    if any(token in text for token in ("中文", "简体中文", "使用中文", "必须是中文")):
        parameters["output_language"] = "zh-CN"
    return parameters


# The extraction contract lives in its own bounded module.  Importing these
# names after the legacy helpers keeps older callers compatible while routing
# every new schedule through the current-turn objective-preserving parser.
from bridge_automation_instruction import (  # noqa: E402
    extract_instruction as _instruction,
    extract_parameters as _automation_parameters,
)


def parse_automation_action(
    message: str,
    history: list[dict] | None = None,
    *,
    current_group_id: str = "",
) -> dict | None:
    """Parse only explicit schedule requests; return ``None`` for normal chat."""

    text = str(message or "").strip()
    if _is_run_now_request(text) and _is_schedule_request(text) and any(hint in text for hint in _UPDATE_HINTS):
        return {
            "action_type": "automation_create_clarification",
            "reason": "multi_action_contract_required",
        }
    if _is_disable_request(text):
        if current_group_id:
            return {"action_type": "automation_disable_blocked", "reason": "owner_private_only"}
        context = f"{_history_text(history)}\n{text}".lower()
        return {
            "action_type": "automation_disable",
            "target_source": "github" if "github" in context or "githu" in context else "latest",
        }
    if _is_run_now_request(text):
        if current_group_id:
            return {"action_type": "automation_run_now_blocked", "reason": "owner_private_only"}
        context = f"{_history_text(history)}\n{text}".lower()
        return {
            "action_type": "automation_run_now",
            "target_source": "github" if "github" in context or "githu" in context else "latest",
        }
    if not _is_schedule_request(text):
        return None
    if current_group_id:
        return {"action_type": "automation_create_blocked", "reason": "owner_private_only"}
    lowered = text.lower()
    if any(hint in lowered for hint in _HIGH_RISK_HINTS):
        return {"action_type": "automation_create_blocked", "reason": "high_risk_schedule"}
    if any(hint in lowered for hint in _UPDATE_HINTS):
        context = f"{_history_text(history)}\n{text}".lower()
        changes = {}
        if any(token in text for token in ("中文", "简体中文", "使用中文", "必须是中文")):
            changes["output_language"] = "zh-CN"
        if "格式" in text and any(hint in text for hint in _READABLE_FORMAT_HINTS):
            changes["delivery_format"] = "conversation"
        if changes:
            return {
                "action_type": "automation_update",
                "target_source": "github" if "github" in context or "githu" in context else "latest",
                "changes": changes,
            }
        return {
            "action_type": "automation_create_clarification",
            "reason": "update_scope_required",
        }
    if not any(hint in lowered for hint in ("每天", "每日", "每晚", "每早")):
        return {
            "action_type": "automation_create_clarification",
            "reason": "unsupported_or_missing_frequency",
        }
    time_of_day = _daily_time(text)
    instruction = _instruction(text)
    if not time_of_day:
        return {"action_type": "automation_create_clarification", "reason": "time_required"}
    if not instruction or instruction in {"提醒我", "推送", "执行"}:
        return {"action_type": "automation_create_clarification", "reason": "instruction_required"}
    action_type = "agent" if any(hint in instruction.lower() for hint in _AGENT_HINTS) else "reminder"
    title_seed = re.sub(r"\s+", " ", instruction)[:36]
    parameters = _automation_parameters(instruction)
    return {
        "action_type": "automation_create",
        "schedule_type": "daily",
        "time_of_day": time_of_day,
        "timezone": DEFAULT_TIMEZONE,
        "job_action_type": action_type,
        "instruction": instruction,
        "parameters": parameters,
        "title": f"每日 {time_of_day} · {title_seed}",
    }


def _authorise_owner(conn: sqlite3.Connection, actor_id: str) -> None:
    access = check_qq_access(
        conn,
        {
            "sender_id": actor_id,
            "event_type": "private",
            "requested_action": "settings",
        },
    )
    if not access.get("allowed") or access.get("role") != "super_admin":
        raise PermissionError("automation_owner_required")


def _job_id(actor_id: str, action: dict) -> str:
    canonical = "|".join(
        (
            str(actor_id or ""),
            str(action.get("schedule_type") or ""),
            str(action.get("time_of_day") or ""),
            str(action.get("timezone") or ""),
            str(action.get("job_action_type") or ""),
            str(action.get("instruction") or ""),
        ),
    )
    return "nl-auto-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _local_due(value: object, zone_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "尚未计算"
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        if zone_name == "Asia/Shanghai":
            zone = timezone(timedelta(hours=8), zone_name)
        elif zone_name in {"UTC", "Etc/UTC"}:
            zone = timezone.utc
        else:
            raise ValueError("invalid_timezone")
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _receipt(action_type: str, status: str, target_id: str = "", **facts: object) -> dict:
    return {
        "receipt_id": "automation-action-" + uuid.uuid4().hex,
        "action_type": action_type,
        "status": status,
        "target_type": "automation_job",
        "target_id": target_id,
        "facts": facts,
    }


def execute_automation_action(
    connect: Callable[[], sqlite3.Connection],
    *,
    actor_id: str,
    action: dict,
    trace_id: str = "",
    preflight: Callable[[dict], dict] | None = None,
) -> dict:
    action_type = str(action.get("action_type") or "")
    reason = str(action.get("reason") or "")
    if action_type == "automation_action_plan":
        target_job_id = str(action.get("target_job_id") or "")
        expected_revision = int(action.get("target_revision") or 0)
        steps = action.get("actions") if isinstance(action.get("actions"), list) else []
        seen_step_ids: set[str] = set()
        valid_step_types = {"automation.schedule.update", "automation.schedule.run_now"}
        plan_error = ""
        if not target_job_id or not expected_revision or not steps:
            plan_error = "automation_action_plan_target_or_steps_missing"
        for step in steps:
            if plan_error:
                break
            if not isinstance(step, dict):
                plan_error = "automation_action_plan_step_invalid"
                break
            step_id = str(step.get("id") or "")
            step_type = str(step.get("type") or "")
            dependencies = step.get("depends_on") if isinstance(step.get("depends_on"), list) else None
            if not step_id or step_id in seen_step_ids or step_type not in valid_step_types or dependencies is None:
                plan_error = "automation_action_plan_step_invalid"
                break
            if any(str(item or "") not in seen_step_ids for item in dependencies):
                plan_error = "automation_action_plan_dependency_invalid"
                break
            seen_step_ids.add(step_id)
        if plan_error:
            return {
                "ok": True,
                "dispatch": "automation_action_plan_failed",
                "reply": "自动化连续动作计划不完整，本轮没有修改或触发任何任务。",
                "automation_job": {"id": target_job_id, "revision": expected_revision},
                "action_receipts": [
                    _receipt("automation.action_plan", "blocked", target_job_id, reason=plan_error),
                ],
                "plan_status": "failed",
            }
        receipts: list[dict] = []
        replies: list[str] = []
        latest_revision = expected_revision
        for step in steps:
            if not isinstance(step, dict):
                continue
            if any(
                receipt.get("status") not in {"completed", "no_op"}
                for receipt in receipts
            ):
                receipts.append(
                    _receipt(
                        str(step.get("type") or "automation.unknown"),
                        "blocked",
                        target_job_id,
                        reason="dependency_failed",
                    ),
                )
                break
            step_type = str(step.get("type") or "")
            nested = {
                "action_type": {
                    "automation.schedule.update": "automation_update",
                    "automation.schedule.run_now": "automation_run_now",
                }.get(step_type, ""),
                "target_job_id": target_job_id,
                "expected_revision": latest_revision,
                "changes": step.get("changes") if isinstance(step.get("changes"), dict) else {},
            }
            result = execute_automation_action(
                connect,
                actor_id=actor_id,
                action=nested,
                trace_id=trace_id,
                preflight=preflight,
            )
            step_receipts = [dict(item) for item in result.get("action_receipts") or [] if isinstance(item, dict)]
            receipts.extend(step_receipts)
            if result.get("reply"):
                replies.append(str(result["reply"]))
            job = result.get("automation_job") if isinstance(result.get("automation_job"), dict) else {}
            latest_revision = int(job.get("revision") or latest_revision)
        succeeded = bool(receipts) and all(item.get("status") in {"completed", "no_op"} for item in receipts)
        return {
            "ok": True,
            "dispatch": "automation_action_plan" if succeeded else "automation_action_plan_failed",
            "reply": "\n".join(replies),
            "automation_job": {"id": target_job_id, "revision": latest_revision},
            "action_receipts": receipts,
            "plan_status": "completed" if succeeded else "failed",
        }
    if action_type == "automation_create_clarification":
        replies = {
            "time_required": "我知道这是长期定时任务，但还缺每天几点执行。请补一个明确时间，例如“每天 09:00”。",
            "instruction_required": "执行时间已经明确，但还缺每次要做的具体内容。请补充要提醒、查询或生成什么。",
            "unsupported_or_missing_frequency": "我识别到你要创建定时任务，但当前还缺明确的每日频率；请告诉我每天几点执行。",
            "update_scope_required": "我知道你要修改现有定时任务，但还缺明确的修改目标；请说明要改哪类任务和具体改动。",
            "multi_action_contract_required": "我知道你要先修改再立即执行，但当前连续动作契约尚未启用；本轮没有跳过修改直接触发。",
        }
        return {
            "ok": True,
            "dispatch": "automation_clarification",
            "reply": replies.get(reason, replies["unsupported_or_missing_frequency"]),
            "action_receipts": [_receipt(action_type, "not_created", reason=reason)],
        }
    if action_type == "automation_create_blocked":
        reply = (
            "群聊里暂不自动创建长期计划，请由 Owner 在私聊中授权。"
            if reason == "owner_private_only"
            else "这条计划包含高风险或对外写入动作，本轮没有自动创建；需要先明确执行边界和审批方式。"
        )
        return {
            "ok": True,
            "dispatch": "automation_blocked",
            "reply": reply,
            "action_receipts": [_receipt(action_type, "blocked", reason=reason)],
        }
    if action_type == "automation_run_now_blocked":
        return {
            "ok": True,
            "dispatch": "automation_blocked",
            "reply": "群聊里不能强制触发 Owner 的长期任务，请由 Owner 在私聊中执行。",
            "action_receipts": [
                _receipt("automation.schedule.run_now", "blocked", reason=reason),
            ],
        }
    if action_type == "automation_disable_blocked":
        return {
            "ok": True,
            "dispatch": "automation_blocked",
            "reply": "群聊里不能停用 Owner 的长期任务，请由 Owner 在私聊中执行。",
            "action_receipts": [
                _receipt("automation.schedule.disable", "blocked", reason=reason),
            ],
        }
    if action_type == "automation_disable":
        return execute_automation_disable(
            connect,
            actor_id=actor_id,
            action=action,
            trace_id=trace_id,
            authorise_owner=_authorise_owner,
            receipt=_receipt,
        )
    if action_type == "automation_run_now":
        try:
            with connect() as conn:
                _authorise_owner(conn, actor_id)
                ensure_automation_tables(conn)
                target_source = str(action.get("target_source") or "latest")
                target_job_id = _clip(action.get("target_job_id"), 80)
                expected_revision = int(action.get("expected_revision") or 0)
                source_filter = ""
                if target_source == "github":
                    source_filter = """
                      AND (
                          lower(j.instruction) LIKE '%github%'
                          OR lower(j.instruction) LIKE '%githu%'
                          OR lower(j.parameters_json) LIKE '%github%'
                      )
                    """
                target_filter = " AND j.id=?" if target_job_id else ""
                params = (actor_id, target_job_id) if target_job_id else (actor_id,)
                row = conn.execute(
                    f"""
                    SELECT j.*,
                           COALESCE((
                               SELECT MAX(r.finished_at) FROM automation_runs r
                               WHERE r.job_id=j.id AND r.status='completed'
                           ),'') AS last_completed_at,
                           COALESCE((
                               SELECT COUNT(*) FROM automation_runs r
                               WHERE r.job_id=j.id AND r.status IN ('running','dispatched')
                           ),0) AS active_run_count
                    FROM automation_jobs j
                    WHERE j.user_id=? AND j.enabled=1
                    {source_filter}
                    {target_filter}
                    ORDER BY last_completed_at DESC,j.updated_at DESC LIMIT 1
                    """,
                    params,
                ).fetchone()
                if row is None:
                    return {
                        "ok": True,
                        "dispatch": "automation_run_now_missing",
                        "reply": "没有找到可立即执行的已启用定时任务，本轮没有触发任何任务。",
                        "action_receipts": [
                            _receipt("automation.schedule.run_now", "not_found", reason="target_missing"),
                        ],
                    }
                job = dict(row)
                if expected_revision and int(job.get("revision") or 0) != expected_revision:
                    return {
                        "ok": True,
                        "dispatch": "automation_run_now_conflict",
                        "reply": "这条定时任务在修改后又发生了变化，本轮没有按旧版本触发。",
                        "automation_job": {"id": job["id"], "revision": int(job.get("revision") or 0)},
                        "action_receipts": [
                            _receipt(
                                "automation.schedule.run_now", "blocked", job["id"],
                                reason="revision_conflict",
                                expected_revision=expected_revision,
                                actual_revision=int(job.get("revision") or 0),
                            ),
                        ],
                    }
                if int(job.get("active_run_count") or 0) > 0 or str(job.get("state") or "") in {
                    "running",
                    "dispatched",
                }:
                    return {
                        "ok": True,
                        "dispatch": "automation_run_now_busy",
                        "reply": "这条定时任务已有一次运行尚未结束，本轮没有重复触发。",
                        "automation_job": {"id": job["id"], "state": job["state"]},
                        "action_receipts": [
                            _receipt(
                                "automation.schedule.run_now",
                                "no_op",
                                job["id"],
                                reason="run_already_active",
                            ),
                        ],
                    }
                triggered_at = datetime.now(timezone.utc).isoformat()
                updated = conn.execute(
                    """
                    UPDATE automation_jobs
                    SET state='scheduled',next_due_at=?,lease_until='',updated_at=?
                    WHERE id=? AND user_id=? AND enabled=1
                      AND state NOT IN ('running','dispatched')
                      AND (?=0 OR revision=?)
                    """,
                    (
                        triggered_at, triggered_at, job["id"], actor_id,
                        expected_revision, expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("automation_run_now_conflict")
                conn.commit()
                job.update(
                    {
                        "state": "scheduled",
                        "next_due_at": triggered_at,
                        "updated_at": triggered_at,
                    },
                )
            return {
                "ok": True,
                "dispatch": "automation_run_now",
                "reply": "已将这条定时任务加入立即执行队列；本次执行结果会作为单独消息发送，原有定时周期保持不变。",
                "automation_job": {
                    "id": job["id"],
                    "state": job["state"],
                    "next_due_at": job["next_due_at"],
                    "timezone": job["timezone"],
                    "enabled": bool(job["enabled"]),
                    "revision": int(job.get("revision") or 1),
                },
                "action_receipts": [
                    _receipt(
                        "automation.schedule.run_now",
                        "completed",
                        job["id"],
                        queued_at=triggered_at,
                        schedule_preserved=True,
                        trace_id=_clip(trace_id, 80),
                    ),
                ],
            }
        except PermissionError:
            return {
                "ok": True,
                "dispatch": "automation_denied",
                "reply": "立即执行长期任务需要 Owner 权限，本轮没有触发任何任务。",
                "action_receipts": [
                    _receipt("automation.schedule.run_now", "denied", reason="owner_required"),
                ],
            }
        except (sqlite3.Error, ValueError, KeyError) as exc:
            reason = _clip(exc, 160) or "unknown_error"
            return {
                "ok": True,
                "dispatch": "automation_failed",
                "reply": f"定时任务没有成功进入执行队列：{reason}。系统没有把它当作已触发。",
                "action_receipts": [
                    _receipt("automation.schedule.run_now", "failed", reason=reason),
                ],
            }
    if action_type == "automation_update":
        try:
            with connect() as conn:
                _authorise_owner(conn, actor_id)
                ensure_automation_tables(conn)
                target_source = str(action.get("target_source") or "latest")
                target_job_id = _clip(action.get("target_job_id"), 80)
                expected_revision = int(action.get("expected_revision") or 0)
                source_filter = ""
                if target_source == "github":
                    source_filter = """
                      AND (
                          lower(j.instruction) LIKE '%github%'
                          OR lower(j.instruction) LIKE '%githu%'
                          OR lower(j.parameters_json) LIKE '%github%'
                      )
                    """
                row = conn.execute(
                    f"""
                    SELECT j.*,
                           COALESCE((
                               SELECT MAX(r.finished_at) FROM automation_runs r
                               WHERE r.job_id=j.id AND r.status='completed'
                           ),'') AS last_completed_at,
                           COALESCE((
                               SELECT COUNT(*) FROM automation_runs r
                               WHERE r.job_id=j.id AND r.status IN ('running','dispatched')
                           ),0) AS active_run_count
                    FROM automation_jobs j
                    WHERE j.user_id=? AND j.enabled=1
                    {source_filter}
                    {" AND j.id=?" if target_job_id else ""}
                    ORDER BY last_completed_at DESC,j.updated_at DESC LIMIT 1
                    """,
                    (actor_id, target_job_id) if target_job_id else (actor_id,),
                ).fetchone()
                if row is None:
                    return {
                        "ok": True,
                        "dispatch": "automation_update_missing",
                        "reply": "没有找到可修改的已启用定时任务，本轮没有写入任何变更。",
                        "action_receipts": [
                            _receipt("automation.schedule.update", "not_found", reason="target_missing"),
                        ],
                    }
                job = dict(row)
                if int(job.get("active_run_count") or 0) > 0:
                    return {
                        "ok": True,
                        "dispatch": "automation_update_busy",
                        "reply": "这条定时任务正在运行，本轮没有修改它，也不会触发依赖动作。",
                        "automation_job": {"id": job["id"], "revision": int(job.get("revision") or 0)},
                        "action_receipts": [
                            _receipt("automation.schedule.update", "blocked", job["id"], reason="run_already_active"),
                        ],
                    }
                if expected_revision and int(job.get("revision") or 0) != expected_revision:
                    return {
                        "ok": True,
                        "dispatch": "automation_update_conflict",
                        "reply": "这条定时任务已经被其他变更更新，本轮没有覆盖新版本。",
                        "automation_job": {"id": job["id"], "revision": int(job.get("revision") or 0)},
                        "action_receipts": [
                            _receipt(
                                "automation.schedule.update", "blocked", job["id"],
                                reason="revision_conflict",
                                expected_revision=expected_revision,
                                actual_revision=int(job.get("revision") or 0),
                            ),
                        ],
                    }
                try:
                    parameters = json.loads(str(job.get("parameters_json") or "{}"))
                except json.JSONDecodeError:
                    parameters = {}
                parameters = parameters if isinstance(parameters, dict) else {}
                changes = action.get("changes") if isinstance(action.get("changes"), dict) else {}
                legacy_parameters = changes.get("legacy_parameters") if isinstance(changes.get("legacy_parameters"), dict) else {}
                for key in ("output_language", "delivery_format"):
                    if key in changes:
                        legacy_parameters[key] = changes[key]
                parameters.update(legacy_parameters)
                output_contract = normalize_output_contract(
                    changes.get("output_contract") or job.get("output_contract_json"),
                )
                contract_json = json.dumps(
                    output_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                contract_hash = output_contract_hash(output_contract)
                instruction = str(job.get("instruction") or "").strip()
                language_rule = "输出要求：除项目名、技术术语和链接外，所有说明使用简体中文。"
                if legacy_parameters.get("output_language") == "zh-CN" and language_rule not in instruction:
                    instruction = f"{instruction}；{language_rule}".strip("；")
                format_rule = (
                    "输出格式要求：使用适合 QQ 阅读的中文聊天式摘要；先给结论，再按条目说明"
                    "关键信息；不要输出数据库字段、内部 ID 或技术表格；链接紧跟对应条目。"
                )
                if legacy_parameters.get("delivery_format") == "conversation" and format_rule not in instruction:
                    instruction = f"{instruction}；{format_rule}".strip("；")
                updated_at = datetime.now(timezone.utc).isoformat()
                updated = conn.execute(
                    """UPDATE automation_jobs
                       SET instruction=?,parameters_json=?,revision=revision+1,
                           output_contract_json=?,output_contract_hash=?,updated_at=?
                       WHERE id=? AND (?=0 OR revision=?)""",
                    (
                        instruction,
                        json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                        contract_json, contract_hash, updated_at,
                        job["id"],
                        expected_revision, expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("automation_update_conflict")
                conn.commit()
                job.update(
                    {
                        "instruction": instruction,
                        "parameters_json": json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                        "updated_at": updated_at,
                        "revision": int(job.get("revision") or 1) + 1,
                        "output_contract_json": contract_json,
                        "output_contract_hash": contract_hash,
                    },
                )
            due = _local_due(job.get("next_due_at"), str(job.get("timezone") or DEFAULT_TIMEZONE))
            change_facts = []
            if legacy_parameters.get("output_language") == "zh-CN":
                change_facts.append("说明统一使用简体中文")
            if legacy_parameters.get("delivery_format") == "conversation":
                change_facts.append("结果改为适合 QQ 阅读的聊天式摘要")
            change_summary = "；".join(change_facts) or "已应用请求中的格式变更"
            return {
                "ok": True,
                "dispatch": "automation_update",
                "reply": f"已修改最近一次匹配的定时任务：{change_summary}。下一次运行：{due}。",
                "automation_job": {
                    "id": job["id"],
                    "time_of_day": job["time_of_day"],
                    "timezone": job["timezone"],
                    "enabled": bool(job["enabled"]),
                    "state": job["state"],
                    "next_due_at": job["next_due_at"],
                    "output_language": parameters.get("output_language", ""),
                    "delivery_format": parameters.get("delivery_format", ""),
                    "revision": int(job.get("revision") or 1),
                    "output_contract_hash": contract_hash,
                },
                "action_receipts": [
                    _receipt(
                        "automation.schedule.update",
                        "completed",
                        job["id"],
                        output_language=parameters.get("output_language", ""),
                        delivery_format=parameters.get("delivery_format", ""),
                        next_due_at=job["next_due_at"],
                        trace_id=_clip(trace_id, 80),
                    ),
                ],
            }
        except PermissionError:
            return {
                "ok": True,
                "dispatch": "automation_denied",
                "reply": "修改长期自动化需要 Owner 权限，本轮没有写入任何变更。",
                "action_receipts": [
                    _receipt("automation.schedule.update", "denied", reason="owner_required"),
                ],
            }
        except (sqlite3.Error, ValueError, KeyError) as exc:
            reason = _clip(exc, 160) or "unknown_error"
            return {
                "ok": True,
                "dispatch": "automation_failed",
                "reply": f"定时任务没有修改成功：{reason}。系统没有把它当作已完成。",
                "action_receipts": [
                    _receipt("automation.schedule.update", "failed", reason=reason),
                ],
            }
    if action_type != "automation_create":
        return {"ok": False, "error": "unsupported_automation_action"}

    try:
        with connect() as conn:
            _authorise_owner(conn, actor_id)
        if preflight is not None and str(action.get("job_action_type") or "") == "agent":
            check = dict(preflight(action) or {})
            if not check.get("ok"):
                reason = _clip(check.get("error_kind") or check.get("error") or "automation_preflight_failed", 160)
                return {
                    "ok": True,
                    "dispatch": "automation_unavailable",
                    "reply": "定时 Agent 未创建：执行环境未就绪，任务没有写入。请先完成模型或工作区配置。",
                    "action_receipts": [_receipt("automation.schedule.create", "blocked", reason=reason)],
                }
        with connect() as conn:
            _authorise_owner(conn, actor_id)
            ensure_automation_tables(conn)
            job_id = _job_id(actor_id, action)
            existed = conn.execute("SELECT id FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
            job = upsert_automation_job(
                conn,
                {
                    "id": job_id,
                    "user_id": actor_id,
                    "title": action["title"],
                    "instruction": action["instruction"],
                    "action_type": action["job_action_type"],
                    "schedule_type": action["schedule_type"],
                    "time_of_day": action["time_of_day"],
                    "timezone": action["timezone"],
                    "enabled": 1,
                    "parameters": action.get("parameters") or {},
                },
            )
            conn.commit()
        status = "no_op" if existed else "completed"
        due = _local_due(job.get("next_due_at"), str(job.get("timezone") or DEFAULT_TIMEZONE))
        reply = (
            f"{'这个定时任务已经存在，我没有重复创建' if existed else '已创建并启用定时任务'}："
            f"每天 {job['time_of_day']}（{job['timezone']}）执行。下一次运行：{due}。"
            f"\n任务内容：{job['instruction']}"
        )
        return {
            "ok": True,
            "dispatch": "automation_action",
            "reply": reply,
            "automation_job": {
                "id": job["id"],
                "schedule_type": job["schedule_type"],
                "time_of_day": job["time_of_day"],
                "timezone": job["timezone"],
                "enabled": bool(job["enabled"]),
                "state": job["state"],
                "next_due_at": job["next_due_at"],
            },
            "action_receipts": [
                _receipt(
                    "automation.schedule.create",
                    status,
                    job["id"],
                    schedule_type=job["schedule_type"],
                    time_of_day=job["time_of_day"],
                    timezone=job["timezone"],
                    enabled=bool(job["enabled"]),
                    next_due_at=job["next_due_at"],
                    trace_id=_clip(trace_id, 80),
                ),
            ],
        }
    except PermissionError:
        return {
            "ok": True,
            "dispatch": "automation_denied",
            "reply": "创建长期自动化需要 Owner 权限，本轮没有创建任何定时任务。",
            "action_receipts": [_receipt("automation.schedule.create", "denied", reason="owner_required")],
        }
    except (sqlite3.Error, ValueError, KeyError) as exc:
        reason = _clip(exc, 160) or "unknown_error"
        return {
            "ok": True,
            "dispatch": "automation_failed",
            "reply": f"定时任务没有创建成功：{reason}。系统没有把它当作已完成。",
            "action_receipts": [_receipt("automation.schedule.create", "failed", reason=reason)],
        }


def build_automation_mode_decision(action: dict) -> dict:
    action_type = str(action.get("action_type") or "automation_create")
    status_action = {
        "automation_create": "automation.schedule.create",
        "automation_update": "automation.schedule.update",
        "automation_disable": "automation.schedule.disable",
        "automation_run_now": "automation.schedule.run_now",
    }.get(action_type, "respond")
    definition = action_definition(status_action)
    planned_actions = []
    if action_type == "automation_action_plan":
        for index, step in enumerate(action.get("actions") or [], start=1):
            if not isinstance(step, dict):
                continue
            step_type = str(step.get("type") or "respond")
            step_definition = action_definition(step_type)
            planned_actions.append(
                {
                    "id": str(step.get("id") or f"action-{index}"),
                    "type": step_type,
                    "intent_id": "intent-1",
                    "objective": "按顺序完成已绑定自动化对象的控制动作",
                    "requires_tools": step_definition.requires_tools,
                    "risk_level": step_definition.risk_level,
                    "depends_on": list(step.get("depends_on") or []),
                },
            )
    if not planned_actions:
        planned_actions = [
            {
                "id": "action-1",
                "type": status_action,
                "intent_id": "intent-1",
                "objective": "返回自动化动作的事实、状态与结构化回执",
                "requires_tools": definition.requires_tools,
                "risk_level": definition.risk_level,
                "depends_on": [],
            },
        ]
    plan = {
        "schema_version": 2,
        "summary_mode": "work",
        "primary_intent": "automation",
        "confidence": 1.0,
        "reason": "命中 Bridge 支持的确定性自然语言自动化能力。",
        "intents": [
            {
                "id": "intent-1",
                "type": "automation",
                "confidence": 1.0,
                "objective": "创建、修改、停用、触发或澄清一个持久化定时计划",
                "requires_tools": definition.requires_tools,
                "risk_level": definition.risk_level,
            },
        ],
        "reply_parts": [],
        "actions": planned_actions,
        "approval_requests": [],
        "memory_candidates": [],
    }
    return {
        "mode": "work",
        "intent": "automation",
        "confidence": 1.0,
        "reason": plan["reason"],
        "work_lifecycle": "none",
        "end_work": False,
        "allow_emoji": False,
        "need_tools": action_type in {
            "automation_create", "automation_update", "automation_disable", "automation_run_now",
        } or any(bool(item.get("requires_tools")) for item in planned_actions),
        "execution_lane": status_action,
        "response_style": "structured",
        "emotion": "neutral",
        "reply_length": "medium",
        "meme_intent": "none",
        "engagement": "respond",
        "source": "automation_control_router",
        "interaction_plan": plan,
    }


def dispatch_automation_action(
    connect: Callable[[], sqlite3.Connection],
    store: object,
    actor_id: str,
    message: str,
    history: list[dict] | None,
    trace_id: str,
    source: str,
    current_group_id: str = "",
    preflight: Callable[[dict], dict] | None = None,
    inbound_context: dict | None = None,
    resolve_target: Callable[[str, dict], dict] | None = None,
) -> dict | None:
    durable = None
    if not current_group_id and resolve_target is not None:
        durable = plan_automation_conversation(
            connect,
            actor_id=actor_id, message=message, history=history,
            inbound_context=inbound_context,
            resolve_target=resolve_target,
        )
    if durable is not None:
        plan = dict(durable.get("plan") or {})
        record = dict(durable.get("record") or {})
        target_job_id = str(record.get("target_job_id") or "")
        if plan.get("status") == "waiting_clarification":
            result = {
                "ok": True,
                "dispatch": "automation_clarification",
                "reply": clarification_reply(plan, target_resolved=bool(target_job_id)),
                "action_receipts": [
                    _receipt(
                        "request_clarification", "pending", target_job_id,
                        reason=str(plan.get("clarification_key") or "clarification_required"),
                        action_plan_id=str(record.get("id") or ""),
                    ),
                ],
            }
        else:
            action = {
                "action_type": "automation_action_plan",
                "target_job_id": target_job_id,
                "target_revision": int(record.get("target_revision") or 0),
                "actions": list(plan.get("actions") or []),
            }
            result = execute_automation_action(
                connect,
                actor_id=actor_id,
                action=action,
                trace_id=trace_id,
                preflight=preflight,
            )
            with connect() as conn:
                finish_action_plan(
                    conn,
                    str(record.get("id") or ""),
                    status=str(result.get("plan_status") or "failed"),
                    receipts=[dict(item) for item in result.get("action_receipts") or [] if isinstance(item, dict)],
                )
                conn.commit()
        mode_action = {
            "action_type": "automation_action_plan",
            "actions": list(plan.get("actions") or []),
        }
        mode_decision = build_automation_mode_decision(mode_action)
        plan_record = store.persist(actor_id, mode_decision, source=source)
        store.record_exchange(
            actor_id,
            message,
            str(result.get("reply") or ""),
            mode_decision,
            source=source,
            inbound_context=inbound_context,
            exchange_metadata={
                "automation_action_plan_id": str(record.get("id") or ""),
                "automation_job_id": target_job_id,
            },
        )
        result.update(
            {
                "mode": "work",
                "intent": "automation",
                "mode_decision": mode_decision,
                "interaction_plan": mode_decision["interaction_plan"],
                "interaction_plan_record": plan_record,
                "automation_action_plan_id": str(record.get("id") or ""),
            },
        )
        return result
    action = parse_automation_action(message, history, current_group_id=current_group_id)
    if action is None:
        return None
    result = execute_automation_action(
        connect,
        actor_id=actor_id,
        action=action,
        trace_id=trace_id,
        preflight=preflight,
    )
    mode_decision = build_automation_mode_decision(action)
    plan_record = store.persist(actor_id, mode_decision, source=source)
    store.record_exchange(
        actor_id,
        message,
        str(result.get("reply") or ""),
        mode_decision,
        source=source,
        inbound_context=inbound_context,
    )
    result.update(
        {
            "mode": "work",
            "intent": "automation",
            "mode_decision": mode_decision,
            "interaction_plan": mode_decision["interaction_plan"],
            "interaction_plan_record": plan_record,
        },
    )
    return result


__all__ = [
    "build_automation_mode_decision",
    "dispatch_automation_action",
    "execute_automation_action",
    "parse_automation_action",
]
