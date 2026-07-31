#!/usr/bin/env python3
"""Authenticated read adapter for Interaction Plan diagnostics."""

from __future__ import annotations

from typing import Callable

from bridge_interaction_repository import (
    interaction_plan_cutover_plan,
    list_interaction_plans,
)


class InteractionPlanHttpApi:
    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path not in {
            "/assistant/interactions",
            "/assistant/interaction-plan/cutover-plan",
        }:
            return False
        try:
            with self._db_connect() as conn:
                if path == "/assistant/interactions":
                    limit = int(query.get("limit", ["50"])[0])
                    thread_id = str(query.get("thread_id", [""])[0] or "").strip()
                    result = {
                        "items": list_interaction_plans(
                            conn,
                            limit=limit,
                            thread_id=thread_id,
                        ),
                    }
                else:
                    result = interaction_plan_cutover_plan(conn)
        except Exception as exc:
            self._json_response(
                request,
                400,
                {"ok": False, "error": str(exc) or type(exc).__name__},
            )
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["InteractionPlanHttpApi"]
