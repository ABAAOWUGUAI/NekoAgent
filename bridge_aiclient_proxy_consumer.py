#!/usr/bin/env python3
"""Fixed AIClient2API consumer of the existing Mihomo proxy authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Mapping
from urllib.parse import urlsplit

import yaml

from bridge_proxy_probe import fast_target_probe


AICLIENT_PROVIDER_ID = "aiclient2api-gemini-antigravity"
AICLIENT_MODEL_ID = "aiclient2api-gemini-2-5-flash"
AICLIENT_PROXY_PROVIDER = "gemini-antigravity"
AICLIENT_PROXY_URL = "http://mihomo:7890"
AICLIENT_TEMP_WINDOWS_RELAY_URL = ""
PROXY_EGRESS_NETWORK = "neko-proxy-egress"
AICLIENT_CONTAINER = "neko-aiclient2api"
MIHOMO_CONTAINER = "mihomo"
AICLIENT_CONFIG_PATH = Path(os.environ.get(
    "AICLIENT_CONFIG_PATH",
    "/var/lib/aiclient2api/configs/config.json",
))
AICLIENT_COMPOSE_PATH = Path(os.environ.get(
    "AICLIENT_COMPOSE_PATH",
    "/opt/agent-stack/aiclient2api/compose.yml",
))
MIHOMO_COMPOSE_PATH = Path(os.environ.get(
    "MIHOMO_COMPOSE_PATH",
    "/opt/agent-stack/mihomo/compose.yml",
))


def desired_proxy_config(current: Mapping[str, object]) -> dict:
    """Preserve credentials while enforcing one selective, fail-closed route."""

    result = dict(current)
    result.update({
        "PROXY_URL": AICLIENT_PROXY_URL,
        "PROXY_ENABLED_PROVIDERS": [AICLIENT_PROXY_PROVIDER],
        "MODEL_FALLBACK_ENABLED": False,
        "providerFallbackChain": {},
        "modelFallbackMapping": {},
    })
    return result


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def persist_proxy_config() -> dict:
    """Atomically update only AIClient2API's existing native config document."""

    try:
        current = json.loads(AICLIENT_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "aiclient_config_unavailable"}
    if not isinstance(current, dict):
        return {"ok": False, "error": "aiclient_config_invalid"}
    desired = desired_proxy_config(current)
    changed = desired != current
    if changed:
        try:
            _atomic_json(AICLIENT_CONFIG_PATH, desired)
        except OSError:
            return {"ok": False, "error": "aiclient_config_write_failed"}
    return {"ok": True, "changed": changed}


def _compose_has_network(path: Path, service: str) -> bool:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        network = (document.get("networks") or {}).get(PROXY_EGRESS_NETWORK) or {}
        service_networks = ((document.get("services") or {}).get(service) or {}).get("networks") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return False
    if isinstance(service_networks, dict):
        service_networks = list(service_networks)
    return bool(
        network.get("external") is True
        and network.get("name") == PROXY_EGRESS_NETWORK
        and PROXY_EGRESS_NETWORK in service_networks
    )


def compose_contract_persistent() -> bool:
    return _compose_has_network(MIHOMO_COMPOSE_PATH, MIHOMO_CONTAINER) and _compose_has_network(
        AICLIENT_COMPOSE_PATH,
        AICLIENT_CONTAINER,
    )


def _container_snapshot(name: str) -> dict:
    try:
        completed = subprocess.run(
            ["docker", "inspect", name],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        document = json.loads(completed.stdout)[0] if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        document = {}
    state = document.get("State") or {}
    health = (state.get("Health") or {}).get("Status") or ""
    return {
        "running": bool(state.get("Running")),
        "healthy": bool(state.get("Running") and health in {"", "healthy"}),
        "health": health or ("running" if state.get("Running") else "unavailable"),
        "networks": sorted((document.get("NetworkSettings") or {}).get("Networks") or {}),
    }


def _load_config() -> dict:
    try:
        value = json.loads(AICLIENT_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _redact_proxy_url(value: object) -> str:
    """Expose proxy routing evidence without credentials or a full host address."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if not parsed.scheme or not host:
            return "configured"
        octets = host.split(".")
        if len(octets) == 4 and all(part.isdigit() for part in octets):
            display_host = ".".join((*octets[:3], "x"))
        elif host == "mihomo":
            display_host = host
        else:
            labels = host.split(".")
            display_host = f"{labels[0]}.*.{labels[-1]}" if len(labels) > 1 else "***"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{display_host}{port}"
    except ValueError:
        return "configured"


def evaluate_proxy_consumer(
    *,
    config: Mapping[str, object],
    role_settings: Mapping[str, object],
    mihomo_networks: list[str],
    aiclient_networks: list[str],
    mihomo_upstream_ok: bool,
    aiclient_health_ok: bool,
    compose_persistent: bool,
) -> dict:
    fallback_disabled = bool(
        config.get("MODEL_FALLBACK_ENABLED") is False
        and config.get("providerFallbackChain") == {}
        and config.get("modelFallbackMapping") == {}
    )
    selective_proxy = bool(
        config.get("PROXY_URL") == AICLIENT_PROXY_URL
        and config.get("PROXY_ENABLED_PROVIDERS") == [AICLIENT_PROXY_PROVIDER]
    )
    role_binding_matches = bool(
        role_settings.get("model_registry_provider_id") == AICLIENT_PROVIDER_ID
        and role_settings.get("model_registry_id") == AICLIENT_MODEL_ID
    )
    shared_network = bool(
        PROXY_EGRESS_NETWORK in mihomo_networks
        and PROXY_EGRESS_NETWORK in aiclient_networks
    )
    checks = (
        (fallback_disabled, "aiclient_fallback_enabled"),
        (selective_proxy, "aiclient_selective_proxy_mismatch"),
        (role_binding_matches, "vision_caption_binding_mismatch"),
        (compose_persistent, "compose_network_not_persistent"),
        (shared_network, "proxy_network_not_attached"),
        (mihomo_upstream_ok, "mihomo_upstream_unavailable"),
        (aiclient_health_ok, "aiclient_unhealthy"),
    )
    error = next((reason for ok, reason in checks if not ok), "")
    current_proxy = str(config.get("PROXY_URL") or "").rstrip("/")
    temporary_windows_relay = current_proxy == AICLIENT_TEMP_WINDOWS_RELAY_URL
    provider_fallback_allowed = bool(config.get("providerFallbackChain"))
    model_fallback_allowed = bool(
        config.get("MODEL_FALLBACK_ENABLED") is True
        or config.get("modelFallbackMapping")
    )
    if temporary_windows_relay and error == "aiclient_selective_proxy_mismatch":
        readiness_code = "OWNER_PROXY_ASSET_REQUIRED"
        readiness_reason = "需要先在服务器代理资产中设定可用的当前订阅与 Proxies 节点。"
    elif error:
        readiness_code = error.upper()
        readiness_reason = error
    else:
        readiness_code = "READY"
        readiness_reason = "受控出站链已就绪。"
    return {
        "ready": not error,
        "error": error,
        "readiness_code": readiness_code,
        "readiness_reason": readiness_reason,
        "current_proxy_endpoint": _redact_proxy_url(current_proxy),
        "target_proxy_endpoint": AICLIENT_PROXY_URL,
        "temporary_windows_relay": temporary_windows_relay,
        "network_name": PROXY_EGRESS_NETWORK,
        "direct_allowed": False,
        "fallback_allowed": False,
        "provider_fallback_allowed": provider_fallback_allowed,
        "model_fallback_allowed": model_fallback_allowed,
        "fallback_disabled": fallback_disabled,
        "credential_configured": bool(config.get("REQUIRED_API_KEY")),
        "selective_proxy_configured": selective_proxy,
        "role_binding_matches": role_binding_matches,
        "compose_persistent": bool(compose_persistent),
        "shared_network_attached": shared_network,
        "mihomo_upstream_ok": bool(mihomo_upstream_ok),
        "aiclient_health_ok": bool(aiclient_health_ok),
    }


def _runtime_proxy_consumer_status(role_settings: Mapping[str, object]) -> dict:
    config = _load_config()
    mihomo = _container_snapshot(MIHOMO_CONTAINER)
    aiclient = _container_snapshot(AICLIENT_CONTAINER)
    upstream = fast_target_probe(
        {
            "name": "google_connectivity",
            "label": "Google connectivity",
            "url": "https://www.gstatic.com/generate_204",
            "required": True,
        },
        proxy="http://127.0.0.1:7890",
        timeout=8,
    )
    evaluation = evaluate_proxy_consumer(
        config=config,
        role_settings=role_settings,
        mihomo_networks=mihomo["networks"],
        aiclient_networks=aiclient["networks"],
        mihomo_upstream_ok=bool(mihomo["running"] and upstream.get("ok")),
        aiclient_health_ok=bool(aiclient["healthy"]),
        compose_persistent=compose_contract_persistent(),
    )
    return {
        "ok": True,
        "consumer_id": "aiclient2api",
        "runtime_role": "vision_caption",
        "provider_id": AICLIENT_PROVIDER_ID,
        "model_id": AICLIENT_MODEL_ID,
        "upstream_provider": AICLIENT_PROXY_PROVIDER,
        "proxy_endpoint": AICLIENT_PROXY_URL,
        "policy_boundary": "capability_policy_is_separate_from_provider_egress",
        "mihomo": {
            "running": mihomo["running"],
            "upstream_ok": bool(upstream.get("ok")),
            "probe_http_code": upstream.get("http_code", "000"),
            "network_attached": PROXY_EGRESS_NETWORK in mihomo["networks"],
        },
        "aiclient": {
            "running": aiclient["running"],
            "health": aiclient["health"],
            "network_attached": PROXY_EGRESS_NETWORK in aiclient["networks"],
        },
        **evaluation,
    }


def runtime_proxy_consumer_status(role_settings: Mapping[str, object]) -> dict:
    """Return runtime truth plus versioned management state without secrets."""

    from bridge_aiclient_proxy_management import (
        AICLIENT_CONFIG_PATH as MANAGEMENT_CONFIG_PATH,
        MIHOMO_SUBSCRIPTION_STATE_PATH,
        control_surface_status,
        evaluate_apply_gate,
    )

    status = _runtime_proxy_consumer_status(role_settings)
    management = control_surface_status(MANAGEMENT_CONFIG_PATH, MIHOMO_SUBSCRIPTION_STATE_PATH)
    runtime = {
        "network_ready": bool(status.get("shared_network_attached")),
        "mihomo_healthy": bool((status.get("mihomo") or {}).get("running") and (status.get("mihomo") or {}).get("upstream_ok")),
        "aiclient_healthy": bool((status.get("aiclient") or {}).get("running") and (status.get("aiclient") or {}).get("health") == "healthy"),
        "direct_allowed": bool(status.get("direct_allowed")),
        "fallback_allowed": bool(status.get("fallback_allowed")),
        "provider_fallback_allowed": bool(status.get("provider_fallback_allowed")),
        "model_fallback_allowed": bool(status.get("model_fallback_allowed")),
        "credential_configured": bool(status.get("credential_configured")),
    }
    try:
        gate = evaluate_apply_gate(
            MANAGEMENT_CONFIG_PATH,
            MIHOMO_SUBSCRIPTION_STATE_PATH,
            runtime,
            expected_revision=int(management.get("revision") or 0),
        )
    except Exception as exc:
        gate = {
            "eligible": False,
            "blockers": [f"management_status_error:{str(exc)[:80]}"],
            "revision": int(management.get("revision") or 0),
        }
    management["apply_eligible"] = bool(gate.get("eligible"))
    management["apply_blockers"] = list(gate.get("blockers") or [])
    status["management"] = management
    latest_test = management.get("latest_test") if isinstance(management.get("latest_test"), dict) else {}

    def receipt_status(key: str) -> str:
        value = latest_test.get(key)
        if not isinstance(value, dict) or "ok" not in value:
            return "unverified"
        return "passed" if bool(value.get("ok")) else "failed"

    has_receipt = bool(latest_test.get("tested_at"))
    status["provider_test"] = {
        "status": ("passed" if latest_test.get("passed") else "failed") if has_receipt else "unverified",
        "tested_at": str(latest_test.get("tested_at") or "")[:80],
        "text_status": receipt_status("text") if has_receipt else "unverified",
        "vision_status": receipt_status("vision") if has_receipt else "unverified",
    }
    status["product_readiness_label"] = "基础代理链已就绪" if status.get("ready") else "基础代理链尚未就绪"
    return status


__all__ = [
    "AICLIENT_CONFIG_PATH",
    "AICLIENT_MODEL_ID",
    "AICLIENT_PROVIDER_ID",
    "AICLIENT_PROXY_PROVIDER",
    "AICLIENT_PROXY_URL",
    "PROXY_EGRESS_NETWORK",
    "compose_contract_persistent",
    "desired_proxy_config",
    "evaluate_proxy_consumer",
    "persist_proxy_config",
    "runtime_proxy_consumer_status",
]
