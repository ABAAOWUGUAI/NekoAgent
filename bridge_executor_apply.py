"""Apply provider-owned executor upstream settings to the fixed local proxy."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
import uuid
from pathlib import Path

from bridge_ops_actions import broker_write
from bridge_provider_secrets import resolve_provider_secret


def _runtime_env_path() -> Path:
    return Path(os.environ.get(
        "CODEX_EXECUTOR_UPSTREAM_ENV_FILE",
        "/var/lib/agent-bridge/executor/proxy.env",
    ))


def _runtime_service() -> str:
    return os.environ.get("CODEX_EXECUTOR_UPSTREAM_SERVICE", "codex-deepseek-proxy.service")


def _chat_completions_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def _upstream(conn: sqlite3.Connection, profile: dict) -> dict:
    row = conn.execute(
        """SELECT m.id AS model_id, m.model, m.max_output_tokens,
                  m.enabled AS model_enabled,
                  p.id AS provider_id, p.base_url, p.api_key, p.secret_ref,
                  p.secret_version, p.secret_rotated_at, p.transport,
                  p.enabled AS provider_enabled
           FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
           WHERE m.id=? AND p.id=?""",
        (profile.get("upstream_model_id"), profile.get("upstream_provider_id")),
    ).fetchone()
    if not row or not int(row["model_enabled"] or 0) or not int(row["provider_enabled"] or 0):
        raise RuntimeError("executor_upstream_model_unavailable")
    item = dict(row)
    if item.get("transport") != "openai_chat_completions":
        raise RuntimeError("executor_upstream_transport_unsupported")
    item["api_key"] = resolve_provider_secret(conn, item)
    if not item.get("api_key"):
        raise RuntimeError("executor_upstream_credential_missing")
    if not item.get("base_url") or not item.get("model"):
        raise RuntimeError("executor_upstream_incomplete")
    return item


def _write_runtime_env(upstream: dict) -> None:
    target = _runtime_env_path()
    if target.is_symlink():
        raise RuntimeError("executor_runtime_env_symlink_refused")
    target.parent.mkdir(parents=True, exist_ok=True)
    values = (str(upstream["api_key"]), str(upstream["model"]), str(upstream["base_url"]))
    if any("\n" in value or "\r" in value for value in values):
        raise RuntimeError("executor_upstream_value_invalid")
    content = "\n".join((
        f"DEEPSEEK_API_KEY={upstream['api_key']}",
        f"DEEPSEEK_MODEL={upstream['model']}",
        f"DEEPSEEK_URL={_chat_completions_url(upstream['base_url'])}",
        f"DEEPSEEK_MAX_TOKENS={max(1, min(int(upstream.get('max_output_tokens') or 900), 8192))}",
        "DEEPSEEK_DEBUG=0",
        "",
    ))
    temporary = target.with_name(f".{target.name}.apply-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _restart_and_verify(expected_model: str) -> None:
    service = _runtime_service().removesuffix(".service")
    broker_write("service_restart", service, idempotency_key=f"executor-apply-{uuid.uuid4().hex}")
    last_error = "executor_proxy_health_timeout"
    for _ in range(20):
        try:
            with urllib.request.urlopen("http://127.0.0.1:5000/healthz", timeout=2) as response:
                payload = json.load(response)
            if response.status == 200 and payload.get("ok") and payload.get("model") == expected_model:
                return
            last_error = "executor_proxy_model_mismatch"
        except Exception:
            last_error = "executor_proxy_health_unavailable"
        time.sleep(0.25)
    raise RuntimeError(last_error)


def apply_executor_profile(conn: sqlite3.Connection, provider_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM model_executor_profiles WHERE provider_id=?",
        (provider_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("executor_profile_missing")
    profile = dict(row)
    version = int(profile.get("config_version") or 0)
    try:
        upstream = _upstream(conn, profile)
        _write_runtime_env(upstream)
        _restart_and_verify(str(upstream["model"]))
    except Exception as exc:
        conn.execute(
            "UPDATE model_executor_profiles SET last_apply_status='failed',last_error=?,updated_at=datetime('now') WHERE provider_id=?",
            (str(exc)[:500], provider_id),
        )
        return {"provider_id": provider_id, "ok": False, "error": str(exc)}
    conn.execute(
        """UPDATE model_executor_profiles
           SET applied_version=config_version,last_apply_status='applied',last_error='',updated_at=datetime('now')
           WHERE provider_id=?""",
        (provider_id,),
    )
    return {"provider_id": provider_id, "ok": True, "applied_version": version}


def apply_profiles_for_dependency(
    conn: sqlite3.Connection, *, provider_id: str = "", model_id: str = "",
) -> list[dict]:
    clauses, params = [], []
    if provider_id:
        clauses.extend(("provider_id=?", "upstream_provider_id=?"))
        params.extend((provider_id, provider_id))
    if model_id:
        clauses.append("upstream_model_id=?")
        params.append(model_id)
    if not clauses:
        return []
    rows = conn.execute(
        "SELECT provider_id,upstream_provider_id,upstream_model_id FROM model_executor_profiles WHERE " + " OR ".join(clauses),
        tuple(params),
    ).fetchall()
    results = []
    for row in rows:
        dependency_changed = bool(
            (model_id and str(row["upstream_model_id"] or "") == model_id)
            or (
                provider_id
                and str(row["upstream_provider_id"] or "") == provider_id
                and str(row["provider_id"] or "") != provider_id
            )
        )
        if dependency_changed:
            conn.execute(
                """UPDATE model_executor_profiles
                   SET config_version=config_version+1,last_apply_status='pending',updated_at=datetime('now')
                   WHERE provider_id=?""",
                (row["provider_id"],),
            )
        results.append(apply_executor_profile(conn, str(row["provider_id"])))
    return results
