#!/usr/bin/env python3
"""QQ runtime diagnostics using logs plus authoritative plugin heartbeats."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from bridge_qq_access_runtime import diagnostic_access_snapshot
from bridge_qq_login_status import probe_napcat_login
from bridge_migrations import MigrationDriftError
from bridge_qq_runtime_service import get_runtime_summary
from bridge_participation_diagnostics import participation_diagnostics
from bridge_qq_qrcode import qrcode_freshness


def _runtime_snapshot(connect) -> dict:
    try:
        with connect() as conn:
            return get_runtime_summary(conn)
    except (MigrationDriftError, sqlite3.Error, ValueError):
        return {"state": "offline", "heartbeat_age_seconds": None}


def collect_qq_diagnostics(
    *,
    assistant_connect,
    capture_command: Callable,
    safe_log_text: Callable,
    last_index: Callable,
    bridge_probe: Callable,
    qrcode_info: Callable,
    qrcode_decode_url: Callable,
    recent_matching_lines: Callable,
    container_file_exists: Callable,
    list_events: Callable,
    napcat_container: str,
    astrbot_container: str,
    qrcode_max_age_seconds: int = 300,
) -> dict:
    started = time.monotonic()
    napcat_ok, napcat_logs = capture_command(
        ["docker", "logs", "--tail", "500", napcat_container], timeout=12,
    )
    astrbot_ok, astrbot_logs = capture_command(
        ["docker", "logs", "--tail", "500", astrbot_container], timeout=12,
    )
    napcat_clean = safe_log_text(napcat_logs)
    astrbot_clean = safe_log_text(astrbot_logs)
    combined = napcat_clean + "\n" + astrbot_clean

    access_control, allowed_ids = diagnostic_access_snapshot(assistant_connect)
    runtime = _runtime_snapshot(assistant_connect)
    runtime_applied = bool(
        runtime.get("state") == "applied"
        and runtime.get("actual_bot_id")
        and runtime.get("applied_version") == runtime.get("config_version")
    )
    live_login = probe_napcat_login(capture_command, napcat_container)
    allowed_ids_text = ",".join(allowed_ids) or "未配置"
    allowed_markers = tuple(f"({item})" for item in allowed_ids)

    login_error_index = last_index(
        napcat_clean,
        ("KickedOffLine", "账号状态变更为离线", "快速登录错误", "用户身份已失效", "请扫描下面的二维码"),
    )
    login_ready_index = last_index(
        napcat_clean,
        ("OneBot11 适配器初始化完成", "登录成功", "接收 <-", "发送 ->"),
    )
    if live_login.get("checked"):
        needs_login = not live_login.get("is_login")
    else:
        needs_login = login_error_index > login_ready_index and not runtime_applied
    send_timeout_index = last_index(napcat_clean, ("NodeIKernelMsgService/sendMsg", "qq_send_timeout_uncertain"))
    send_path_degraded = send_timeout_index > login_ready_index
    if needs_login:
        qq_status = "login_required"
    elif send_path_degraded:
        qq_status = "degraded"
    elif live_login.get("checked"):
        qq_status = "online"
    else:
        qq_status = "online" if login_ready_index >= 0 or runtime_applied else "unknown"

    open_index = last_index(astrbot_clean, ("aiocqhttp(OneBot v11) 适配器已连接", "GET /ws"))
    close_index = last_index(astrbot_clean, ("连接意外关闭",))
    onebot_connected = open_index >= 0 and open_index > close_index
    plugin_loaded = "Plugin codex_agent" in astrbot_clean or "Loading plugin astrbot_plugin_codex_agent" in astrbot_clean
    bridge = bridge_probe()
    qrcode = qrcode_info()
    decode_url = qrcode_decode_url(napcat_clean)
    qrcode_mtime = int(qrcode.get("mtime") or 0)
    qrcode_fresh, qrcode_age_seconds = qrcode_freshness(
        qrcode, int(qrcode_max_age_seconds or 300), now=int(time.time()),
    )

    allowed_receive_lines = []
    allowed_send_lines = []
    for line in napcat_clean.splitlines():
        if not allowed_markers or not any(marker in line for marker in allowed_markers):
            continue
        if "接收 <- 私聊" in line:
            allowed_receive_lines.append(line.strip()[:240])
        if "发送 -> 私聊" in line:
            allowed_send_lines.append(line.strip()[:240])

    plugin_lines = recent_matching_lines(
        astrbot_clean,
        ("codex_agent", "Codex task", "Codex call failed", "task create failed"),
        limit=10,
    )
    connection_lines = recent_matching_lines(
        combined,
        (
            "KickedOffLine", "账号状态变更为离线", "快速登录错误", "请扫描下面的二维码",
            "aiocqhttp(OneBot v11) 适配器已连接", "连接意外关闭", "OneBot11 适配器初始化完成",
        ),
        limit=12,
    )
    plugin_loaded = runtime_applied or plugin_loaded or bool(plugin_lines) or container_file_exists(
        astrbot_container, "/AstrBot/data/plugins/astrbot_plugin_codex_agent/main.py",
    )
    onebot_connected = (
        onebot_connected or bool(allowed_receive_lines) or bool(allowed_send_lines)
    ) and not needs_login

    if needs_login and qrcode.get("available") and not qrcode_fresh:
        recommendation = "QQ 登录态已失效，现有二维码已经过期；请刷新并等待新二维码生成后再扫码。"
    elif needs_login:
        recommendation = "QQ 登录态已失效，需要重新扫码登录 NapCat。"
    elif send_path_degraded:
        recommendation = "QQ 连接仍在，但最近的发送在 QQ 内核确认阶段超时；请重新登录 NapCat 后再做一次短消息测试。"
    elif qq_status != "online":
        recommendation = "未确认 QQ 登录状态，请先查看 NapCat 日志或运行时心跳。"
    elif not onebot_connected:
        recommendation = "QQ 已登录，但 OneBot 到 AstrBot 的 WebSocket 未确认连接，需要检查网络。"
    elif not plugin_loaded:
        recommendation = "AstrBot 在线，但 codex_agent 插件未加载，需要检查插件目录或重启 AstrBot。"
    elif not bridge["ok"]:
        recommendation = "插件所在容器无法访问 codex-qq-bridge，需要检查 Bridge URL 或 Docker 网络。"
    elif not allowed_receive_lines:
        recommendation = f"链路在线，但最近未看到白名单 QQ {allowed_ids_text} 的私聊消息；请发送“状态”做一次触发测试。"
    elif not allowed_send_lines:
        recommendation = "已看到白名单私聊进入 NapCat，但最近未看到发送记录；请查看插件路由日志和 AstrBot 日志。"
    else:
        recommendation = "QQ 到 AstrBot 到 Bridge 的链路基本在线；如单条消息无回复，优先查看插件路由日志和任务详情。"

    audit_events = list_events(user_id=allowed_ids[0], limit=16) if allowed_ids else list_events(limit=16)
    participation = participation_diagnostics(assistant_connect)
    audit_recent_reply = any(item["stage"] in {"reply_complete", "task_created", "error"} for item in audit_events[:8])
    checks = [
        {"name": "qq_login", "ok": qq_status == "online" and not needs_login, "label": "QQ 登录"},
        {"name": "send_path", "ok": not send_path_degraded and not needs_login, "label": "QQ 发送"},
        {"name": "onebot", "ok": onebot_connected, "label": "OneBot 连接"},
        {"name": "plugin", "ok": plugin_loaded, "label": "插件加载"},
        {"name": "bridge", "ok": bridge["ok"], "label": "Bridge 连通"},
        {"name": "recent_receive", "ok": bool(allowed_receive_lines), "label": "白名单消息接收"},
        {
            "name": "recent_reply",
            "ok": (bool(allowed_send_lines) or audit_recent_reply) and not send_path_degraded,
            "label": "回复/处理记录",
        },
    ]
    return {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "napcat_logs_ok": napcat_ok,
        "astrbot_logs_ok": astrbot_ok,
        "allowed_qq_ids": allowed_ids,
        "access_control": access_control,
        "qq_status": qq_status,
        "needs_login": needs_login,
        "send_path_degraded": send_path_degraded,
        "runtime_state": runtime.get("state"),
        "runtime_config_applied": runtime_applied,
        "runtime_heartbeat_age_seconds": runtime.get("heartbeat_age_seconds"),
        "live_login_checked": bool(live_login.get("checked")),
        "live_login_error_kind": str(live_login.get("error_kind") or ""),
        "qrcode_available": bool(needs_login and qrcode_fresh),
        "qrcode_upstream_available": bool(needs_login and live_login.get("qrcode_available")),
        "qrcode_stale": bool(needs_login and qrcode.get("available") and not qrcode_fresh),
        "qrcode_url": "/qq/qrcode" if needs_login and qrcode_fresh else "",
        "qrcode_path": qrcode.get("path", ""),
        "qrcode_size": qrcode.get("size", 0),
        "qrcode_mtime": qrcode_mtime,
        "qrcode_age_seconds": qrcode_age_seconds,
        "qrcode_decode_url": decode_url,
        "onebot_connected": onebot_connected,
        "plugin_loaded": plugin_loaded,
        "bridge_reachable_from_astrbot": bridge["ok"],
        "bridge_url": bridge["url"],
        "bridge_probe_output": bridge["output"],
        "recent_allowed_receives": allowed_receive_lines[-6:],
        "recent_allowed_sends": allowed_send_lines[-6:],
        "connection_events": connection_lines,
        "plugin_events": plugin_lines,
        "audit_events": audit_events,
        "participation": participation,
        "checks": checks,
        "recommendation": recommendation,
    }


__all__ = ["collect_qq_diagnostics"]
