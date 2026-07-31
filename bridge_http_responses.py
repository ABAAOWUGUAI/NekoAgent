#!/usr/bin/env python3
"""Shared HTTP response helpers for the legacy Bridge adapters."""

from __future__ import annotations

import gzip
import http.server
import json
from typing import Mapping


def _write_payload(handler: http.server.BaseHTTPRequestHandler, payload: bytes) -> bool:
    """Write a response body without logging expected client disconnect tracebacks."""
    try:
        handler.wfile.write(payload)
        return True
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
        return False


def _notify_successful_mutation(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    payload: dict,
) -> None:
    """Notify the owning handler after a successful state-changing response.

    Domain HTTP adapters all share these response helpers.  Keeping the hook
    here lets the composition root invalidate read projections without making
    every adapter import the main Bridge module.
    """

    method = str(getattr(handler, "command", "GET") or "GET").upper()
    if method in {"GET", "HEAD", "OPTIONS"} or not 200 <= int(status) < 300:
        return
    callback = getattr(handler, "on_successful_mutation", None)
    if not callable(callback):
        return
    try:
        callback(status, payload)
    except Exception:
        # Cache invalidation is best-effort and must never corrupt an already
        # committed mutation or prevent its response from reaching the client.
        return


def maybe_gzip(
    handler: http.server.BaseHTTPRequestHandler,
    payload: bytes,
) -> tuple[bytes, bool]:
    accepts = "gzip" in str(handler.headers.get("Accept-Encoding") or "").lower()
    if not accepts or len(payload) < 1024:
        return payload, False
    return gzip.compress(payload, compresslevel=5, mtime=0), True


def json_response(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    payload: dict,
    *,
    headers: Mapping[str, str] | None = None,
):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body, compressed = maybe_gzip(handler, body)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for name, value in (headers or {}).items():
        name = str(name)
        value = str(value)
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            continue
        handler.send_header(name, value)
    if compressed:
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Encoding")
    handler.end_headers()
    _notify_successful_mutation(handler, status, payload)
    _write_payload(handler, body)


def json_response_with_cookie(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    payload: dict,
    cookie: str,
):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body, compressed = maybe_gzip(handler, body)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    if compressed:
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Encoding")
    handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    _notify_successful_mutation(handler, status, payload)
    _write_payload(handler, body)


def html_response(handler: http.server.BaseHTTPRequestHandler, status: int, body: str):
    payload = body.encode("utf-8")
    payload, compressed = maybe_gzip(handler, payload)
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    if compressed:
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Encoding")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; connect-src 'self'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'",
    )
    handler.end_headers()
    _write_payload(handler, payload)


def binary_response(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    payload: bytes,
    content_type: str,
    *,
    cache_control: str = "no-store",
    etag: str = "",
):
    if etag and handler.headers.get("If-None-Match", "").strip('"') == etag:
        handler.send_response(304)
        handler.send_header("ETag", f'"{etag}"')
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    can_compress = content_type.startswith("text/") or content_type.startswith(
        "application/javascript",
    )
    compressed = False
    if can_compress:
        payload, compressed = maybe_gzip(handler, payload)
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    if compressed:
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Encoding")
    handler.send_header("Cache-Control", cache_control)
    if etag:
        handler.send_header("ETag", f'"{etag}"')
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    if handler.command != "HEAD":
        _write_payload(handler, payload)


def redirect_response(handler: http.server.BaseHTTPRequestHandler, location: str):
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


__all__ = [
    "binary_response",
    "html_response",
    "json_response",
    "json_response_with_cookie",
    "maybe_gzip",
    "redirect_response",
]
