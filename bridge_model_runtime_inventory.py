#!/usr/bin/env python3
"""Secret-free, read-only model inventories for platform-owned runtimes."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path


MAIBOT_MODEL_CONFIG = Path(
    os.environ.get("MAIBOT_MODEL_CONFIG", "/root/MaiBot/docker-config/mmc/model_config.toml")
)
ASTRBOT_CONFIG = Path(
    os.environ.get("ASTRBOT_CONFIG", "/opt/agent-stack/astrbot/data/cmd_config.json")
)


def _safe_toml_value(raw: str) -> object:
    text = raw.split("#", 1)[0].strip()
    if not text:
        return ""
    normalized = re.sub(r"\btrue\b", "True", text, flags=re.IGNORECASE)
    normalized = re.sub(r"\bfalse\b", "False", normalized, flags=re.IGNORECASE)
    try:
        value = ast.literal_eval(normalized)
    except (SyntaxError, ValueError):
        return text.strip('"\'')[:160]
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [str(item)[:160] for item in value[:32]]
    return ""


def read_maibot_inventory(path: Path = MAIBOT_MODEL_CONFIG) -> dict:
    inventory = {
        "runtime_owner": "maibot",
        "label": "MaiBot",
        "config_mode": "read_only",
        "status": "missing",
        "providers": [],
        "models": [],
        "role_bindings": [],
        "source_label": "MaiBot model_config.toml",
    }
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return inventory
    current_kind = ""
    current: dict | None = None
    for raw in lines:
        line = raw.strip()
        header = line.split("#", 1)[0].strip()
        if header == "[[models]]":
            current_kind = "model"
            current = {}
            inventory["models"].append(current)
            continue
        if header == "[[api_providers]]":
            current_kind = "provider"
            current = {}
            inventory["providers"].append(current)
            continue
        task_match = re.fullmatch(r"\[model_task_config\.([A-Za-z0-9_-]+)\]", header)
        if task_match:
            current_kind = "task"
            current = {"role": task_match.group(1), "models": []}
            inventory["role_bindings"].append(current)
            continue
        if header.startswith("["):
            current_kind = ""
            current = None
            continue
        match = re.match(r"([A-Za-z0-9_-]+)\s*=\s*(.*)", line)
        if not match or current is None:
            continue
        key, value_raw = match.groups()
        value = _safe_toml_value(value_raw)
        if current_kind == "model" and key in {"name", "model_identifier", "api_provider"}:
            current[key] = value
        elif current_kind == "provider" and key in {"name", "client_type"}:
            current[key] = value
        elif current_kind == "task" and key == "model_list":
            current["models"] = value if isinstance(value, list) else [str(value)]
    inventory["providers"] = [item for item in inventory["providers"] if item.get("name")]
    inventory["models"] = [
        item for item in inventory["models"] if item.get("name") or item.get("model_identifier")
    ]
    inventory["role_bindings"] = [item for item in inventory["role_bindings"] if item.get("models")]
    inventory["status"] = "ready"
    return inventory


def _astrbot_provider_public(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    public = {}
    for key in ("id", "provider_id", "type", "provider_type", "model", "model_name", "enable", "enabled"):
        value = item.get(key)
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            public[key] = value
    models = item.get("models") or item.get("model_list")
    if isinstance(models, list):
        public["models"] = [str(value)[:160] for value in models[:32] if isinstance(value, (str, int, float))]
    return public or None


def read_astrbot_inventory(path: Path = ASTRBOT_CONFIG) -> dict:
    inventory = {
        "runtime_owner": "astrbot",
        "label": "AstrBot",
        "config_mode": "read_only",
        "status": "missing",
        "providers": [],
        "models": [],
        "role_bindings": [],
        "source_label": "AstrBot cmd_config.json",
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return inventory
    except (TypeError, ValueError):
        inventory["status"] = "invalid"
        return inventory
    providers = []
    for item in data.get("provider") or []:
        public = _astrbot_provider_public(item)
        if public:
            providers.append(public)
    settings = data.get("provider_settings") or {}
    default_id = str(settings.get("default_provider_id") or "").strip()
    inventory["providers"] = providers
    inventory["models"] = [
        {"name": model, "provider_id": provider.get("id") or provider.get("provider_id") or ""}
        for provider in providers
        for model in provider.get("models", [])
    ]
    if default_id:
        inventory["role_bindings"] = [{"role": "default_chat", "models": [default_id]}]
    inventory["status"] = "ready"
    return inventory


def runtime_inventories(platform_registry: dict) -> list[dict]:
    platform_models = []
    for item in platform_registry.get("models") or []:
        platform_models.append(
            {
                "id": item.get("id") or "",
                "name": item.get("label") or item.get("model") or item.get("id") or "",
                "provider_id": item.get("provider_id") or "",
                "capabilities": list(item.get("capabilities") or []),
            }
        )
    platform = {
        "runtime_owner": "platform",
        "label": "Agent Platform",
        "config_mode": "managed",
        "status": "ready",
        "providers": [
            {
                "id": item.get("id") or "",
                "name": item.get("name") or item.get("id") or "",
                "transport": item.get("transport") or "",
                "billing_scope": item.get("billing_scope") or "",
            }
            for item in platform_registry.get("providers") or []
        ],
        "models": platform_models,
        "role_bindings": [
            {
                "role": item.get("role") or "",
                "models": [value for value in (item.get("primary_model_id"), item.get("fallback_model_id")) if value],
            }
            for item in platform_registry.get("roles") or []
        ],
        "source_label": "Platform model registry",
    }
    return [platform, read_maibot_inventory(), read_astrbot_inventory()]
