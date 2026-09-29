#!/usr/bin/env python3
"""Owner-only Bridge adapter for the isolated BE-4 evaluator.

This module sends only opaque bindings and allowlisted Candidate patches across
the local evaluator boundary.  It cannot load a Vault, model credential or
private signing key, and it never enables Delivery, Knowledge, Memory, Task or
Approval work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import uuid

from bridge_assistant_identity import current_assistant
from bridge_behavior_benchmark_registry import BehaviorBenchmarkRegistryError, record_reference_baseline
from bridge_behavior_candidate_registry import (
    BehaviorCandidateRegistryError,
    load_offline_candidate_for_private_evaluation,
    receive_signed_candidate_evaluation,
)
from bridge_behavior_owner_authorization import (
    BehaviorOwnerAuthorizationError,
    preflight_behavior_owner_authorization,
)
from bridge_behavior_private_evaluator_client import PrivateEvaluatorClient, PrivateEvaluatorClientError


EVALUATOR_BASE_PATH = "/assistant/behavior-growth/evaluator"
REFERENCE_PLAN_PATH = EVALUATOR_BASE_PATH + "/reference/plan"
REFERENCE_RUN_PATH = EVALUATOR_BASE_PATH + "/reference/run"
CANDIDATE_PLAN_PATH = EVALUATOR_BASE_PATH + "/candidate/plan"
CANDIDATE_RUN_PATH = EVALUATOR_BASE_PATH + "/candidate/run"
_POST_PATHS = frozenset({REFERENCE_PLAN_PATH, REFERENCE_RUN_PATH, CANDIDATE_PLAN_PATH, CANDIDATE_RUN_PATH})


class BehaviorPrivateEvaluatorHttpError(ValueError):
    """An Owner request or evaluator response was not safe to apply."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _opaque_run_ref() -> str:
    return "benchmark-run:" + uuid.uuid4().hex


def _strict_string(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_payload_invalid")
    return value.strip()


def _fingerprint(client: PrivateEvaluatorClient) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
        raw = client.verification_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except Exception as exc:
        raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_verification_key_invalid") from exc
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _active_assistant(conn) -> str:
    assistant = current_assistant(conn) or {}
    identifier = str(assistant.get("id") or "").strip()
    if not identifier:
        raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_assistant_unavailable")
    return identifier


def _plan_binding(plan: Mapping[str, object], *, assistant_id: str, candidate: Mapping[str, object] | None = None) -> dict[str, object]:
    fields = (
        "benchmark_ref", "benchmark_hash", "reference_policy_ref", "evaluator_ref", "run_ref",
        "signing_key_ref", "public_key_fingerprint",
    )
    if not isinstance(plan, Mapping) or any(type(plan.get(field)) is not str or not str(plan[field]).strip() for field in fields):
        raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_plan_invalid")
    if str(plan.get("assistant_id") or "") != assistant_id:
        raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_plan_assistant_mismatch")
    binding: dict[str, object] = {
        "assistant_id": assistant_id,
        **{field: str(plan[field]) for field in fields},
    }
    if candidate is not None:
        candidate_ref = str(candidate.get("candidate_id") or "").strip()
        policy_bundle_hash = str(candidate.get("policy_bundle_hash") or "").strip()
        if not candidate_ref or not policy_bundle_hash:
            raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_candidate_invalid")
        if str(plan.get("candidate_ref") or "") != candidate_ref or str(plan.get("policy_bundle_hash") or "") != policy_bundle_hash:
            raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_candidate_plan_mismatch")
        binding.update(candidate_ref=candidate_ref, policy_bundle_hash=policy_bundle_hash)
    return binding


def _public_plan(plan: Mapping[str, object], *, candidate: bool) -> dict[str, object]:
    names = {
        "assistant_id", "benchmark_ref", "benchmark_hash", "reference_policy_ref", "evaluator_ref", "run_ref",
        "signing_key_ref", "public_key_fingerprint", "provider_ref", "model_ref", "thinking_ref", "case_count", "campaign_budget",
    }
    if candidate:
        names |= {"candidate_ref", "policy_bundle_hash"}
    return {name: plan[name] for name in sorted(names) if name in plan}


class BehaviorPrivateEvaluatorHttpApi:
    """Explicit Owner action surface; outer Bridge auth remains authoritative."""

    def __init__(self, db_connect: Callable, json_response: Callable, *, client_factory: Callable[[], PrivateEvaluatorClient] = PrivateEvaluatorClient.from_environment) -> None:
        self._db_connect = db_connect
        self._json_response = json_response
        self._client_factory = client_factory

    @staticmethod
    def matches_post(path: str) -> bool:
        return path in _POST_PATHS

    def _failure(self, request, exc: Exception) -> bool:
        if isinstance(exc, (BehaviorPrivateEvaluatorHttpError, BehaviorOwnerAuthorizationError, BehaviorBenchmarkRegistryError, BehaviorCandidateRegistryError)):
            error = str(exc) or "behavior_private_evaluator_invalid"
            status = 409 if "not_active" in error or "mismatch" in error or "already" in error or "not_offline" in error else 400
        elif isinstance(exc, PrivateEvaluatorClientError):
            error, status = "behavior_private_evaluator_unavailable", 503
        else:
            error, status = "behavior_private_evaluator_unavailable", 503
        self._json_response(request, status, {"ok": False, "error": error})
        return True

    def handle_get(self, request, path: str) -> bool:
        if path != EVALUATOR_BASE_PATH:
            return False
        try:
            client = self._client_factory()
            with self._db_connect() as conn:
                assistant_id = _active_assistant(conn)
            plan = client.reference_plan(assistant_id)
            if str(plan.get("evaluator_ref") or "") != client.evaluator_ref or str(plan.get("signing_key_ref") or "") != client.signing_key_ref:
                raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_identity_mismatch")
            if str(plan.get("public_key_fingerprint") or "") != _fingerprint(client):
                raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_identity_mismatch")
            result = {"state": "ready", "private_material_exposed": False, **_public_plan(plan, candidate=False)}
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: object) -> bool:
        if path not in _POST_PATHS:
            return False
        try:
            if type(payload) is not dict:
                raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_payload_invalid")
            expected = {
                REFERENCE_PLAN_PATH: set(),
                REFERENCE_RUN_PATH: {"authorization_ref", "run_ref"},
                CANDIDATE_PLAN_PATH: {"candidate_ref"},
                CANDIDATE_RUN_PATH: {"candidate_ref", "authorization_ref", "run_ref"},
            }[path]
            if set(payload) != expected:
                raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_payload_invalid")
            timestamp = _now()
            client = self._client_factory()
            fingerprint = _fingerprint(client)
            with self._db_connect() as conn:
                assistant_id = _active_assistant(conn)
                if path == REFERENCE_PLAN_PATH:
                    plan = {**client.reference_plan(assistant_id), "run_ref": _opaque_run_ref()}
                    binding = _plan_binding(plan, assistant_id=assistant_id)
                    if binding["public_key_fingerprint"] != fingerprint:
                        raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_identity_mismatch")
                    result = {"purpose": "reference_baseline_freeze", "authorization_binding": binding, "private_material_exposed": False}
                elif path == REFERENCE_RUN_PATH:
                    run_ref = _strict_string(payload["run_ref"])
                    plan = {**client.reference_plan(assistant_id), "run_ref": run_ref}
                    binding = _plan_binding(plan, assistant_id=assistant_id)
                    if binding["public_key_fingerprint"] != fingerprint:
                        raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_identity_mismatch")
                    preflight_behavior_owner_authorization(
                        conn, authorization_ref=_strict_string(payload["authorization_ref"]), purpose="reference_baseline_freeze",
                        assistant_id=assistant_id, binding=binding, now=timestamp,
                    )
                    receipt = client.run_reference(assistant_id, run_ref, timestamp)
                    result = record_reference_baseline(
                        conn, receipt, verification_key=client.verification_key, assistant_id=assistant_id,
                        owner_authorization_ref=_strict_string(payload["authorization_ref"]), received_at=timestamp,
                    )
                    result = {"state": result["state"], "benchmark_ref": result["benchmark_ref"], "benchmark_hash": result["benchmark_hash"], "private_material_exposed": False}
                else:
                    candidate = load_offline_candidate_for_private_evaluation(
                        conn, assistant_id=assistant_id, candidate_id=_strict_string(payload["candidate_ref"]),
                    )
                    if path == CANDIDATE_PLAN_PATH:
                        plan = client.candidate_plan(assistant_id, candidate, _opaque_run_ref())
                        binding = _plan_binding(plan, assistant_id=assistant_id, candidate=candidate)
                        if binding["public_key_fingerprint"] != fingerprint:
                            raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_identity_mismatch")
                        result = {"purpose": "private_candidate_evaluation", "authorization_binding": binding, "private_material_exposed": False}
                    else:
                        run_ref = _strict_string(payload["run_ref"])
                        plan = client.candidate_plan(assistant_id, candidate, run_ref)
                        binding = _plan_binding(plan, assistant_id=assistant_id, candidate=candidate)
                        if binding["public_key_fingerprint"] != fingerprint:
                            raise BehaviorPrivateEvaluatorHttpError("behavior_private_evaluator_identity_mismatch")
                        authorization_ref = _strict_string(payload["authorization_ref"])
                        preflight_behavior_owner_authorization(
                            conn, authorization_ref=authorization_ref, purpose="private_candidate_evaluation",
                            assistant_id=assistant_id, binding=binding, now=timestamp,
                        )
                        receipt = client.run_candidate(assistant_id, candidate, authorization_ref, run_ref, timestamp)
                        result = receive_signed_candidate_evaluation(
                            conn, receipt, verification_key_resolver=lambda key_ref: client.verification_key if key_ref == client.signing_key_ref else None,
                            received_at=timestamp,
                        )
                        result = {"state": result["state"], "candidate_ref": result["candidate_id"], "private_material_exposed": False}
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = [
    "BehaviorPrivateEvaluatorHttpApi",
    "CANDIDATE_PLAN_PATH",
    "CANDIDATE_RUN_PATH",
    "EVALUATOR_BASE_PATH",
    "REFERENCE_PLAN_PATH",
    "REFERENCE_RUN_PATH",
]
