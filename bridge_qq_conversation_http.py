#!/usr/bin/env python3
"""Authenticated HTTP adapter for the B2 QQ product read model."""

from __future__ import annotations

from typing import Callable
from urllib.parse import unquote

from bridge_qq_conversation_read import QqConversationReadService


def _status(error: str) -> int:
    if error.endswith("_not_available"):
        return 404
    return 400


class QqConversationHttpApi:
    def __init__(self, service: QqConversationReadService, json_response: Callable) -> None:
        self._service = service
        self._json_response = json_response

    def _failure(self, request, exc: Exception) -> bool:
        error = str(exc) or "qq_conversation_read_failed"
        self._json_response(request, _status(error), {"ok": False, "error": error})
        return True

    def handle_get(self, request, path: str, query: dict) -> bool:
        try:
            if path == "/assistant/qq/conversations":
                result = self._service.index(
                    kind=str(query.get("kind", [""])[0]),
                    limit=int(query.get("limit", ["20"])[0]),
                    cursor=str(query.get("cursor", [""])[0]),
                )
            elif path.startswith("/assistant/qq/conversations/") and path.endswith("/timeline"):
                encoded_ref = path[len("/assistant/qq/conversations/"):-len("/timeline")].strip("/")
                result = self._service.timeline(
                    unquote(encoded_ref),
                    limit=int(query.get("limit", ["40"])[0]),
                    cursor=str(query.get("cursor", [""])[0]),
                )
            elif path.startswith("/assistant/qq/conversations/") and path.endswith("/summary"):
                encoded_ref = path[len("/assistant/qq/conversations/"):-len("/summary")].strip("/")
                result = self._service.summary(
                    unquote(encoded_ref),
                    window_hours=int(query.get("window_hours", ["168"])[0]),
                )
            elif path.startswith("/assistant/qq/events/") and path.endswith("/inspector"):
                encoded_ref = path[len("/assistant/qq/events/"):-len("/inspector")].strip("/")
                result = self._service.event_inspector(unquote(encoded_ref))
            else:
                return False
        except (TypeError, ValueError) as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["QqConversationHttpApi"]
