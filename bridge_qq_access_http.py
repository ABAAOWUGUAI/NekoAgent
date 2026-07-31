#!/usr/bin/env python3
"""Authenticated HTTP adapter for Gate C1 QQ access control."""

from __future__ import annotations

from bridge_qq_access_service import (
    check_qq_access,
    get_qq_access_settings,
    qq_access_cutover_plan,
    set_qq_access_feature,
    update_qq_access_settings,
)


POST_PATHS = {
    "/qq/settings",
    "/qq/access/cutover",
    "/qq/access/check",
}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class QqAccessHttpApi:
    def __init__(self, assistant_connect, json_response) -> None:
        self._assistant_connect = assistant_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path in POST_PATHS

    def _error(self, request, exc: Exception) -> None:
        if isinstance(exc, ValueError):
            message = str(exc) or "qq_access_request_invalid"
            status = (
                409
                if any(
                    marker in message
                    for marker in (
                        "stale_",
                        "_conflict",
                        "last_super_admin",
                        "_prerequisite_",
                    )
                )
                else 400
            )
        else:
            message = "qq_access_internal_error"
            status = 500
        self._json_response(request, status, {"ok": False, "error": message})

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path not in {"/qq/settings", "/qq/access/cutover"}:
            return False
        try:
            with self._assistant_connect() as conn:
                result = (
                    get_qq_access_settings(conn)
                    if path == "/qq/settings"
                    else qq_access_cutover_plan(conn)
                )
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path not in POST_PATHS:
            return False
        try:
            with self._assistant_connect() as conn:
                if path == "/qq/settings":
                    result = update_qq_access_settings(
                        conn,
                        payload,
                        idempotency_key=str(
                            request.headers.get("Idempotency-Key") or "",
                        ).strip(),
                        changed_by="web_admin",
                    )
                elif path == "/qq/access/cutover":
                    result = set_qq_access_feature(
                        conn,
                        _truthy(payload.get("enabled")),
                        expect_plan_checksum=str(payload.get("plan_checksum") or ""),
                        changed_by="web_admin",
                    )
                else:
                    result = check_qq_access(conn, payload)
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["POST_PATHS", "QqAccessHttpApi"]
