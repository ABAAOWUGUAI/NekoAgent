#!/usr/bin/env python3
"""Authenticated HTTP adapter for the Daily Assistant Home read model."""

from __future__ import annotations


class AssistantHomeHttpApi:
    def __init__(self, service, json_response) -> None:
        self._service = service
        self._json_response = json_response

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path not in {"/assistant/home", "/assistant/attention"}:
            return False
        try:
            limit = int(query.get("limit", ["20" if path.endswith("attention") else "12"])[0])
            if limit < 1 or limit > 100:
                raise ValueError("assistant_home_limit_invalid")
            force = str(query.get("force", query.get("force_refresh", ["0"]))[0]).lower() in {
                "1", "true", "yes",
            }
            result = (
                self._service.attention(limit=limit, force=force)
                if path.endswith("/attention")
                else self._service.home(limit=limit, force=force)
            )
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            status = 409 if message.endswith("_disabled") else 400
            self._json_response(request, status, {"ok": False, "error": message})
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["AssistantHomeHttpApi"]
