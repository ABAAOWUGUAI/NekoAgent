#!/usr/bin/env python3
"""Provider, model, and runtime-role registry.

One OpenAI-compatible setting is not a model management system.  This module
separates credentials (providers), model capabilities (catalog entries), and
the role each model plays in the assistant runtime.

v2: Added TRUSTED_EXECUTOR_PROVIDER_IDS whitelist for work_executor,
    bind_model_role now enforces provider whitelist + disabled fallback for executor,
    runtime_settings_for_role fails-closed on missing work_executor binding,
    removed auto-supports_tools for codex kind providers.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

from bridge_model_control import (
    capabilities_for_payload,
    capabilities_from_row,
    ensure_model_contract_schema,
    normalize_provider_contract,
    role_model_compatible,
    sync_legacy_mirror,
)
from bridge_provider_secrets import ProviderSecretStore, ensure_provider_secret_columns, prepare_provider_secret_update, provider_secret_public, resolve_provider_secret
from bridge_executor_profiles import (
    ensure_executor_profile_schema,
    executor_model_display_label,
    executor_runtime_status,
    executor_upstream_summary,
    get_executor_profile,
    public_executor_profile,
    upsert_executor_profile,
)
from bridge_executor_verification import (
    executor_eligibility_state,
    work_executor_activation_guard,
    work_executor_bind_guard,
)


MODEL_ROLES = {
    "classifier": {"label": "旧版综合判断", "description": "兼容旧绑定；新链路使用场景化判断。", "visible": False},
    "daily_chat": {"label": "旧版日常聊天", "description": "兼容旧绑定；新链路使用对话回复。", "visible": False},
    "interaction_classifier": {
        "label": "交互意图判断",
        "description": "只判断日常/工作/混合模式和交互计划，不决定是否回应。",
    },
    "conversation_engagement": {
        "label": "群参与判断",
        "description": "只判断自然群场景是否值得插话；私聊和明确指向不调用。",
    },
    "conversation_reply": {
        "label": "对话回复",
        "description": "负责私聊与群聊中的自然回复和情绪表达。",
    },
    "vision_caption": {"label": "识图理解", "description": "只负责把图片转换为受控的视觉描述，不替代对话回复模型。"},
    "work_planner": {"label": "工作规划",
                     "description": "负责工作请求的澄清、方案和即时分析，不直接执行服务器工具。"},
    "work_executor": {"label": "工作执行",
                      "description": "负责代码、终端和文件操作。支持原生 Codex 和 DeepSeek 代理两种执行器。"},
}
PROVIDER_KINDS = {
    "codex",
    "openai",
    "openai-compatible",
    "openrouter",
    "anthropic",
    "gemini",
    "azure-openai",
    "ollama",
    "lm-studio",
}
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "开启"}


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _slug(value: object, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    if not text:
        text = fallback
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", text):
        raise ValueError("invalid_registry_id")
    return text


def _mask_secret(value: object) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 8:
        return raw[:2] + "***" if raw else ""
    return raw[:4] + "***" + raw[-4:]


def _public_base_url(value: object) -> str:
    """Return an endpoint safe for the browser; never project URL credentials."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.hostname:
            return ""
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))
    except ValueError:
        return ""


def _public_provider(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> dict:
    item = provider_secret_public(conn, row, _mask_secret)
    item["base_url"] = _public_base_url(item.get("base_url"))
    return item


def ensure_model_registry_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            base_url TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            timeout_seconds INTEGER NOT NULL DEFAULT 60,
            last_test_status TEXT NOT NULL DEFAULT '',
            last_test_latency REAL,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_catalog (
            id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL,
            label TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            context_window INTEGER NOT NULL DEFAULT 0,
            max_output_tokens INTEGER NOT NULL DEFAULT 900,
            supports_tools INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(provider_id) REFERENCES model_providers(id)
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_role_bindings (
            role TEXT PRIMARY KEY,
            primary_model_id TEXT NOT NULL DEFAULT '',
            fallback_model_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_catalog_provider ON model_catalog(provider_id, enabled)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS model_registry_meta (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL DEFAULT '',
               updated_at TEXT NOT NULL
           )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(model_catalog)").fetchall()}
    for name, definition in (
        ("input_price_per_million", "REAL"),
        ("output_price_per_million", "REAL"),
        ("price_currency", "TEXT NOT NULL DEFAULT 'USD'"),
        ("price_source", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE model_catalog ADD COLUMN {name} {definition}")
    ensure_model_contract_schema(conn)
    ensure_provider_secret_columns(conn)
    ensure_executor_profile_schema(conn)

    # 审计日志表
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS role_binding_change_log (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            old_primary_model_id TEXT NOT NULL DEFAULT '',
            new_primary_model_id TEXT NOT NULL DEFAULT '',
            old_fallback_model_id TEXT NOT NULL DEFAULT '',
            new_fallback_model_id TEXT NOT NULL DEFAULT '',
            changed_by TEXT NOT NULL DEFAULT 'admin',
            client_ip TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )

    # 代理探测日志表
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_probe_log (
            id TEXT PRIMARY KEY,
            probe_type TEXT NOT NULL,
            executor_id TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            latency_ms REAL,
            error_message TEXT NOT NULL DEFAULT '',
            triggered_by TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_probe_created ON proxy_probe_log(created_at DESC)")


def _legacy_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha1(value.encode('utf-8', 'ignore')).hexdigest()[:8]}"


def seed_model_registry(conn: sqlite3.Connection, settings: dict, *, force_chat_bindings: bool = False) -> None:
    now = utc_now()
    initialized = conn.execute(
        "SELECT value FROM model_registry_meta WHERE key = 'legacy_seed_complete'"
    ).fetchone()
    provider_count = int(conn.execute("SELECT COUNT(*) FROM model_providers").fetchone()[0])
    if initialized or provider_count:
        conn.execute(
            """INSERT INTO model_registry_meta(key, value, updated_at) VALUES ('legacy_seed_complete', '1', ?)
               ON CONFLICT(key) DO NOTHING""",
            (now,),
        )
        ensure_model_contract_schema(conn)
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO model_providers(
            id, name, kind, base_url, api_key, enabled, timeout_seconds,
            last_test_status, last_test_latency, last_error, created_at, updated_at
        ) VALUES ('codex-login', 'Codex ChatGPT 登录态', 'codex', '', '', 1, 180, '', NULL, '', ?, ?)
        """,
        (now, now),
    )
    codex_model = _clip(settings.get("codex_model"), 160)
    conn.execute(
        """
        INSERT OR IGNORE INTO model_catalog(
            id, provider_id, label, model, enabled, context_window,
            max_output_tokens, supports_tools, notes, created_at, updated_at
        ) VALUES ('codex-default', 'codex-login', 'Codex 默认执行器', ?, 1, 0, 0, 1,
            '使用服务器 Codex CLI 的 ChatGPT 登录态，负责工具和文件操作。', ?, ?)
        """,
        (codex_model, now, now),
    )
    if codex_model:
        conn.execute(
            "UPDATE model_catalog SET model = ?, updated_at = ? WHERE id = 'codex-default' AND model = ''",
            (codex_model, now),
        )

    active_chat_model = "codex-default"
    provider_kind = str(settings.get("chat_provider") or "codex").strip()
    base_url = _clip(settings.get("chat_base_url"), 1000)
    model_name = _clip(settings.get("chat_model"), 200)
    api_key = str(settings.get("chat_api_key") or "").strip()
    if provider_kind == "openai-compatible" and base_url and model_name:
        provider_id = _legacy_id("chat", base_url)
        model_id = _legacy_id(provider_id, model_name)
        display_name = _clip(settings.get("chat_provider_preset"), 80) or "OpenAI-compatible"
        upsert_provider(
            conn,
            {
                "id": provider_id,
                "name": display_name,
                "kind": "openai-compatible",
                "base_url": base_url,
                "api_key": api_key,
                "enabled": True,
                "timeout_seconds": 60,
            },
        )
        try:
            max_tokens = max(64, min(int(float(settings.get("chat_max_tokens") or 900)), 32768))
        except (TypeError, ValueError):
            max_tokens = 900
        conn.execute(
            """
            INSERT INTO model_catalog(
                id, provider_id, label, model, enabled, context_window,
                max_output_tokens, supports_tools, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 0, ?, 0, '由旧版聊天 Provider 配置迁移。', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                provider_id = excluded.provider_id,
                label = excluded.label,
                model = excluded.model,
                max_output_tokens = excluded.max_output_tokens,
                updated_at = excluded.updated_at
            """,
            (model_id, provider_id, model_name, model_name, max_tokens, now, now),
        )
        active_chat_model = model_id

    for role in MODEL_ROLES:
        if role == "vision_caption":
            continue
        model_id = "codex-default" if role == "work_executor" else active_chat_model
        conn.execute(
            """
            INSERT OR IGNORE INTO model_role_bindings(role, primary_model_id, fallback_model_id, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (role, model_id, "codex-default" if role != "work_executor" else "", now),
        )
    # ``force_chat_bindings`` remains in the signature for old callers only.
    # Existing bindings are the sole writable authority and are never reset by
    # legacy Assistant settings.
    conn.execute(
        "INSERT OR REPLACE INTO model_registry_meta(key, value, updated_at) VALUES ('legacy_seed_complete', '1', ?)",
        (now,),
    )
    ensure_model_contract_schema(conn)


def list_model_registry(conn: sqlite3.Connection) -> dict:
    provider_rows = conn.execute("SELECT * FROM model_providers ORDER BY enabled DESC, name, id").fetchall()
    model_rows = conn.execute(
        """
        SELECT m.*, p.name AS provider_name, p.kind AS provider_kind,
               p.transport, p.billing_scope, p.runtime_owner, p.config_mode,
               p.trusted_for_executor, p.enabled AS provider_enabled
        FROM model_catalog m JOIN model_providers p ON p.id = m.provider_id
        ORDER BY m.enabled DESC, p.name, m.label, m.id
        """,
    ).fetchall()
    binding_rows = {
        row["role"]: dict(row)
        for row in conn.execute("SELECT * FROM model_role_bindings").fetchall()
    }
    models = [dict(row) for row in model_rows]
    for model in models:
        model["capabilities"] = capabilities_from_row(model)
    model_map = {item["id"]: item for item in models}
    roles = []
    for role, definition in MODEL_ROLES.items():
        if definition.get("visible") is False:
            continue
        binding = binding_rows.get(role, {"primary_model_id": "", "fallback_model_id": "", "updated_at": ""})
        primary_id = binding.get("primary_model_id") or ""
        fallback_id = binding.get("fallback_model_id") or ""
        primary_info = model_map.get(primary_id)
        roles.append({
            "role": role,
            "label": definition["label"],
            "description": definition["description"],
            "primary_model_id": primary_id,
            "fallback_model_id": fallback_id,
            "primary": primary_info,
            "fallback": model_map.get(fallback_id),
            "updated_at": binding.get("updated_at") or "",
        })

    # Compute compatibility from explicit transport and capability contracts.
    executor_profiles = {str(row["provider_id"]): dict(row)
                         for row in conn.execute("SELECT * FROM model_executor_profiles").fetchall()}
    for m in models:
        compatible, _ = role_model_compatible("work_executor", m)
        if m.get("transport") == "codex_cli_custom_provider":
            profile = executor_profiles.get(str(m.get("provider_id") or ""))
            compatible = compatible and bool(profile and int(profile.get("enabled") or 0))
            upstream = executor_upstream_summary(conn, profile)
            m["configured_label"] = m.get("label") or ""
            m["executor_upstream"] = upstream
            m["label"] = executor_model_display_label(m, upstream)
        m["can_bind_work_executor"] = bool(
            int(m.get("enabled") or 0) and int(m.get("provider_enabled") or 0) and compatible
        )
        # E1: full server-computed eligibility per model (Chinese reason +
        # configuration entry) so disabled models are shown, not hidden.
        m["executor_eligibility"] = executor_eligibility_state(conn, str(m.get("id") or ""))

    providers = []
    for row in provider_rows:
        item = _public_provider(conn, row)
        profile = executor_profiles.get(str(item.get("id") or ""))
        verification = None
        if profile and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_verification_state'").fetchone():
            row = conn.execute(
                "SELECT status, verified_at, verification_hash, reason_code, last_error FROM executor_verification_state WHERE provider_id=?",
                (str(item.get("id") or ""),)).fetchone()
            verification = dict(row) if row else None
        item["executor_profile"] = public_executor_profile(profile, verification=verification)
        if item.get("transport") == "codex_cli_custom_provider":
            item["executor_upstream"] = executor_upstream_summary(conn, profile)
            item["executor_runtime"] = executor_runtime_status(profile)
        providers.append(item)
    return {
        "providers": providers,
        "models": models,
        "roles": roles,
    }


def _ensure_executor_model(conn: sqlite3.Connection, provider_id: str, payload: dict) -> None:
    """Auto-create the work-executor catalog model for a custom-provider Draft.

    Role routing binds models, not connections/profiles.  A custom provider
    therefore owns exactly one ``codex_cli_custom_provider`` catalog entry
    (id = provider id) mirroring its upstream model, so the work_executor
    route can select it after isolated verification.  An existing entry is
    never overwritten here, and an id collision owned by another provider is
    ignored instead of failing the provider save.
    """
    upstream_model_id = str(payload.get("executor_upstream_model_id") or "").strip()
    upstream_provider_id = str(payload.get("executor_upstream_provider_id") or "").strip()
    if not upstream_model_id:
        return
    upstream = conn.execute(
        """SELECT m.* FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
           WHERE m.id=? AND p.id=?""",
        (upstream_model_id, upstream_provider_id),
    ).fetchone()
    if not upstream:
        return
    model_name = str(upstream["model"] or "").strip()
    if not model_name:
        return
    existing = conn.execute(
        "SELECT provider_id FROM model_catalog WHERE id=?",
        (provider_id,),
    ).fetchone()
    if existing:
        if str(existing["provider_id"] or "") == provider_id:
            return
        return
    try:
        upsert_model(conn, {
            "id": provider_id,
            "provider_id": provider_id,
            "label": str(upstream["label"] or model_name or provider_id),
            "model": model_name,
            "capabilities": capabilities_from_row(upstream),
            "enabled": 1,
            "context_window": int(upstream["context_window"] or 0),
            "max_output_tokens": int(upstream["max_output_tokens"] or 4096),
        })
    except (ValueError, TypeError):
        return


def upsert_provider(conn: sqlite3.Connection, payload: dict) -> dict:
    provider_id = _slug(payload.get("id"), "provider")
    provider_kind = str(payload.get("kind") or "openai-compatible").strip()
    if provider_kind not in PROVIDER_KINDS:
        raise ValueError("invalid_provider_kind")
    name = _clip(payload.get("name") or provider_id, 120)
    base_url = _clip(payload.get("base_url"), 1000)
    contract = normalize_provider_contract(payload, provider_id=provider_id, kind=provider_kind)
    existing_provider = conn.execute(
        "SELECT transport FROM model_providers WHERE id = ?",
        (provider_id,),
    ).fetchone()
    if existing_provider and str(existing_provider["transport"] or "") != contract["transport"]:
        model_refs = [
            dict(item) for item in conn.execute(
                "SELECT id FROM model_catalog WHERE provider_id = ? ORDER BY id",
                (provider_id,),
            ).fetchall()
        ]
        profile_refs = [
            dict(item) for item in conn.execute(
                "SELECT provider_id FROM model_executor_profiles WHERE provider_id = ?",
                (provider_id,),
            ).fetchall()
        ]
        verification_refs = []
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_verification_state'",
        ).fetchone():
            verification_refs = [
                dict(item) for item in conn.execute(
                    "SELECT provider_id FROM executor_verification_state WHERE provider_id = ?",
                    (provider_id,),
                ).fetchall()
            ]
        if model_refs or profile_refs or verification_refs:
            error = ValueError("provider_transport_change_requires_rebind")
            error.dependencies = {
                "models": [item["id"] for item in model_refs],
                "executor_profiles": [item["provider_id"] for item in profile_refs],
                "executor_verification": [item["provider_id"] for item in verification_refs],
            }
            raise error
    # ``kind`` is display/category metadata, not a network-security boundary.
    # A codex-shaped entry may still use an HTTP transport, so validate the
    # effective transport before this URL is ever persisted or projected back
    # to the browser.
    local_transports_without_endpoint = {
        "codex_cli_chatgpt",
        "codex_cli_custom_provider",
    }
    if contract["transport"] in local_transports_without_endpoint:
        if base_url:
            raise ValueError("invalid_provider_base_url")
    else:
        try:
            parsed = urlparse(base_url)
        except ValueError as exc:
            raise ValueError("invalid_provider_base_url") from exc
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_provider_base_url")
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise ValueError("insecure_remote_base_url")
    clear_api_key = truthy(payload.get("clear_api_key"))
    api_key = "" if clear_api_key else str(payload.get("api_key") or "").strip()
    enabled = 1 if truthy(payload.get("enabled", "1")) else 0
    timeout = max(5, min(int(payload.get("timeout_seconds") or 60), 600))
    if contract["transport"] == "codex_cli_chatgpt":
        duplicate = conn.execute(
            "SELECT id FROM model_providers WHERE transport = ? AND id <> ? LIMIT 1",
            ("codex_cli_chatgpt", provider_id)).fetchone()
        if duplicate:
            raise ValueError("codex_login_instance_already_exists")
    now = utc_now()
    secret_ref, secret_version, rotated_at, new_ref = prepare_provider_secret_update(conn, provider_id, api_key, clear_api_key, now)
    try:
        conn.execute(
        """
        INSERT INTO model_providers(
            id, name, kind, base_url, api_key, enabled, timeout_seconds,
            last_test_status, last_test_latency, last_error, created_at, updated_at,
            transport, billing_scope, runtime_owner, config_mode, trusted_for_executor,
            secret_ref, secret_version, secret_rotated_at
        ) VALUES (?, ?, ?, ?, '', ?, ?, '', NULL, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            base_url = excluded.base_url,
            api_key = CASE WHEN ? = 1 OR excluded.secret_ref <> model_providers.secret_ref THEN '' ELSE model_providers.api_key END,
            enabled = excluded.enabled,
            timeout_seconds = excluded.timeout_seconds,
            transport = excluded.transport,
            billing_scope = excluded.billing_scope,
            runtime_owner = excluded.runtime_owner,
            config_mode = excluded.config_mode,
            trusted_for_executor = excluded.trusted_for_executor,
            secret_ref = excluded.secret_ref,
            secret_version = excluded.secret_version,
            secret_rotated_at = excluded.secret_rotated_at,
            updated_at = excluded.updated_at
        """,
        (
            provider_id, name, provider_kind, base_url, enabled, timeout,
            now, now, contract["transport"], contract["billing_scope"],
            contract["runtime_owner"], contract["config_mode"],
            1 if contract["trusted_for_executor"] else 0,
            secret_ref, secret_version, rotated_at,
            1 if clear_api_key else 0,
        ),
        )
        upsert_executor_profile(conn, provider_id, {**payload, "transport": contract["transport"]})
        if contract["transport"] == "codex_cli_custom_provider":
            _ensure_executor_model(conn, provider_id, payload)
    except Exception:
        if new_ref:
            ProviderSecretStore.for_connection(conn).delete_ref(provider_id, new_ref)
        raise
    row = conn.execute("SELECT * FROM model_providers WHERE id = ?", (provider_id,)).fetchone()
    return _public_provider(conn, row)


def upsert_model(conn: sqlite3.Connection, payload: dict) -> dict:
    provider_id = _clip(payload.get("provider_id"), 64)
    provider = conn.execute("SELECT * FROM model_providers WHERE id = ?", (provider_id,)).fetchone()
    if not provider:
        raise ValueError("provider_not_found")
    label = _clip(payload.get("label") or payload.get("model"), 120)
    model_name = _clip(payload.get("model"), 200)
    if not label:
        raise ValueError("model_label_required")
    if provider["kind"] != "codex" and not model_name:
        raise ValueError("model_name_required")
    model_id = _slug(payload.get("id") or f"{provider_id}-{model_name or 'default'}", "model")
    existing = conn.execute("SELECT provider_id FROM model_catalog WHERE id = ?", (model_id,)).fetchone()
    if existing and str(existing["provider_id"] or "") != provider_id:
        raise ValueError("model_provider_move_requires_copy")
    try:
        context_window = max(0, min(int(payload.get("context_window") or 0), 10_000_000))
        max_output = max(0, min(int(payload.get("max_output_tokens") or 900), 131072))
    except (TypeError, ValueError):
        context_window, max_output = 0, 900
    capabilities_json = capabilities_for_payload(payload)
    supports_tools = "tools" in capabilities_from_row({"capabilities_json": capabilities_json})
    now = utc_now()
    def optional_price(key: str) -> float | None:
        raw = str(payload.get(key) or "").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_model_price") from exc
        if value < 0 or value > 1_000_000:
            raise ValueError("invalid_model_price")
        return value

    input_price = optional_price("input_price_per_million")
    output_price = optional_price("output_price_per_million")
    conn.execute(
        """
        INSERT INTO model_catalog(
            id, provider_id, label, model, enabled, context_window,
            max_output_tokens, supports_tools, notes, input_price_per_million,
            output_price_per_million, price_currency, price_source, created_at, updated_at,
            capabilities_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            provider_id = excluded.provider_id,
            label = excluded.label,
            model = excluded.model,
            enabled = excluded.enabled,
            context_window = excluded.context_window,
            max_output_tokens = excluded.max_output_tokens,
            supports_tools = excluded.supports_tools,
            notes = excluded.notes,
            input_price_per_million = excluded.input_price_per_million,
            output_price_per_million = excluded.output_price_per_million,
            price_currency = excluded.price_currency,
            price_source = excluded.price_source,
            capabilities_json = excluded.capabilities_json,
            updated_at = excluded.updated_at
        """,
        (model_id, provider_id, label, model_name,
         1 if truthy(payload.get("enabled", "1")) else 0, context_window, max_output,
         1 if supports_tools else 0, _clip(payload.get("notes"), 500), input_price, output_price,
         (_clip(payload.get("price_currency") or "USD", 12) or "USD").upper(),
         _clip(payload.get("price_source"), 500), now, now, capabilities_json),
    )
    return dict(conn.execute("SELECT * FROM model_catalog WHERE id = ?", (model_id,)).fetchone())


def bind_model_role(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    _custom_runtime_activated: bool = False,
) -> dict:
    role = str(payload.get("role") or "").strip()
    if role not in MODEL_ROLES:
        raise ValueError("invalid_model_role")
    primary_id = _clip(payload.get("primary_model_id") or payload.get("model_id"), 64)
    fallback_id = _clip(payload.get("fallback_model_id"), 64)

    # Lock the candidate before reading role/dependency state.  The role table
    # intentionally remains compatible with legacy rows without foreign keys,
    # so delete and bind must serialize at the SQLite write-lock boundary.
    if conn.in_transaction:
        conn.execute("UPDATE model_catalog SET id = id WHERE id = ?", (primary_id,))
    else:
        conn.execute("BEGIN IMMEDIATE")

    # An unchanged save is an idempotent readback, not a new authorization
    # decision.  This keeps a grandfathered binding operable while its failed
    # or stale verification is surfaced to the owner; only a changed binding
    # must pass the current fail-closed eligibility gate.
    old = conn.execute(
        "SELECT * FROM model_role_bindings WHERE role = ?",
        (role,),
    ).fetchone()
    old_primary = old["primary_model_id"] if old else ""
    old_fallback = old["fallback_model_id"] if old else ""
    if old and old_primary == primary_id and old_fallback == fallback_id:
        result = dict(old)
        result.update({"role": role, "no_op": True})
        return result

    primary = conn.execute(
        """
        SELECT m.*, p.id AS provider_id, p.kind AS provider_kind, p.transport,
               p.billing_scope, p.runtime_owner, p.config_mode, p.trusted_for_executor,
               p.enabled AS provider_enabled
        FROM model_catalog m JOIN model_providers p ON p.id = m.provider_id WHERE m.id = ?
        """,
        (primary_id,),
    ).fetchone()
    if not primary or not int(primary["enabled"] or 0) or not int(primary["provider_enabled"] or 0):
        raise ValueError("primary_model_unavailable")

    compatible, reason = role_model_compatible(role, primary)
    if not compatible:
        if reason == "role_capability_mismatch" and role == "work_executor":
            reason = "work_executor_requires_tool_support"
        raise ValueError(reason)

    # ``codex_cli_custom_provider`` reaches one process-wide proxy runtime.
    # It is a work-executor adapter, not a general conversation-model
    # transport: allowing it on chat/participation roles would either race a
    # work switch or silently require that unrelated role to share the active
    # executor's upstream.  Existing legacy bindings are handled fail-closed
    # by the runtime identity guard; new bindings are prohibited here.
    if (
        str(primary["transport"] or "") == "codex_cli_custom_provider"
        and role != "work_executor"
    ):
        raise ValueError("custom_executor_role_not_supported")

    # E5: a work_executor bind re-validates the full server-side eligibility
    # contract (model/provider enabled, trusted transport, tool capability,
    # profile applied, runtime ready, verification passed and hash current).
    if role == "work_executor":
        allowed, bind_reason = work_executor_bind_guard(conn, primary_id)
        if not allowed:
            raise ValueError(bind_reason)
        if (
            str(primary["transport"] or "") == "codex_cli_custom_provider"
            and not _custom_runtime_activated
        ):
            raise ValueError("work_executor_activation_required")

    if role == "work_executor":
        if fallback_id:
            raise ValueError("work_executor_fallback_not_supported")
    elif fallback_id:
        fallback = conn.execute(
            "SELECT m.*, p.transport, p.billing_scope, p.runtime_owner, p.config_mode, p.trusted_for_executor"
            " FROM model_catalog m JOIN model_providers p ON p.id = m.provider_id"
            " WHERE m.id = ? AND m.enabled = 1 AND p.enabled = 1",
            (fallback_id,)).fetchone()
        if not fallback:
            raise ValueError("fallback_model_unavailable")
        fallback_compatible, _ = role_model_compatible(role, fallback)
        if not fallback_compatible:
            raise ValueError("fallback_model_capability_mismatch")

    now = utc_now()
    try:
        conn.execute(
            """
            INSERT INTO model_role_bindings(role, primary_model_id, fallback_model_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(role) DO UPDATE SET
                primary_model_id = excluded.primary_model_id,
                fallback_model_id = excluded.fallback_model_id,
                updated_at = excluded.updated_at
            """,
            (role, primary_id, fallback_id, now),
        )
        conn.execute(
            """
            INSERT INTO role_binding_change_log(
                id, role, old_primary_model_id, new_primary_model_id,
                old_fallback_model_id, new_fallback_model_id,
                changed_by, client_ip, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'admin', '', ?)
            """,
            (str(uuid.uuid4()), role, old_primary, primary_id,
             old_fallback, fallback_id, now),
        )
        sync_legacy_mirror(conn)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return dict(conn.execute(
        "SELECT * FROM model_role_bindings WHERE role = ?", (role,)).fetchone())


def activate_work_executor(conn: sqlite3.Connection, model_id: str) -> dict:
    """Atomically make one verified custom-executor Draft the live runtime.

    Draft saves and candidate verification never call this function.  The
    activation path alone may mutate the singleton proxy, and it holds the
    runtime writer lock from preflight through the role binding commit.
    """
    model_id = _clip(model_id, 64)
    row = conn.execute(
        """
        SELECT m.id, m.provider_id, p.transport
        FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
        WHERE m.id=?
        """,
        (model_id,),
    ).fetchone()
    if not row:
        raise ValueError("primary_model_unavailable")
    if str(row["transport"] or "") != "codex_cli_custom_provider":
        binding = bind_model_role(conn, {
            "role": "work_executor",
            "primary_model_id": model_id,
            "fallback_model_id": "",
        })
        return {"ok": True, "binding": binding, "activation": "not_required"}

    from bridge_executor_apply import (
        _apply_executor_profile_locked,
        _restart_and_verify,
        _restart_without_runtime,
        _restore_runtime_env,
        _runtime_env_model,
        _runtime_env_snapshot,
        clear_executor_activation_journal,
        executor_runtime_exclusive_lock,
        write_executor_activation_journal,
    )

    provider_id = str(row["provider_id"] or "")
    with executor_runtime_exclusive_lock():
        allowed, reason = work_executor_activation_guard(conn, model_id)
        if not allowed:
            raise ValueError(reason)
        target, previous = _runtime_env_snapshot()
        previous_profile = conn.execute(
            "SELECT applied_version,last_apply_status,last_error FROM model_executor_profiles WHERE provider_id=?",
            (provider_id,),
        ).fetchone()
        if not previous_profile:
            raise RuntimeError("executor_profile_missing")
        old = conn.execute(
            "SELECT primary_model_id,fallback_model_id FROM model_role_bindings WHERE role='work_executor'",
        ).fetchone()
        journal = write_executor_activation_journal(
            activation_id=uuid.uuid4().hex,
            target_provider_id=provider_id,
            target_model_id=model_id,
            previous_primary_model_id=str(old["primary_model_id"] if old else ""),
            previous_fallback_model_id=str(old["fallback_model_id"] if old else ""),
        )
        try:
            result = _apply_executor_profile_locked(conn, provider_id)
            if not result.get("ok"):
                try:
                    clear_executor_activation_journal()
                except RuntimeError:
                    pass
                return {"ok": False, "error": result.get("error") or "executor_runtime_apply_failed", "binding": {}}
            # _apply_executor_profile_locked updates the candidate's apply state.
            # Commit that state before the existing binding writer starts its own
            # BEGIN IMMEDIATE transaction.
            conn.commit()
            binding = bind_model_role(conn, {
                "role": "work_executor",
                "primary_model_id": model_id,
                "fallback_model_id": "",
            }, _custom_runtime_activated=True)
        except BaseException as exc:
            rollback_error = ""
            try:
                previous_model = _restore_runtime_env(target, previous)
                if previous_model:
                    _restart_and_verify(previous_model)
                else:
                    _restart_without_runtime()
                conn.execute(
                    """UPDATE model_executor_profiles
                       SET applied_version=?,last_apply_status=?,last_error=?,updated_at=?
                       WHERE provider_id=?""",
                    (
                        int(previous_profile["applied_version"] or 0),
                        str(previous_profile["last_apply_status"] or "pending"),
                        str(previous_profile["last_error"] or ""),
                        utc_now(),
                        provider_id,
                    ),
                )
                conn.commit()
            except Exception:
                rollback_error = "executor_runtime_rollback_failed"
            if not rollback_error:
                try:
                    clear_executor_activation_journal()
                except RuntimeError:
                    # Keep the journal when clean-up itself failed: startup
                    # recovery will inspect the still-old role binding and
                    # repeat the deterministic rollback.
                    rollback_error = "executor_activation_journal_clear_failed"
            if rollback_error:
                raise RuntimeError(rollback_error) from exc
            raise
        try:
            clear_executor_activation_journal()
        except RuntimeError:
            # The role binding and proxy already agree.  Keep the non-secret
            # journal so the next boot can positively confirm and clear it;
            # do not relabel a completed Owner switch as a failed operation.
            pass
    return {"ok": True, "binding": binding, "activation": "activated"}


def reconcile_active_work_executor_runtime(conn: sqlite3.Connection) -> dict:
    """Restore the singleton runtime identity for an already-bound executor.

    This is a release preflight compatibility operation, not a model-selection
    or activation API.  Older active profiles predate the non-secret provider
    and config markers used by task-time split-brain detection.  Reconciliation
    may re-apply *only* the exact custom provider already bound to
    ``work_executor``.  A Draft, missing binding, stale profile, or failed
    proxy health remains fail-closed.
    """
    row = conn.execute(
        """
        SELECT b.primary_model_id, m.provider_id, p.transport
        FROM model_role_bindings b
        JOIN model_catalog m ON m.id=b.primary_model_id
        JOIN model_providers p ON p.id=m.provider_id
        WHERE b.role='work_executor'
        LIMIT 1
        """,
    ).fetchone()
    if not row:
        return {"ok": True, "state": "not_required", "reason": "work_executor_binding_missing"}
    if str(row["transport"] or "") != "codex_cli_custom_provider":
        return {"ok": True, "state": "not_required", "reason": "non_custom_executor"}

    provider_id = str(row["provider_id"] or "").strip()
    profile = get_executor_profile(conn, provider_id)
    if not profile or not int(profile.get("enabled") or 0):
        raise RuntimeError("executor_profile_missing")
    if (
        int(profile.get("config_version") or 0) <= 0
        or int(profile.get("config_version") or 0) != int(profile.get("applied_version") or 0)
        or str(profile.get("last_apply_status") or "") != "applied"
    ):
        raise RuntimeError("executor_bound_profile_not_applied")

    from bridge_executor_apply import (
        _apply_executor_profile_locked,
        executor_runtime_exclusive_lock,
        executor_runtime_identity_matches,
    )

    upstream = executor_upstream_summary(conn, profile)
    if executor_runtime_identity_matches(
        provider_id,
        int(profile.get("config_version") or 0),
        str(upstream.get("model") or ""),
    ):
        return {
            "ok": True,
            "state": "already_reconciled",
            "provider_id": provider_id,
            "config_version": int(profile.get("config_version") or 0),
        }

    with executor_runtime_exclusive_lock():
        # The locked apply takes an exact file snapshot, writes identity
        # markers, restarts the proxy, and restores the old file if its health
        # gate fails.  It never reads any unbound Draft.
        result = _apply_executor_profile_locked(conn, provider_id)
        if not result.get("ok"):
            return {
                "ok": False,
                "state": "blocked",
                "error": result.get("error") or "executor_runtime_apply_failed",
            }
        conn.commit()
    return {
        "ok": True,
        "state": "reconciled",
        "provider_id": provider_id,
        "config_version": int(profile.get("config_version") or 0),
    }


def _set_work_executor_binding_for_recovery(
    conn: sqlite3.Connection,
    *,
    primary_model_id: str,
    fallback_model_id: str,
    old_primary_model_id: str,
    old_fallback_model_id: str,
) -> None:
    """Restore an interrupted switch's exact prior role binding.

    This is intentionally private to boot recovery.  It does not select a new
    model or relax normal bind validation: it only writes the identifiers that
    the pre-switch journal captured before the proxy was touched.
    """
    now = utc_now()
    # Recovery may have just restored a custom profile, which updates its
    # apply state in the caller's transaction.  A nested BEGIN would then
    # fail and leave proxy A restored while SQLite still points to B.  A
    # savepoint preserves that outer transaction when present and is itself
    # atomic when recovery owns the connection.
    conn.execute("SAVEPOINT executor_activation_recovery_binding")
    try:
        if primary_model_id:
            conn.execute(
                """
                INSERT INTO model_role_bindings(role,primary_model_id,fallback_model_id,updated_at)
                VALUES('work_executor',?,?,?)
                ON CONFLICT(role) DO UPDATE SET
                    primary_model_id=excluded.primary_model_id,
                    fallback_model_id=excluded.fallback_model_id,
                    updated_at=excluded.updated_at
                """,
                (primary_model_id, fallback_model_id, now),
            )
        else:
            conn.execute("DELETE FROM model_role_bindings WHERE role='work_executor'")
        conn.execute(
            """
            INSERT INTO role_binding_change_log(
                id,role,old_primary_model_id,new_primary_model_id,
                old_fallback_model_id,new_fallback_model_id,changed_by,client_ip,created_at
            ) VALUES(?, 'work_executor', ?, ?, ?, ?, 'activation_recovery', '', ?)
            """,
            (
                str(uuid.uuid4()), old_primary_model_id, primary_model_id,
                old_fallback_model_id, fallback_model_id, now,
            ),
        )
        sync_legacy_mirror(conn)
        conn.execute("RELEASE SAVEPOINT executor_activation_recovery_binding")
    except Exception:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT executor_activation_recovery_binding")
            conn.execute("RELEASE SAVEPOINT executor_activation_recovery_binding")
        except Exception:
            pass
        raise


def _custom_executor_binding_row(conn: sqlite3.Connection, model_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT m.id AS model_id,m.provider_id,p.transport
        FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
        WHERE m.id=?
        """,
        (model_id,),
    ).fetchone()


def recover_interrupted_work_executor_activation(conn: sqlite3.Connection) -> dict:
    """Resolve an activation journal left by a process interruption.

    If SQLite already records the intended target *and* the live proxy proves
    the same non-secret identity, recovery only clears the journal.  Every
    other recoverable state restores the exact pre-switch binding and runtime.
    Unknown changes block startup instead of guessing which executor may run.
    """
    from bridge_executor_apply import (
        _apply_executor_profile_locked,
        _restart_without_runtime,
        _restore_runtime_env,
        _runtime_env_snapshot,
        clear_executor_activation_journal,
        executor_runtime_exclusive_lock,
        executor_runtime_identity_matches,
        read_executor_activation_journal,
    )

    with executor_runtime_exclusive_lock():
        journal = read_executor_activation_journal()
        if not journal:
            return {"ok": True, "state": "not_required"}

        target_model_id = str(journal["target_model_id"])
        target_provider_id = str(journal["target_provider_id"])
        previous_primary = str(journal["previous_primary_model_id"])
        previous_fallback = str(journal["previous_fallback_model_id"])
        target_binding = _custom_executor_binding_row(conn, target_model_id)
        if (
            not target_binding
            or str(target_binding["transport"] or "") != "codex_cli_custom_provider"
            or str(target_binding["provider_id"] or "") != target_provider_id
        ):
            # The journal is an interruption-recovery record, not an alternate
            # configuration channel.  Even a syntactically valid file must be
            # bound to the exact custom provider/model pair selected before
            # the proxy was touched.
            raise RuntimeError("executor_activation_recovery_target_invalid")
        current = conn.execute(
            "SELECT primary_model_id,fallback_model_id FROM model_role_bindings WHERE role='work_executor'",
        ).fetchone()
        current_primary = str(current["primary_model_id"] if current else "")
        current_fallback = str(current["fallback_model_id"] if current else "")
        allowed_current = {previous_primary, target_model_id}
        if current_primary not in allowed_current or (
            current_primary == previous_primary and current_fallback != previous_fallback
        ):
            raise RuntimeError("executor_activation_recovery_binding_conflict")

        target_profile = get_executor_profile(conn, target_provider_id)
        target_upstream = executor_upstream_summary(conn, target_profile) if target_profile else {}
        target_live = bool(
            current_primary == target_model_id
            and target_profile
            and str(target_upstream.get("model") or "")
            and executor_runtime_identity_matches(
                target_provider_id,
                target_profile.get("config_version"),
                str(target_upstream.get("model") or ""),
            )
        )
        if target_live:
            clear_executor_activation_journal()
            return {"ok": True, "state": "finalized_target", "provider_id": target_provider_id}

        if previous_primary:
            previous = _custom_executor_binding_row(conn, previous_primary)
            if not previous:
                raise RuntimeError("executor_activation_recovery_previous_binding_missing")
            if str(previous["transport"] or "") == "codex_cli_custom_provider":
                result = _apply_executor_profile_locked(conn, str(previous["provider_id"] or ""))
                if not result.get("ok"):
                    raise RuntimeError("executor_activation_recovery_runtime_restore_failed")
            else:
                target, _ = _runtime_env_snapshot()
                _restore_runtime_env(target, None)
                _restart_without_runtime()
        else:
            target, _ = _runtime_env_snapshot()
            _restore_runtime_env(target, None)
            _restart_without_runtime()

        if current_primary != previous_primary or current_fallback != previous_fallback:
            _set_work_executor_binding_for_recovery(
                conn,
                primary_model_id=previous_primary,
                fallback_model_id=previous_fallback,
                old_primary_model_id=current_primary,
                old_fallback_model_id=current_fallback,
            )
        clear_executor_activation_journal()
        return {"ok": True, "state": "restored_previous", "provider_id": str(previous_primary or "")}


def _runtime_row(conn: sqlite3.Connection, role: str, *, fallback: bool = False) -> sqlite3.Row | None:
    column = "fallback_model_id" if fallback else "primary_model_id"
    return conn.execute(
        f"""
        SELECT m.*, p.name AS provider_name, p.kind AS provider_kind,
               p.base_url, p.api_key, p.secret_ref, p.secret_version,
               p.secret_rotated_at, p.timeout_seconds, p.transport,
               p.billing_scope, p.runtime_owner, p.config_mode,
               p.trusted_for_executor, p.enabled AS provider_enabled
        FROM model_role_bindings b
        JOIN model_catalog m ON m.id = b.{column}
        JOIN model_providers p ON p.id = m.provider_id
        WHERE b.role = ? AND m.enabled = 1 AND p.enabled = 1
        LIMIT 1
        """,
        (role,),
    ).fetchone()


def runtime_settings_for_role(conn: sqlite3.Connection, role: str, fallback_settings: dict) -> dict:
    if role not in MODEL_ROLES:
        raise ValueError("invalid_model_role")
    row = _runtime_row(conn, role)

    # work_executor: 不尝试 fallback — 管理员手动切换
    if not row:
        if role == "work_executor":
            raise RuntimeError("work_executor_binding_missing")
        row = _runtime_row(conn, role, fallback=True)

    settings = dict(fallback_settings)
    if not row:
        settings["model_role"] = role
        settings["model_registry_fallback"] = True
        if role == "vision_caption":
            settings["model_registry_id"] = ""
            settings["model_capabilities"] = []
        return settings
    item = dict(row)
    item["api_key"] = resolve_provider_secret(conn, item)
    settings.update(
        {
            "model_role": role,
            "model_registry_id": item["id"],
            "model_registry_provider_id": item["provider_id"],
            "model_registry_fallback": False,
            "model_input_price_per_million": item.get("input_price_per_million"),
            "model_output_price_per_million": item.get("output_price_per_million"),
            "model_price_currency": item.get("price_currency") or "USD",
            "model_transport": item.get("transport") or "",
            "model_billing_scope": item.get("billing_scope") or "",
            "model_runtime_owner": item.get("runtime_owner") or "platform",
            "model_capabilities": capabilities_from_row(item),
        },
    )
    if item["transport"] in {"codex_cli_chatgpt", "codex_cli_custom_provider"}:
        settings["chat_provider"] = "codex"
        settings["codex_model"] = item.get("model") or ""
        if item["transport"] == "codex_cli_custom_provider":
            profile = get_executor_profile(conn, item.get("provider_id") or "")
            if not profile or not int(profile.get("enabled") or 0):
                raise RuntimeError("executor_profile_missing")
            settings["executor_profile"] = profile
            settings["codex_profile"] = profile.get("profile_name") or ""
            upstream = executor_upstream_summary(conn, profile)
            if not upstream.get("model"):
                raise RuntimeError("executor_upstream_model_unavailable")
            settings["codex_model"] = upstream["model"]
    else:
        settings.update(
            {
                "chat_provider": "openai-compatible",
                "chat_provider_preset": item.get("provider_id") or "custom",
                "chat_base_url": item.get("base_url") or "",
                "chat_api_key": item.get("api_key") or "",
                "chat_model": item.get("model") or "",
                "chat_max_tokens": str(item.get("max_output_tokens") or settings.get("chat_max_tokens") or 900),
                "chat_provider_label": item.get("provider_name") or "OpenAI-compatible",
            },
        )
    return settings

def list_role_change_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 200))
    rows = conn.execute(
        "SELECT * FROM role_binding_change_log ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
def provider_test_settings(conn: sqlite3.Connection, payload: dict, fallback_settings: dict) -> tuple[dict, dict]:
    model_id = _clip(payload.get("model_id"), 64)
    if not model_id:
        role = str(payload.get("role") or "conversation_reply").strip()
        row = _runtime_row(conn, role)
    else:
        row = conn.execute(
            """
            SELECT m.*, p.name AS provider_name, p.kind AS provider_kind,
                   p.base_url, p.api_key, p.secret_ref, p.secret_version,
                   p.secret_rotated_at, p.timeout_seconds, p.transport,
                   p.billing_scope, p.runtime_owner, p.config_mode,
                   p.trusted_for_executor, p.enabled AS provider_enabled
            FROM model_catalog m JOIN model_providers p ON p.id = m.provider_id
            WHERE m.id = ? AND m.enabled = 1 AND p.enabled = 1
            """,
            (model_id,),
        ).fetchone()
    if not row:
        raise ValueError("model_unavailable")
    item = dict(row)
    item["api_key"] = resolve_provider_secret(conn, item)
    settings = dict(fallback_settings)
    settings.update(
        {
            "model_role": str(payload.get("role") or "connection_test"),
            "model_registry_id": item.get("id") or "",
            "model_registry_provider_id": item.get("provider_id") or "",
            "model_input_price_per_million": item.get("input_price_per_million"),
            "model_output_price_per_million": item.get("output_price_per_million"),
            "model_price_currency": item.get("price_currency") or "USD",
            "model_transport": item.get("transport") or "",
            "model_billing_scope": item.get("billing_scope") or "",
            "model_runtime_owner": item.get("runtime_owner") or "platform",
            "model_capabilities": capabilities_from_row(item),
        }
    )
    if item["transport"] in {"codex_cli_chatgpt", "codex_cli_custom_provider"}:
        settings.update({"chat_provider": "codex", "codex_model": item.get("model") or ""})
        if item["transport"] == "codex_cli_custom_provider":
            profile = get_executor_profile(conn, item.get("provider_id") or "")
            if not profile or not int(profile.get("enabled") or 0):
                raise ValueError("executor_profile_missing")
            settings["executor_profile"] = profile
            settings["codex_profile"] = profile.get("profile_name") or ""
            upstream = executor_upstream_summary(conn, profile)
            if not upstream.get("model"):
                raise ValueError("executor_upstream_model_unavailable")
            settings["codex_model"] = upstream["model"]
    else:
        settings.update(
            {
                "chat_provider": "openai-compatible",
                "chat_base_url": item.get("base_url") or "",
                "chat_api_key": item.get("api_key") or "",
                "chat_model": item.get("model") or "",
                "chat_max_tokens": str(item.get("max_output_tokens") or 900),
                "chat_provider_label": item.get("provider_name") or "OpenAI-compatible",
            },
        )
    return settings, item
def record_provider_test(conn: sqlite3.Connection, provider_id: str, result: dict) -> None:
    conn.execute(
        """
        UPDATE model_providers
        SET last_test_status = ?, last_test_latency = ?, last_error = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            "passed" if result.get("ok") else "failed",
            result.get("duration"),
            "" if result.get("ok") else _clip(result.get("error") or result.get("error_kind"), 500),
            utc_now(),
            str(provider_id or "").strip(),
        ),
    )
