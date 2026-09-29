"""Authenticated HTTP adapter for the B2 Owner Brief."""

from __future__ import annotations


def _limit(query: dict) -> int:
    try:
        value = int(query.get("limit", ["6"])[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("owner_brief_limit_invalid") from exc
    if value < 1 or value > 6:
        raise ValueError("owner_brief_limit_invalid")
    return value


class OwnerBriefHttpApi:
    def __init__(self, service, json_response) -> None:
        self._service = service
        self._json_response = json_response

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path != "/assistant/owner-brief":
            return False
        try:
            force = str(query.get("force", ["0"])[0]).lower() in {"1", "true", "yes"}
            result = self._service.brief(limit=_limit(query), force=force)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self._json_response(request, 400, {"ok": False, "error": message})
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["OwnerBriefHttpApi"]
