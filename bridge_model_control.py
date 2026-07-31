#!/usr/bin/env python3
"""Model transport, billing, capability, and migration contracts.

This module owns schema metadata and batch migration mechanics. Runtime calls
remain in ``bridge_model_registry`` so model metadata cannot silently become an
execution adapter.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bridge_sqlite_safety import backup_sqlite_database


TRANSPORTS = {
    "codex_cli_chatgpt",
    "openai_chat_completions",
    "azure_openai_chat_completions",
    "anthropic_messages",
    "google_gemini_generate_content",
    "codex_cli_custom_provider",
}
BILLING_SCOPES = {"chatgpt_subscription", "api_key", "local_proxy"}
RUNTIME_OWNERS = {"platform", "maibot", "astrbot"}
CONFIG_MODES = {"managed", "read_only"}
MODEL_CAPABILITIES = {"text", "tools", "vision", "embedding", "structured_output"}
ROLE_REQUIREMENTS = {
    "classifier": {"text"},
    "daily_chat": {"text"},
    "interaction_classifier": {"text"},
    "conversation_engagement": {"text"},
    "conversation_reply": {"text"},
    "vision_caption": {"text", "vision"},
    "work_planner": {"text"},
    "work_executor": {"text", "tools"},
}
EXECUTOR_TRANSPORTS = {"codex_cli_chatgpt", "codex_cli_custom_provider"}
TRANSPORT_BILLING_SCOPES = {
    "codex_cli_chatgpt": {"chatgpt_subscription"},
    "codex_cli_custom_provider": {"local_proxy"},
    "openai_chat_completions": {"api_key", "local_proxy"},
    "azure_openai_chat_completions": {"api_key"},
    "anthropic_messages": {"api_key"},
    "google_gemini_generate_content": {"api_key"},
}
MODEL_PROFILES = {"all_roles_to_selected_model"}
LEGACY_MODEL_KEYS = {
    "chat_provider",
    "chat_provider_preset",
    "chat_base_url",
    "chat_model",
    "chat_api_key",
    "clear_chat_api_key",
    "chat_max_tokens",
    "codex_model",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: tuple[tuple[str, str], ...]) -> None:
    existing = _columns(conn, table)
    for name, definition in definitions:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_model_contract_schema(conn: sqlite3.Connection) -> None:
    """Add v3 contract columns and backfill deterministic legacy semantics."""

    _add_columns(
        conn,
        "model_providers",
        (
            ("transport", "TEXT NOT NULL DEFAULT ''"),
            ("billing_scope", "TEXT NOT NULL DEFAULT ''"),
            ("runtime_owner", "TEXT NOT NULL DEFAULT 'platform'"),
            ("config_mode", "TEXT NOT NULL DEFAULT 'managed'"),
            ("trusted_for_executor", "INTEGER NOT NULL DEFAULT 0"),
        ),
    )
    _add_columns(
        conn,
        "model_catalog",
        (("capabilities_json", "TEXT NOT NULL DEFAULT '[]'"),),
    )
    conn.execute(
        """
        UPDATE model_providers
        SET transport = CASE
                WHEN id = 'codex-login' THEN 'codex_cli_chatgpt'
                WHEN id = 'deepseek-proxy' THEN 'codex_cli_custom_provider'
                WHEN kind = 'openai-compatible' THEN 'openai_chat_completions'
                ELSE 'codex_cli_custom_provider'
            END,
            billing_scope = CASE
                WHEN id = 'codex-login' THEN 'chatgpt_subscription'
                WHEN id = 'deepseek-proxy' THEN 'local_proxy'
                WHEN kind = 'openai-compatible' THEN 'api_key'
                ELSE 'local_proxy'
            END,
            runtime_owner = 'platform',
            config_mode = 'managed',
            trusted_for_executor = CASE WHEN id IN ('codex-login', 'deepseek-proxy') THEN 1 ELSE 0 END
        WHERE transport = '' OR billing_scope = ''
        """
    )
    rows = conn.execute("SELECT id, supports_tools, capabilities_json FROM model_catalog").fetchall()
    for row in rows:
        raw = str(row["capabilities_json"] or "[]")
        try:
            current = json.loads(raw)
        except (TypeError, ValueError):
            current = []
        if current:
            continue
        caps = {"text"}
        if int(row["supports_tools"] or 0):
            caps.add("tools")
        conn.execute(
            "UPDATE model_catalog SET capabilities_json = ? WHERE id = ?",
            (encode_capabilities(caps), row["id"]),
        )


def encode_capabilities(values: object) -> str:
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, ValueError):
            values = [part.strip() for part in values.split(",")]
    if not isinstance(values, (list, tuple, set)):
        values = []
    normalized = sorted({str(value).strip() for value in values if str(value).strip()})
    if not set(normalized).issubset(MODEL_CAPABILITIES):
        raise ValueError("invalid_model_capability")
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def capabilities_for_payload(payload: dict) -> str:
    raw = payload.get("capabilities")
    if raw is None:
        raw = ["text"] + (["tools"] if str(payload.get("supports_tools") or "").lower() in {
            "1", "true", "yes", "on", "enabled", "开启",
        } else [])
    encoded = encode_capabilities(raw)
    if "text" not in json.loads(encoded) and not ({"vision", "embedding"} & set(json.loads(encoded))):
        raise ValueError("model_capability_required")
    return encoded


def capabilities_from_row(row: sqlite3.Row | dict) -> list[str]:
    try:
        values = json.loads(str(dict(row).get("capabilities_json") or "[]"))
    except (TypeError, ValueError):
        values = []
    return sorted({str(value) for value in values if str(value) in MODEL_CAPABILITIES})


def normalize_provider_contract(payload: dict, *, provider_id: str, kind: str) -> dict:
    kind_defaults = {
        "codex": ("codex_cli_custom_provider", "local_proxy"),
        "openai": ("openai_chat_completions", "api_key"),
        "openai-compatible": ("openai_chat_completions", "api_key"),
        "openrouter": ("openai_chat_completions", "api_key"),
        "azure-openai": ("azure_openai_chat_completions", "api_key"),
        "anthropic": ("anthropic_messages", "api_key"),
        "gemini": ("google_gemini_generate_content", "api_key"),
        "ollama": ("openai_chat_completions", "local_proxy"),
        "lm-studio": ("openai_chat_completions", "local_proxy"),
    }
    default_transport, default_billing = kind_defaults.get(
        kind,
        ("openai_chat_completions", "api_key"),
    )
    defaults = {
        "transport": default_transport,
        "billing_scope": default_billing,
        "runtime_owner": "platform",
        "config_mode": "managed",
        "trusted_for_executor": False,
    }
    requested_transport = str(payload.get("transport") or defaults["transport"]).strip()
    requested_billing = str(payload.get("billing_scope") or defaults["billing_scope"]).strip()
    result = {
        "transport": requested_transport,
        "billing_scope": requested_billing,
        "runtime_owner": str(payload.get("runtime_owner") or defaults["runtime_owner"]).strip(),
        "config_mode": str(payload.get("config_mode") or defaults["config_mode"]).strip(),
        "trusted_for_executor": requested_transport == "codex_cli_chatgpt" or str(
            payload.get("trusted_for_executor") or ""
        ).strip().lower() in {"1", "true", "yes", "on"},
    }
    if result["transport"] not in TRANSPORTS:
        raise ValueError("invalid_provider_transport")
    if result["billing_scope"] not in BILLING_SCOPES:
        raise ValueError("invalid_provider_billing_scope")
    if result["runtime_owner"] != "platform" or result["config_mode"] != "managed":
        raise ValueError("runtime_owned_provider_read_only")
    if result["billing_scope"] not in TRANSPORT_BILLING_SCOPES[result["transport"]]:
        raise ValueError("provider_billing_transport_mismatch")
    return result


def role_model_compatible(role: str, model: sqlite3.Row | dict) -> tuple[bool, str]:
    if role not in ROLE_REQUIREMENTS:
        return False, "invalid_model_role"
    item = dict(model)
    caps = set(capabilities_from_row(item))
    if role == "work_executor":
        if not int(item.get("trusted_for_executor") or 0):
            return False, "untrusted_work_executor_provider"
        if item.get("transport") not in EXECUTOR_TRANSPORTS:
            return False, "work_executor_transport_not_supported"
    if not ROLE_REQUIREMENTS[role].issubset(caps):
        return False, "role_capability_mismatch"
    return True, "ok"


def contract_catalog() -> dict:
    return {
        "transports": sorted(TRANSPORTS),
        "billing_scopes": sorted(BILLING_SCOPES),
        "runtime_owners": sorted(RUNTIME_OWNERS),
        "config_modes": sorted(CONFIG_MODES),
        "capabilities": sorted(MODEL_CAPABILITIES),
        "role_requirements": {key: sorted(value) for key, value in ROLE_REQUIREMENTS.items()},
        "profiles": sorted(MODEL_PROFILES),
    }


def legacy_mirror(conn: sqlite3.Connection, role: str = "conversation_reply") -> dict:
    row = conn.execute(
        """
        SELECT m.model, m.max_output_tokens, p.id AS provider_id, p.name AS provider_name,
               p.kind, p.transport, p.base_url
        FROM model_role_bindings b
        JOIN model_catalog m ON m.id = b.primary_model_id
        JOIN model_providers p ON p.id = m.provider_id
        WHERE b.role = ?
        """,
        (role,),
    ).fetchone()
    if not row:
        return {}
    item = dict(row)
    if item["transport"] in {"codex_cli_chatgpt", "codex_cli_custom_provider"}:
        return {
            "chat_provider": "codex",
            "chat_provider_preset": "codex",
            "chat_base_url": "",
            "chat_model": "",
            "codex_model": item.get("model") or "",
            "chat_max_tokens": str(item.get("max_output_tokens") or 900),
        }
    return {
        "chat_provider": "openai-compatible",
        "chat_provider_preset": item.get("provider_id") or "custom",
        "chat_base_url": item.get("base_url") or "",
        "chat_model": item.get("model") or "",
        "codex_model": "",
        "chat_max_tokens": str(item.get("max_output_tokens") or 900),
    }


def sync_legacy_mirror(conn: sqlite3.Connection) -> dict:
    mirror = legacy_mirror(conn)
    if not mirror or "settings" not in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        return mirror
    now = utc_now()
    for key, value in mirror.items():
        conn.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, str(value), now),
        )
    return mirror


def validate_legacy_model_write(conn: sqlite3.Connection, payload: dict) -> None:
    requested = LEGACY_MODEL_KEYS & set(payload)
    if not requested:
        return
    mirror = legacy_mirror(conn)
    if "chat_api_key" in requested and str(payload.get("chat_api_key") or "").strip():
        raise ValueError("legacy_model_settings_read_only")
    if "clear_chat_api_key" in requested and str(payload.get("clear_chat_api_key") or "").lower() in {
        "1", "true", "yes", "on",
    }:
        raise ValueError("legacy_model_settings_read_only")
    for key in requested - {"chat_api_key", "clear_chat_api_key"}:
        if key in mirror and str(payload.get(key) or "").strip() != str(mirror[key]).strip():
            raise ValueError("legacy_model_settings_read_only")


def _migration_state(conn: sqlite3.Connection, profile: str, target_model_id: str) -> dict:
    if profile not in MODEL_PROFILES:
        raise ValueError("unknown_model_profile")
    target_model_id = str(target_model_id or "").strip()
    if not target_model_id:
        raise ValueError("profile_target_required")
    rows = {row["role"]: dict(row) for row in conn.execute("SELECT * FROM model_role_bindings").fetchall()}
    # Bulk text-model migration must never silently bind the opt-in vision
    # route to a text-only model.
    target = {
        role: {"primary_model_id": target_model_id, "fallback_model_id": ""}
        for role in ROLE_REQUIREMENTS
        if role != "vision_caption"
    }
    return {"profile": profile, "target_model_id": target_model_id, "current": rows, "target": target}


def migration_preview(
    conn: sqlite3.Connection,
    profile: str = "all_roles_to_selected_model",
    target_model_id: str = "",
) -> dict:
    state = _migration_state(conn, profile, target_model_id)
    target_ids = sorted({item["primary_model_id"] for item in state["target"].values()})
    placeholders = ",".join("?" for _ in target_ids)
    models = {
        row["id"]: dict(row)
        for row in conn.execute(
            f"""SELECT m.*, p.transport, p.billing_scope, p.runtime_owner,
                       p.trusted_for_executor, p.enabled AS provider_enabled
                FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
                WHERE m.id IN ({placeholders})""",
            tuple(target_ids),
        ).fetchall()
    }
    changes = []
    for role, target in state["target"].items():
        model = models.get(target["primary_model_id"])
        if not model or not int(model.get("enabled") or 0) or not int(model.get("provider_enabled") or 0):
            raise ValueError("profile_model_unavailable")
        compatible, reason = role_model_compatible(role, model)
        if not compatible:
            raise ValueError(reason)
        current = state["current"].get(role, {"primary_model_id": "", "fallback_model_id": ""})
        changes.append(
            {
                "role": role,
                "from": {
                    "primary_model_id": current.get("primary_model_id") or "",
                    "fallback_model_id": current.get("fallback_model_id") or "",
                },
                "to": dict(target),
                "transport": model["transport"],
                "billing_scope": model["billing_scope"],
                "capabilities": capabilities_from_row(model),
                "changed": any((current.get(key) or "") != value for key, value in target.items()),
            }
        )
    fingerprint_source = json.dumps(
        {"profile": profile, "target_model_id": target_model_id, "changes": changes},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "profile": profile,
        "target_model_id": target_model_id,
        "changes": changes,
        "tests_required": target_ids,
        "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
    }


def apply_migration_profile(conn: sqlite3.Connection, payload: dict) -> dict:
    profile = str(payload.get("profile") or "all_roles_to_selected_model").strip()
    target_model_id = str(payload.get("target_model_id") or payload.get("model_id") or "").strip()
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        preview = migration_preview(conn, profile, target_model_id)
        if str(payload.get("fingerprint") or "") != preview["fingerprint"]:
            raise ValueError("stale_migration_preview")
        for change in preview["changes"]:
            if not change["changed"]:
                continue
            role = change["role"]
            conn.execute(
                """INSERT INTO model_role_bindings(role, primary_model_id, fallback_model_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(role) DO UPDATE SET primary_model_id=excluded.primary_model_id,
                     fallback_model_id=excluded.fallback_model_id, updated_at=excluded.updated_at""",
                (role, change["to"]["primary_model_id"], change["to"]["fallback_model_id"], now),
            )
            conn.execute(
                """INSERT INTO role_binding_change_log(
                     id, role, old_primary_model_id, new_primary_model_id,
                     old_fallback_model_id, new_fallback_model_id, changed_by, client_ip, created_at
                   ) VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, 'profile_migration', '', ?)""",
                (
                    role,
                    change["from"]["primary_model_id"], change["to"]["primary_model_id"],
                    change["from"]["fallback_model_id"], change["to"]["fallback_model_id"], now,
                ),
            )
        sync_legacy_mirror(conn)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {**preview, "applied": True, "applied_at": now}
