#!/usr/bin/env python3
"""Signed, body-free receipts emitted by the private BE-4 evaluator.

The private evaluator may hold cases, Gold answers and Rubrics in memory, but
the Bridge can only accept this compact HMAC-authenticated receipt.  The
signing key is supplied by the evaluator deployment and is never serialized.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import base64
import json
import re

try:  # Missing crypto must close BE-4 receipt intake, not prevent Bridge startup.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:  # pragma: no cover - covered by isolated dependency preflight.
    InvalidSignature = ValueError
    Ed25519PrivateKey = None  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment,misc]

from bridge_behavior_benchmark_contract import BenchmarkSummary, FrozenBenchmark


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_METRICS = frozenset(
    {
        "total_score", "reply_obligation_recall", "ambient_intrusion_rate", "citation_completeness",
        "mean_cost_microunits", "p95_latency_ms", "mean_output_tokens",
    },
)
_PRIVATE_OR_BODY_FIELD = re.compile(
    r"(?:^|_)(?:body|content|gold|message|prompt|raw|response|rubric|scenario|text)(?:$|_)",
)


class BehaviorBenchmarkReceiptError(ValueError):
    """A private-evaluator receipt was malformed, leaked content or was unsigned."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise BehaviorBenchmarkReceiptError(error)
    return text


def _utc(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_time_invalid") from exc
    if parsed.tzinfo is None:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_time_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def _require_crypto() -> None:
    if Ed25519PrivateKey is None or Ed25519PublicKey is None:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_crypto_unavailable")


def _assert_body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key or "").strip().lower()
            if _PRIVATE_OR_BODY_FIELD.search(name):
                raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_body_free_violation")
            _assert_body_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_body_free(item)


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _metrics(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != _METRICS:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_metrics_invalid")
    result: dict[str, float] = {}
    for name in sorted(_METRICS):
        try:
            metric = float(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_metrics_invalid") from exc
        if metric < 0:
            raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_metrics_invalid")
        result[name] = metric
    return result


def _campaign_budget(value: object) -> dict[str, int]:
    expected = {"max_mean_cost_microunits", "max_p95_latency_ms", "max_mean_output_tokens"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_budget_invalid")
    result: dict[str, int] = {}
    for name in sorted(expected):
        try:
            amount = int(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_budget_invalid") from exc
        if amount < 1:
            raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_budget_invalid")
        result[name] = amount
    return result


def _payload_from_models(
    frozen: FrozenBenchmark,
    summary: BenchmarkSummary,
    *,
    evaluator_ref: object,
    run_ref: object,
    signing_key_ref: object,
    completed_at: object,
) -> dict[str, object]:
    if not isinstance(frozen, FrozenBenchmark) or not isinstance(summary, BenchmarkSummary):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid")
    if (
        summary.benchmark_ref != frozen.benchmark_ref
        or summary.benchmark_hash != frozen.benchmark_hash
        or summary.provider_ref != frozen.provider_ref
        or summary.model_ref != frozen.model_ref
        or summary.thinking_ref != frozen.thinking_ref
        or summary.reference_policy_ref != frozen.reference_policy_ref
        or summary.case_count != frozen.case_count
        or summary.runs_per_case < 3
    ):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_identity_mismatch")
    try:
        hard_failures = int(summary.hard_failure_count)
    except (TypeError, ValueError) as exc:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid") from exc
    if hard_failures < 0:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid")
    return {
        "schema_version": 2,
        "receipt_kind": "reference_baseline",
        "run_ref": _ref(run_ref, error="behavior_benchmark_receipt_ref_invalid"),
        "evaluator_ref": _ref(evaluator_ref, error="behavior_benchmark_receipt_ref_invalid"),
        "signing_key_ref": _ref(signing_key_ref, error="behavior_benchmark_receipt_ref_invalid"),
        "completed_at": _utc(completed_at),
        "benchmark": {
            "benchmark_ref": _ref(frozen.benchmark_ref, error="behavior_benchmark_receipt_identity_mismatch"),
            "benchmark_hash": str(frozen.benchmark_hash),
            "owner_approval_ref": _ref(frozen.owner_approval_ref, error="behavior_benchmark_receipt_identity_mismatch"),
            "provider_ref": _ref(frozen.provider_ref, error="behavior_benchmark_receipt_identity_mismatch"),
            "model_ref": _ref(frozen.model_ref, error="behavior_benchmark_receipt_identity_mismatch"),
            "thinking_ref": _ref(frozen.thinking_ref, error="behavior_benchmark_receipt_identity_mismatch"),
            "reference_policy_ref": _ref(frozen.reference_policy_ref, error="behavior_benchmark_receipt_identity_mismatch"),
            "case_count": int(frozen.case_count),
            "campaign_budget": _campaign_budget(frozen.campaign_budget),
        },
        "summary": {
            "runs_per_case": int(summary.runs_per_case),
            "reference_metrics": _metrics(summary.reference_metrics),
            "hard_failure_count": hard_failures,
        },
    }


def sign_reference_baseline_receipt(
    frozen: FrozenBenchmark,
    summary: BenchmarkSummary,
    *,
    evaluator_ref: object,
    run_ref: object,
    signing_key_ref: object,
    signing_key: bytes,
    completed_at: object,
) -> dict[str, object]:
    """Create a verifier-safe receipt inside the private evaluator process."""

    payload = _payload_from_models(
        frozen,
        summary,
        evaluator_ref=evaluator_ref,
        run_ref=run_ref,
        signing_key_ref=signing_key_ref,
        completed_at=completed_at,
    )
    _require_crypto()
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_key_invalid")
    signature = base64.urlsafe_b64encode(signing_key.sign(_canonical(payload))).decode("ascii").rstrip("=")
    return {**payload, "signature": signature}


def verify_reference_baseline_receipt(value: Mapping[str, object], *, verification_key: object) -> dict[str, object]:
    """Verify a complete opaque receipt before it can cross into the Bridge."""

    _require_crypto()
    if not isinstance(verification_key, Ed25519PublicKey):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_verification_key_invalid")
    if not isinstance(value, Mapping):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid")
    _assert_body_free(value)
    expected = {
        "schema_version", "receipt_kind", "run_ref", "evaluator_ref", "signing_key_ref", "completed_at",
        "benchmark", "summary", "signature",
    }
    if set(value) != expected or value.get("schema_version") != 2 or value.get("receipt_kind") != "reference_baseline":
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid")
    signature = str(value.get("signature") or "")
    if not _SIGNATURE_RE.fullmatch(signature):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_signature_invalid")
    payload = {key: value[key] for key in expected - {"signature"}}
    try:
        verification_key.verify(base64.urlsafe_b64decode(signature + "=="), _canonical(payload))
    except (ValueError, InvalidSignature) as exc:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_signature_invalid")
    benchmark = value.get("benchmark")
    summary = value.get("summary")
    if not isinstance(benchmark, Mapping) or not isinstance(summary, Mapping):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid")
    if set(benchmark) != {
        "benchmark_ref", "benchmark_hash", "owner_approval_ref", "provider_ref", "model_ref", "thinking_ref",
        "reference_policy_ref", "case_count",
        "campaign_budget",
    } or set(summary) != {"runs_per_case", "reference_metrics", "hard_failure_count"}:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid")
    try:
        case_count = int(benchmark["case_count"])
        repetitions = int(summary["runs_per_case"])
        hard_failures = int(summary["hard_failure_count"])
    except (TypeError, ValueError) as exc:
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid") from exc
    if case_count < 1 or repetitions < 3 or hard_failures < 0 or not str(benchmark.get("benchmark_hash") or "").startswith("sha256:"):
        raise BehaviorBenchmarkReceiptError("behavior_benchmark_receipt_invalid")
    return {
        "run_ref": _ref(value.get("run_ref"), error="behavior_benchmark_receipt_ref_invalid"),
        "evaluator_ref": _ref(value.get("evaluator_ref"), error="behavior_benchmark_receipt_ref_invalid"),
        "signing_key_ref": _ref(value.get("signing_key_ref"), error="behavior_benchmark_receipt_ref_invalid"),
        "completed_at": _utc(value.get("completed_at")),
        "benchmark_ref": _ref(benchmark.get("benchmark_ref"), error="behavior_benchmark_receipt_identity_mismatch"),
        "benchmark_hash": str(benchmark["benchmark_hash"]),
        "owner_approval_ref": _ref(benchmark.get("owner_approval_ref"), error="behavior_benchmark_receipt_identity_mismatch"),
        "provider_ref": _ref(benchmark.get("provider_ref"), error="behavior_benchmark_receipt_identity_mismatch"),
        "model_ref": _ref(benchmark.get("model_ref"), error="behavior_benchmark_receipt_identity_mismatch"),
        "thinking_ref": _ref(benchmark.get("thinking_ref"), error="behavior_benchmark_receipt_identity_mismatch"),
        "reference_policy_ref": _ref(benchmark.get("reference_policy_ref"), error="behavior_benchmark_receipt_identity_mismatch"),
        "case_count": case_count,
        "campaign_budget": _campaign_budget(benchmark.get("campaign_budget")),
        "runs_per_case": repetitions,
        "reference_metrics": _metrics(summary.get("reference_metrics")),
        "hard_failure_count": hard_failures,
    }


__all__ = [
    "BehaviorBenchmarkReceiptError",
    "sign_reference_baseline_receipt",
    "verify_reference_baseline_receipt",
]
