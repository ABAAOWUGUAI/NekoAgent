#!/usr/bin/env python3
"""Deterministic natural-language automation actions for private assistant chat.

The model may help execute a scheduled agent job later, but it never parses the
schedule, grants authority, or writes the durable job.  This module owns that
control-plane boundary and only auto-enables complete, low-risk Owner requests.
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
from bridge_automation_disable import execute_automation_disable
from bridge_qq_access_service import check_qq_access


_SCHEDULE_HINTS = (
    "定时任务", "定时计划", "定时提醒", "每天", "每日", "每晚", "每早",
    "每周", "每隔", "定期", "按时", "到点",
)
_CREATE_HINTS = ("做一个", "创建", "新建", "设置", "安排", "帮我", "要求", "提醒", "推送")
_UPDATE_HINTS = ("修改", "改成", "改为", "调整", "更新")
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


def parse_automation_action(
    message: str,
    history: list[dict] | None = None,
    *,
    current_group_id: str = "",
) -> dict | None:
    """Parse only explicit schedule requests; return ``None`` for normal chat."""

    text = str(message or "").strip()
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
        if any(token in lowered for token in ("github", "githu")) and any(
            token in text for token in ("中文", "简体中文", "使用中文", "必须是中文")
        ):
            return {
                "action_type": "automation_update",
                "target_source": "github",
                "changes": {"output_language": "zh-CN"},
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
    if action_type == "automation_create_clarification":
        replies = {
            "time_required": "我知道这是长期定时任务，但还缺每天几点执行。请补一个明确时间，例如“每天 09:00”。",
            "instruction_required": "执行时间已经明确，但还缺每次要做的具体内容。请补充要提醒、查询或生成什么。",
            "unsupported_or_missing_frequency": "我识别到你要创建定时任务，但当前还缺明确的每日频率；请告诉我每天几点执行。",
            "update_scope_required": "我知道你要修改现有定时任务，但还缺明确的修改目标；请说明要改哪类任务和具体改动。",
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
                    ORDER BY last_completed_at DESC,j.updated_at DESC LIMIT 1
                    """,
                    (actor_id,),
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
                    """,
                    (triggered_at, triggered_at, job["id"], actor_id),
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
                row = conn.execute(
                    """
                    SELECT j.*,
                           COALESCE((
                               SELECT MAX(r.finished_at) FROM automation_runs r
                               WHERE r.job_id=j.id AND r.status='completed'
                           ),'') AS last_completed_at
                    FROM automation_jobs j
                    WHERE j.user_id=? AND j.enabled=1
                      AND (
                          lower(j.instruction) LIKE '%github%'
                          OR lower(j.instruction) LIKE '%githu%'
                          OR lower(j.parameters_json) LIKE '%github%'
                      )
                    ORDER BY last_completed_at DESC,j.updated_at DESC LIMIT 1
                    """,
                    (actor_id,),
                ).fetchone()
                if row is None:
                    return {
                        "ok": True,
                        "dispatch": "automation_update_missing",
                        "reply": "没有找到可修改的 GitHub 定时任务，本轮没有写入任何变更。",
                        "action_receipts": [
                            _receipt("automation.schedule.update", "not_found", reason="target_missing"),
                        ],
                    }
                job = dict(row)
                try:
                    parameters = json.loads(str(job.get("parameters_json") or "{}"))
                except json.JSONDecodeError:
                    parameters = {}
                parameters = parameters if isinstance(parameters, dict) else {}
                changes = action.get("changes") if isinstance(action.get("changes"), dict) else {}
                parameters.update(changes)
                instruction = str(job.get("instruction") or "").strip()
                language_rule = "输出要求：除项目名、技术术语和链接外，所有说明使用简体中文。"
                if changes.get("output_language") == "zh-CN" and language_rule not in instruction:
                    instruction = f"{instruction}；{language_rule}".strip("；")
                updated_at = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """UPDATE automation_jobs
                       SET instruction=?,parameters_json=?,updated_at=?
                       WHERE id=?""",
                    (
                        instruction,
                        json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                        updated_at,
                        job["id"],
                    ),
                )
                conn.commit()
                job.update(
                    {
                        "instruction": instruction,
                        "parameters_json": json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                        "updated_at": updated_at,
                    },
                )
            due = _local_due(job.get("next_due_at"), str(job.get("timezone") or DEFAULT_TIMEZONE))
            return {
                "ok": True,
                "dispatch": "automation_update",
                "reply": (
                    "已修改最近一次执行的 GitHub 定时任务：后续结果除项目名、技术术语和链接外，"
                    f"统一使用简体中文。下一次运行：{due}。"
                ),
                "automation_job": {
                    "id": job["id"],
                    "time_of_day": job["time_of_day"],
                    "timezone": job["timezone"],
                    "enabled": bool(job["enabled"]),
                    "state": job["state"],
                    "next_due_at": job["next_due_at"],
                    "output_language": "zh-CN",
                },
                "action_receipts": [
                    _receipt(
                        "automation.schedule.update",
                        "completed",
                        job["id"],
                        output_language="zh-CN",
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
    plan = {
        "schema_version": 1,
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
        "actions": [
            {
                "id": "action-1",
                "type": status_action,
                "intent_id": "intent-1",
                "objective": "返回自动化动作的事实、状态与结构化回执",
                "requires_tools": definition.requires_tools,
                "risk_level": definition.risk_level,
            },
        ],
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
        },
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
) -> dict | None:
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
