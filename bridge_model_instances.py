#!/usr/bin/env python3
"""User-owned model connection lifecycle.

Templates contain no credentials or real bindings. Providers and models are
ordinary user records; role bindings are the only routing authority.
"""

from __future__ import annotations

import sqlite3


CONNECTION_TEMPLATES = (
    {
        "key": "codex-chatgpt",
        "label": "Codex · ChatGPT 登录",
        "kind": "codex",
        "transport": "codex_cli_chatgpt",
        "billing_scope": "chatgpt_subscription",
        "trusted_for_executor": True,
        "description": "复用用户已完成的 Codex ChatGPT 登录；平台只保存路由元数据。",
    },
    {
        "key": "openai",
        "label": "OpenAI API",
        "kind": "openai",
        "transport": "openai_chat_completions",
        "billing_scope": "api_key",
        "base_url": "https://api.openai.com/v1",
        "description": "OpenAI 官方 Chat Completions 接口；Key 只保存在服务端。",
        "help_url": "https://platform.openai.com/docs/api-reference/chat",
    },
    {
        "key": "openai-compatible",
        "label": "OpenAI-compatible API",
        "kind": "openai-compatible",
        "transport": "openai_chat_completions",
        "billing_scope": "api_key",
        "description": "用户提供端点、模型名和 API Key，费用归对应 API 账户。",
    },
    {
        "key": "openrouter",
        "label": "OpenRouter",
        "kind": "openrouter",
        "transport": "openai_chat_completions",
        "billing_scope": "api_key",
        "base_url": "https://openrouter.ai/api/v1",
        "description": "通过 OpenRouter 账户访问其模型目录；费用归用户自己的 OpenRouter 账户。",
        "help_url": "https://openrouter.ai/docs/api/reference/overview",
    },
    {
        "key": "anthropic",
        "label": "Anthropic API",
        "kind": "anthropic",
        "transport": "anthropic_messages",
        "billing_scope": "api_key",
        "base_url": "https://api.anthropic.com",
        "description": "使用 Anthropic Messages 原生协议，不通过 OpenAI 兼容转换。",
        "help_url": "https://docs.anthropic.com/en/api/messages",
    },
    {
        "key": "google-gemini",
        "label": "Google Gemini API",
        "kind": "gemini",
        "transport": "google_gemini_generate_content",
        "billing_scope": "api_key",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "description": "使用 Gemini generateContent 原生协议。",
        "help_url": "https://ai.google.dev/api/generate-content",
    },
    {
        "key": "azure-openai",
        "label": "Azure OpenAI",
        "kind": "azure-openai",
        "transport": "azure_openai_chat_completions",
        "billing_scope": "api_key",
        "description": "Base URL 填写包含 deployment 与 api-version 的完整 Chat Completions 端点。",
        "help_url": "https://learn.microsoft.com/azure/ai-foundry/openai/reference",
    },
    {
        "key": "ollama",
        "label": "Ollama（本机）",
        "kind": "ollama",
        "transport": "openai_chat_completions",
        "billing_scope": "local_proxy",
        "base_url": "http://127.0.0.1:11434/v1",
        "description": "连接服务器本机 Ollama 的 OpenAI 兼容端点；无需 API Key。",
        "help_url": "https://docs.ollama.com/api/openai-compatibility",
    },
    {
        "key": "lm-studio",
        "label": "LM Studio（本机）",
        "kind": "lm-studio",
        "transport": "openai_chat_completions",
        "billing_scope": "local_proxy",
        "base_url": "http://127.0.0.1:1234/v1",
        "description": "连接服务器本机 LM Studio 兼容端点；无需 API Key。",
    },
    {
        "key": "codex-custom",
        "label": "Codex · 自定义 Provider",
        "kind": "codex",
        "transport": "codex_cli_custom_provider",
        "billing_scope": "local_proxy",
        "description": "路由到用户自管的 Codex Provider；执行器信任需单独确认。",
    },
)


def connection_templates() -> list[dict]:
    return [dict(item) for item in CONNECTION_TEMPLATES]


def delete_model(conn: sqlite3.Connection, model_id: str) -> dict:
    model_id = str(model_id or "").strip()
    row = conn.execute("SELECT id, provider_id, label FROM model_catalog WHERE id = ?", (model_id,)).fetchone()
    if row is None:
        raise ValueError("model_not_found")
    roles = [
        dict(item) for item in conn.execute(
            """SELECT role, primary_model_id, fallback_model_id FROM model_role_bindings
               WHERE primary_model_id = ? OR fallback_model_id = ? ORDER BY role""",
            (model_id, model_id),
        ).fetchall()
    ]
    if roles:
        error = ValueError("model_in_use")
        error.dependencies = {"roles": [item["role"] for item in roles]}
        raise error
    executor_rows = conn.execute(
        "SELECT provider_id FROM model_executor_profiles WHERE upstream_model_id=?",
        (model_id,),
    ).fetchall()
    if executor_rows:
        error = ValueError("model_used_by_executor_profile")
        error.dependencies = {"executor_profiles": [item["provider_id"] for item in executor_rows]}
        raise error
    conn.execute("DELETE FROM model_catalog WHERE id = ?", (model_id,))
    return {"deleted": model_id, "provider_id": row["provider_id"], "label": row["label"]}


def delete_provider(conn: sqlite3.Connection, provider_id: str) -> dict:
    provider_id = str(provider_id or "").strip()
    row = conn.execute(
        "SELECT id, name, runtime_owner, config_mode FROM model_providers WHERE id = ?",
        (provider_id,),
    ).fetchone()
    if row is None:
        raise ValueError("provider_not_found")
    if row["runtime_owner"] != "platform" or row["config_mode"] != "managed":
        raise ValueError("runtime_owned_provider_read_only")
    models = [
        dict(item) for item in conn.execute(
            "SELECT id, label FROM model_catalog WHERE provider_id = ? ORDER BY label, id",
            (provider_id,),
        ).fetchall()
    ]
    if models:
        error = ValueError("provider_has_models")
        error.dependencies = {"models": [item["id"] for item in models]}
        raise error
    conn.execute("DELETE FROM model_executor_profiles WHERE provider_id = ?", (provider_id,))
    conn.execute("DELETE FROM model_providers WHERE id = ?", (provider_id,))
    return {"deleted": provider_id, "name": row["name"]}


def dependency_error_payload(exc: Exception) -> dict:
    return {
        "ok": False,
        "error": str(exc),
        "dependencies": getattr(exc, "dependencies", []),
    }
