#!/usr/bin/env python3
"""Authenticated AC-4 cutover adapter for natural group participation."""

from __future__ import annotations

from bridge_group_participation_policy import (
    natural_group_cutover_plan,
    set_natural_group_participation_feature,
)


PATH = "/assistant/groups/cutover"


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class GroupParticipationHttpApi:
    def __init__(self, assistant_connect, json_response) -> None:
        self._assistant_connect = assistant_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path == PATH

    def handle_get(self, request, path: str) -> bool:
        if path != PATH:
            return False
        try:
            with self._assistant_connect() as conn:
                result = natural_group_cutover_plan(conn)
        except Exception as exc:
            self._json_response(request, 500, {"ok": False, "error": type(exc).__name__})
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path != PATH:
            return False
        try:
            with self._assistant_connect() as conn:
                result = set_natural_group_participation_feature(
                    conn,
                    _truthy(payload.get("enabled")),
                    expect_plan_checksum=str(payload.get("plan_checksum") or ""),
                )
        except ValueError as exc:
            message = str(exc) or "group_participation_cutover_invalid"
            status = 409 if "stale_" in message or "required" in message else 400
            self._json_response(request, status, {"ok": False, "error": message})
            return True
        except Exception as exc:
            self._json_response(request, 500, {"ok": False, "error": type(exc).__name__})
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["GroupParticipationHttpApi", "PATH"]

