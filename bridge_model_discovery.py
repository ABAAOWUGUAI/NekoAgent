"""Controlled discovery and pre-save validation for saved model providers.

The console never accepts an arbitrary discovery URL or credential. It asks a
persisted, enabled connection for its own catalog through the connection's
declared protocol, then validates one returned model before a catalog record
is saved. Provider names never control discovery behaviour.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

from bridge_model_adapters import MODEL_CLIENT_USER_AGENT
from bridge_provider_errors import provider_http_error_facts, provider_transport_error_kind
from bridge_provider_secrets import resolve_provider_secret


MAX_DISCOVERED_MODELS = 500
MAX_RESPONSE_BYTES = 1_000_000


def _provider_row(conn, provider_id: object):
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        raise ValueError("provider_id_required")
    row = conn.execute("SELECT * FROM model_providers WHERE id = ?", (provider_id,)).fetchone()
    if not row:
        raise ValueError("provider_not_found")
    return dict(row)


def _catalog_url(base_url: object) -> str:
    """Derive an OpenAI-compatible models endpoint without preserving secrets."""

    parts = urlsplit(str(base_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("invalid_provider_base_url")
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/models", "", ""))


def _native_catalog_url(base_url: object, suffix: str, *, query: dict[str, str] | None = None) -> str:
    parts = urlsplit(str(base_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("invalid_provider_base_url")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/{suffix.lstrip('/')}", urlencode(query or {}), ""))


def _anthropic_catalog_url(base_url: object) -> str:
    parts = urlsplit(str(base_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("invalid_provider_base_url")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/v1/models", "limit=100", ""))


def _failure(provider: dict, kind: str, error: str, *, retryable: bool = False, owner_action_required: bool = False, duration: float | None = None) -> dict:
    return {
        "ok": False,
        "provider_id": str(provider.get("id") or ""),
        "provider_label": str(provider.get("name") or provider.get("id") or ""),
        "models": [],
        "count": 0,
        "error_kind": kind,
        "error": error,
        "retryable": bool(retryable),
        "owner_action_required": bool(owner_action_required),
        "duration": duration,
    }


def _discovery_ready(provider: dict) -> str:
    if not int(provider.get("enabled") or 0):
        return "provider_disabled"
    transport = str(provider.get("transport") or "")
    if transport == "azure_openai_chat_completions":
        # Azure's account model list is not an executable target: requests use
        # a separately managed deployment name embedded in this connection's
        # endpoint. Do not let a base-model list silently masquerade as a
        # deployable model selector.
        return "model_discovery_requires_azure_deployment"
    if transport in {"codex_cli_chatgpt", "codex_cli_custom_provider"}:
        return "model_discovery_not_available_for_codex_connection"
    if transport not in {
        "openai_chat_completions",
        "anthropic_messages",
        "google_gemini_generate_content",
    }:
        return "model_discovery_unsupported_transport"
    return ""


def _extract_models(payload: object, transport: str) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("provider_models_response_invalid")
    candidates = payload.get("data")
    if not isinstance(candidates, list):
        candidates = payload.get("models")
    if not isinstance(candidates, list):
        raise ValueError("provider_models_response_invalid")
    names: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, dict):
            if transport == "google_gemini_generate_content":
                if "generateContent" not in (candidate.get("supportedGenerationMethods") or []):
                    continue
                value = candidate.get("baseModelId") or candidate.get("name")
            else:
                value = candidate.get("id") or candidate.get("name")
        else:
            value = candidate
        name = str(value or "").strip()
        if transport == "google_gemini_generate_content" and name.startswith("models/"):
            name = name.removeprefix("models/")
        if name and len(name) <= 200:
            names.add(name)
    return [{"id": name, "label": name} for name in sorted(names, key=str.casefold)[:MAX_DISCOVERED_MODELS]]


def _discovery_request(provider: dict, api_key: str) -> tuple[str, dict[str, str]]:
    """Build the read-only catalog request for one supported protocol."""

    transport = str(provider.get("transport") or "")
    headers = {"Accept": "application/json", "User-Agent": MODEL_CLIENT_USER_AGENT}
    if transport == "openai_chat_completions":
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return _catalog_url(provider.get("base_url")), headers
    if transport == "anthropic_messages":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        return _anthropic_catalog_url(provider.get("base_url")), headers
    if transport == "google_gemini_generate_content":
        headers["x-goog-api-key"] = api_key
        return _native_catalog_url(provider.get("base_url"), "models", query={"pageSize": str(MAX_DISCOVERED_MODELS)}), headers
    raise ValueError("model_discovery_unsupported_transport")


def discover_provider_models(
    conn,
    provider_id: object,
    *,
    opener_for_url: Callable[[str], object],
) -> dict:
    """Read only the saved provider's protocol-specific model list.

    Returned data is intentionally limited to model identifiers and typed
    failure facts.  Provider response bodies and credentials never cross the
    bridge boundary.
    """

    provider = _provider_row(conn, provider_id)
    readiness = _discovery_ready(provider)
    if readiness:
        return _failure(provider, "provider_config", readiness, owner_action_required=True)
    try:
        api_key = resolve_provider_secret(conn, provider)
        url, headers = _discovery_request(provider, api_key)
    except Exception:
        return _failure(provider, "provider_config", "provider_secret_or_url_unavailable", owner_action_required=True)
    if not api_key and str(provider.get("billing_scope") or "api_key") != "local_proxy":
        return _failure(provider, "provider_config", "provider_secret_missing", owner_action_required=True)
    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        opener = opener_for_url(url)
        with opener.open(request, timeout=max(5, min(int(provider.get("timeout_seconds") or 60), 120))) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return _failure(provider, "provider_response_too_large", "provider_models_response_too_large", duration=round(time.monotonic() - started, 3))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        facts = provider_http_error_facts(exc.code, detail)
        return _failure(provider, facts["kind"], facts["error"], retryable=facts["retryable"], owner_action_required=facts["owner_action_required"], duration=round(time.monotonic() - started, 3))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _failure(provider, provider_transport_error_kind(exc), "provider_models_request_failed", retryable=True, duration=round(time.monotonic() - started, 3))
    try:
        models = _extract_models(
            json.loads(raw.decode("utf-8", "replace")), str(provider.get("transport") or ""),
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return _failure(provider, "provider_response_invalid", "provider_models_response_invalid", duration=round(time.monotonic() - started, 3))
    return {
        "ok": True,
        "provider_id": str(provider.get("id") or ""),
        "provider_label": str(provider.get("name") or provider.get("id") or ""),
        "models": models,
        "count": len(models),
        "error_kind": "",
        "error": "",
        "retryable": False,
        "owner_action_required": False,
        "duration": round(time.monotonic() - started, 3),
    }


def discovered_model_validation_settings(conn, provider_id: object, model: object, fallback_settings: dict) -> tuple[dict, dict]:
    """Build an ephemeral validation target; never create a model-catalog row."""

    provider = _provider_row(conn, provider_id)
    readiness = _discovery_ready(provider)
    if readiness:
        raise ValueError(readiness)
    model_name = str(model or "").strip()
    if not model_name or len(model_name) > 200:
        raise ValueError("discovered_model_name_invalid")
    try:
        api_key = resolve_provider_secret(conn, provider)
    except Exception as exc:
        raise ValueError("provider_secret_or_url_unavailable") from exc
    if not api_key and str(provider.get("billing_scope") or "api_key") != "local_proxy":
        raise ValueError("provider_secret_missing")
    settings = dict(fallback_settings)
    settings.update(
        {
            "chat_provider": "external_api",
            "chat_base_url": str(provider.get("base_url") or ""),
            "chat_api_key": api_key,
            "chat_model": model_name,
            "chat_provider_label": str(provider.get("name") or provider.get("id") or "API connection"),
            "model_transport": str(provider.get("transport") or ""),
            "model_billing_scope": str(provider.get("billing_scope") or "api_key"),
            "model_registry_provider_id": str(provider.get("id") or ""),
        }
    )
    return settings, {"provider_name": settings["chat_provider_label"], "model": model_name}


__all__ = ["discover_provider_models", "discovered_model_validation_settings"]
