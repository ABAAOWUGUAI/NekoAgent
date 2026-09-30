#!/usr/bin/env python3
"""Deterministic, scope-safe binding of work feedback to an existing Goal."""

from __future__ import annotations

import re
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from bridge_task_expression import (
    normalize_task_failure_projection,
    task_failure_projection,
    valid_retained_research_evidence,
)


DEFAULT_FOLLOWUP_WINDOW_HOURS = 72
_OPEN_GOAL_STATUSES = {"active", "waiting_user", "failed"}
_SHORT_CONTEXT_PHRASES = {
    "继续",
    "继续吧",
    "接着做",
    "继续处理",
    "然后呢",
    "再改一下",
    "继续修改",
    "继续改",
    "再调整一下",
    "重新做",
    "重做",
}
_WORK_CONTEXT_MARKERS = (
    "任务",
    "目标",
    "结果",
    "版本",
    "成品",
    "文件",
    "执行",
    "完成",
    "修改",
    "调整",
    "审批",
    "确认",
    "失败",
    "重试",
)
_FAILURE_FOLLOWUP_KINDS = {
    "explain_failure",
    "retry_failure",
    "change_method",
    "dismiss_failure",
}
_RETRY_FAILURE_FORMS = {
    "再试一次",
    "重来一次",
    "再来",
    "那再弄一次",
}
_CHANGE_METHOD_FORMS = {
    "换个方法",
    "那换个方法",
    "那用别的方法试试",
    "换一种方式",
}
_DISMISS_FAILURE_FORMS = {
    "算了",
    "不弄了",
    "先这样吧",
}
_EXPLAIN_FAILURE_FORMS = {
    "为什么",
    "什么情况",
    "具体什么情况",
    "具体什么情况你倒是说啊",
    "怎么回事",
    "为什么失败",
    "刚才为什么失败",
    "哪里出问题",
    "哪里出问题了",
    "怎么没成功",
    "刚才怎么没成功",
    "怎么失败",
    "你倒是说",
    "你倒是说啊",
}
_TASK_REFERENCE_PATTERN = r"(?:任务\s*#?|#)([0-9a-f]{8})\b"


def _normalise_message(message: str) -> str:
    return " ".join(str(message or "").strip().split()).lower()


def _semantic_key(message: str) -> str:
    return re.sub(r"[\s，。！？?!、,.；;：:]+", "", _normalise_message(message))


def classify_goal_followup(message: str) -> str:
    text = _normalise_message(message)
    if not text or len(text) > 4000:
        return ""
    failure_text = re.sub(_TASK_REFERENCE_PATTERN, "", text, flags=re.I).strip()
    key = _semantic_key(failure_text)
    if len(key) <= 40:
        if key in _RETRY_FAILURE_FORMS:
            return "retry_failure"
        if key in _CHANGE_METHOD_FORMS:
            return "change_method"
        if key in _DISMISS_FAILURE_FORMS:
            return "dismiss_failure"
        if key in _EXPLAIN_FAILURE_FORMS:
            return "explain_failure"
    if text in {"可以了", "就这样", "这版可以", "就按这版", "通过", "没问题了", "完成了"} or text.startswith(("这版可以", "就按这版")):
        return "accepted"
    if text.startswith(("不要这版", "这版不行", "放弃这个结果", "这个结果不要")):
        return "rejected"
    if text.startswith(("纠正一下", "不是这个意思", "不是这样", "我说错了")) or (text.startswith("不是") and "而是" in text):
        return "corrected"
    if text in {"继续", "继续吧", "接着做", "继续处理", "然后呢"}:
        return "continue"
    if text.startswith(("继续修改", "继续改", "改成", "修改为", "再调整", "重新做", "重做", "再来一版", "不对，", "不对,")):
        return "needs_change"
    return ""


def _task_references(message: str) -> list[str]:
    references = []
    for match in re.finditer(_TASK_REFERENCE_PATTERN, str(message or ""), flags=re.I):
        task_id = str(match.group(1)).lower()
        if task_id not in references:
            references.append(task_id)
    return references


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    return _parse_timestamp(value) or datetime.now(timezone.utc)


def _has_recent_work_context(history: Iterable[Mapping[str, object]] | None) -> bool:
    items = list(history or [])
    for item in reversed(items[-4:]):
        if str(item.get("role") or "").strip().lower() != "assistant":
            continue
        content = str(item.get("content") or "")
        return any(marker in content for marker in _WORK_CONTEXT_MARKERS)
    return False


def _candidate_summary(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "goal_id": str(row["id"]),
        "goal_status": str(row["goal_status"] if "goal_status" in keys else row["status"]),
        "legacy_task_id": str(row["legacy_task_id"] or ""),
        "title": str(row["title"] or "")[:80],
        "updated_at": str(
            row["run_updated_at"]
            if "run_updated_at" in keys
            else row["updated_at"] or ""
        ),
    }


def _resolution(kind: str, state: str, *, reason: str, candidates: list[dict] | None = None, task_ref: str = "") -> dict:
    return {
        "kind": kind,
        "resolution": state,
        "reason": reason,
        "task_reference": task_ref,
        "candidates": list(candidates or []),
    }


def _decoded_object(value: object) -> dict:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _retained_research_state(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    now: str | datetime | None,
) -> tuple[bool, int]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "evidence" not in tables:
        return False, 0
    rows = conn.execute(
        "SELECT source_uri,content_hash,excerpt,metadata_json,expires_at FROM evidence WHERE run_id=?",
        (str(run_id),),
    ).fetchall()
    current = _now(now)
    valid = 0
    for row in rows:
        metadata = _decoded_object(row["metadata_json"])
        if valid_retained_research_evidence(
            {
                "source_uri": row["source_uri"],
                "content_hash": row["content_hash"],
                "excerpt": row["excerpt"],
                "facts": metadata.get("facts"),
                "expires_at": row["expires_at"],
            },
            now=current,
        ):
            valid += 1
    return valid > 0, valid


def _task_delivery_states(
    conn: sqlite3.Connection,
    task_ids: Iterable[str],
) -> dict[str, str]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "tasks" not in tables:
        return {}
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
    if "delivery_status" not in columns:
        return {}
    ids = [str(task_id) for task_id in task_ids if str(task_id)]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id,delivery_status FROM tasks WHERE id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    return {str(row["id"]): str(row["delivery_status"] or "") for row in rows}


def _failure_projection_for_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: str | datetime | None,
) -> tuple[dict, bool, int]:
    error_data = _decoded_object(row["error_json"])
    projection = error_data.get("public_projection")
    retained_valid, evidence_count = _retained_research_state(
        conn,
        str(row["run_id"] or ""),
        now=now,
    )
    if not isinstance(projection, dict):
        projection = task_failure_projection(
            {
                "id": str(row["legacy_task_id"] or ""),
                "run_id": str(row["run_id"] or ""),
                "goal_id": str(row["id"] or ""),
                "status": "failed",
                "error_kind": str(error_data.get("kind") or ""),
                "_retained_research_valid": retained_valid,
            },
        )
    else:
        projection = normalize_task_failure_projection(
            projection,
            retained_research=retained_valid,
        )
    projection.update({
        "task_id": str(row["legacy_task_id"] or ""),
        "run_id": str(row["run_id"] or ""),
        "goal_id": str(row["id"] or ""),
    })
    return projection, retained_valid, evidence_count


def load_durable_failure_projection(
    conn: sqlite3.Connection,
    legacy_task_id: str,
    *,
    now: str | datetime | None = None,
) -> dict:
    """Load and validity-reconcile one failed Run's durable public facts."""

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT g.id,g.status AS goal_status,r.id AS run_id,r.legacy_task_id,
            r.status AS run_status,r.input_json,r.error_json,r.updated_at AS run_updated_at
        FROM runs r JOIN goals g ON g.id=r.goal_id
        WHERE r.legacy_task_id=? AND r.status IN ('failed','timed_out','interrupted')
        LIMIT 1""",
        (str(legacy_task_id or ""),),
    ).fetchone()
    if row is None:
        return {}
    projection, _retained_valid, _evidence_count = _failure_projection_for_row(
        conn,
        row,
        now=now,
    )
    return projection


def resolve_goal_followup(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    channel: str,
    conversation_ref: str,
    message: str,
    recent_context: Iterable[Mapping[str, object]] | None = None,
    now: str | datetime | None = None,
    max_age_hours: int = DEFAULT_FOLLOWUP_WINDOW_HOURS,
) -> dict | None:
    """Resolve work continuation only when the current scope supplies enough evidence."""

    kind = classify_goal_followup(message)
    if not kind:
        return None
    conn.row_factory = sqlite3.Row
    task_refs = _task_references(message)
    if len(task_refs) > 1:
        return _resolution(
            kind,
            "ambiguous",
            reason="multiple_explicit_task_references",
        )
    task_ref = task_refs[0] if task_refs else ""
    failure_followup = kind in _FAILURE_FOLLOWUP_KINDS
    if task_ref:
        status_clause = "AND r.status IN ('failed','timed_out','interrupted')" if failure_followup else ""
        goal = conn.execute(
            f"""SELECT g.*,g.status AS goal_status,r.id AS run_id,r.legacy_task_id,
                r.status AS run_status,r.input_json,r.error_json,r.updated_at AS run_updated_at
            FROM runs r JOIN goals g ON g.id=r.goal_id
            WHERE r.legacy_task_id=? AND g.actor_id=? AND g.channel=? AND g.conversation_ref=?
              AND g.status NOT IN ('cancelled','superseded') {status_clause} LIMIT 1""",
            (task_ref, actor_id, channel, conversation_ref),
        ).fetchone()
        if goal is None:
            return _resolution(kind, "not_found", reason="task_reference_not_in_scope", task_ref=task_ref)
        resolved_by = "task_reference"
    else:
        if failure_followup:
            rows = conn.execute(
                """SELECT g.*,g.status AS goal_status,r.id AS run_id,r.legacy_task_id,
                    r.status AS run_status,r.input_json,r.error_json,r.updated_at AS run_updated_at
                FROM goals g JOIN runs r ON r.goal_id=g.id
                WHERE g.actor_id=? AND g.channel=? AND g.conversation_ref=?
                  AND g.status NOT IN ('cancelled','superseded')
                ORDER BY r.updated_at DESC,r.id DESC LIMIT 20""",
                (actor_id, channel, conversation_ref),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT g.*,g.status AS goal_status,r.id AS run_id,r.legacy_task_id,
                    r.status AS run_status,r.input_json,r.error_json,r.updated_at AS run_updated_at
                FROM goals g LEFT JOIN runs r ON r.id=g.current_run_id
                WHERE g.actor_id=? AND g.channel=? AND g.conversation_ref=?
                  AND g.status IN ('active','waiting_user','failed','completed')
                ORDER BY g.updated_at DESC,g.id DESC LIMIT 20""",
                (actor_id, channel, conversation_ref),
            ).fetchall()
        cutoff = _now(now) - timedelta(hours=max(1, min(int(max_age_hours or 1), 24 * 30)))
        recent = [
            row for row in rows
            if (
                _parse_timestamp(row["run_updated_at"] if failure_followup else row["updated_at"])
                or datetime.min.replace(tzinfo=timezone.utc)
            ) >= cutoff
        ]
        if failure_followup:
            if not recent:
                return _resolution(kind, "not_found", reason="no_recent_failed_task_in_scope")
            delivery_states = _task_delivery_states(
                conn,
                (str(row["legacy_task_id"] or "") for row in recent),
            )
            delivered = [
                row
                for row in recent
                if delivery_states.get(str(row["legacy_task_id"] or "")) == "sent"
                and str(row["run_status"] or "") in {
                    "succeeded", "failed", "timed_out", "cancelled", "interrupted",
                }
            ]
            focus = delivered or recent
            latest_at = str(focus[0]["run_updated_at"] or "")
            if delivered:
                focus_time = _parse_timestamp(latest_at) or datetime.min.replace(tzinfo=timezone.utc)
                newer_nonfailure = [
                    row for row in recent
                    if (_parse_timestamp(row["run_updated_at"]) or datetime.min.replace(tzinfo=timezone.utc)) > focus_time
                    and str(row["run_status"] or "") not in {"failed", "timed_out", "interrupted"}
                ]
                if newer_nonfailure:
                    return _resolution(kind, "not_found", reason="latest_terminal_not_failed")
            equally_recent = [row for row in focus if str(row["run_updated_at"] or "") == latest_at]
            if any(
                str(row["run_status"] or "") not in {"failed", "timed_out", "interrupted"}
                for row in equally_recent
            ):
                return _resolution(kind, "not_found", reason="latest_terminal_not_failed")
            if len(equally_recent) > 1:
                return _resolution(
                    kind,
                    "ambiguous",
                    reason="multiple_equally_recent_failed_tasks_in_scope",
                    candidates=[_candidate_summary(row) for row in equally_recent[:3]],
                )
            goal = equally_recent[0]
            resolved_by = (
                "latest_delivered_failed_terminal_in_scope"
                if delivered
                else "latest_failed_terminal_in_scope"
            )
        else:
            open_rows = [row for row in recent if str(row["status"]) in _OPEN_GOAL_STATUSES]
            eligible = open_rows or [row for row in recent if str(row["status"]) == "completed"]
            if not eligible:
                return _resolution(kind, "not_found", reason="no_recent_goal_in_scope")
            if len(eligible) > 1:
                return _resolution(
                    kind,
                    "ambiguous",
                    reason="multiple_recent_goals_in_scope",
                    candidates=[_candidate_summary(row) for row in eligible[:3]],
                )
            if _normalise_message(message) in _SHORT_CONTEXT_PHRASES and not _has_recent_work_context(recent_context):
                return _resolution(
                    kind,
                    "needs_context",
                    reason="short_reference_without_work_context",
                    candidates=[_candidate_summary(eligible[0])],
                )
            goal = eligible[0]
            resolved_by = "unique_recent_open_goal" if open_rows else "unique_recent_completed_goal"

    revision = conn.execute(
        "SELECT id,revision_number,status FROM goal_revisions WHERE goal_id=? ORDER BY revision_number DESC LIMIT 1",
        (goal["id"],),
    ).fetchone()
    artifact = None
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "artifacts" in tables:
        artifact = conn.execute(
            "SELECT id,current_version_id,title FROM artifacts WHERE source_goal_id=? ORDER BY updated_at DESC LIMIT 1",
            (goal["id"],),
        ).fetchone()
    result = {
        "kind": kind,
        "resolution": "resolved",
        "resolved_by": resolved_by,
        "goal_id": str(goal["id"]),
        "goal_status": str(goal["goal_status"] if "goal_status" in set(goal.keys()) else goal["status"]),
        "run_id": str(goal["run_id"] or ""),
        "legacy_task_id": str(goal["legacy_task_id"] or ""),
        "revision_id": str(revision["id"] if revision else ""),
        "revision_number": int(revision["revision_number"] or 0) if revision else 0,
        "artifact_id": str(artifact["id"] if artifact else ""),
        "artifact_version_id": str(artifact["current_version_id"] if artifact else ""),
        "artifact_title": str(artifact["title"] if artifact else ""),
        "title": str(goal["title"] or "")[:240],
    }
    if failure_followup:
        projection, retained_valid, evidence_count = _failure_projection_for_row(
            conn,
            goal,
            now=now,
        )
        result.update({
            "failure_projection": projection,
            "retained_research_valid": retained_valid,
            "retained_evidence_count": evidence_count,
            "original_objective": str(
                _decoded_object(goal["input_json"]).get("origin_message")
                or goal["title"]
                or ""
            )[:12000],
        })
    return result


def followup_resolution_reply(target: dict) -> str:
    state = str(target.get("resolution") or "")
    if state == "ambiguous":
        lines = ["我找到多个近期工作，不能安全判断你指的是哪一个："]
        for item in list(target.get("candidates") or [])[:3]:
            task_id = str(item.get("legacy_task_id") or "")[:8] or "无编号"
            lines.append(f"- 任务 #{task_id} · {str(item.get('title') or '未命名工作')[:40]}")
        lines.append("请说一下任务标题；如果你手边有任务编号，也可以带上编号回复。")
        return "\n".join(lines)
    if state == "needs_context":
        candidate = list(target.get("candidates") or [{}])[0]
        task_id = str(candidate.get("legacy_task_id") or "")[:8]
        suffix = f"任务 #{task_id}" if task_id else "这项工作"
        return f"我不确定你说的是不是{suffix}。请补充要继续做什么，或带上任务编号。"
    task_ref = str(target.get("task_reference") or "")
    if task_ref:
        return f"我没有在当前对话中找到任务 #{task_ref}。请在原私聊或原群聊中继续，或使用当前对话里的任务编号。"
    return "我知道你想继续，但当前对话里没有可安全续接的近期工作。请说明任务内容或带上任务编号。"


def followup_prompt_context(target: dict | None) -> list[str]:
    if not target or target.get("resolution") != "resolved":
        return []
    lines = [
        "",
        "本轮工作连续性:",
        f"- 延续 Goal: {target.get('goal_id')}",
        f"- 上一 Run: {target.get('run_id') or '(无)'}",
        f"- 当前 Revision: {target.get('revision_number') or 0}",
    ]
    if target.get("artifact_id"):
        lines.append(f"- 当前 Artifact: {target.get('artifact_title') or target.get('artifact_id')} / {target.get('artifact_version_id') or '(无版本)'}")
    if target.get("kind") == "continue":
        lines.append("- 延续同一目标；先根据现有状态判断下一步，不得凭空扩大目标范围。")
    elif target.get("kind") in _FAILURE_FOLLOWUP_KINDS:
        lines.append(f"- 失败工作: {target.get('title') or target.get('legacy_task_id')}")
        lines.append(f"- 失败追问语义: {target.get('kind')}")
        lines.append("- 服务器已绑定同一失败 Task / Run / Goal；不得把短追问改写成无关新目标。")
    else:
        lines.append("- 必须保留同一目标的已有证据和约束；本轮只处理用户明确提出的修改或纠正。")
    return lines


__all__ = [
    "classify_goal_followup",
    "followup_prompt_context",
    "followup_resolution_reply",
    "load_durable_failure_projection",
    "resolve_goal_followup",
]
