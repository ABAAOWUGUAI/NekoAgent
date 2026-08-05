#!/usr/bin/env python3
"""Protocol adapters for user-owned model connections.

This module only builds and parses HTTP payloads. Network routing, retries,
credential storage, and usage persistence stay in the bridge runtime.
"""

from __future__ import annotations

import hashlib
from urllib.parse import quote, urlparse


MODEL_CLIENT_USER_AGENT = "PrivateAIAssistant/1.0"


def _deepseek_cache_scope(settings: dict) -> str:
    """Return a stable, opaque DeepSeek KV-cache namespace for one Assistant.

    The provider's `user_id` controls cache isolation.  Never place a QQ id or
    other channel identifier in that field; it must not become a provider-side
    copy of a user identifier.  A scope is emitted only for known DeepSeek
    routes and only when the Assistant Instance authority is available.
    """

    route = " ".join(
        str(settings.get(key) or "").lower()
        for key in ("chat_model", "chat_provider_preset", "model_registry_provider_id")
    )
    host = (urlparse(str(settings.get("chat_base_url") or "")).hostname or "").lower()
    assistant_id = str(settings.get("assistant_id") or "").strip()
    if host != "api.deepseek.com" or "deepseek" not in route or not assistant_id:
        return ""
    role = str(settings.get("model_role") or "conversation").strip() or "conversation"
    contract = str(settings.get("prompt_cache_contract_version") or "role-cache-v2").strip()[:80]
    variant = str(settings.get("prompt_cache_variant") or f"role-{role}").strip()[:80]
    return "pai-" + hashlib.sha256(
        f"assistant-cache:{contract}:{assistant_id}:{role}:{variant}".encode("utf-8"),
    ).hexdigest()[:48]


def _deepseek_role_controls(settings: dict) -> dict:
    """Return only documented controls for the official DeepSeek V4 route."""

    host = (urlparse(str(settings.get("chat_base_url") or "")).hostname or "").lower()
    model = str(settings.get("chat_model") or "").lower()
    if host != "api.deepseek.com" or not model.startswith("deepseek-v4-"):
        return {}
    role = str(settings.get("model_role") or "").strip()
    if role not in {"conversation_engagement", "interaction_classifier", "conversation_reply"}:
        return {}
    controls: dict[str, object] = {"thinking": {"type": "disabled"}}
    if role in {"conversation_engagement", "interaction_classifier"}:
        controls["response_format"] = {"type": "json_object"}
    return controls


def _temperature(settings: dict) -> float:
    try:
        value = float(settings.get("chat_temperature") or 0.7)
    except (TypeError, ValueError):
        value = 0.7
    return max(0.0, min(value, 2.0))


def _max_tokens(settings: dict) -> int:
    try:
        value = int(float(settings.get("chat_max_tokens") or 900))
    except (TypeError, ValueError):
        value = 900
    return max(1, min(value, 8192))


def _chat_completion_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        ).strip()
    return str(content or "")


def prepare_model_request(settings: dict, messages: list[dict]) -> dict:
    """Return a secret-bearing request spec for the selected native protocol."""

    transport = str(settings.get("model_transport") or "openai_chat_completions").strip()
    base_url = str(settings.get("chat_base_url") or "").strip().rstrip("/")
    api_key = str(settings.get("chat_api_key") or "").strip()
    model = str(settings.get("chat_model") or "").strip()
    temperature = _temperature(settings)
    max_tokens = _max_tokens(settings)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # A stable service identity is required by some Cloudflare-protected
        # OpenAI-compatible gateways.  Falling back to Python-urllib's default
        # signature can be rejected before provider authentication is reached.
        "User-Agent": MODEL_CLIENT_USER_AGENT,
    }

    if transport == "anthropic_messages":
        system = "\n\n".join(
            _message_text(item) for item in messages if item.get("role") == "system"
        ).strip()
        conversation = [
            {
                "role": "assistant" if item.get("role") == "assistant" else "user",
                "content": _message_text(item),
            }
            for item in messages
            if item.get("role") != "system"
        ]
        payload = {
            "model": model,
            "messages": conversation,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        return {
            "transport": transport,
            "provider": "anthropic",
            "url": f"{base_url}/v1/messages",
            "headers": headers,
            "payload": payload,
        }

    if transport == "google_gemini_generate_content":
        system = "\n\n".join(
            _message_text(item) for item in messages if item.get("role") == "system"
        ).strip()
        contents = [
            {
                "role": "model" if item.get("role") == "assistant" else "user",
                "parts": [{"text": _message_text(item)}],
            }
            for item in messages
            if item.get("role") != "system"
        ]
        payload = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        headers["x-goog-api-key"] = api_key
        return {
            "transport": transport,
            "provider": "google-gemini",
            "url": f"{base_url}/models/{quote(model, safe='')}:generateContent",
            "headers": headers,
            "payload": payload,
        }

    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if transport == "azure_openai_chat_completions":
        headers["api-key"] = api_key
        return {
            "transport": transport,
            "provider": "azure-openai",
            "url": base_url,
            "headers": headers,
            "payload": payload,
        }

    payload["model"] = model
    payload.update(_deepseek_role_controls(settings))
    cache_scope = _deepseek_cache_scope(settings)
    if cache_scope:
        payload["user_id"] = cache_scope
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return {
        "transport": "openai_chat_completions",
        "provider": "openai-compatible",
        "url": _chat_completion_url(base_url),
        "headers": headers,
        "payload": payload,
    }


def parse_model_response(transport: str, data: dict) -> tuple[str, dict]:
    """Normalize a native provider response to reply text and token usage."""

    if transport == "anthropic_messages":
        reply = "\n".join(
            str(item.get("text") or "")
            for item in (data.get("content") or [])
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        raw = data.get("usage") or {}
        prompt = int(raw.get("input_tokens") or 0)
        completion = int(raw.get("output_tokens") or 0)
        return reply, {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    if transport == "google_gemini_generate_content":
        candidates = data.get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        reply = "\n".join(
            str(item.get("text") or "") for item in parts if isinstance(item, dict)
        ).strip()
        raw = data.get("usageMetadata") or {}
        prompt = int(raw.get("promptTokenCount") or 0)
        completion = int(raw.get("candidatesTokenCount") or 0)
        return reply, {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": int(raw.get("totalTokenCount") or prompt + completion),
        }

    choices = data.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    content = message.get("content")
    if isinstance(content, str):
        reply = content.strip()
    elif isinstance(content, list):
        # Some OpenAI-compatible providers use the typed content-parts shape.
        # Only final text parts are sendable; reasoning_content is deliberately
        # excluded so hidden reasoning can never become a QQ reply.
        reply = "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
            and str(item.get("type") or "text") in {"text", "output_text"}
        ).strip()
    else:
        reply = ""
    return reply, data.get("usage") or {}


def openai_response_facts(data: dict, reply: str, usage: dict) -> dict:
    """Return content-free protocol facts for empty-response diagnosis."""

    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    reasoning = message.get("reasoning_content")
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else {}
    content = message.get("content") if message else None
    if not isinstance(data, dict):
        response_shape = "payload_not_object"
    elif not isinstance(choices, list):
        response_shape = "choices_missing"
    elif not choices:
        response_shape = "choices_empty"
    elif not isinstance(choices[0], dict):
        response_shape = "choice_not_object"
    elif not message:
        response_shape = "message_missing"
    elif "content" not in message:
        response_shape = "content_missing"
    elif isinstance(content, str):
        response_shape = "content_text" if content.strip() else "content_empty"
    elif isinstance(content, list):
        response_shape = "content_parts"
    else:
        response_shape = "content_unsupported"
    return {
        "finish_reason": str(choice.get("finish_reason") or "")[:40],
        "reasoning_only": bool(not reply and isinstance(reasoning, str) and reasoning.strip()),
        "reasoning_tokens": int((details or {}).get("reasoning_tokens") or 0),
        "response_shape": response_shape,
    }
