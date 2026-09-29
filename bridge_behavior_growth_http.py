#!/usr/bin/env python3
"""Read-only HTTP adapter for the Assistant Profile growth projection."""

from __future__ import annotations

from typing import Callable

from bridge_assistant_identity import current_assistant
from bridge_behavior_observation import list_behavior_cases, list_behavior_clusters
from bridge_behavior_combined_shadow_runtime import (
    combined_shadow_runtime_projection,
    list_combined_shadow_receipts,
)
from bridge_behavior_growth_service import behavior_growth_summary


class BehaviorEvolutionHttpApi:
    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @staticmethod
    def _query_limit(query: dict) -> int:
        """Accept both direct tests and ``urllib.parse.parse_qs`` values."""

        value = query.get("limit", 50)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else 50
        try:
            return int(value)
        except (TypeError, ValueError):
            return 50

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path not in {
            "/assistant/behavior-growth",
            "/assistant/behavior-growth/cases",
            "/assistant/behavior-growth/clusters",
            "/assistant/behavior-growth/combined-shadow",
        }:
            return False
        try:
            with self._db_connect() as conn:
                assistant_id = str((current_assistant(conn) or {}).get("id") or "").strip()
                if not assistant_id:
                    raise ValueError("behavior_growth_assistant_unavailable")
                if path == "/assistant/behavior-growth/cases":
                    limit = self._query_limit(query)
                    result = {"items": list_behavior_cases(conn, assistant_id=assistant_id, limit=limit)}
                elif path == "/assistant/behavior-growth/clusters":
                    limit = self._query_limit(query)
                    result = {"items": list_behavior_clusters(conn, assistant_id=assistant_id, limit=limit)}
                elif path == "/assistant/behavior-growth/combined-shadow":
                    limit = self._query_limit(query)
                    result = {
                        "projection": combined_shadow_runtime_projection(conn, assistant_id=assistant_id),
                        "items": list_combined_shadow_receipts(conn, assistant_id=assistant_id, limit=limit),
                    }
                else:
                    result = behavior_growth_summary(conn)
        except Exception:
            self._json_response(request, 503, {"ok": False, "error": "behavior_growth_unavailable"})
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["BehaviorEvolutionHttpApi"]
