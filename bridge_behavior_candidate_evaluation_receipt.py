#!/usr/bin/env python3
"""Asymmetric, body-free BE-4 Candidate Evaluation receipts.

The private evaluator signs this compact aggregate with an Ed25519 *private*
key.  The Bridge receives only the receipt and a resolved public key.  It
therefore never needs a signing secret, private Benchmark material, Gold,
Rubric, prompts, or model output in order to verify a cross-process result.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import base64
import json
import math
import re

try:  # Deployment dependency: absent crypto must disable this receiver, not Bridge startup.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:  # pragma: no cover - exercised by production dependency preflight.
    InvalidSignature = ValueError
    Ed25519PrivateKey = None  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment,misc]


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_ASSISTANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,191}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_BODY_FIELD = re.compile(
    r"(?:^|_)(?:body|content|gold|message|prompt|raw|response|rubric|scenario|text)(?:$|_)"
)
_METRICS = frozenset(
    {
        "total_score", "reply_obligation_recall", "ambient_intrusion_rate", "citation_completeness",
        "mean_cost_microunits", "p95_latency_ms", "mean_output_tokens",
    }
)
_BUDGET = frozenset({"max_mean_cost_microunits", "max_p95_latency_ms", "max_mean_output_tokens"})


class BehaviorCandidateEvaluationReceiptError(ValueError):
    """A Candidate Evaluation receipt was malformed, leaked, or unsigned."""


def _require_crypto() -> None:
    if Ed25519PrivateKey is None or Ed25519PublicKey is None:
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_crypto_unavailable")


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _ref(value: object, *, error: str) -> str:
    result = str(value or "").strip()
    if not _REF_RE.fullmatch(result):
        raise BehaviorCandidateEvaluationReceiptError(error)
    return result


def _hash(value: object, *, error: str) -> str:
    result = str(value or "").strip()
    if not _HASH_RE.fullmatch(result):
        raise BehaviorCandidateEvaluationReceiptError(error)
    return result


def _assistant_id(value: object, *, error: str) -> str:
    result = str(value or "").strip()
    if not _ASSISTANT_ID_RE.fullmatch(result):
        raise BehaviorCandidateEvaluationReceiptError(error)
    return result


def _utc(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_time_invalid") from exc
    if parsed.tzinfo is None:
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_time_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def _assert_body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _BODY_FIELD.search(str(key or "").strip().lower()):
                raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_body_free_violation")
            _assert_body_free(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_body_free(nested)


def _metrics(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != _METRICS:
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_metrics_invalid")
    result: dict[str, float] = {}
    for name in sorted(_METRICS):
        if isinstance(value[name], bool):
            raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_metrics_invalid")
        try:
            amount = float(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_metrics_invalid") from exc
        if not math.isfinite(amount) or amount < 0:
            raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_metrics_invalid")
        result[name] = amount
    return result


def _budget(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _BUDGET:
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_budget_invalid")
    result: dict[str, int] = {}
    for name in sorted(_BUDGET):
        if isinstance(value[name], bool):
            raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_budget_invalid")
        try:
            amount = int(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_budget_invalid") from exc
        if amount < 1 or str(amount) != str(value[name]).strip():
            raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_budget_invalid")
        result[name] = amount
    return result


def _count(value: object, *, name: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_invalid")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_invalid") from exc
    if number < minimum or str(number) != str(value).strip():
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_invalid")
    return number


def _payload_from_evaluation(
    evaluation: object,
    *,
    assistant_id: object,
    owner_approval_ref: object,
    owner_authorization_ref: object,
    evaluator_ref: object,
    run_ref: object,
    signing_key_ref: object,
    completed_at: object,
) -> dict[str, object]:
    fields = {
        "candidate_ref", "reference_policy_ref", "policy_bundle_hash", "benchmark_ref", "benchmark_hash",
        "provider_ref", "model_ref", "thinking_ref", "case_count", "runs_per_case", "campaign_budget",
        "candidate_metrics", "hard_failure_count",
    }
    if any(not hasattr(evaluation, name) for name in fields):
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_invalid")
    return {
        "schema_version": 2,
        "receipt_kind": "candidate_evaluation",
        "run_ref": _ref(run_ref, error="behavior_candidate_receipt_ref_invalid"),
        "evaluator_ref": _ref(evaluator_ref, error="behavior_candidate_receipt_ref_invalid"),
        "signing_key_ref": _ref(signing_key_ref, error="behavior_candidate_receipt_ref_invalid"),
        "completed_at": _utc(completed_at),
        "assistant_id": _assistant_id(assistant_id, error="behavior_candidate_receipt_identity_mismatch"),
        "owner_authorization_ref": _ref(owner_authorization_ref, error="behavior_candidate_receipt_identity_mismatch"),
        "candidate": {
            "candidate_ref": _ref(getattr(evaluation, "candidate_ref"), error="behavior_candidate_receipt_identity_mismatch"),
            "reference_policy_ref": _ref(getattr(evaluation, "reference_policy_ref"), error="behavior_candidate_receipt_identity_mismatch"),
            "policy_bundle_hash": _hash(getattr(evaluation, "policy_bundle_hash"), error="behavior_candidate_receipt_identity_mismatch"),
        },
        "benchmark": {
            "benchmark_ref": _ref(getattr(evaluation, "benchmark_ref"), error="behavior_candidate_receipt_identity_mismatch"),
            "benchmark_hash": _hash(getattr(evaluation, "benchmark_hash"), error="behavior_candidate_receipt_identity_mismatch"),
            "owner_approval_ref": _ref(owner_approval_ref, error="behavior_candidate_receipt_identity_mismatch"),
            "provider_ref": _ref(getattr(evaluation, "provider_ref"), error="behavior_candidate_receipt_identity_mismatch"),
            "model_ref": _ref(getattr(evaluation, "model_ref"), error="behavior_candidate_receipt_identity_mismatch"),
            "thinking_ref": _ref(getattr(evaluation, "thinking_ref"), error="behavior_candidate_receipt_identity_mismatch"),
            "case_count": _count(getattr(evaluation, "case_count"), name="case_count", minimum=1),
            "campaign_budget": _budget(getattr(evaluation, "campaign_budget")),
        },
        "summary": {
            "runs_per_case": _count(getattr(evaluation, "runs_per_case"), name="runs_per_case", minimum=3),
            "candidate_metrics": _metrics(getattr(evaluation, "candidate_metrics")),
            "hard_failure_count": _count(getattr(evaluation, "hard_failure_count"), name="hard_failure_count", minimum=0),
        },
    }


def sign_candidate_evaluation_receipt(
    evaluation: object,
    *,
    assistant_id: object,
    owner_approval_ref: object,
    owner_authorization_ref: object,
    evaluator_ref: object,
    run_ref: object,
    signing_key_ref: object,
    signing_key: object,
    completed_at: object,
) -> dict[str, object]:
    """Sign an evaluator-only aggregate; no private material is serialized."""

    _require_crypto()
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_key_invalid")
    payload = _payload_from_evaluation(
        evaluation,
        assistant_id=assistant_id,
        owner_approval_ref=owner_approval_ref,
        owner_authorization_ref=owner_authorization_ref,
        evaluator_ref=evaluator_ref,
        run_ref=run_ref,
        signing_key_ref=signing_key_ref,
        completed_at=completed_at,
    )
    signature = base64.urlsafe_b64encode(signing_key.sign(_canonical(payload))).decode("ascii").rstrip("=")
    return {**payload, "signature": signature}


def verify_candidate_evaluation_receipt(
    value: Mapping[str, object],
    *,
    verification_key: object,
) -> dict[str, object]:
    """Verify an opaque receipt with a public key, then normalize its facts."""

    _require_crypto()
    if not isinstance(verification_key, Ed25519PublicKey):
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_verification_key_invalid")
    if not isinstance(value, Mapping):
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_invalid")
    _assert_body_free(value)
    expected = {
        "schema_version", "receipt_kind", "run_ref", "evaluator_ref", "signing_key_ref", "completed_at", "assistant_id", "owner_authorization_ref",
        "candidate", "benchmark", "summary", "signature",
    }
    if set(value) != expected or value.get("schema_version") != 2 or value.get("receipt_kind") != "candidate_evaluation":
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_invalid")
    signature = str(value.get("signature") or "")
    if not _SIGNATURE_RE.fullmatch(signature):
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_signature_invalid")
    try:
        raw_signature = base64.urlsafe_b64decode(signature + "==")
        verification_key.verify(raw_signature, _canonical({key: value[key] for key in expected - {"signature"}}))
    except (ValueError, InvalidSignature) as exc:
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_signature_invalid") from exc
    candidate = value.get("candidate")
    benchmark = value.get("benchmark")
    summary = value.get("summary")
    if (
        not isinstance(candidate, Mapping) or set(candidate) != {"candidate_ref", "reference_policy_ref", "policy_bundle_hash"}
        or not isinstance(benchmark, Mapping) or set(benchmark) != {
            "benchmark_ref", "benchmark_hash", "owner_approval_ref", "provider_ref", "model_ref", "thinking_ref",
            "case_count", "campaign_budget",
        }
        or not isinstance(summary, Mapping) or set(summary) != {"runs_per_case", "candidate_metrics", "hard_failure_count"}
    ):
        raise BehaviorCandidateEvaluationReceiptError("behavior_candidate_receipt_invalid")
    return {
        "run_ref": _ref(value.get("run_ref"), error="behavior_candidate_receipt_ref_invalid"),
        "evaluator_ref": _ref(value.get("evaluator_ref"), error="behavior_candidate_receipt_ref_invalid"),
        "signing_key_ref": _ref(value.get("signing_key_ref"), error="behavior_candidate_receipt_ref_invalid"),
        "completed_at": _utc(value.get("completed_at")),
        "assistant_id": _assistant_id(value.get("assistant_id"), error="behavior_candidate_receipt_identity_mismatch"),
        "owner_authorization_ref": _ref(value.get("owner_authorization_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "candidate_ref": _ref(candidate.get("candidate_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "reference_policy_ref": _ref(candidate.get("reference_policy_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "policy_bundle_hash": _hash(candidate.get("policy_bundle_hash"), error="behavior_candidate_receipt_identity_mismatch"),
        "benchmark_ref": _ref(benchmark.get("benchmark_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "benchmark_hash": _hash(benchmark.get("benchmark_hash"), error="behavior_candidate_receipt_identity_mismatch"),
        "owner_approval_ref": _ref(benchmark.get("owner_approval_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "provider_ref": _ref(benchmark.get("provider_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "model_ref": _ref(benchmark.get("model_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "thinking_ref": _ref(benchmark.get("thinking_ref"), error="behavior_candidate_receipt_identity_mismatch"),
        "case_count": _count(benchmark.get("case_count"), name="case_count", minimum=1),
        "campaign_budget": _budget(benchmark.get("campaign_budget")),
        "runs_per_case": _count(summary.get("runs_per_case"), name="runs_per_case", minimum=3),
        "candidate_metrics": _metrics(summary.get("candidate_metrics")),
        "hard_failure_count": _count(summary.get("hard_failure_count"), name="hard_failure_count", minimum=0),
    }


__all__ = [
    "BehaviorCandidateEvaluationReceiptError",
    "sign_candidate_evaluation_receipt",
    "verify_candidate_evaluation_receipt",
]
