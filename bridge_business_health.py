#!/usr/bin/env python3
"""Business-level health aggregation for the assistant product.

Process liveness is intentionally not presented as proof that QQ, Codex, model
routing, delivery, or preview actually work. Unknown evidence remains unknown.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable

from bridge_assistant_identity import current_assistant
from bridge_migrations import utc_now
from bridge_model_control import role_model_compatible


STATUS_ORDER = {
    "healthy": 0,
    "unknown": 1,
    "degraded": 2,
    "unavailable": 3,
}
GOOD_TEST_STATES = {"ok", "success", "passed", "healthy"}


def _check(
    check_id: str,
    label: str,
    status: str,
    summary: str,
    *,
    evidence_type: str,
    next_action: str = "",
    metrics: dict | None = None,
) -> dict:
    if status not in STATUS_ORDER:
        raise ValueError("invalid_business_health_status")
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "summary": summary,
        "evidence_type": evidence_type,
        "next_action": next_action,
        "metrics": dict(metrics or {}),
    }


def _safe_probe(probe: Callable[[], dict] | None) -> tuple[dict | None, str]:
    if probe is None:
        return None, "probe_not_configured"
    try:
        result = probe()
    except Exception:
        return None, "probe_failed"
    return (dict(result), "") if isinstance(result, dict) else (None, "probe_invalid")


class BusinessHealthService:
    def __init__(
        self,
        assistant_connect: Callable[[], sqlite3.Connection],
        task_connect: Callable[[], sqlite3.Connection],
        delivery_reader: Callable[[int], list[dict]],
        *,
        qq_probe: Callable[[], dict] | None = None,
        codex_probe: Callable[[], dict] | None = None,
        artifact_probe: Callable[[], dict] | None = None,
        worker_health_reader: Callable[[], dict] | None = None,
    ) -> None:
        self._assistant_connect = assistant_connect
        self._task_connect = task_connect
        self._delivery_reader = delivery_reader
        self._qq_probe = qq_probe
        self._codex_probe = codex_probe
        self._artifact_probe = artifact_probe
        self._worker_health_reader = worker_health_reader

    def _worker_check(self) -> dict | None:
        if self._worker_health_reader is None:
            return None
        try:
            records = dict(self._worker_health_reader() or {})
        except Exception:
            return _check(
                "background_workers", "后台任务", "unavailable",
                "后台任务健康状态无法读取。", evidence_type="worker_registry_failed",
                next_action="检查 Bridge Worker 注册表与进程日志。",
            )
        if not records or any(not bool(item.get("started")) for item in records.values()):
            return _check(
                "background_workers", "后台任务", "unknown",
                "后台任务尚未全部完成首次运行。", evidence_type="worker_registry",
                next_action="等待一个 Worker 周期后重新检查。",
                metrics={"registered": len(records), "started": sum(bool(item.get("started")) for item in records.values())},
            )
        now = datetime.now(timezone.utc)
        stale = 0
        degraded = 0
        unavailable = 0
        for item in records.values():
            failures = int(item.get("consecutive_failures") or 0)
            if failures >= 5:
                unavailable += 1
            elif failures:
                degraded += 1
            last_success = str(item.get("last_success_at") or "")
            if last_success and not bool(item.get("in_progress")):
                try:
                    age = (now - datetime.fromisoformat(last_success)).total_seconds()
                    if age > int(item.get("stale_after_seconds") or 180):
                        stale += 1
                except ValueError:
                    stale += 1
            elif not last_success and not failures:
                degraded += 1
        if unavailable or stale:
            status = "unavailable"
            summary = "后台任务存在持续失败或长时间未成功运行。"
            action = "查看 Worker 健康详情并恢复数据库或依赖服务。"
        elif degraded:
            status = "degraded"
            summary = "后台任务最近出现失败，正在自动退避重试。"
            action = "检查稳定错误类型；恢复成功后状态会自动清除。"
        else:
            status = "healthy"
            summary = "后台任务均已在预期时间内成功运行。"
            action = ""
        return _check(
            "background_workers", "后台任务", status, summary,
            evidence_type="worker_registry", next_action=action,
            metrics={
                "registered": len(records),
                "degraded": degraded,
                "unavailable": unavailable,
                "stale": stale,
                "workers": {
                    worker_id: {
                        "in_progress": bool(item.get("in_progress")),
                        "consecutive_failures": int(item.get("consecutive_failures") or 0),
                        "last_success_at": str(item.get("last_success_at") or ""),
                        "last_error_type": str(item.get("last_error_type") or ""),
                    }
                    for worker_id, item in records.items()
                },
            },
        )

    @staticmethod
    def _feature_flags(conn: sqlite3.Connection) -> dict[str, bool]:
        table = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='assistant_feature_flags'
            """,
        ).fetchone()
        if not table:
            return {}
        return {
            str(row[0]): bool(int(row[1]))
            for row in conn.execute(
                "SELECT name,enabled FROM assistant_feature_flags",
            ).fetchall()
        }

    @staticmethod
    def _model_check(conn: sqlite3.Connection) -> tuple[dict, dict]:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        }
        required_tables = {
            "model_providers",
            "model_catalog",
            "model_role_bindings",
        }
        if not required_tables.issubset(tables):
            return (
                _check(
                    "model_routes",
                    "模型路由",
                    "unavailable",
                    "模型目录或路由表不存在。",
                    evidence_type="schema",
                    next_action="初始化模型管理数据后重新检查。",
                ),
                {},
            )
        rows = conn.execute(
            """
            SELECT b.role,b.primary_model_id,m.label,m.enabled,
                   p.id AS provider_id,p.name AS provider_name,p.enabled AS provider_enabled,
                   p.last_test_status,p.transport,p.billing_scope,p.runtime_owner,
                   p.config_mode,p.trusted_for_executor,m.capabilities_json,
                   m.supports_tools
            FROM model_role_bindings b
            LEFT JOIN model_catalog m ON m.id=b.primary_model_id
            LEFT JOIN model_providers p ON p.id=m.provider_id
            """,
        ).fetchall()
        roles = {str(row["role"]): dict(row) for row in rows}
        role_states = {}
        missing = []
        incompatible = []
        untested = []
        for role in (
            "interaction_classifier",
            "conversation_engagement",
            "conversation_reply",
            "work_planner",
            "work_executor",
        ):
            item = roles.get(role)
            if (
                not item
                or not item.get("primary_model_id")
                or not int(item.get("enabled") or 0)
                or not int(item.get("provider_enabled") or 0)
            ):
                missing.append(role)
                role_states[role] = "unavailable"
                continue
            compatible, reason = role_model_compatible(role, item)
            if not compatible:
                incompatible.append(f"{role}:{reason}")
                role_states[role] = "unavailable"
                continue
            tested = str(item.get("last_test_status") or "").lower() in GOOD_TEST_STATES
            if not tested:
                untested.append(role)
                role_states[role] = "unknown"
            else:
                role_states[role] = "healthy"
        if missing or incompatible:
            status = "unavailable"
            summary = "存在未绑定、停用或不兼容的模型角色。"
            action = "检查模型连接、能力声明和角色路由。"
        elif untested:
            status = "unknown"
            summary = "所有角色均已绑定，但部分连接缺少最近一次成功验证证据。"
            action = "在验证台运行对应连接的能力测试。"
        else:
            status = "healthy"
            summary = "五个模型角色均有可用且最近验证成功的路由。"
            action = ""
        return (
            _check(
                "model_routes",
                "模型路由",
                status,
                summary,
                evidence_type="registry_and_last_test",
                next_action=action,
                metrics={
                    "ready_roles": sum(1 for value in role_states.values() if value == "healthy"),
                    "unknown_roles": len(untested),
                    "unavailable_roles": len(missing) + len(incompatible),
                },
            ),
            {"roles": role_states, "rows": roles},
        )

    @staticmethod
    def _qq_check(raw: dict | None, error: str, *, live: bool) -> dict:
        if not live:
            return _check(
                "qq_channel",
                "QQ 消息链路",
                "unknown",
                "尚未运行本次实时 QQ 业务探测。",
                evidence_type="not_probed",
                next_action="点击“运行完整诊断”验证登录、OneBot、插件和 Bridge 链路。",
            )
        if raw is None:
            return _check(
                "qq_channel",
                "QQ 消息链路",
                "unavailable",
                "QQ 业务探测未能完成。",
                evidence_type=error,
                next_action="检查 NapCat、AstrBot 和 Bridge 运行状态。",
            )
        qq_online = raw.get("qq_status") == "online" and not bool(raw.get("needs_login"))
        onebot = bool(raw.get("onebot_connected"))
        plugin = bool(raw.get("plugin_loaded"))
        bridge = bool(raw.get("bridge_reachable_from_astrbot"))
        send_degraded = bool(raw.get("send_path_degraded"))
        if qq_online and onebot and plugin and bridge and not send_degraded:
            status = "healthy"
            summary = "QQ 登录、OneBot、插件和 Bridge 链路均通过实时探测。"
            action = ""
        elif qq_online or onebot or bridge:
            status = "degraded"
            summary = "QQ 链路仅部分可用，不能以容器运行状态替代业务成功。"
            action = "查看 QQ 专项诊断中的失败检查项。"
        else:
            status = "unavailable"
            summary = "QQ 登录或消息链路不可用。"
            action = "先恢复 QQ 登录与 OneBot 链路，再进行消息验证。"
        return _check(
            "qq_channel",
            "QQ 消息链路",
            status,
            summary,
            evidence_type="live_business_probe",
            next_action=action,
            metrics={
                "qq_online": qq_online,
                "onebot_connected": onebot,
                "plugin_loaded": plugin,
                "bridge_reachable": bridge,
                "send_path_degraded": send_degraded,
            },
        )

    @staticmethod
    def _codex_check(
        raw: dict | None,
        error: str,
        *,
        live: bool,
        model_context: dict,
    ) -> dict:
        executor = (model_context.get("rows") or {}).get("work_executor") or {}
        route_present = bool(executor.get("primary_model_id"))
        if not live:
            return _check(
                "codex_executor",
                "任务执行器",
                "unknown" if route_present else "unavailable",
                (
                    "执行器已有路由，但尚未运行本次适配器探测。"
                    if route_present
                    else "没有可用的工作执行器路由。"
                ),
                evidence_type="routing_only",
                next_action="运行完整诊断以验证执行器、代理与所选模型。",
            )
        if raw is None:
            return _check(
                "codex_executor",
                "任务执行器",
                "unavailable",
                "任务执行器探测失败。",
                evidence_type=error,
                next_action="检查 Codex CLI、执行器路由与本地模型代理。",
            )
        ok = bool(raw.get("ok"))
        adapter = str(raw.get("adapter") or executor.get("transport") or "unknown")
        auth_required = bool(raw.get("auth_required"))
        return _check(
            "codex_executor",
            "任务执行器",
            "healthy" if ok and route_present else "unavailable",
            (
                "任务执行器、路由与当前适配器均可用。"
                if ok and route_present
                else "任务执行器、适配器或路由不可用。"
            ),
            evidence_type="live_executor_probe",
            next_action="" if ok and route_present else "检查 work_executor 路由、Codex CLI 与本地模型代理。",
            metrics={
                "adapter": adapter,
                "auth_required": auth_required,
                "route_present": route_present,
                "cli_ok": bool(raw.get("cli_ok")),
                "login_ok": bool(raw.get("login_ok")) if auth_required else None,
                "profile_applied": bool(raw.get("profile_applied")),
                "profile_file_ok": bool(raw.get("profile_file_ok")),
                "credential_ok": bool(raw.get("credential_ok")),
                "sandbox_ok": bool(raw.get("sandbox_ok")),
                "workspace_ok": bool(raw.get("workspace_ok")),
                "proxy_ok": bool(raw.get("proxy_ok")),
                "model_match": bool(raw.get("model_match")),
            },
        )

    @staticmethod
    def _artifact_check(
        raw: dict | None,
        error: str,
        *,
        live: bool,
        enabled: bool,
    ) -> dict:
        if not enabled:
            return _check(
                "artifact_preview",
                "成品预览",
                "unavailable",
                "成品预览功能未启用。",
                evidence_type="feature_flag",
                next_action="完成 Gate 7 切换后再启用预览。",
            )
        if not live:
            return _check(
                "artifact_preview",
                "成品预览",
                "unknown",
                "预览功能已启用，但尚未运行本次存储、Broker 与来源隔离探测。",
                evidence_type="feature_flag_only",
                next_action="运行完整诊断验证预览链路。",
            )
        if raw is None:
            return _check(
                "artifact_preview",
                "成品预览",
                "unavailable",
                "成品预览探测失败。",
                evidence_type=error,
                next_action="检查预览服务、授权 Broker 与存储。",
            )
        ok = bool(raw.get("ok"))
        return _check(
            "artifact_preview",
            "成品预览",
            "healthy" if ok else "degraded",
            (
                "存储、授权 Broker、HTTPS 来源隔离与预览配置通过探测。"
                if ok
                else "成品预览仅部分满足上线条件。"
            ),
            evidence_type="live_cutover_probe",
            next_action="" if ok else "打开成品预览专项检查查看未通过项。",
        )

    def summary(self, *, live: bool = False) -> dict:
        checks: list[dict] = []
        with self._assistant_connect() as assistant_conn:
            flags = self._feature_flags(assistant_conn)
            try:
                assistant = current_assistant(assistant_conn)
            except Exception:
                assistant = None
            checks.append(
                _check(
                    "assistant_identity",
                    "当前助手",
                    (
                        "healthy"
                        if assistant and flags.get("assistant_identity_v2")
                        else "unavailable"
                    ),
                    (
                        "当前助手身份可用，平台与具体角色资源保持分离。"
                        if assistant and flags.get("assistant_identity_v2")
                        else "当前助手身份或身份切换功能不可用。"
                    ),
                    evidence_type="database_and_feature_flag",
                    next_action="" if assistant else "检查助手身份迁移与当前实例。",
                ),
            )
            model_check, model_context = self._model_check(assistant_conn)
            checks.append(model_check)
            artifact_enabled = bool(flags.get("artifact_preview_v2"))
            relationship_enabled = bool(flags.get("relationship_proactive_v2"))
        with self._task_connect() as task_conn:
            try:
                task_conn.execute("SELECT 1").fetchone()
                task_tables = {
                    str(row[0])
                    for row in task_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'",
                    ).fetchall()
                }
                task_ready = "tasks" in task_tables
            except Exception:
                task_ready = False
        checks.append(
            _check(
                "task_store",
                "任务存储",
                "healthy" if task_ready else "unavailable",
                "任务数据库可读。" if task_ready else "任务数据库不可读或任务表缺失。",
                evidence_type="database_readiness",
                next_action="" if task_ready else "检查任务数据库与 migration。",
            ),
        )
        worker_check = self._worker_check()
        if worker_check is not None:
            checks.append(worker_check)
        try:
            deliveries = list(self._delivery_reader(200))
            delivery_error = ""
        except Exception:
            deliveries = []
            delivery_error = "delivery_reader_failed"
        pending = sum(
            1
            for item in deliveries
            if str(item.get("state") or "") in {"available", "scheduled", "leased"}
        )
        dead = sum(
            1 for item in deliveries if str(item.get("state") or "") == "dead_letter"
        )
        checks.append(
            _check(
                "delivery",
                "结果送达",
                (
                    "unavailable"
                    if delivery_error
                    else "degraded" if dead else "healthy"
                ),
                (
                    "送达读取失败。"
                    if delivery_error
                    else f"待送达 {pending} 项，死信 {dead} 项。"
                ),
                evidence_type=delivery_error or "outbox_state",
                next_action="处理送达死信。" if dead else "",
                metrics={"pending": pending, "dead_letter": dead},
            ),
        )
        qq_raw, qq_error = _safe_probe(self._qq_probe) if live else (None, "")
        codex_raw, codex_error = _safe_probe(self._codex_probe) if live else (None, "")
        artifact_raw, artifact_error = (
            _safe_probe(self._artifact_probe) if live else (None, "")
        )
        checks.append(self._qq_check(qq_raw, qq_error, live=live))
        checks.append(
            self._codex_check(
                codex_raw,
                codex_error,
                live=live,
                model_context=model_context,
            ),
        )
        checks.append(
            self._artifact_check(
                artifact_raw,
                artifact_error,
                live=live,
                enabled=artifact_enabled,
            ),
        )
        checks.append(
            _check(
                "relationship_proactive",
                "关系与主动行为",
                "healthy" if relationship_enabled else "unavailable",
                (
                    "关系状态、社交主动与运营通知分离功能已启用。"
                    if relationship_enabled
                    else "Gate 8 关系与主动行为功能尚未切换。"
                ),
                evidence_type="feature_flag",
                next_action="" if relationship_enabled else "完成 Gate 8 迁移、测试与切换。",
            ),
        )
        overall = max(
            (item["status"] for item in checks),
            key=lambda status: STATUS_ORDER[status],
        )
        counts = {
            status: sum(1 for item in checks if item["status"] == status)
            for status in STATUS_ORDER
        }
        return {
            "generated_at": utc_now(),
            "live_probe": bool(live),
            "status": overall,
            "counts": counts,
            "checks": checks,
            "contract": {
                "unknown_is_healthy": False,
                "process_running_is_business_ready": False,
                "secret_fields_included": False,
            },
        }


__all__ = ["BusinessHealthService", "STATUS_ORDER"]
