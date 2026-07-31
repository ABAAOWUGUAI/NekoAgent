#!/usr/bin/env python3
"""Read-only Daily Assistant Home and Attention projections.

The projection deliberately owns no workflow state. Every item is rebuilt from
Assistant Identity, Goal/Run and Delivery source records on each request.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter
from typing import Callable, Iterable

from bridge_assistant_identity import current_assistant
from bridge_assistant_identity_schema import IDENTITY_FEATURE_FLAG
from bridge_conversation_memory import list_threads
from bridge_conversation_memory_schema import MEMORY_SCOPE_FEATURE_FLAG
from bridge_migrations import utc_now
from bridge_platform_repository import PlatformRepository
from bridge_formal_approval import FormalApprovalRepository
from bridge_artifact_repository import ArtifactRepository
from bridge_continuity_projection import continuity_summary
from bridge_learning_service import learning_summary


DAILY_SHELL_FEATURE_FLAG = "daily_shell_v2"
ATTENTION_GOAL_STATES = {"waiting_user", "failed"}
ATTENTION_RUN_STATES = {"waiting_approval", "failed", "timed_out"}
ACTIVE_GOAL_STATES = {"active", "waiting_user"}
ACTIVE_RUN_STATES = {"queued", "running", "waiting_approval"}
FORBIDDEN_PUBLIC_KEYS = {
    "api_key",
    "cookie",
    "destination",
    "input_json",
    "output_json",
    "password",
    "payload",
    "payload_json",
    "prompt",
    "result",
    "token",
    "user_id",
}
PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2}


def _feature_flags(conn: sqlite3.Connection) -> dict[str, bool]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_feature_flags'",
    ).fetchone()
    if not table:
        return {}
    return {
        str(row[0]): bool(int(row[1]))
        for row in conn.execute("SELECT name,enabled FROM assistant_feature_flags")
    }


def daily_shell_feature_enabled(conn: sqlite3.Connection) -> bool:
    return bool(_feature_flags(conn).get(DAILY_SHELL_FEATURE_FLAG))


def _clip(value: object, limit: int = 120) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:limit]


def _action(source_type: str, source_id: object, *, view: str = "tasks") -> dict:
    return {
        "label": (
            "审阅操作" if source_type == "approval"
            else "查看任务" if view == "tasks"
            else "检查日常链路"
        ),
        "view": view,
        "source_type": source_type,
        "source_id": str(source_id or ""),
    }


def _attention_item(
    *,
    source_type: str,
    source_id: object,
    source_state: str,
    priority: str,
    title: str,
    reason: str,
    risk: str,
    updated_at: object,
    view: str = "tasks",
) -> dict:
    source_id = str(source_id or "")
    return {
        "dedupe_key": f"{source_type}:{source_id}:{source_state}",
        "source_type": source_type,
        "source_id": source_id,
        "priority": priority,
        "title": title,
        "reason": reason,
        "risk": risk,
        "next_action": _action(source_type, source_id, view=view),
        "invalidates_when": f"{source_type}.state no longer {source_state}",
        "updated_at": str(updated_at or ""),
    }


def _health_attention(health: dict) -> list[dict]:
    items: list[dict] = []
    checks = [
        (
            "assistant",
            bool(health.get("assistant_ready")),
            "当前助手尚未就绪",
            "日常首页无法确定正在使用的助手身份。",
        ),
        (
            "identity",
            bool(health.get("identity_ready")),
            "助手身份切换尚未完成",
            "前台仍可能读取旧角色设置，暂不应作为稳定日常入口。",
        ),
        (
            "memory",
            bool(health.get("memory_ready")),
            "对话与记忆作用域尚未完成",
            "跨渠道上下文边界尚未进入已验证读路径。",
        ),
    ]
    if "daily_shell_ready" in health:
        checks.append((
            "daily_shell",
            bool(health.get("daily_shell_ready")),
            "日常空间尚未启用",
            "当前仍处于切换或回滚状态，日常首页不是稳定读路径。",
        ))
    for source_id, ready, title, reason in checks:
        if ready:
            continue
        items.append(_attention_item(
            source_type="business_health",
            source_id=source_id,
            source_state="not_ready",
            priority="critical",
            title=title,
            reason=reason,
            risk="继续使用不会自动修复该配置，需要进入管理中心检查。",
            updated_at=health.get("generated_at"),
            view="services",
        ))
    return items


def build_attention_projection(
    goals: Iterable[dict],
    runs: Iterable[dict],
    deliveries: Iterable[dict],
    health: dict,
    *,
    approvals: Iterable[dict] = (),
    limit: int = 20,
) -> dict:
    """Create a safe, disposable projection from authoritative source states."""

    items: list[dict] = []
    for goal in goals:
        status = str(goal.get("status") or "")
        if status == "waiting_user":
            items.append(_attention_item(
                source_type="goal",
                source_id=goal.get("id"),
                source_state=status,
                priority="high",
                title=_clip(goal.get("title")) or "任务结果等待确认",
                reason="本次执行已经完成，任务仍在等待你确认结果。",
                risk="不确认不会关闭任务，也不会重复执行。",
                updated_at=goal.get("updated_at"),
            ))
        elif status == "failed":
            items.append(_attention_item(
                source_type="goal",
                source_id=goal.get("id"),
                source_state=status,
                priority="high",
                title=_clip(goal.get("title")) or "任务未完成",
                reason="当前任务没有达到完成条件，需要查看失败范围。",
                risk="任务保持失败状态，除非你选择继续、重试或取消。",
                updated_at=goal.get("updated_at"),
            ))

    pending_approval_runs: set[str] = set()
    for approval in approvals:
        if str(approval.get("status") or "") != "pending":
            continue
        run_id = str(approval.get("run_id") or "")
        if run_id:
            pending_approval_runs.add(run_id)
        items.append(_attention_item(
            source_type="approval",
            source_id=approval.get("id"),
            source_state="pending",
            priority="high",
            title=_clip(approval.get("action_summary")) or "有一项操作等待确认",
            reason="执行已暂停；确认、修改后确认或拒绝前都不会继续。",
            risk="批准只覆盖当前动作摘要、参数版本与目标环境，内容变化后旧批准自动失效。",
            updated_at=approval.get("updated_at") or approval.get("created_at"),
        ))

    for run in runs:
        status = str(run.get("status") or "")
        if status == "waiting_approval" and str(run.get("id") or "") in pending_approval_runs:
            continue
        if status not in ATTENTION_RUN_STATES:
            continue
        copy = {
            "waiting_approval": (
                "有一项操作等待确认",
                "执行已暂停，确认前不会继续该操作。",
                "未确认时不会执行；后续 Gate 将提供正式批准与拒绝。",
            ),
            "failed": (
                "本次执行失败",
                "这次尝试没有成功完成，任务仍保留可追踪记录。",
                "重复重试前应先查看失败范围。",
            ),
            "timed_out": (
                "本次执行超时",
                "执行超过限制时间后已停止等待。",
                "继续前应确认是任务过大、模型延迟还是运行环境问题。",
            ),
        }[status]
        items.append(_attention_item(
            source_type="run",
            source_id=run.get("id"),
            source_state=status,
            priority="high",
            title=copy[0],
            reason=copy[1],
            risk=copy[2],
            updated_at=run.get("updated_at"),
        ))

    for delivery in deliveries:
        if str(delivery.get("state") or "") != "dead_letter":
            continue
        items.append(_attention_item(
            source_type="delivery",
            source_id=delivery.get("id"),
            source_state="dead_letter",
            priority="critical",
            title="结果送达失败",
            reason="一条结果多次投递后仍未成功，需要人工检查。",
            risk="任务结果仍保留，但目标渠道没有确认收到。",
            updated_at=delivery.get("updated_at") or delivery.get("created_at"),
        ))

    items.extend(_health_attention(health))
    unique: dict[str, dict] = {}
    for item in items:
        unique.setdefault(item["dedupe_key"], item)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            PRIORITY_ORDER.get(str(item.get("priority")), 99),
            str(item.get("updated_at") or ""),
        ),
        reverse=False,
    )
    # Preserve priority order while making newer records first within a priority.
    ordered = sorted(
        ordered,
        key=lambda item: PRIORITY_ORDER.get(str(item.get("priority")), 99),
    )
    for priority in PRIORITY_ORDER:
        indexes = [index for index, item in enumerate(ordered) if item["priority"] == priority]
        if indexes:
            replacement = sorted(
                (ordered[index] for index in indexes),
                key=lambda item: str(item.get("updated_at") or ""),
                reverse=True,
            )
            for index, item in zip(indexes, replacement):
                ordered[index] = item
    counts = Counter(item["priority"] for item in ordered)
    safe_limit = max(1, min(int(limit or 20), 100))
    return {
        "total": len(ordered),
        "counts": {
            "critical": counts.get("critical", 0),
            "high": counts.get("high", 0),
            "normal": counts.get("normal", 0),
        },
        "items": ordered[:safe_limit],
    }


def _task_status(goal: dict, run: dict) -> tuple[str, str]:
    goal_status = str(goal.get("status") or "")
    run_status = str(run.get("status") or "")
    if goal_status == "waiting_user":
        return "waiting_user", "等待你确认"
    return {
        "queued": ("queued", "等待开始"),
        "running": ("running", "正在执行"),
        "waiting_approval": ("waiting_user", "等待你确认"),
        "failed": ("needs_attention", "执行失败"),
        "timed_out": ("needs_attention", "执行超时"),
        "succeeded": ("finalizing", "整理结果"),
    }.get(run_status, ("preparing", "准备中"))


def build_task_projection(goals: Iterable[dict], runs: Iterable[dict], *, limit: int = 12) -> list[dict]:
    runs = list(runs)
    run_by_id = {str(item.get("id") or ""): item for item in runs}
    latest_by_goal: dict[str, dict] = {}
    for run in runs:
        latest_by_goal.setdefault(str(run.get("goal_id") or ""), run)
    records = []
    for goal in goals:
        if str(goal.get("status") or "") not in ACTIVE_GOAL_STATES:
            continue
        run = run_by_id.get(str(goal.get("current_run_id") or "")) or latest_by_goal.get(
            str(goal.get("id") or ""),
            {},
        )
        status, label = _task_status(goal, run)
        records.append({
            "source_type": "goal",
            "source_id": str(goal.get("id") or ""),
            "title": _clip(goal.get("title")) or "未命名任务",
            "status": status,
            "status_label": label,
            "updated_at": str(run.get("updated_at") or goal.get("updated_at") or ""),
            "legacy_task_id": str(run.get("legacy_task_id") or goal.get("legacy_root_task_id") or ""),
        })
    return sorted(
        records,
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )[:max(1, min(int(limit or 12), 50))]


def _business_health(
    assistant_conn: sqlite3.Connection,
    assistant: dict | None,
    deliveries: list[dict],
) -> dict:
    flags = _feature_flags(assistant_conn)
    pending = sum(
        1
        for item in deliveries
        if str(item.get("state") or "") in {"available", "scheduled", "leased"}
    )
    dead_letter = sum(1 for item in deliveries if str(item.get("state") or "") == "dead_letter")
    result = {
        "generated_at": utc_now(),
        "assistant_ready": assistant is not None,
        "identity_ready": bool(flags.get(IDENTITY_FEATURE_FLAG)),
        "memory_ready": bool(flags.get(MEMORY_SCOPE_FEATURE_FLAG)),
        "daily_shell_ready": bool(flags.get(DAILY_SHELL_FEATURE_FLAG)),
        "delivery_pending": pending,
        "delivery_dead_letter": dead_letter,
    }
    result["status"] = (
        "attention"
        if not result["assistant_ready"]
        or not result["identity_ready"]
        or not result["memory_ready"]
        or not result["daily_shell_ready"]
        or dead_letter
        else "healthy"
    )
    return result


def _assert_public_projection(value: object, *, path: str = "root") -> list[str]:
    violations: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_PUBLIC_KEYS:
                violations.append(f"{path}.{key}")
            violations.extend(_assert_public_projection(item, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(_assert_public_projection(item, path=f"{path}[{index}]"))
    return violations


class AssistantHomeService:
    def __init__(
        self,
        assistant_connect: Callable[[], sqlite3.Connection],
        task_connect: Callable[[], sqlite3.Connection],
        delivery_reader: Callable[[int], list[dict]],
    ) -> None:
        self._assistant_connect = assistant_connect
        self._task_connect = task_connect
        self._delivery_reader = delivery_reader
        self._cache: dict[int, tuple[float, dict]] = {}
        self._cache_lock = threading.RLock()
        self._cache_ttl = 4.0
        self._cache_revision = 0

    def invalidate(self) -> None:
        with self._cache_lock:
            self._cache_revision += 1
            self._cache.clear()

    def _sources(self, limit: int, *, force: bool = False) -> dict:
        safe_limit = max(1, min(int(limit or 20), 100))
        force_rebuild = bool(force)
        for attempt in range(2):
            now_mono = time.monotonic()
            with self._cache_lock:
                cached = self._cache.get(safe_limit)
                if cached and not force_rebuild and now_mono - cached[0] < self._cache_ttl:
                    return cached[1]
                build_revision = self._cache_revision
            with self._assistant_connect() as assistant_conn:
                assistant = current_assistant(assistant_conn)
                conversations = list_threads(assistant_conn, limit=min(safe_limit, 20))
                with self._task_connect() as task_conn:
                    repository = PlatformRepository(task_conn)
                    goals = repository.list_goals(limit=max(50, safe_limit))
                    runs = repository.list_runs(limit=max(100, safe_limit * 3))
                    approvals = FormalApprovalRepository(task_conn).list(
                        status="pending",
                        limit=max(50, safe_limit),
                    )
                    artifacts = ArtifactRepository(task_conn).list_artifacts(limit=safe_limit)
                deliveries = self._delivery_reader(max(50, safe_limit))
                health = _business_health(assistant_conn, assistant, deliveries)
                feature_enabled = daily_shell_feature_enabled(assistant_conn)
                continuity = continuity_summary(assistant_conn, limit=safe_limit)
                try:
                    learning = learning_summary(assistant_conn, limit=1)
                    learning = {
                        "feature_enabled": bool(learning.get("feature_enabled")),
                        "low_risk_enabled": bool(learning.get("low_risk_enabled")),
                        "counts": dict(learning.get("counts") or {}),
                    }
                except (sqlite3.Error, ValueError):
                    learning = {
                        "feature_enabled": False,
                        "low_risk_enabled": False,
                        "counts": {},
                    }
            result = {
                "assistant": assistant,
                "conversations": conversations,
                "goals": goals,
                "runs": runs,
                "approvals": approvals,
                "artifacts": artifacts,
                "deliveries": deliveries,
                "health": health,
                "feature_enabled": feature_enabled,
                "continuity": continuity,
                "learning": learning,
            }
            with self._cache_lock:
                # A mutation may commit while this projection is being assembled.
                # Do not return or cache that older snapshot. Rebuild once for the
                # current request, but cap retries so sustained writes cannot
                # livelock Home reads.
                revision_changed = build_revision != self._cache_revision
                if not revision_changed:
                    self._cache[safe_limit] = (time.monotonic(), result)
                    return result
            if attempt == 0:
                force_rebuild = True
                continue
            return result

        raise AssertionError("assistant_home_projection_retry_exhausted")

    def attention(self, *, limit: int = 20, force: bool = False) -> dict:
        sources = self._sources(limit, force=force)
        attention = build_attention_projection(
            sources["goals"],
            sources["runs"],
            sources["deliveries"],
            sources["health"],
            approvals=sources["approvals"],
            limit=limit,
        )
        return {
            "generated_at": utc_now(),
            "feature": {
                "name": DAILY_SHELL_FEATURE_FLAG,
                "enabled": sources["feature_enabled"],
            },
            "attention": attention,
            "continuity": sources["continuity"],
            "learning": sources["learning"],
        }

    def home(self, *, limit: int = 12, force: bool = False) -> dict:
        sources = self._sources(limit, force=force)
        assistant = sources["assistant"]
        public_assistant = None
        if assistant:
            public_assistant = {
                "id": assistant["id"],
                "display_name": assistant["display_name"],
                "status": assistant["status"],
                "appearance": assistant.get("appearance"),
                "updated_at": assistant.get("updated_at"),
            }
        result = {
            "generated_at": utc_now(),
            "feature": {
                "name": DAILY_SHELL_FEATURE_FLAG,
                "enabled": sources["feature_enabled"],
            },
            "assistant": public_assistant,
            "attention": build_attention_projection(
                sources["goals"],
                sources["runs"],
                sources["deliveries"],
                sources["health"],
                approvals=sources["approvals"],
                limit=limit,
            ),
            "active_tasks": build_task_projection(
                sources["goals"],
                sources["runs"],
                limit=limit,
            ),
            "recent_conversations": sources["conversations"][:limit],
            "recent_artifacts": {
                "items": [
                    {
                        "id": item["id"],
                        "kind": item["kind"],
                        "title": item["title"],
                        "updated_at": item["updated_at"],
                        "version_number": int((item.get("current_version") or {}).get("version_number") or 0),
                        "state": str((item.get("current_version") or {}).get("state") or "preparing"),
                    }
                    for item in sources["artifacts"]
                ],
                "capability_status": "available",
            },
            "proactive_status": {
                "capability_status": "existing_controls_only_gate8_pending",
            },
            "business_health": sources["health"],
            "continuity": sources["continuity"],
            "learning": sources["learning"],
        }
        violations = _assert_public_projection(result)
        if violations:
            raise ValueError("assistant_home_privacy_contract_failed")
        return result

    def cutover_plan(self) -> dict:
        sources = self._sources(100, force=True)
        attention = build_attention_projection(
            sources["goals"],
            sources["runs"],
            sources["deliveries"],
            sources["health"],
            approvals=sources["approvals"],
            limit=100,
        )
        duplicate_keys = [
            key
            for key, count in Counter(
                item["dedupe_key"]
                for item in attention["items"]
            ).items()
            if count > 1
        ]
        privacy_violations = _assert_public_projection({
            "attention": attention,
            "active_tasks": build_task_projection(sources["goals"], sources["runs"], limit=50),
        })
        health = sources["health"]
        blockers = [
            key
            for key in ("assistant_ready", "identity_ready", "memory_ready")
            if not health.get(key)
        ]
        payload = {
            "feature_enabled": sources["feature_enabled"],
            "source_counts": {
                "goals": sum(1 for item in sources["goals"] if item.get("status") in ATTENTION_GOAL_STATES),
                "runs": sum(1 for item in sources["runs"] if item.get("status") in ATTENTION_RUN_STATES),
                "dead_letters": sum(
                    1 for item in sources["deliveries"] if item.get("state") == "dead_letter"
                ),
                "approvals": len(sources["approvals"]),
            },
            "projected": attention["total"],
            "duplicate_keys": duplicate_keys,
            "privacy_violations": privacy_violations,
            "blockers": blockers,
        }
        checksum = hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()
        return {
            "ok": not duplicate_keys and not privacy_violations and not blockers,
            **payload,
            "plan_checksum": checksum,
        }

    def set_feature(self, enabled: bool, *, expect_plan_checksum: str) -> dict:
        plan = self.cutover_plan()
        if str(expect_plan_checksum or "") != plan["plan_checksum"]:
            raise ValueError("stale_daily_shell_cutover_plan")
        if enabled and not plan["ok"]:
            raise ValueError("daily_shell_shadow_compare_failed")
        with self._assistant_connect() as conn:
            conn.execute(
                """
                INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
                """,
                (DAILY_SHELL_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
            )
        return self.cutover_plan()


__all__ = [
    "AssistantHomeService",
    "DAILY_SHELL_FEATURE_FLAG",
    "build_attention_projection",
    "build_task_projection",
    "daily_shell_feature_enabled",
]
