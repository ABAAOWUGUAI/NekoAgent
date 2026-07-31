#!/usr/bin/env python3
"""Read-only Codex installation and authentication diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import getpass
import threading
import time

try:
    import pwd
except ImportError:  # Windows test environment
    pwd = None


_STATUS_CACHE: dict = {}
_STATUS_CACHE_AT = 0.0
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_TTL = 30.0


def _command(args: list[str], timeout: int = 12) -> dict:
    try:
        completed = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "output": "", "error": str(exc)}
    output = (completed.stdout or completed.stderr or "").strip()
    return {"ok": completed.returncode == 0, "output": output[:500], "error": "" if completed.returncode == 0 else output[:500]}


def codex_operations_status(*, force: bool = False) -> dict:
    global _STATUS_CACHE, _STATUS_CACHE_AT
    with _STATUS_CACHE_LOCK:
        if not force and _STATUS_CACHE and time.monotonic() - _STATUS_CACHE_AT < _STATUS_CACHE_TTL:
            return dict(_STATUS_CACHE)
    executable = shutil.which("codex") or "codex"
    version = _command([executable, "--version"])
    login = _command([executable, "login", "status"])
    install = _command(["npm", "list", "-g", "--depth=0", "@openai/codex"])
    service_user = pwd.getpwuid(os.geteuid()).pw_name if pwd is not None else getpass.getuser()
    result = {
        "ok": bool(version["ok"] and login["ok"]),
        "version": version["output"],
        "executable": os.path.realpath(executable) if os.path.exists(executable) else executable,
        "install_method": "npm-global" if "@openai/codex@" in install["output"] else "unknown",
        "package": next((line.strip() for line in install["output"].splitlines() if "@openai/codex@" in line), ""),
        "service_user": service_user,
        "login_state": "authenticated" if login["ok"] else "login_required",
        "login_message": login["output"],
        "credential_policy": "认证缓存属于运行服务账号；禁止复制、下载或共享 auth.json。",
        "login_steps": [
            f"以 {service_user} 身份登录服务器",
            "运行 codex login status 检查现有状态",
            "需要登录时运行 codex login --device-auth，并在自己的浏览器完成确认",
            "再次运行 codex login status；不要传输认证缓存文件",
        ],
        "upgrade_steps": [
            "记录 codex --version、codex login status 和 npm 安装版本",
            "备份 ~/.codex/config.toml 与项目数据库，不备份或分发认证 Token",
            "执行 codex update（当前安装支持自更新）",
            "验证新版本、登录状态和一次只读 codex exec 冒烟",
            "重启 Bridge 并验证 /health；失败时恢复原 npm 版本与配置",
        ],
    }
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE = dict(result)
        _STATUS_CACHE_AT = time.monotonic()
    return result
