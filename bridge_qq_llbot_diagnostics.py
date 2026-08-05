#!/usr/bin/env python3
"""Privacy-safe LLBot diagnostics built from service and runtime facts."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable

from bridge_migrations import MigrationDriftError
from bridge_participation_diagnostics import participation_diagnostics
from bridge_qq_access_runtime import diagnostic_access_snapshot
from bridge_qq_runtime_service import get_runtime_summary


def _runtime_snapshot(connect) -> dict:
    try:
        with connect() as conn:
            return get_runtime_summary(conn)
    except (MigrationDriftError, sqlite3.Error, ValueError):
        return {"state": "offline", "heartbeat_age_seconds": None}


def _safe_event(item: dict) -> dict:
    """Return audit metadata only; never expose QQ ids or message bodies."""

    return {
        key: item.get(key, "")
        for key in ("created_at", "trace_id", "stage", "action", "status", "task_id")
    }


def _service_event_lines(text: str, limit: int = 12) -> list[str]:
    markers = (
        "登录成功", "快速登录", "quick login", "WebSocket", "OneBot",
        "KickedOffLine", "登录已失效", "鉴权失败", "auth failed",
    )
    result: list[str] = []
    for raw in str(text or "").splitlines():
        if not any(marker.lower() in raw.lower() for marker in markers):
            continue
        line = re.sub(r"\b\d{5,20}\b", "[account]", raw)
        line = re.sub(r"(?i)(token|secret|password)(\s*[:=]\s*)\S+", r"\1\2[redacted]", line)
        result.append(line.strip()[:240])
    return result[-limit:]


def collect_llbot_diagnostics(
    *,
    assistant_connect,
    task_connect=None,
    service_status: Callable[[], dict],
    service_logs: Callable[[], tuple[bool, str]],
    bridge_probe: Callable[[], dict],
    container_file_exists: Callable[[str, str], bool],
    list_events: Callable,
    astrbot_container: str,
) -> dict:
    started = time.monotonic()
    access_control, allowed_ids = diagnostic_access_snapshot(assistant_connect)
    runtime = _runtime_snapshot(assistant_connect)
    service = service_status()
    logs_ok, logs = service_logs()
    bridge = bridge_probe()

    runtime_applied = bool(
        runtime.get("state") == "applied"
        and runtime.get("actual_bot_id")
        and runtime.get("applied_version") == runtime.get("config_version")
    )
    service_active = bool(service.get("ok") and service.get("status") == "active")
    qq_online = service_active and runtime_applied
    needs_login = service_active and runtime.get("state") in {"offline", "pending"}
    onebot_connected = qq_online
    plugin_loaded = runtime_applied or container_file_exists(
        astrbot_container, "/AstrBot/data/plugins/astrbot_plugin_codex_agent/main.py",
    )

    raw_events = list_events(user_id=allowed_ids[0], limit=16) if allowed_ids else list_events(limit=16)
    audit_events = [_safe_event(dict(item)) for item in raw_events]
    recent_receives = [
        f"{item.get('created_at', '')} · received · {item.get('status') or '-'}"
        for item in audit_events if item.get("stage") == "received"
    ][-6:]
    recent_sends = [
        f"{item.get('created_at', '')} · {item.get('stage')} · {item.get('status') or '-'}"
        for item in audit_events if item.get("stage") in {"reply_ready", "reply_complete"}
    ][-6:]

    if not service_active:
        qq_status = "offline"
        recommendation = "LLBot 服务未运行；先恢复 llbot.service，再检查 OneBot 与 AstrBot。"
    elif needs_login:
        qq_status = "login_required"
        recommendation = (
            "LLBot 已运行但尚未确认 QQ 登录。请通过服务器本地 3080 端口的安全 SSH 隧道进入 LLBot WebUI 扫码；"
            "控制台不会保存或代填 WebUI 密码。"
        )
    elif not runtime_applied:
        qq_status = "unknown"
        recommendation = "LLBot 服务在线，但 AstrBot 运行时心跳尚未应用；检查 OneBot 反向 WebSocket。"
    elif not bridge.get("ok"):
        qq_status = "online"
        recommendation = "QQ 与 OneBot 已在线，但 AstrBot 暂时无法访问 Bridge；检查容器到宿主机的 Bridge 地址。"
    else:
        qq_status = "online"
        recommendation = "LLBot、OneBot、AstrBot 插件和 Bridge 均在线；消息异常时优先查看消息审计与 Delivery 状态。"

    participation = participation_diagnostics(assistant_connect, task_connect=task_connect)
    checks = [
        {"name": "llbot_service", "ok": service_active, "label": "LLBot 服务"},
        {"name": "qq_login", "ok": qq_online, "label": "QQ 登录"},
        {"name": "onebot", "ok": onebot_connected, "label": "OneBot 连接"},
        {"name": "plugin", "ok": plugin_loaded, "label": "插件加载"},
        {"name": "bridge", "ok": bool(bridge.get("ok")), "label": "Bridge 连通"},
        {"name": "recent_receive", "ok": bool(recent_receives), "label": "最近接收"},
        {"name": "recent_reply", "ok": bool(recent_sends), "label": "最近回复"},
    ]
    return {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "adapter_id": "llbot",
        "adapter_label": "LLBot",
        "adapter_service": "llbot.service",
        "service_active": service_active,
        "service_status": service.get("status", "unknown"),
        "service_logs_ok": logs_ok,
        "allowed_qq_ids": allowed_ids,
        "access_control": access_control,
        "qq_status": qq_status,
        "needs_login": needs_login,
        "send_path_degraded": False,
        "runtime_state": runtime.get("state"),
        "runtime_config_applied": runtime_applied,
        "runtime_heartbeat_age_seconds": runtime.get("heartbeat_age_seconds"),
        "live_login_checked": runtime_applied,
        "live_login_error_kind": "none" if qq_online else "runtime_not_applied",
        "qrcode_supported": False,
        "qrcode_available": False,
        "qrcode_upstream_available": False,
        "qrcode_stale": False,
        "qrcode_url": "",
        "qrcode_path": "",
        "qrcode_size": 0,
        "qrcode_mtime": 0,
        "qrcode_age_seconds": None,
        "qrcode_decode_url": "",
        "login_management": "ssh_tunnel_webui",
        "login_management_hint": "SSH 隧道访问服务器 127.0.0.1:3080，再在 LLBot WebUI 中扫码。",
        "onebot_connected": onebot_connected,
        "plugin_loaded": plugin_loaded,
        "bridge_reachable_from_astrbot": bool(bridge.get("ok")),
        "bridge_url": bridge.get("url", ""),
        "bridge_probe_output": bridge.get("output", ""),
        "recent_allowed_receives": recent_receives,
        "recent_allowed_sends": recent_sends,
        "connection_events": _service_event_lines(logs),
        "plugin_events": [],
        "audit_events": audit_events,
        "participation": participation,
        "checks": checks,
        "recommendation": recommendation,
    }


__all__ = ["collect_llbot_diagnostics"]
