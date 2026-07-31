#!/usr/bin/env python3
"""Bounded JSON object reader for Bridge HTTP handlers."""

from __future__ import annotations

import json


def read_json_object(request, max_bytes: int) -> tuple[dict | None, int, str]:
    raw_length = str(request.headers.get("Content-Length") or "0").strip()
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        return None, 400, "content_length_invalid"
    if length < 0:
        return None, 400, "content_length_invalid"
    if length > max(0, int(max_bytes)):
        request.close_connection = True
        return None, 413, "request_body_too_large"
    try:
        raw = request.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, 400, "json_body_invalid"
    if not isinstance(payload, dict):
        return None, 400, "json_object_required"
    return payload, 0, ""


__all__ = ["read_json_object"]
