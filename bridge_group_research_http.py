#!/usr/bin/env python3
"""Owner HTTP surface for the explicitly opt-in R7 group research pilot."""

from __future__ import annotations

from typing import Callable

from bridge_group_research_service import (
    get_group_research_policy,
    list_group_research_runs,
    set_group_research_feature_flags,
    set_group_research_policy,
)


class GroupResearchHttpApi:
    PATH = "/assistant/group-research"
    POLICY_PATH = PATH + "/policy"

    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @classmethod
    def matches_post(cls, path: str) -> bool:
        return path == cls.POLICY_PATH

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path != self.PATH:
            return False
        try:
            limit = int(query.get("limit", ["50"])[0])
            with self._db_connect() as conn:
                result = {
                    "policy": get_group_research_policy(conn),
                    "runs": list_group_research_runs(conn, limit=limit),
                }
        except Exception as exc:
            self._json_response(request, 400, {"ok": False, "error": str(exc) or type(exc).__name__})
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path != self.POLICY_PATH:
            return False
        try:
            with self._db_connect() as conn:
                policy_fields = {
                    "enabled", "scope_mode", "pilot_group_id", "auto_publish_low_public",
                    "max_runs_per_day", "max_runs_per_group_day", "freshness_hours",
                }
                if policy_fields & set(payload):
                    policy = set_group_research_policy(
                        conn,
                        enabled=payload.get("enabled") if "enabled" in payload else None,
                        scope_mode=payload.get("scope_mode") if "scope_mode" in payload else None,
                        pilot_group_id=payload.get("pilot_group_id") if "pilot_group_id" in payload else None,
                        auto_publish_low_public=(
                            payload.get("auto_publish_low_public")
                            if "auto_publish_low_public" in payload else None
                        ),
                        max_runs_per_day=payload.get("max_runs_per_day"),
                        max_runs_per_group_day=payload.get("max_runs_per_group_day"),
                        freshness_hours=payload.get("freshness_hours"),
                        expected_version=payload.get("version"),
                        actor="owner",
                    )
                else:
                    policy = get_group_research_policy(conn)
                if (
                    "feature_enabled" in payload
                    or "autoknowledge_feature_enabled" in payload
                ):
                    policy = set_group_research_feature_flags(
                        conn,
                        feature_enabled=(
                            payload.get("feature_enabled")
                            if "feature_enabled" in payload else None
                        ),
                        autoknowledge_feature_enabled=(
                            payload.get("autoknowledge_feature_enabled")
                            if "autoknowledge_feature_enabled" in payload else None
                        ),
                    )
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._json_response(request, 409 if error.endswith("_conflict") else 400, {"ok": False, "error": error})
            return True
        self._json_response(request, 200, {"ok": True, "policy": policy})
        return True


__all__ = ["GroupResearchHttpApi"]
