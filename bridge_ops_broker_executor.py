"""Allowlisted root-side executor for the restricted Ops Broker."""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any

from bridge_qq_login_status import _NAPCAT_LOGIN_PROBE

MAX_OUTPUT = 200_000
MAX_QRCODE_BYTES = 1_000_000
NAPCAT_QRCODE_PATH = "/app/napcat/cache/qrcode.png"
ADMIN_TOKEN_PATH = Path(os.environ.get(
    "ADMIN_TOKEN_PATH",
    "/etc/agent-bridge/secrets/admin-token",
))
CHANNEL_TOKEN_PATH = Path(os.environ.get(
    "CHANNEL_TOKEN_PATH",
    "/etc/agent-bridge/secrets/qq-channel-token",
))
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|cookie|secret)\s*[=:]\s*)[^\s,;]+"),
)

_BRIDGE_HEALTH_PROBE = r'''
import urllib.request

# The container-to-host bridge address is fixed by the deployment network.
# Do not accept a caller-provided URL or arbitrary command text here.
with urllib.request.urlopen("/health", timeout=3) as response:
    print(response.read().decode("utf-8"))
'''


def redact_output(value: str) -> str:
    text = str(value or "")[-MAX_OUTPUT:]
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


def _run(args: list[str], timeout: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            args, text=True, capture_output=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        return False, redact_output(str(exc))
    # Status commands must not turn harmless CLI warnings into the state
    # value.  Logs may legitimately use stderr, so stdout remains preferred
    # and stderr is used only when stdout is empty.
    output = redact_output(completed.stdout if (completed.stdout or "").strip() else (completed.stderr or ""))
    return completed.returncode == 0, output


def _run_bytes(args: list[str], timeout: int) -> tuple[bool, bytes, str]:
    try:
        completed = subprocess.run(
            args, capture_output=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        return False, b"", redact_output(str(exc))
    error = redact_output(
        (completed.stderr or b"").decode("utf-8", errors="replace"),
    )
    return completed.returncode == 0, completed.stdout or b"", error


def _replace_admin_token(token: str) -> dict[str, Any]:
    """Atomically replace the one fixed admin secret without invoking a shell."""

    path = ADMIN_TOKEN_PATH
    try:
        channel_token = CHANNEL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return {"ok": False, "error": "channel_token_file_missing"}
    if channel_token and hmac.compare_digest(token.encode("utf-8"), channel_token.encode("utf-8")):
        return {"ok": False, "error": "admin_token_matches_channel_token"}
    if path.is_symlink():
        return {"ok": False, "error": "admin_token_symlink_forbidden"}
    try:
        current = path.stat()
    except OSError:
        return {"ok": False, "error": "admin_token_file_missing"}
    mode = stat.S_IMODE(current.st_mode)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or mode not in {0o600, 0o640}
    ):
        return {"ok": False, "error": "admin_token_file_policy_invalid"}

    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temp_path, flags, mode)
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, current.st_uid, current.st_gid)
        payload = (token + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        readback = path.read_text(encoding="utf-8").strip()
        if not hmac.compare_digest(readback.encode("utf-8"), token.encode("utf-8")):
            return {"ok": False, "error": "admin_token_readback_failed"}
        return {"ok": True, "changed": True}
    except OSError:
        return {"ok": False, "error": "admin_token_write_failed"}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def execute_read(request: dict[str, Any]) -> dict[str, Any]:
    action = request["action"]
    target = request["target"]
    args = request.get("args") or {}
    timeout = int(args.get("timeout_seconds") or 8)
    if action == "service_status":
        ok, output = _run(["systemctl", "is-active", target], min(timeout, 10))
        return {"ok": ok and output.strip() == "active", "target": target, "status": output.strip() or "unknown"}
    if action == "service_logs":
        lines = int(args.get("lines") or 100)
        ok, output = _run(["journalctl", "-u", target, "-n", str(lines), "--no-pager"], min(timeout, 30))
        return {"ok": ok, "target": target, "lines": lines, "output": output}
    if action == "container_status":
        ok, output = _run(["docker", "inspect", "-f", "{{.State.Status}}", target], min(timeout, 10))
        return {"ok": ok and output.strip() == "running", "target": target, "status": output.strip() or "unknown"}
    if action == "container_list" and target == "docker":
        ok, output = _run(["docker", "ps", "--format", "{{json .}}"], min(timeout, 15))
        containers = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            containers.append({
                "id": item.get("ID", ""),
                "name": item.get("Names", ""),
                "image": item.get("Image", ""),
                "status": item.get("Status", ""),
                "state": item.get("State", ""),
                "ports": item.get("Ports", ""),
                "running_for": item.get("RunningFor", ""),
            })
        return {"ok": ok, "target": target, "containers": containers}
    if action == "container_logs":
        lines = int(args.get("lines") or 100)
        ok, output = _run(["docker", "logs", "--tail", str(lines), target], min(timeout, 30))
        return {"ok": ok, "target": target, "lines": lines, "output": output}
    if action == "container_env":
        name = str(args.get("name") or "")
        ok, output = _run(["docker", "exec", target, "printenv", name], min(timeout, 10))
        return {"ok": ok, "target": target, "name": name, "value": output.strip() if ok else ""}
    if action == "container_file_exists":
        path = str(args.get("path") or "")
        ok, _ = _run(["docker", "exec", target, "test", "-s", path], min(timeout, 10))
        return {"ok": ok, "target": target, "path": path, "exists": ok}
    if action == "qq_qrcode_info":
        if target != "maim-bot-napcat":
            return {"ok": False, "error": "qq_qrcode_target_forbidden"}
        ok, output = _run(
            [
                "docker", "exec", target, "sh", "-lc",
                (
                    "if [ -s /app/napcat/cache/qrcode.png ]; then "
                    "stat -c '%s %Y %n' /app/napcat/cache/qrcode.png; "
                    "else exit 1; fi"
                ),
            ],
            min(timeout, 10),
        )
        if not ok:
            return {"ok": False, "target": target, "path": NAPCAT_QRCODE_PATH}
        parts = output.split(maxsplit=2)
        if len(parts) != 3:
            return {"ok": False, "target": target, "path": NAPCAT_QRCODE_PATH}
        try:
            size = int(parts[0])
            mtime = int(float(parts[1]))
        except ValueError:
            return {"ok": False, "target": target, "path": NAPCAT_QRCODE_PATH}
        return {
            "ok": size > 0,
            "target": target,
            "path": parts[2],
            "size": size,
            "mtime": mtime,
        }
    if action == "qq_qrcode_png":
        if target != "maim-bot-napcat":
            return {"ok": False, "error": "qq_qrcode_target_forbidden"}
        ok, payload, error = _run_bytes(
            ["docker", "exec", target, "cat", NAPCAT_QRCODE_PATH],
            min(timeout, 10),
        )
        if not ok:
            return {"ok": False, "target": target, "error": error or "qrcode_read_failed"}
        if not payload or len(payload) > MAX_QRCODE_BYTES:
            return {"ok": False, "target": target, "error": "qrcode_size_invalid"}
        return {
            "ok": True,
            "target": target,
            "path": NAPCAT_QRCODE_PATH,
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }
    if action == "qq_login_probe":
        ok, output = _run(["docker", "exec", target, "python3", "-c", _NAPCAT_LOGIN_PROBE], min(timeout, 20))
        return {"ok": ok, "target": target, "output": output}
    if action == "container_bridge_probe":
        ok, output = _run(["docker", "exec", target, "python3", "-c", _BRIDGE_HEALTH_PROBE], min(timeout, 10))
        return {"ok": ok and '"ok": true' in output.lower(), "target": target, "output": output}
    if action == "config_test" and target == "mihomo":
        ok, output = _run(
            ["docker", "exec", "mihomo", "/mihomo", "-t", "-d", "/root/.config/mihomo"],
            min(timeout, 30),
        )
        return {"ok": ok, "target": target, "output": output}
    return {"ok": False, "error": "ops_read_action_unimplemented"}


def execute_write(request: dict[str, Any]) -> dict[str, Any]:
    """Execute only fixed argv or bounded, product-owned plugin operations."""

    action = request["action"]
    target = request["target"]
    args = request.get("args") or {}
    if action == "service_restart":
        ok, output = _run(["systemctl", "restart", f"{target}.service"], 30)
        return {"ok": ok, "target": target, "restarted": ok, "error": "" if ok else output}
    if action == "container_restart":
        ok, output = _run(["docker", "restart", "--time", "20", target], 30)
        return {"ok": ok, "target": target, "restarted": ok, "error": "" if ok else output}
    if action == "proxy_reload":
        ok, output = _run(["docker", "kill", "--signal", "HUP", "mihomo"], 20)
        return {"ok": ok, "target": target, "reloaded": ok, "error": "" if ok else output}
    if action == "astrbot_plugin_set_enabled":
        from bridge_capability_registry import set_plugin_enabled

        return set_plugin_enabled(str(args["plugin_id"]), bool(args["enabled"]))
    if action == "astrbot_plugin_operate":
        from bridge_plugin_marketplace import operate_market_plugin

        database = os.environ.get("ASSISTANT_DB_PATH", "/var/lib/agent-bridge/assistant.sqlite3")
        with sqlite3.connect(database, timeout=20) as conn:
            conn.row_factory = sqlite3.Row
            return operate_market_plugin(conn, {
                "action": str(args["operation"]),
                "plugin_id": str(args["plugin_id"]),
                "confirm_risk": True,
            })
    if action == "admin_token_rotate" and target == "bridge-admin-token":
        return _replace_admin_token(str(args["new_token"]))
    return {"ok": False, "error": "ops_write_action_unimplemented"}


def execute(request: dict[str, Any]) -> dict[str, Any]:
    """Executor entry point."""

    if request.get("action") in {
        "service_status", "service_logs", "container_status", "container_list",
        "container_logs", "container_env", "container_file_exists", "qq_login_probe",
        "container_bridge_probe", "config_test", "qq_qrcode_info", "qq_qrcode_png",
    }:
        return execute_read(request)
    return execute_write(request)


__all__ = ["execute", "execute_read", "execute_write", "redact_output"]
