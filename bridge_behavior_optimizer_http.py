#!/usr/bin/env python3
"""Owner control-plane adapter for offline Behavior Policy optimization only."""

from __future__ import annotations

from typing import Callable

from bridge_behavior_policy_optimization_runtime import (
    OPTIMIZER_CUTOVER_PATH,
    BehaviorPolicyOptimizationRuntimeError,
    behavior_policy_optimizer_plan,
    set_behavior_policy_optimizer_cutover,
)


class BehaviorPolicyOptimizerHttpApi:
    """Admin-routed plan/read and checksum-bound offline-only cutover API.

    Authentication stays at the Bridge route boundary.  The QQ channel has no
    allow-list entry for this path, so it cannot toggle the optimizer.
    """

    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path == OPTIMIZER_CUTOVER_PATH

    def handle_get(self, request, path: str) -> bool:
        if path != OPTIMIZER_CUTOVER_PATH:
            return False
        try:
            with self._db_connect() as conn:
                result = behavior_policy_optimizer_plan(conn)
        except Exception as exc:
            self._json_response(request, 503, {"ok": False, "error": str(exc) or type(exc).__name__})
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path != OPTIMIZER_CUTOVER_PATH:
            return False
        try:
            if not isinstance(payload, dict) or set(payload) != {"enabled", "plan_checksum"}:
                raise BehaviorPolicyOptimizationRuntimeError("behavior_optimizer_cutover_payload_invalid")
            with self._db_connect() as conn:
                result = set_behavior_policy_optimizer_cutover(
                    conn,
                    enabled=payload["enabled"],
                    expect_plan_checksum=payload["plan_checksum"],
                )
        except BehaviorPolicyOptimizationRuntimeError as exc:
            error = str(exc) or "behavior_optimizer_cutover_invalid"
            status = 409 if error.startswith("stale_") or error.startswith("behavior_optimizer_prerequisite_required") else 400
            self._json_response(request, status, {"ok": False, "error": error})
            return True
        except Exception as exc:
            self._json_response(request, 503, {"ok": False, "error": str(exc) or type(exc).__name__})
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["BehaviorPolicyOptimizerHttpApi"]
