#!/usr/bin/env python3
"""Authenticated HTTP adapter for Assistant Identity resource ownership."""

from __future__ import annotations

import json
from typing import Callable
from urllib.parse import unquote

from bridge_assistant_identity import (
    current_assistant,
    identity_cutover_plan,
    identity_resources,
    list_assistants,
    update_assistant,
)
from bridge_assistant_resources import (
    activate_assistant,
    archive_assistant,
    archive_voice_pack,
    create_assistant,
    create_voice_pack,
)


def _error_status(message: str) -> int:
    if message == "assistant_version_required":
        return 428
    if "not_found" in message or message == "active_assistant_missing":
        return 404
    if any(
        marker in message
        for marker in (
            "in_use",
            "replacement_required",
            "stale_",
            "version_conflict",
            "shadow_compare_failed",
            "_disabled",
        )
    ):
        return 409
    return 400


class AssistantIdentityHttpApi:
    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    def _failure(self, request, exc: Exception) -> bool:
        message = str(exc) or type(exc).__name__
        self._json_response(
            request,
            _error_status(message),
            {"ok": False, "error": message},
        )
        return True

    def handle_get(self, request, path: str) -> bool:
        handlers = {
            "/assistant/instances": list_assistants,
            "/assistant/instances/current": current_assistant,
            "/assistant/identity/resources": identity_resources,
            "/assistant/identity/cutover-plan": identity_cutover_plan,
        }
        handler = handlers.get(path)
        if handler is None:
            return False
        try:
            with self._db_connect() as conn:
                result = handler(conn)
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        try:
            with self._db_connect() as conn:
                if path == "/assistant/instances":
                    result = create_assistant(conn, payload)
                    status = 201
                elif path == "/assistant/instances/activate":
                    result = activate_assistant(
                        conn,
                        str(payload.get("assistant_id") or "").strip(),
                        channel="web",
                    )
                    status = 200
                elif path == "/assistant/instances/archive":
                    result = archive_assistant(
                        conn,
                        str(payload.get("assistant_id") or "").strip(),
                        replacement_assistant_id=str(
                            payload.get("replacement_assistant_id") or "",
                        ).strip(),
                        channel="web",
                    )
                    status = 200
                elif path == "/assistant/voice-packs":
                    result = create_voice_pack(conn, payload)
                    status = 201
                elif path == "/assistant/voice-packs/archive":
                    result = archive_voice_pack(
                        conn,
                        str(payload.get("voice_pack_id") or "").strip(),
                    )
                    status = 200
                else:
                    return False
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, status, {"ok": True, "result": result})
        return True

    def handle_patch(self, request, path: str, payload: dict) -> bool:
        prefix = "/assistant/instances/"
        if not path.startswith(prefix):
            return False
        assistant_id = unquote(path[len(prefix):]).strip()
        if not assistant_id or "/" in assistant_id:
            return False
        if not str(payload.get("expected_updated_at") or "").strip():
            self._json_response(
                request,
                428,
                {"ok": False, "error": "assistant_version_required"},
            )
            return True
        try:
            with self._db_connect() as conn:
                result = update_assistant(
                    conn,
                    assistant_id,
                    payload,
                    channel="web",
                )
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


class AssistantIdentityPatchMixin:
    """Keep PATCH parsing out of the legacy Bridge handler."""

    identity_http_api: AssistantIdentityHttpApi
    conversation_memory_http_api: object | None = None

    def do_PATCH(self):
        api = self.identity_http_api
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            api._json_response(self, 403, {"ok": False, "error": "forbidden"})
            return
        try:
            length = max(0, int(self.headers.get("Content-Length") or "0"))
            if length > 65536:
                self.close_connection = True
                api._json_response(self, 413, {"ok": False, "error": "request_body_too_large"})
                return
            payload = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("json_object_required")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            api._json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        if api.handle_patch(self, path, payload):
            return
        memory_api = self.conversation_memory_http_api
        if memory_api is not None and memory_api.handle_patch(self, path, payload):
            return
        api._json_response(self, 404, {"ok": False, "error": "not_found"})


__all__ = ["AssistantIdentityHttpApi", "AssistantIdentityPatchMixin"]
