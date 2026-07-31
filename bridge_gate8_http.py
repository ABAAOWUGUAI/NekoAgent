#!/usr/bin/env python3
"""Authenticated HTTP adapter for Gate 8 management capabilities."""

from __future__ import annotations

from bridge_model_routing_presets import (
    apply_routing_preset,
    list_routing_presets,
    routing_preset_preview,
)
from bridge_relationship_service import (
    get_notification_policy,
    get_relationship_state,
    get_social_proactive_policy,
    relationship_proactive_cutover_plan,
    set_relationship_proactive_feature,
    update_notification_policy,
    update_relationship_state,
    update_social_proactive_policy,
)
from bridge_proactive_messaging_policy import (
    get_proactive_messaging_policy,
    list_proactive_messaging_policies,
    update_proactive_messaging_policy,
)
from bridge_proactive_review import decide_proactive_review, list_proactive_reviews


POST_PATHS = {
    "/assistant/relationship",
    "/assistant/notification-policy",
    "/assistant/proactive/social-policy",
    "/assistant/relationship/cutover",
    "/assistant/models/presets/apply",
    "/assistant/proactive/messaging-policy",
}


def _first(query: dict, name: str, default: str = "") -> str:
    values = query.get(name, [default])
    return str(values[0] if values else default)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class Gate8HttpApi:
    def __init__(self, assistant_connect, health_service, json_response) -> None:
        self._assistant_connect = assistant_connect
        self._health_service = health_service
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path in POST_PATHS or path.startswith("/assistant/proactive/reviews/")

    def _error(self, request, exc: Exception) -> None:
        if isinstance(exc, ValueError):
            message = str(exc) or "gate8_request_invalid"
            status = (
                409
                if any(
                    marker in message
                    for marker in (
                        "stale_",
                        "_conflict",
                        "_disabled",
                        "_unavailable",
                        "_prerequisite_",
                    )
                )
                else 400
            )
        else:
            message = "gate8_internal_error"
            status = 500
        self._json_response(
            request,
            status,
            {"ok": False, "error": message},
        )

    def handle_get(self, request, path: str, query: dict) -> bool:
        supported = {
            "/assistant/relationship",
            "/assistant/notification-policy",
            "/assistant/proactive/social-policy",
            "/assistant/proactive/messaging-policy",
            "/assistant/proactive/reviews",
            "/assistant/relationship/cutover",
            "/assistant/models/presets",
            "/assistant/models/presets/preview",
            "/assistant/health/business",
        }
        if path not in supported:
            return False
        try:
            if path == "/assistant/health/business":
                live = _first(query, "live").lower() in {"1", "true", "yes", "on"}
                result = self._health_service.summary(live=live)
            else:
                with self._assistant_connect() as conn:
                    if path == "/assistant/relationship":
                        result = get_relationship_state(
                            conn,
                            user_id=_first(query, "user_id", "admin"),
                            scope_type=_first(query, "scope_type", "private_user"),
                            scope_id=_first(query, "scope_id"),
                        )
                    elif path == "/assistant/notification-policy":
                        result = get_notification_policy(
                            conn,
                            user_id=_first(query, "user_id", "admin"),
                            channel_scope=_first(query, "channel_scope", "owner"),
                        )
                    elif path == "/assistant/proactive/social-policy":
                        result = get_social_proactive_policy(
                            conn,
                            user_id=_first(query, "user_id", "admin"),
                        )
                    elif path == "/assistant/proactive/messaging-policy":
                        target_type = _first(query, "target_type", "global")
                        target_id = _first(query, "target_id")
                        result = {
                            "policy": get_proactive_messaging_policy(
                                conn,
                                target_type=target_type,
                                target_id=target_id,
                            ),
                            "policies": list_proactive_messaging_policies(conn),
                        }
                    elif path == "/assistant/proactive/reviews":
                        result = {"reviews": list_proactive_reviews(conn, limit=_first(query, "limit", "50"))}
                    elif path == "/assistant/relationship/cutover":
                        result = relationship_proactive_cutover_plan(conn)
                    elif path == "/assistant/models/presets/preview":
                        result = routing_preset_preview(
                            conn,
                            _first(query, "preset", "balanced"),
                        )
                    else:
                        result = list_routing_presets(conn)
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        is_review_post = path.startswith("/assistant/proactive/reviews/")
        if path not in POST_PATHS and not is_review_post:
            return False
        idempotency_key = str(
            request.headers.get("Idempotency-Key") or "",
        ).strip()
        try:
            with self._assistant_connect() as conn:
                if path == "/assistant/relationship":
                    result = update_relationship_state(
                        conn,
                        payload,
                        idempotency_key=idempotency_key,
                    )
                elif path == "/assistant/notification-policy":
                    result = update_notification_policy(
                        conn,
                        payload,
                        idempotency_key=idempotency_key,
                    )
                elif path == "/assistant/proactive/social-policy":
                    result = update_social_proactive_policy(
                        conn,
                        payload,
                        idempotency_key=idempotency_key,
                    )
                elif path == "/assistant/proactive/messaging-policy":
                    result = update_proactive_messaging_policy(
                        conn,
                        payload,
                        idempotency_key=idempotency_key,
                    )
                elif is_review_post:
                    result = decide_proactive_review(
                        conn,
                        path.rsplit("/", 1)[-1],
                        str(payload.get("decision") or ""),
                        idempotency_key=idempotency_key,
                    )
                elif path == "/assistant/relationship/cutover":
                    result = set_relationship_proactive_feature(
                        conn,
                        _truthy(payload.get("enabled")),
                        expect_plan_checksum=str(
                            payload.get("plan_checksum") or "",
                        ),
                    )
                else:
                    result = apply_routing_preset(
                        conn,
                        payload,
                        changed_by="admin",
                        client_ip=str(request.client_address[0] or ""),
                    )
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["Gate8HttpApi", "POST_PATHS"]
