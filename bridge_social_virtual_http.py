#!/usr/bin/env python3
"""Admin-only HTTP adapter for SocialOpportunity and Virtual Life V1."""

from __future__ import annotations

from bridge_assistant_migrations import record_security_audit
from bridge_social_opportunity import (
    list_feedback,
    list_opportunities,
    set_social_opportunity_feature,
)
from bridge_social_virtual_schema import (
    SOCIAL_OPPORTUNITY_FEATURE_FLAG,
    SOCIAL_VIRTUAL_MIGRATION_CHECKSUM,
    VIRTUAL_LIFE_FEATURE_FLAG,
)
from bridge_virtual_life import (
    event_action,
    generate_events,
    get_profile,
    list_event_audits,
    list_events,
    list_templates,
    set_virtual_life_feature,
    update_profile,
    upsert_template,
)


POST_PATHS = {
    "/assistant/social-virtual/cutover",
    "/assistant/virtual-life/profile",
    "/assistant/virtual-life/templates",
    "/assistant/virtual-life/generate",
    "/assistant/virtual-life/events/action",
}


def _first(query: dict, name: str, default: str = "") -> str:
    values = query.get(name, [default])
    return str(values[0] if values else default)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _limit(query: dict, default: int = 50) -> int:
    try:
        return max(1, min(int(_first(query, "limit", str(default))), 300))
    except ValueError:
        return default


def _status(query: dict) -> str | None:
    value = _first(query, "status", "").strip().lower()
    if not value:
        return None
    if value not in {"open", "decided"}:
        raise ValueError("social_opportunity_status_invalid")
    return value


def _cutover_state(conn) -> dict:
    rows = conn.execute(
        "SELECT name,enabled,updated_at FROM assistant_feature_flags WHERE name IN (?,?)",
        (SOCIAL_OPPORTUNITY_FEATURE_FLAG, VIRTUAL_LIFE_FEATURE_FLAG),
    ).fetchall()
    flags = {str(row[0]): {"enabled": bool(row[1]), "updated_at": str(row[2])} for row in rows}
    return {"contract_checksum": SOCIAL_VIRTUAL_MIGRATION_CHECKSUM, "flags": flags}


class SocialVirtualHttpApi:
    def __init__(self, assistant_connect, json_response) -> None:
        self._assistant_connect = assistant_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path in POST_PATHS

    def _error(self, request, exc: Exception) -> None:
        if isinstance(exc, ValueError):
            message = str(exc) or "social_virtual_request_invalid"
            status = 409 if any(marker in message for marker in ("stale_", "_conflict", "_disabled", "_unavailable")) else 400
        else:
            message, status = "social_virtual_internal_error", 500
        self._json_response(request, status, {"ok": False, "error": message})

    def handle_get(self, request, path: str, query: dict) -> bool:
        supported = {
            "/assistant/social-virtual/cutover",
            "/assistant/social/opportunities",
            "/assistant/social/feedback",
            "/assistant/virtual-life/profile",
            "/assistant/virtual-life/templates",
            "/assistant/virtual-life/events",
            "/assistant/virtual-life/audits",
        }
        if path not in supported:
            return False
        try:
            with self._assistant_connect() as conn:
                if path == "/assistant/social-virtual/cutover":
                    result = _cutover_state(conn)
                elif path == "/assistant/social/opportunities":
                    result = {
                        "opportunities": list_opportunities(
                            conn,
                            limit=_limit(query),
                            status=_status(query),
                        )
                    }
                elif path == "/assistant/social/feedback":
                    result = {"feedback": list_feedback(conn, limit=_limit(query))}
                elif path == "/assistant/virtual-life/profile":
                    result = get_profile(conn)
                elif path == "/assistant/virtual-life/templates":
                    result = {"templates": list_templates(conn)}
                elif path == "/assistant/virtual-life/events":
                    result = {"events": list_events(conn, include_deleted=_truthy(_first(query, "include_deleted")), limit=_limit(query, 100))}
                else:
                    result = {"audits": list_event_audits(conn, event_id=_first(query, "event_id"), limit=_limit(query, 100))}
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path not in POST_PATHS:
            return False
        key = str(request.headers.get("Idempotency-Key") or "").strip()
        try:
            with self._assistant_connect() as conn:
                if path == "/assistant/social-virtual/cutover":
                    if str(payload.get("contract_checksum") or "") != SOCIAL_VIRTUAL_MIGRATION_CHECKSUM:
                        raise ValueError("social_virtual_cutover_checksum_conflict")
                    social = set_social_opportunity_feature(conn, _truthy(payload.get("social_enabled")))
                    virtual = set_virtual_life_feature(conn, _truthy(payload.get("virtual_life_enabled")))
                    result = {"social": social, "virtual_life": virtual, **_cutover_state(conn)}
                elif path == "/assistant/virtual-life/profile":
                    result = update_profile(conn, payload, idempotency_key=key)
                elif path == "/assistant/virtual-life/templates":
                    result = upsert_template(conn, payload, idempotency_key=key)
                elif path == "/assistant/virtual-life/generate":
                    result = generate_events(conn, payload, idempotency_key=key)
                else:
                    result = event_action(conn, payload, idempotency_key=key)
                record_security_audit(
                    conn,
                    "assistant_social_virtual_write",
                    "success",
                    client_ip=str(request.client_address[0] or ""),
                    detail={"path": path},
                )
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["POST_PATHS", "SocialVirtualHttpApi"]
