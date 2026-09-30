#!/usr/bin/env python3
"""Owner-authenticated HTTP controls for the zero-send paired Shadow cutover."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

from bridge_behavior_paired_shadow_cutover import (
    PAIRED_SHADOW_CUTOVER_PATH,
    BehaviorPairedShadowCutoverError,
    behavior_paired_shadow_cutover_plan,
    set_behavior_paired_shadow_cutover,
)


PAIRED_SHADOW_SET_PATH = PAIRED_SHADOW_CUTOVER_PATH + "/set"
PAIRED_SHADOW_REVOKE_PATH = PAIRED_SHADOW_CUTOVER_PATH + "/revoke"
_POST_PATHS = frozenset({PAIRED_SHADOW_SET_PATH, PAIRED_SHADOW_REVOKE_PATH})


def _strict_string(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _authorization_binding(plan: Mapping[str, object]) -> dict | None:
    reference = plan.get("reference")
    candidate = plan.get("candidate")
    scopes = plan.get("admitted_scope_refs")
    if (
        plan.get("preconditions") != {"state": "ready", "reason": "ready"}
        or not isinstance(reference, Mapping)
        or not isinstance(candidate, Mapping)
        or not isinstance(scopes, list)
    ):
        return None
    return {
        "assistant_id": plan["assistant_id"],
        "candidate_ref": candidate["candidate_ref"],
        "policy_bundle_hash": candidate["policy_bundle_hash"],
        "reference_policy_ref": reference["reference_policy_ref"],
        "benchmark_ref": reference["benchmark_ref"],
        "benchmark_hash": reference["benchmark_hash"],
        "scope_refs": list(scopes),
        "cutover_plan_checksum": plan["plan_checksum"],
    }


def _project(plan: Mapping[str, object], *, revoked: bool = False) -> dict:
    current = plan.get("current_binding")
    has_row = isinstance(current, Mapping) and current.get("state") != "not_configured"
    if revoked:
        state = "revoked"
    elif plan.get("state") == "enabled":
        state = "active"
    elif has_row:
        state = "revoked"
    else:
        state = "default_off"
    result = {
        "schema_version": plan.get("schema_version"),
        "feature": plan.get("feature"),
        "state": state,
        "preconditions": plan.get("preconditions"),
        "assistant_id": plan.get("assistant_id"),
        "admitted_scope_count": plan.get("admitted_scope_count", 0),
        "reference": plan.get("reference"),
        "candidate": plan.get("candidate"),
        "current_binding": plan.get("current_binding"),
        "delivery_enabled": False,
        "formal_domain_writes": [],
        "model_or_network_execution": False,
        "reversible": True,
        "plan_checksum": plan.get("plan_checksum"),
    }
    binding = _authorization_binding(plan)
    if binding is not None:
        result["authorization_binding"] = binding
    return result


class BehaviorPairedShadowHttpApi:
    """Checksum-bound plan/set/revoke adapter behind the Bridge Owner guard."""

    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path in _POST_PATHS

    def _failure(self, request, exc: Exception) -> bool:
        if isinstance(exc, BehaviorPairedShadowCutoverError):
            error = str(exc) or "behavior_paired_shadow_invalid"
            status = 409 if (
                error.startswith("stale_")
                or "prerequisite_required" in error
                or "owner_authorization_required" in error
            ) else 400
            self._json_response(request, status, {"ok": False, "error": error})
            return True
        self._json_response(request, 503, {"ok": False, "error": "behavior_paired_shadow_unavailable"})
        return True

    def handle_get(self, request, path: str) -> bool:
        if path != PAIRED_SHADOW_CUTOVER_PATH:
            return False
        try:
            with self._db_connect() as conn:
                result = _project(behavior_paired_shadow_cutover_plan(conn))
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: object) -> bool:
        if path not in _POST_PATHS:
            return False
        try:
            if type(payload) is not dict:
                raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_payload_invalid")
            expected = (
                {"authorization_ref", "plan_checksum"}
                if path == PAIRED_SHADOW_SET_PATH
                else {"plan_checksum"}
            )
            if set(payload) != expected or any(not _strict_string(value) for value in payload.values()):
                raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_payload_invalid")
            with self._db_connect() as conn:
                result = set_behavior_paired_shadow_cutover(
                    conn,
                    enabled=path == PAIRED_SHADOW_SET_PATH,
                    expect_plan_checksum=payload["plan_checksum"],
                    authorization_ref=payload.get("authorization_ref"),
                )
                projected = _project(result, revoked=path == PAIRED_SHADOW_REVOKE_PATH)
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": projected})
        return True


__all__ = [
    "PAIRED_SHADOW_SET_PATH",
    "PAIRED_SHADOW_REVOKE_PATH",
    "BehaviorPairedShadowHttpApi",
]
