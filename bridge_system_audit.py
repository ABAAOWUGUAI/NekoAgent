#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bridge_proxy_probe import fast_target_probe as proxy_target_probe
from bridge_executor_health import probe_executor_path
from bridge_ops_broker_client import OpsBrokerClient
from bridge_qq_runtime_service import get_runtime_summary


AGENT_STACK = Path("/opt/agent-stack")
BRIDGE_DIR = AGENT_STACK / "codex-qq-bridge"
TASK_DB = Path(os.environ.get("TASK_DB_PATH", "/var/lib/agent-bridge/tasks.sqlite3"))
ASSISTANT_DB = Path(os.environ.get("ASSISTANT_DB_PATH", "/var/lib/agent-bridge/assistant.sqlite3"))
BRIDGE_FILE = BRIDGE_DIR / "codex_qq_bridge.py"
ADMIN_FILE = BRIDGE_DIR / "admin_console.py"
ASTRBOT_CONTAINER = "astrbot"
NAPCAT_CONTAINER = "maim-bot-napcat"
QQ_ADAPTER = os.environ.get("QQ_ADAPTER", "napcat").strip().lower()
LLBOT_SERVICE = os.environ.get("LLBOT_SERVICE", "llbot").strip() or "llbot"
MIHOMO_PROXY = "http://127.0.0.1:7890"
PROXY_TARGETS = (
    {"name": "chatgpt", "label": "ChatGPT", "url": "https://chatgpt.com/cdn-cgi/trace", "required": True},
    {"name": "openai", "label": "OpenAI API", "url": "https://api.openai.com/v1/models", "required": True},
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(args: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        completed = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        text = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return completed.returncode == 0, text
    except Exception as exc:
        return False, str(exc)


def _json_get(url: str, timeout: int = 8, proxy: str | None = None) -> tuple[bool, dict, str]:
    command = ["curl", "-sS", "--connect-timeout", "5", "--max-time", str(timeout)]
    if proxy:
        command.extend(["--proxy", proxy])
    else:
        command.extend(["--noproxy", "*"])
    command.append(url)
    ok, output = _run(command, timeout=timeout + 3)
    if not ok:
        return False, {}, output[:300]
    try:
        return True, json.loads(output or "{}"), ""
    except json.JSONDecodeError:
        return False, {}, "invalid json response"


def _curl_status(url: str, *, proxy: str | None = None, timeout: int = 10) -> dict:
    started = time.monotonic()
    command = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{time_total} %{errormsg}",
        "--connect-timeout",
        "5",
        "--max-time",
        str(timeout),
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    else:
        command.extend(["--noproxy", "*"])
    command.append(url)
    ok, output = _run(command, timeout=timeout + 4)
    parts = output.split(" ", 2)
    code = parts[0] if parts else "000"
    error = parts[2] if len(parts) > 2 else ("" if ok else output)
    return {
        "ok": ok and code in {"200", "204", "301", "302", "401", "403"},
        "http_code": code,
        "duration": round(time.monotonic() - started, 2),
        "error": error[:240],
    }


def _finding(severity: str, area: str, title: str, detail: str, action: str) -> dict:
    return {
        "severity": severity,
        "area": area,
        "title": title,
        "detail": detail,
        "action": action,
    }


def _systemd_status(name: str) -> dict:
    try:
        result = OpsBrokerClient().request({"action": "service_status", "target": name, "args": {}})
        data = dict(result.get("data") or {})
        return {"name": name, "ok": bool(result.get("ok") and data.get("ok")), "status": data.get("status", "unknown")}
    except Exception:
        return {"name": name, "ok": False, "status": "broker_unavailable"}


def _container_status(name: str) -> dict:
    try:
        result = OpsBrokerClient().request({"action": "container_status", "target": name, "args": {}})
        data = dict(result.get("data") or {})
        return {"name": name, "ok": bool(result.get("ok") and data.get("ok")), "status": data.get("status", "unknown")}
    except Exception:
        return {"name": name, "ok": False, "status": "broker_unavailable"}


def _task_summary() -> dict:
    summary = {"ok": False, "total": 0, "active": 0, "failed_recent": 0, "qq_delivery_failed": 0}
    if not TASK_DB.exists():
        summary["error"] = "task db not found"
        return summary
    try:
        with closing(sqlite3.connect(str(TASK_DB))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT status, source, delivery_status, created_at FROM tasks ORDER BY created_at DESC LIMIT 80"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    except Exception as exc:
        summary["error"] = str(exc)
        return summary
    active = {"queued", "running"}
    failed = {"failed", "timeout"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    def recent(row: sqlite3.Row) -> bool:
        try:
            created = datetime.fromisoformat(str(row["created_at"] or "").replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return created >= cutoff
        except ValueError:
            return False

    recent_rows = [row for row in rows if recent(row)]
    summary.update(
        {
            "ok": True,
            "total": total,
            "active": sum(1 for row in rows if row["status"] in active),
            "failed_recent": sum(1 for row in recent_rows[:20] if row["status"] in failed),
            "qq_delivery_failed": sum(
                1
                for row in recent_rows[:40]
                if row["source"] == "qq" and row["delivery_status"] == "failed"
            ),
        },
    )
    return summary


def _qq_summary() -> dict:
    if QQ_ADAPTER == "llbot":
        service = _systemd_status(LLBOT_SERVICE)
        try:
            with closing(sqlite3.connect(str(ASSISTANT_DB))) as conn:
                conn.row_factory = sqlite3.Row
                runtime = get_runtime_summary(conn)
        except Exception as exc:
            return {
                "ok": False,
                "status": "unknown",
                "adapter": "llbot",
                "error": str(exc)[:240],
            }
        applied = bool(
            runtime.get("state") == "applied"
            and runtime.get("actual_bot_id")
            and runtime.get("applied_version") == runtime.get("config_version")
        )
        if not service.get("ok"):
            status = "offline"
        elif applied:
            status = "online"
        elif runtime.get("state") in {"offline", "pending"}:
            status = "login_required"
        else:
            status = "unknown"
        return {
            "ok": status == "online",
            "status": status,
            "adapter": "llbot",
            "service_status": service.get("status", "unknown"),
            "runtime_state": runtime.get("state", "offline"),
            "recent_offline": status in {"offline", "login_required"},
            "send_path_degraded": False,
        }
    try:
        result = OpsBrokerClient().request({
            "action": "container_logs", "target": NAPCAT_CONTAINER,
            "args": {"lines": 500, "timeout_seconds": 12},
        })
        data = dict(result.get("data") or {})
        ok, napcat = bool(result.get("ok") and data.get("ok")), str(data.get("output") or "")
    except Exception:
        ok, napcat = False, "broker_unavailable"
    if not ok:
        return {"ok": False, "status": "unknown", "error": napcat[:240]}
    offline_index = max(
        napcat.rfind("KickedOffLine"),
        napcat.rfind("账号状态变更为离线"),
        napcat.rfind("登录已失效"),
        napcat.rfind("用户身份已失效"),
        napcat.rfind("快速登录错误"),
        napcat.rfind("请扫描下面的二维码"),
    )
    online_index = max(napcat.rfind("OneBot11 适配器初始化完成"), napcat.rfind("接收 <-"), napcat.rfind("发送 ->"))
    send_timeout_index = napcat.rfind("NodeIKernelMsgService/sendMsg")
    login_required = offline_index >= 0 and offline_index > online_index
    send_degraded = send_timeout_index >= 0 and send_timeout_index > online_index
    if login_required:
        status = "login_required"
    elif send_degraded:
        status = "degraded"
    else:
        status = "online" if online_index >= 0 else "unknown"
    return {
        "ok": status == "online",
        "status": status,
        "recent_offline": login_required,
        "send_path_degraded": send_degraded,
    }


def _codex_summary() -> dict:
    result = probe_executor_path(ASSISTANT_DB)
    return {
        "ok": bool(result.get("ok")),
        "status": "ready" if result.get("ok") else "check",
        "adapter": result.get("adapter", "unknown"),
        "error": result.get("error", ""),
    }


def _proxy_summary() -> dict:
    config_ok, config, config_error = _json_get("http://127.0.0.1:9090/configs", timeout=8)
    # These network checks are independent.  Run them together and use the
    # short-circuit probe so a healthy first client does not pay for two extra
    # diagnostics before the console can render the result.
    with ThreadPoolExecutor(max_workers=len(PROXY_TARGETS) + 1, thread_name_prefix="system-audit") as pool:
        target_futures = [
            pool.submit(proxy_target_probe, target, proxy=MIHOMO_PROXY, timeout=8)
            for target in PROXY_TARGETS
        ]
        ip_future = pool.submit(
            _json_get,
            "https://api.ipify.org?format=json",
            timeout=8,
            proxy=MIHOMO_PROXY,
        )
        tests = [future.result() for future in target_futures]
        ip_ok, ip_data, ip_error = ip_future.result()
    chatgpt = next((item for item in tests if item["name"] == "chatgpt"), {})
    openai = next((item for item in tests if item["name"] == "openai"), {})
    return {
        "ok": config_ok and any(item.get("ok") for item in tests),
        "mode": config.get("mode") if config_ok else "",
        "config_ok": config_ok,
        "config_error": config_error,
        "chatgpt": chatgpt,
        "openai": openai,
        "tests": tests,
        "proxy_ip": ip_data.get("ip", "") if ip_ok else "",
        "proxy_ip_error": ip_error,
    }


def _resource_summary() -> dict:
    disk = shutil.disk_usage("/")
    mem_total = 0
    mem_available = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            value = int(raw.strip().split()[0]) * 1024
            if key == "MemTotal":
                mem_total = value
            elif key == "MemAvailable":
                mem_available = value
    except Exception:
        pass
    disk_used_ratio = disk.used / disk.total if disk.total else 0
    mem_used_ratio = (mem_total - mem_available) / mem_total if mem_total else 0
    return {
        "ok": disk_used_ratio < 0.9 and mem_used_ratio < 0.9,
        "disk_used_ratio": round(disk_used_ratio, 3),
        "mem_used_ratio": round(mem_used_ratio, 3),
        "disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 2),
        "mem_available_gb": round(mem_available / 1024 / 1024 / 1024, 2) if mem_available else 0,
    }


def _codebase_summary() -> dict:
    bridge_size = BRIDGE_FILE.stat().st_size if BRIDGE_FILE.exists() else 0
    admin_size = ADMIN_FILE.stat().st_size if ADMIN_FILE.exists() else 0
    return {
        "ok": bridge_size <= 316_000 and admin_size <= 180_000,
        "bridge_kb": round(bridge_size / 1024, 1),
        "admin_kb": round(admin_size / 1024, 1),
    }


def system_audit() -> dict:
    started = time.monotonic()
    findings: list[dict] = []

    services = [_systemd_status("codex-qq-bridge"), _systemd_status("docker")]
    if QQ_ADAPTER == "llbot":
        services.append(_systemd_status(LLBOT_SERVICE))
        container_names = (ASTRBOT_CONTAINER, "mihomo", "maim-bot-core")
    else:
        container_names = (ASTRBOT_CONTAINER, NAPCAT_CONTAINER, "mihomo", "maim-bot-core")
    containers = [_container_status(name) for name in container_names]
    for item in services:
        if not item["ok"]:
            findings.append(_finding("critical", "service", f"{item['name']} 未运行", item["status"], "先恢复 systemd 服务，再验证上层功能。"))
    for item in containers:
        if not item["ok"]:
            findings.append(_finding("critical", "container", f"{item['name']} 容器异常", item["status"], "检查 docker logs 并重启对应容器。"))

    resources = _resource_summary()
    if resources["disk_used_ratio"] >= 0.9:
        findings.append(_finding("critical", "resource", "磁盘空间不足", f"根分区使用率 {resources['disk_used_ratio']:.0%}", "清理日志、备份或扩容磁盘。"))
    elif resources["disk_used_ratio"] >= 0.8:
        findings.append(_finding("warning", "resource", "磁盘空间偏高", f"根分区使用率 {resources['disk_used_ratio']:.0%}", "排查 Docker 镜像、日志和备份占用。"))
    if resources["mem_used_ratio"] >= 0.9:
        findings.append(_finding("warning", "resource", "内存压力偏高", f"内存使用率 {resources['mem_used_ratio']:.0%}", "观察容器内存占用，必要时限制或扩容。"))

    codex = _codex_summary()
    if not codex["ok"]:
        findings.append(_finding("critical", "codex", "任务执行器状态异常", codex.get("error", "")[:240], "检查执行器适配器、Codex CLI、模型代理与路由。"))

    proxy = _proxy_summary()
    if not proxy["config_ok"]:
        findings.append(_finding("critical", "proxy", "mihomo 控制接口不可用", proxy.get("config_error", ""), "先恢复 mihomo 控制接口。"))
    elif not proxy["ok"]:
        detail = f"chatgpt={proxy['chatgpt']['http_code']} openai={proxy['openai']['http_code']}"
        findings.append(_finding("critical", "proxy", "代理无法支撑 Codex/OpenAI 出站", detail, "到代理页运行 AI 可用性检测，切换可用节点或更新订阅。"))

    qq = _qq_summary()
    if qq["status"] == "login_required":
        if QQ_ADAPTER == "llbot":
            findings.append(_finding("critical", "qq", "QQ 登录态未确认", "LLBot 已运行，但 AstrBot 运行时心跳未确认登录。", "通过 SSH 隧道进入 LLBot WebUI 扫码，并检查 OneBot 反向 WebSocket。"))
        else:
            findings.append(_finding("critical", "qq", "QQ 登录态失效", "NapCat 日志显示账号离线或登录失效。", "进入 QQ 链路页刷新二维码并扫码。"))
    elif qq["status"] == "degraded":
        findings.append(_finding("critical", "qq", "QQ 发送链路异常", "NapCat 在 QQ 内核 sendMsg 阶段超时。", "重新登录 NapCat，再用短消息验证实际发送。"))
    elif not qq["ok"]:
        findings.append(_finding("warning", "qq", "QQ 链路状态不确定", qq.get("error", "未确认最近收发事件。"), "发送一条私聊测试并查看 QQ 链路页审计。"))

    tasks = _task_summary()
    if not tasks["ok"]:
        findings.append(_finding("warning", "task", "任务数据库不可读", tasks.get("error", ""), "检查 tasks.sqlite3 权限和 bridge 日志。"))
    else:
        if tasks["active"] >= 3:
            findings.append(_finding("warning", "task", "任务队列堆积", f"{tasks['active']} 个任务仍在 queued/running。", "查看任务页，取消卡死任务或降低并发触发。"))
        if tasks["failed_recent"] >= 3:
            findings.append(_finding("warning", "task", "近期任务失败偏多", f"最近 20 个任务中 {tasks['failed_recent']} 个失败或超时。", "优先检查代理和 Codex CLI，再重试任务。"))
        if tasks["qq_delivery_failed"]:
            findings.append(_finding("warning", "qq", "QQ 任务结果推送异常", f"近期 {tasks['qq_delivery_failed']} 个 QQ 任务未成功推送。", "检查 AstrBot 插件日志和 QQ 链路审计。"))

    codebase = _codebase_summary()
    if not codebase["ok"]:
        findings.append(_finding("warning", "maintainability", "核心文件仍偏大", f"bridge={codebase['bridge_kb']}KB admin={codebase['admin_kb']}KB", "后续继续拆分 proxy、task、admin API 模块，降低维护和上下文成本。"))

    severity_cost = {"critical": 25, "warning": 10, "info": 0}
    score = max(0, 100 - sum(severity_cost.get(item["severity"], 0) for item in findings))
    if score >= 85:
        level = "healthy"
    elif score >= 65:
        level = "attention"
    else:
        level = "critical"

    return {
        "ok": True,
        "generated_at": _now(),
        "duration": round(time.monotonic() - started, 2),
        "score": score,
        "level": level,
        "summary": {
            "services": services,
            "containers": containers,
            "resources": resources,
            "codex": codex,
            "proxy": proxy,
            "qq": qq,
            "tasks": tasks,
            "codebase": codebase,
        },
        "findings": findings,
    }
