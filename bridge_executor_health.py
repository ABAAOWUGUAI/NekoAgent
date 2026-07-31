"""Adapter-aware, secret-free executor readiness probe."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

from bridge_executor_profiles import executor_runtime_status

def _run(args: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        completed = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    except Exception:
        return False, "executor_probe_failed"
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, output[-400:]


def _route(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT b.primary_model_id,m.model,p.id AS provider_id,p.transport,
               p.enabled AS provider_enabled,m.enabled AS model_enabled,
               ep.profile_name,ep.credential_source,ep.upstream_provider_id,
               ep.upstream_model_id,ep.enabled AS profile_enabled,
               ep.applied_version,ep.config_version,ep.last_apply_status,
               upstream.model AS upstream_model
        FROM model_role_bindings b
        LEFT JOIN model_catalog m ON m.id=b.primary_model_id
        LEFT JOIN model_providers p ON p.id=m.provider_id
        LEFT JOIN model_executor_profiles ep ON ep.provider_id=p.id
        LEFT JOIN model_catalog upstream ON upstream.id=ep.upstream_model_id
        WHERE b.role='work_executor'
        LIMIT 1
        """,
    ).fetchone()
    return dict(row) if row else {}


def probe_executor(conn: sqlite3.Connection, *, codex_path: str = "codex") -> dict:
    """Check the configured adapter without making a paid model request."""

    route = _route(conn)
    transport = str(route.get("transport") or "")
    route_ok = bool(
        route.get("primary_model_id")
        and int(route.get("provider_enabled") or 0)
        and int(route.get("model_enabled") or 0)
    )
    cli_ok, cli_output = _run([codex_path, "--version"])
    result = {
        "ok": False,
        "adapter": transport or "unconfigured",
        "route_present": route_ok,
        "cli_ok": cli_ok,
        "auth_required": transport == "codex_cli_chatgpt",
        "profile_applied": False,
        "proxy_ok": False,
        "model_match": False,
    }
    if not route_ok or not cli_ok:
        result["error"] = "executor_route_unavailable" if not route_ok else "codex_cli_unavailable"
        return result
    if transport == "codex_cli_chatgpt":
        login_ok, login_output = _run([codex_path, "login", "status"])
        lowered = login_output.lower()
        result["login_ok"] = login_ok and any(token in lowered for token in ("logged in", "authenticated", "chatgpt"))
        result["ok"] = bool(result["login_ok"])
        if not result["ok"]:
            result["error"] = "codex_login_required"
        return result
    if transport != "codex_cli_custom_provider":
        result["error"] = "executor_adapter_unsupported"
        return result
    result["profile_applied"] = bool(
        int(route.get("profile_enabled") or 0)
        and int(route.get("applied_version") or 0) == int(route.get("config_version") or -1)
        and str(route.get("last_apply_status") or "") == "applied"
    )
    runtime = executor_runtime_status({**route, "enabled": route.get("profile_enabled")})
    result.update({
        "profile_file_ok": bool(runtime.get("profile_available")),
        "credential_ok": bool(runtime.get("credential_available")),
        "sandbox_ok": bool(runtime.get("sandbox_available")),
        "workspace_ok": bool(runtime.get("workspace_available")),
    })
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/healthz", timeout=3) as response:
            payload = json.load(response)
        result["proxy_ok"] = response.status == 200 and bool(payload.get("ok"))
        expected_model = str(route.get("upstream_model") or "")
        result["model_match"] = bool(expected_model and payload.get("model") == expected_model)
    except Exception:
        result["error"] = "executor_proxy_unavailable"
        return result
    result["ok"] = bool(
        result["profile_applied"]
        and runtime.get("ready")
        and result["proxy_ok"]
        and result["model_match"]
    )
    if not result["ok"]:
        result["error"] = "executor_profile_or_model_mismatch"
    return result


def probe_executor_path(path: Path | str) -> dict:
    try:
        with sqlite3.connect(str(path), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            return probe_executor(conn)
    except Exception:
        return {"ok": False, "adapter": "unknown", "error": "executor_registry_unavailable"}


__all__ = ["probe_executor", "probe_executor_path"]
