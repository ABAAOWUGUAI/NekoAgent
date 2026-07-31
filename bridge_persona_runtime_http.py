#!/usr/bin/env python3
"""Authenticated HTTP adapter for the PI-4 Persona workspace."""

from __future__ import annotations

import sqlite3
from typing import Callable

from bridge_persona_runtime import (
    persona_workspace,
    preview_persona_workspace,
    runtime_persona_metadata,
    save_persona_workspace,
)


def _error_status(message: str) -> int:
    if message == "assistant_version_required":
        return 428
    if message.endswith("not_found") or message == "active_assistant_missing":
        return 404
    if "version_conflict" in message:
        return 409
    return 400


class PersonaRuntimeHttpApi:
    """Small adapter; authorization and request-size limits stay in BridgeHandler."""

    WORKSPACE_PATH = "/assistant/persona-workspace"
    RUNTIME_PATH = "/assistant/persona-workspace/runtime"
    PREVIEW_PATH = "/assistant/persona-workspace/preview"

    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    def matches_post(self, path: str) -> bool:
        return path in {self.WORKSPACE_PATH, self.PREVIEW_PATH}

    def _failure(self, request, exc: Exception) -> bool:
        if isinstance(exc, sqlite3.Error):
            self._json_response(
                request,
                503,
                {"ok": False, "error": "persona_workspace_unavailable"},
            )
            return True
        message = str(exc) or type(exc).__name__
        self._json_response(
            request,
            _error_status(message),
            {"ok": False, "error": message},
        )
        return True

    def handle_get(self, request, path: str) -> bool:
        if path not in {self.WORKSPACE_PATH, self.RUNTIME_PATH}:
            return False
        try:
            with self._db_connect() as conn:
                result = (
                    runtime_persona_metadata(conn)
                    if path == self.RUNTIME_PATH
                    else persona_workspace(conn)
                )
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if not self.matches_post(path):
            return False
        try:
            with self._db_connect() as conn:
                result = (
                    preview_persona_workspace(conn, payload)
                    if path == self.PREVIEW_PATH
                    else save_persona_workspace(conn, payload)
                )
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["PersonaRuntimeHttpApi"]
