#!/usr/bin/env python3
"""Stable provider failure taxonomy shared by channel adapters."""

from __future__ import annotations

import json


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


def provider_http_error_kind(status: int, detail: str) -> str:
    return provider_http_error_facts(status, detail)["kind"]


def provider_http_error_facts(status: int, detail: str) -> dict:
    """Return a content-free, actionable classification for an HTTP failure."""

    lowered = str(detail or "").lower()
    try:
        body = json.loads(str(detail or ""))
    except (TypeError, ValueError):
        body = {}
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
    "provider_http_error_facts",
    "provider_http_error_kind",
    "provider_transport_error_kind",
]
