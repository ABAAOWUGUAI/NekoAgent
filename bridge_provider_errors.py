#!/usr/bin/env python3
"""Stable provider failure taxonomy shared by channel adapters."""

from __future__ import annotations

import json
import re


INVALID_MODEL_MARKERS = (
    "model_not_found",
    "model not found",
    "invalid model",
    "unknown model",
    "unsupported model",
    "model does not exist",
    "does not exist or you do not have access to it",
    "no such model",
)


def is_hard_quota_response(detail: object, *, status: int | None = None) -> bool:
    """Match only HTTP 429 plus the proven Go hard-usage-limit type."""

    roots: list[object] = []
    inherited_status = status
    if isinstance(detail, (dict, list)):
        roots.append(detail)
    else:
        text = str(detail or "").strip()
        if not text or len(text) > 10000:
            return False
        try:
            roots.append(json.loads(text))
        except (TypeError, ValueError, RecursionError):
            # Codex exec's turn.failed error boundary exposes provider detail
            # through `error.message`.  Accept only a proven HTTP-429 prefix
            # and a bounded, independently decoded JSON body.
            if inherited_status is None and re.search(
                r"(?i)\b(?:unexpected\s+status|http(?:/[0-9.]+)?)\s*[:=]?\s*429\b",
                text,
            ):
                inherited_status = 429
            if inherited_status != 429:
                return False
            decoder = json.JSONDecoder()
            starts = [index for index, char in enumerate(text) if char in "{["][:20]
            for index in starts:
                try:
                    value, _end = decoder.raw_decode(text[index:])
                except (TypeError, ValueError, RecursionError):
                    continue
                roots.append(value)
            if not roots:
                return False

    def walk(value: object, inherited_status: int | None, depth: int) -> bool:
        if depth > 6:
            return False
        if isinstance(value, dict):
            local_status = inherited_status
            for key in ("status", "status_code", "http_status", "code"):
                try:
                    candidate = int(value.get(key) or 0)
                except (TypeError, ValueError):
                    candidate = 0
                if candidate:
                    local_status = candidate
                    break
            error = value.get("error")
            if (
                local_status == 429
                and isinstance(error, dict)
                and str(error.get("type") or "") == "GoUsageLimitError"
            ):
                return True
            return any(walk(item, local_status, depth + 1) for item in value.values())
        if isinstance(value, list):
            return any(walk(item, inherited_status, depth + 1) for item in value[:50])
        if isinstance(value, str) and len(value) <= 10000:
            text = value.strip()
            if text.startswith(("{", "[")):
                try:
                    nested = json.loads(text)
                except (TypeError, ValueError):
                    return False
                return walk(nested, inherited_status, depth + 1)
        return False

    return any(walk(root, inherited_status, 0) for root in roots)


def provider_http_error_kind(status: int, detail: str) -> str:
    return provider_http_error_facts(status, detail)["kind"]


def provider_http_error_facts(status: int, detail: str) -> dict:
    """Return a content-free, actionable classification for an HTTP failure."""

    lowered = str(detail or "").lower()
    try:
        body = json.loads(str(detail or ""))
    except (TypeError, ValueError):
        body = {}
    if is_hard_quota_response(body if body else detail, status=status):
        return {
            "kind": "hard_quota",
            "error": "provider_hard_quota",
            "upstream_error_code": None,
            "retryable": False,
            "owner_action_required": True,
        }
    if isinstance(body, dict) and body.get("cloudflare_error"):
        try:
            error_code = int(body.get("error_code") or 0)
        except (TypeError, ValueError):
            error_code = 0
        return {
            "kind": "waf",
            "error": f"provider_cloudflare_{error_code}" if error_code else "provider_cloudflare_blocked",
            "upstream_error_code": error_code or None,
            "retryable": bool(body.get("retryable")),
            "owner_action_required": bool(body.get("owner_action_required")),
        }
    if any(marker in lowered for marker in INVALID_MODEL_MARKERS):
        kind = "invalid_model"
    elif status in {401, 403}:
        kind = "auth"
    elif status == 429:
        kind = "rate_limit" if "rate" in lowered and "quota" not in lowered else "quota"
    elif status == 402:
        kind = "quota"
    elif status >= 500:
        kind = "upstream"
    else:
        kind = "http"
    return {
        "kind": kind,
        "error": f"provider_http_{status}",
        "upstream_error_code": None,
        "retryable": status == 429 or status >= 500,
        "owner_action_required": status in {401, 402, 403},
    }


def provider_transport_error_kind(exc: BaseException) -> str:
    lowered = str(exc or "").lower()
    if any(marker in lowered for marker in INVALID_MODEL_MARKERS):
        return "invalid_model"
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    return "network"


__all__ = [
    "is_hard_quota_response",
    "provider_http_error_facts",
    "provider_http_error_kind",
    "provider_transport_error_kind",
]
