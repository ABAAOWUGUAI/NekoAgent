#!/usr/bin/env python3
"""Admin HTTP adapter for the unified learning inbox and context trace."""

from __future__ import annotations

from typing import Callable
from urllib.parse import unquote

from bridge_learning_service import (
    learning_summary,
    list_learning_trace,
    record_learning_feedback,
    set_learning_flags,
)


class LearningHttpApi:
    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return (
            path == "/assistant/learning/policy"
            or path == "/assistant/learning/feedback"
            or (path.startswith("/assistant/learning/candidates/") and path.endswith("/feedback"))
        )

    def _failure(self, request, exc: Exception) -> bool:
        message = str(exc) or type(exc).__name__
        status = 404 if message.endswith("_not_found") else 409 if message.endswith("_conflict") else 400
        self._json_response(request, status, {"ok": False, "error": message})
        return True

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path not in {
            "/assistant/learning",
            "/assistant/learning/candidates",
            "/assistant/learning/timeline",
            "/assistant/learning/trace",
        }:
            return False
        try:
            limit = int(query.get("limit", ["50"])[0])
            with self._db_connect() as conn:
                if path in {"/assistant/learning", "/assistant/learning/candidates"}:
                    result = learning_summary(conn, limit=limit)
                else:
                    result = {
                        "items": list_learning_trace(
                            conn,
                            thread_id=str(query.get("thread_id", [""])[0] or ""),
                            limit=limit,
                        ),
                    }
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if not self.matches_post(path):
            return False
        try:
            with self._db_connect() as conn:
                if path == "/assistant/learning/policy":
                    result = set_learning_flags(
                        conn,
                        enabled=payload.get("enabled") if "enabled" in payload else None,
                        low_risk=payload.get("low_risk") if "low_risk" in payload else None,
                        owner_group_expression_feedback=(
                            payload.get("owner_group_expression_feedback")
                            if "owner_group_expression_feedback" in payload else None
                        ),
                    )
                else:
                    candidate_id = str(payload.get("candidate_id") or "")
                    if "/candidates/" in path:
                        candidate_id = unquote(path.split("/")[4])
                    result = record_learning_feedback(
                        conn,
                        candidate_id,
                        feedback_type=str(payload.get("feedback_type") or payload.get("type") or ""),
                        actor_ref="owner",
                        note=str(payload.get("note") or ""),
                        idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or ""),
                    )
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["LearningHttpApi"]
