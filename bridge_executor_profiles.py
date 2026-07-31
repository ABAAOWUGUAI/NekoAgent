#!/usr/bin/env python3
"""Versioned, provider-owned Codex executor profile configuration.

Product configuration (which Codex profile a connection uses) belongs in the
Assistant database. Host security policy (where profiles, credentials and
workspaces live) remains deployment-owned and is intentionally not writable
from the Web UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bridge_migrations import MigrationDriftError


EXECUTOR_PROFILE_COLUMNS = (
    "provider_id",
    "adapter_type",
    "profile_name",
    "credential_source",
    "upstream_provider_id",
    "upstream_model_id",
    "enabled",
    "config_version",
    "applied_version",
    "last_apply_status",
    "last_error",
    "created_at",
    "updated_at",
)
EXECUTOR_ADAPTER_TYPES = {"codex_cli_profile"}
EXECUTOR_CREDENTIAL_SOURCES = {"proxy_access_key"}
PROFILE_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


def _contract_payload() -> str:
    return json.dumps(
        {
            "table": "model_executor_profiles",
            "columns": list(EXECUTOR_PROFILE_COLUMNS),
            "adapter_types": sorted(EXECUTOR_ADAPTER_TYPES),
            "credential_sources": sorted(EXECUTOR_CREDENTIAL_SOURCES),
            "host_policy": [
                "CODEX_EXECUTOR_PROFILE_DIR",
                "CODEX_PROXY_ACCESS_KEY_FILE",
                "CODEX_EXECUTOR_WORKSPACE_ROOT",
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


EXECUTOR_PROFILE_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "开启"}


def validate_profile_name(value: object) -> str:
    profile_name = str(value or "").strip()
    if not PROFILE_NAME_PATTERN.fullmatch(profile_name):
        raise ValueError("invalid_executor_profile_name")
    return profile_name


def ensure_executor_profile_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_executor_profiles (
            provider_id TEXT PRIMARY KEY,
            adapter_type TEXT NOT NULL DEFAULT 'codex_cli_profile',
            profile_name TEXT NOT NULL,
            credential_source TEXT NOT NULL DEFAULT 'proxy_access_key',
            upstream_provider_id TEXT NOT NULL DEFAULT '',
            upstream_model_id TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            config_version INTEGER NOT NULL DEFAULT 1 CHECK(config_version > 0),
            applied_version INTEGER NOT NULL DEFAULT 0 CHECK(applied_version >= 0),
            last_apply_status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(provider_id) REFERENCES model_providers(id) ON DELETE RESTRICT
        )
        """,
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_executor_profile_name ON model_executor_profiles(profile_name)",
    )


def apply_executor_profiles_v1(conn: sqlite3.Connection) -> None:
    ensure_executor_profile_schema(conn)
    now = utc_now()
    provider_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_providers'",
    ).fetchone()
    if not provider_table:
        return
    legacy = conn.execute(
        "SELECT id FROM model_providers WHERE id='deepseek-proxy' AND transport='codex_cli_custom_provider'",
    ).fetchone()
    if legacy:
        conn.execute(
            """
            INSERT OR IGNORE INTO model_executor_profiles(
                provider_id,adapter_type,profile_name,credential_source,enabled,
                upstream_provider_id,upstream_model_id,config_version,applied_version,
                last_apply_status,last_error,created_at,updated_at
            ) SELECT ?, 'codex_cli_profile', ?, 'proxy_access_key', 1,
                COALESCE(p.id,''), COALESCE(m.id,''), 1, 0, 'pending', '', ?, ?
              FROM (SELECT 1) seed
              LEFT JOIN model_role_bindings b ON b.role='conversation_reply'
              LEFT JOIN model_catalog m ON m.id=b.primary_model_id
              LEFT JOIN model_providers p ON p.id=m.provider_id
            """,
            ("deepseek-proxy", "deepseek-proxy", now, now),
        )


def require_executor_profile_schema(conn: sqlite3.Connection) -> dict:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_executor_profiles'",
    ).fetchone()
    if not table:
        raise MigrationDriftError("executor_profile_schema_drift:table")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_executor_profiles)")}
    missing = sorted(set(EXECUTOR_PROFILE_COLUMNS) - columns)
    if missing:
        raise MigrationDriftError("executor_profile_schema_drift:" + ",".join(missing))
    return {"ok": True, "columns": list(EXECUTOR_PROFILE_COLUMNS)}


def upsert_executor_profile(conn: sqlite3.Connection, provider_id: str, payload: dict) -> dict | None:
    transport = str(payload.get("transport") or "").strip()
    existing = conn.execute(
        "SELECT * FROM model_executor_profiles WHERE provider_id=?",
        (provider_id,),
    ).fetchone()
    if transport != "codex_cli_custom_provider":
        if existing:
            conn.execute("DELETE FROM model_executor_profiles WHERE provider_id=?", (provider_id,))
        return None

    profile_name = validate_profile_name(
        payload.get("executor_profile_name")
        or (dict(existing).get("profile_name") if existing else "")
        or provider_id,
    )
    adapter_type = str(payload.get("executor_adapter_type") or "codex_cli_profile").strip()
    credential_source = str(payload.get("executor_credential_source") or "proxy_access_key").strip()
    if adapter_type not in EXECUTOR_ADAPTER_TYPES:
        raise ValueError("invalid_executor_adapter_type")
    if credential_source not in EXECUTOR_CREDENTIAL_SOURCES:
        raise ValueError("invalid_executor_credential_source")
    enabled = 1 if truthy(payload.get("executor_enabled", payload.get("enabled", "1"))) else 0
    upstream_provider_id = str(
        payload.get("executor_upstream_provider_id")
        or (dict(existing).get("upstream_provider_id") if existing else "")
        or ""
    ).strip()
    upstream_model_id = str(
        payload.get("executor_upstream_model_id")
        or (dict(existing).get("upstream_model_id") if existing else "")
        or ""
    ).strip()
    if upstream_provider_id or upstream_model_id:
        upstream = conn.execute(
            """SELECT m.id, m.provider_id, m.enabled, p.transport, p.enabled AS provider_enabled
               FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
               WHERE m.id=? AND p.id=?""",
            (upstream_model_id, upstream_provider_id),
        ).fetchone()
        if not upstream or not int(upstream["enabled"] or 0) or not int(upstream["provider_enabled"] or 0):
            raise ValueError("executor_upstream_model_unavailable")
        if str(upstream["transport"] or "") not in {"openai_chat_completions"}:
            raise ValueError("executor_upstream_transport_unsupported")
    else:
        raise ValueError("executor_upstream_required")
    changed = not existing or any(
        (
            str(dict(existing).get("adapter_type") or "") != adapter_type,
            str(dict(existing).get("profile_name") or "") != profile_name,
            str(dict(existing).get("credential_source") or "") != credential_source,
            int(dict(existing).get("enabled") or 0) != enabled,
            str(dict(existing).get("upstream_provider_id") or "") != upstream_provider_id,
            str(dict(existing).get("upstream_model_id") or "") != upstream_model_id,
        ),
    )
    version = int(dict(existing).get("config_version") or 0) + (1 if changed else 0) if existing else 1
    now = utc_now()
    conn.execute(
        """
        INSERT INTO model_executor_profiles(
            provider_id,adapter_type,profile_name,credential_source,upstream_provider_id,
            upstream_model_id,enabled,
            config_version,applied_version,last_apply_status,last_error,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'pending','',?,?)
        ON CONFLICT(provider_id) DO UPDATE SET
            adapter_type=excluded.adapter_type,
            profile_name=excluded.profile_name,
            credential_source=excluded.credential_source,
            upstream_provider_id=excluded.upstream_provider_id,
            upstream_model_id=excluded.upstream_model_id,
            enabled=excluded.enabled,
            config_version=excluded.config_version,
            applied_version=CASE WHEN excluded.config_version=model_executor_profiles.config_version
                                 THEN model_executor_profiles.applied_version ELSE model_executor_profiles.applied_version END,
            last_apply_status=CASE WHEN excluded.config_version=model_executor_profiles.config_version
                                   THEN model_executor_profiles.last_apply_status ELSE 'pending' END,
            last_error='',
            updated_at=excluded.updated_at
        """,
        (
            provider_id, adapter_type, profile_name, credential_source,
            upstream_provider_id, upstream_model_id, enabled, version,
            int(dict(existing).get("applied_version") or 0) if existing else 0, now, now,
        ),
    )
    return dict(conn.execute(
        "SELECT * FROM model_executor_profiles WHERE provider_id=?",
        (provider_id,),
    ).fetchone())


def get_executor_profile(conn: sqlite3.Connection, provider_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM model_executor_profiles WHERE provider_id=?",
        (str(provider_id or "").strip(),),
    ).fetchone()
    return dict(row) if row else None


def executor_upstream_summary(conn: sqlite3.Connection, profile: dict | None) -> dict:
    if not profile:
        return {}
    row = conn.execute(
        """SELECT p.name AS provider_name, p.transport, p.enabled AS provider_enabled,
                  m.label AS model_label, m.model, m.enabled AS model_enabled
           FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
           WHERE p.id=? AND m.id=?""",
        (profile.get("upstream_provider_id"), profile.get("upstream_model_id")),
    ).fetchone()
    return dict(row) if row else {}


def executor_profile_path(profile_name: str) -> Path:
    safe_name = validate_profile_name(profile_name)
    root = Path(os.environ.get(
        "CODEX_EXECUTOR_PROFILE_DIR",
        "/var/lib/agent-bridge/codex-profiles",
    ))
    return root / f"{safe_name}.config.toml"


def executor_workspace_root() -> Path:
    return Path(os.environ.get("CODEX_EXECUTOR_WORKSPACE_ROOT", "/opt/agent-workspace"))


def executor_credential_path(source: str) -> Path:
    if source != "proxy_access_key":
        raise ValueError("invalid_executor_credential_source")
    return Path(os.environ.get("CODEX_PROXY_ACCESS_KEY_FILE", "/etc/codex-proxy/access.key"))


def read_executor_credential(source: str) -> str:
    try:
        raw = executor_credential_path(source).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("CODEX_PROXY_ACCESS_KEY="):
            return line.split("=", 1)[1].strip()
    return raw


def profile_sha256(profile_name: str) -> str:
    try:
        return hashlib.sha256(executor_profile_path(profile_name).read_bytes()).hexdigest()
    except OSError:
        return ""


def executor_runtime_status(profile: dict | None) -> dict:
    if not profile:
        return {
            "configured": False,
            "ready": False,
            "profile_available": False,
            "credential_available": False,
            "sandbox_available": bool(shutil.which("bwrap")),
            "workspace_available": executor_workspace_root().is_dir(),
            "error": "executor_profile_not_configured",
        }
    profile_available = bool(profile_sha256(str(profile.get("profile_name") or "")))
    credential_available = bool(read_executor_credential(str(profile.get("credential_source") or "")))
    sandbox_available = bool(shutil.which("bwrap"))
    workspace_available = executor_workspace_root().is_dir()
    enabled = bool(int(profile.get("enabled") or 0))
    errors = []
    if not enabled:
        errors.append("executor_profile_disabled")
    if not profile_available:
        errors.append("executor_profile_missing")
    if not credential_available:
        errors.append("executor_credential_missing")
    if not sandbox_available:
        errors.append("executor_sandbox_unavailable")
    if not workspace_available:
        errors.append("executor_workspace_missing")
    if not profile.get("upstream_provider_id") or not profile.get("upstream_model_id"):
        errors.append("executor_upstream_required")
    if str(profile.get("last_apply_status") or "").strip() != "applied":
        errors.append("executor_runtime_not_applied")
    if int(profile.get("applied_version") or 0) != int(profile.get("config_version") or 0):
        if "executor_runtime_not_applied" not in errors:
            errors.append("executor_runtime_not_applied")
    return {
        "configured": True,
        "ready": not errors,
        "profile_available": profile_available,
        "credential_available": credential_available,
        "sandbox_available": sandbox_available,
        "workspace_available": workspace_available,
        "config_version": int(profile.get("config_version") or 0),
        "applied_version": int(profile.get("applied_version") or 0),
        "error": errors[0] if errors else "",
        "errors": errors,
    }


def public_executor_profile(profile: dict | None) -> dict | None:
    if not profile:
        return None
    item = {
        key: profile.get(key)
        for key in (
            "provider_id", "adapter_type", "profile_name", "enabled",
            "upstream_provider_id", "upstream_model_id",
            "config_version", "applied_version", "last_apply_status",
            "last_error", "updated_at",
        )
    }
    item["credential_source"] = "受保护的代理凭证"
    item["runtime"] = executor_runtime_status(profile)
    return item
