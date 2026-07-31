#!/usr/bin/env python3
"""Authenticated HTTP adapter for Conversation and Memory scope resources."""

from __future__ import annotations

from typing import Callable
from urllib.parse import unquote

from bridge_conversation_memory import (
    conversation_memory_cutover_plan,
    list_threads,
    memory_scope_catalog,
    thread_messages,
    update_memory,
)


def _status(message: str) -> int:
    if message == "memory_version_required":
        return 428
    if "not_found" in message:
        return 404
    if "version_conflict" in message or "_disabled" in message or message.startswith("stale_"):
        return 409
    return 400


class ConversationMemoryHttpApi:
    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    def _failure(self, request, exc: Exception) -> bool:
        message = str(exc) or type(exc).__name__
        self._json_response(
            request,
            _status(message),
            {"ok": False, "error": message},
        )
        return True

    def handle_get(self, request, path: str, query: dict) -> bool:
        try:
            with self._db_connect() as conn:
                if path == "/assistant/conversations":
                    limit = int(query.get("limit", ["50"])[0])
                    result = list_threads(conn, limit=limit)
                elif path.startswith("/assistant/conversations/") and path.endswith("/messages"):
                    thread_id = unquote(path.split("/")[3]).strip()
                    limit = int(query.get("limit", ["50"])[0])
                    result = thread_messages(conn, thread_id, limit=limit)
                elif path == "/assistant/memories/scopes":
                    result = memory_scope_catalog()
                elif path == "/assistant/memory-scope/cutover-plan":
                    result = conversation_memory_cutover_plan(conn)
                else:
                    return False
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_patch(self, request, path: str, payload: dict) -> bool:
        prefix = "/assistant/memories/"
        if not path.startswith(prefix):
            return False
        memory_id = unquote(path[len(prefix):]).strip()
        if not memory_id or "/" in memory_id:
            return False
        try:
            with self._db_connect() as conn:
                result = update_memory(conn, memory_id, payload)
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["ConversationMemoryHttpApi"]
