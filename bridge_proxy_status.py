#!/usr/bin/env python3
"""Proxy status detection — TCP reachability + /healthz + upstream probe."""

import json
import os
import subprocess
import socket
import time
import urllib.error
import urllib.request
import shutil
from datetime import datetime, timezone
from bridge_executor_profiles import executor_workspace_root, read_executor_credential


PROXY_HOST = "127.0.0.1"
PROXY_PORT = 5000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proxy_access_key() -> str:
    """Read proxy access key from the system environment file."""
    return read_executor_credential("proxy_access_key")


def _tcp_check() -> dict:
    started = time.monotonic()
    try:
        with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=3):
            return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _healthz_check() -> dict:
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            f"http://{PROXY_HOST}:{PROXY_PORT}/healthz",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return {
                "ok": data.get("ok", False),
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "model": data.get("model", ""),
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _upstream_check() -> dict:
    """Send a minimal upstream request to verify DeepSeek connectivity."""
    started = time.monotonic()
    access_key = _proxy_access_key()
    try:
        # Use the actual model name from config, not __probe__ which gets rejected by whitelist
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
        data = json.dumps({
            "model": model,
            "input": [{"role": "user", "content": "ping"}],
        }).encode()
        headers = {"Content-Type": "application/json"}
        if access_key:
            headers["Authorization"] = f"Bearer {access_key}"
        req = urllib.request.Request(
            f"http://{PROXY_HOST}:{PROXY_PORT}/v1/responses",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {exc.code}: {body}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def proxy_status() -> dict:
    """Return proxy composite status (no API cost)."""
    tcp = _tcp_check()
    healthz = {}
    if tcp["ok"]:
        healthz = _healthz_check()
    return {
        "ok": tcp["ok"] and healthz.get("ok", False),
        "tcp": tcp,
        "healthz": healthz,
        "probed_at": utc_now(),
    }


def proxy_full_probe() -> dict:
    """Return full probe result (upstream included, incurs API cost)."""
    status = proxy_status()
    upstream = _upstream_check()
    status["upstream"] = upstream
    status["ok"] = status["ok"] and upstream["ok"]
    return status


def proxy_executor_test(timeout: int = 60, executor: dict | None = None) -> dict:
    """Run a minimal read-only Codex CLI request through the custom provider."""

    started = time.monotonic()
    executor = dict(executor or {})
    target = {
        "provider_id": str(executor.get("provider_id") or "deepseek-proxy"),
        "model_id": str(executor.get("model_id") or ""),
        "model": str(executor.get("model_name") or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")),
        "adapter": str(executor.get("adapter") or "codex_custom_provider"),
        "profile_name": str(executor.get("profile_name") or ""),
    }
    if executor and target["adapter"] not in {"codex_custom_provider", "deepseek_proxy"}:
        return {
            "ok": False, "error": "work_executor_not_custom_codex_provider", "latency_ms": 0,
            "tested_target": target, "binding_matches": False,
        }
    if not shutil.which("bwrap"):
        return {
            "ok": False, "error": "executor_sandbox_unavailable", "latency_ms": 0,
            "tested_target": target, "binding_matches": True,
        }
    if not target["profile_name"]:
        return {
            "ok": False, "error": "executor_profile_missing", "latency_ms": 0,
            "tested_target": target, "binding_matches": True,
        }
    access_key = read_executor_credential(str(executor.get("credential_source") or "proxy_access_key"))
    if not access_key:
        return {
            "ok": False, "error": "proxy_access_key_missing", "latency_ms": 0,
            "tested_target": target, "binding_matches": True,
        }
    env = os.environ.copy()
    for key in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION",
        "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
    ):
        env.pop(key, None)
    env["CODEX_PROXY_ACCESS_KEY"] = access_key
    args = [
        "codex", "exec", "--skip-git-repo-check",
        "--profile", target["profile_name"], "--json", "--ephemeral",
        "--model", target["model"],
        "--sandbox", "read-only",
    ]
    try:
        completed = subprocess.run(
            args,
            input="Reply with exactly PROXY_EXEC_OK. Do not call tools.",
            text=True,
            capture_output=True,
            cwd=str(executor_workspace_root()),
            env=env,
            timeout=max(10, min(int(timeout), 120)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "executor_test_timeout",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "tested_target": target, "binding_matches": True,
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": str(exc)[:200],
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "tested_target": target, "binding_matches": True,
        }
    output = (completed.stdout or "")
    ok = completed.returncode == 0 and "PROXY_EXEC_OK" in output
    error = "" if ok else (completed.stderr or "executor_test_failed")[:300]
    return {
        "ok": ok,
        "returncode": completed.returncode,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "error": error,
        "tested_target": target,
        "binding_matches": True,
    }
