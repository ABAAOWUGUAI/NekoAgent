#!/usr/bin/env python3
from __future__ import annotations


PROVIDER_PRESETS = {
    "codex": {
        "key": "codex",
        "label": "Codex CLI",
        "provider": "codex",
        "base_url": "",
        "model": "",
        "description": "Use ChatGPT login state through Codex CLI for work tasks.",
    },
    "openai": {
        "key": "openai",
        "label": "OpenAI",
        "provider": "openai-compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "description": "OpenAI-compatible chat completions endpoint.",
    },
    "deepseek": {
        "key": "deepseek",
        "label": "DeepSeek",
        "provider": "openai-compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "description": "DeepSeek OpenAI-compatible API preset.",
    },
    "custom": {
        "key": "custom",
        "label": "Custom OpenAI-compatible",
        "provider": "openai-compatible",
        "base_url": "",
        "model": "",
        "description": "Any third-party endpoint that supports OpenAI-compatible chat completions.",
    },
}


def provider_presets_public() -> list[dict]:
    return [
        {
            "key": item["key"],
            "label": item["label"],
            "provider": item["provider"],
            "base_url": item["base_url"],
            "model": item["model"],
            "description": item["description"],
        }
        for item in PROVIDER_PRESETS.values()
    ]


def provider_preset(key: str | None) -> dict:
    normalized = str(key or "").strip().lower()
    return PROVIDER_PRESETS.get(normalized) or PROVIDER_PRESETS["custom"]


def provider_label(settings: dict) -> str:
    registry_label = str(settings.get("chat_provider_label") or "").strip()
    if registry_label:
        return registry_label
    preset = provider_preset(settings.get("chat_provider_preset"))
    if preset["key"] != "custom":
        return preset["label"]
    provider = str(settings.get("chat_provider") or "codex")
    if provider == "codex":
        return PROVIDER_PRESETS["codex"]["label"]
    return PROVIDER_PRESETS["custom"]["label"]


def apply_provider_preset(payload: dict) -> dict:
    updated = dict(payload)
    preset_key = str(updated.get("chat_provider_preset") or "").strip().lower()
    if not preset_key:
        return updated
    preset = provider_preset(preset_key)
    updated["chat_provider_preset"] = preset["key"]
    updated["chat_provider"] = preset["provider"]
    if preset["base_url"] and not str(updated.get("chat_base_url") or "").strip():
        updated["chat_base_url"] = preset["base_url"]
    if preset["model"] and not str(updated.get("chat_model") or "").strip():
        updated["chat_model"] = preset["model"]
    if preset["key"] == "codex":
        updated["chat_base_url"] = ""
        updated["chat_model"] = ""
    return updated
