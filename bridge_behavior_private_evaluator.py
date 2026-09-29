#!/usr/bin/env python3
"""In-memory BE-4 evaluator that exposes only a signed body-free receipt.

This module is intended for the separately operated private evaluator process.
It receives private benchmark material and temporary model replies in memory,
then discards both after converting every repeated case into aggregate metrics.
No SQLite, Delivery, Knowledge, Memory, Task or Approval dependency is allowed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import math
import weakref

from bridge_behavior_benchmark_contract import PrivateBenchmarkVault, run_reference_baseline
from bridge_behavior_benchmark_receipt import sign_reference_baseline_receipt
from bridge_behavior_candidate_evaluation_receipt import sign_candidate_evaluation_receipt
from bridge_behavior_evolution_contract import BE_HARD_FAILURE_CLASSES, make_policy_bundle_candidate


_RUNNER_FIELDS = frozenset({"ok", "output", "cost_microunits", "latency_ms", "output_tokens"})
_JUDGMENT_FIELDS = frozenset(
    {"total_score", "reply_obligation_hit", "ambient_intrusion", "citation_complete", "hard_failure_classes"},
)
_METRIC_NAMES = (
    "total_score",
    "reply_obligation_recall",
    "ambient_intrusion_rate",
    "citation_completeness",
    "mean_cost_microunits",
    "p95_latency_ms",
    "mean_output_tokens",
)


class PrivateBehaviorEvaluatorError(RuntimeError):
    """The private evaluator could not produce a complete, safe aggregate."""


class _EvaluationProvenance:
    """An in-process, non-serializable capability minted only by this evaluator."""


_EVALUATION_PROVENANCE: weakref.WeakSet[_EvaluationProvenance] = weakref.WeakSet()


@dataclass(frozen=True)
class OfflineCandidateEvaluation:
    """Body-free candidate aggregate emitted by the privileged evaluator only.

    The hidden provenance capability is deliberately neither serializable nor
    accepted from callers.  It prevents the control-plane optimizer from
    accepting a hand-constructed score object in this process.  It is not a
    replacement for process isolation: a cross-process deployment must invoke
    the evaluator's decision endpoint rather than deserialize this object.
    """

    candidate_ref: str
    reference_policy_ref: str
    policy_bundle_hash: str
    benchmark_ref: str
    benchmark_hash: str
    provider_ref: str
    model_ref: str
    thinking_ref: str
    case_count: int
    runs_per_case: int
    campaign_budget: dict[str, int]
    candidate_metrics: dict[str, float]
    hard_failure_count: int
    _provenance: _EvaluationProvenance | None = field(default=None, init=False, repr=False, compare=False)


def is_private_candidate_evaluation(value: object) -> bool:
    """Whether an evaluation was minted in this evaluator process.

    No private benchmark case, Gold, Rubric, temporary answer, or signing key
    is exposed by this check.
    """

    return (
        isinstance(value, OfflineCandidateEvaluation)
        and isinstance(value._provenance, _EvaluationProvenance)
        and value._provenance in _EVALUATION_PROVENANCE
    )


def _model_result(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _RUNNER_FIELDS or value.get("ok") is not True:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_model_failed")
    output = value.get("output")
    if not isinstance(output, str) or not output.strip() or len(output.encode("utf-8")) > 65536:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_model_failed")
    result: dict[str, object] = {"output": output}
    for name in ("cost_microunits", "latency_ms", "output_tokens"):
        try:
            amount = int(value[name])
        except (TypeError, ValueError) as exc:
            raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_usage_invalid") from exc
        if amount < 0:
            raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_usage_invalid")
        result[name] = amount
    return result


def _judgment(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_judgment_invalid") from exc
    if not isinstance(parsed, Mapping) or set(parsed) != _JUDGMENT_FIELDS:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_judgment_invalid")
    try:
        score = float(parsed["total_score"])
    except (TypeError, ValueError) as exc:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_judgment_invalid") from exc
    if not 0 <= score <= 100:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_judgment_invalid")
    booleans = ("reply_obligation_hit", "ambient_intrusion", "citation_complete")
    if any(type(parsed[name]) is not bool for name in booleans):
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_judgment_invalid")
    failures = parsed["hard_failure_classes"]
    if (
        not isinstance(failures, list)
        or any(str(item) not in BE_HARD_FAILURE_CLASSES for item in failures)
        or len(set(failures)) != len(failures)
    ):
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_judgment_invalid")
    return {
        "total_score": score,
        "reply_obligation_hit": bool(parsed["reply_obligation_hit"]),
        "ambient_intrusion": bool(parsed["ambient_intrusion"]),
        "citation_complete": bool(parsed["citation_complete"]),
        "hard_failure_classes": list(failures),
    }


def _reference_prompt(case: Mapping[str, object], identity: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "kind": "behavior_reference_response",
            "reference_policy_ref": identity["reference_policy_ref"],
            "scenario": case["scenario"],
            "instruction": "Generate the Reference assistant response for this private benchmark case only.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _judge_prompt(case: Mapping[str, object], response: str, identity: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "kind": "behavior_private_judge",
            "reference_policy_ref": identity["reference_policy_ref"],
            "scenario": case["scenario"],
            "gold": case["gold"],
            "rubric": case["rubric"],
            "response": response,
            "output_schema": {
                "total_score": "number 0..100",
                "reply_obligation_hit": "boolean",
                "ambient_intrusion": "boolean",
                "citation_complete": "boolean",
                "hard_failure_classes": sorted(BE_HARD_FAILURE_CLASSES),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _candidate_prompt(
    case: Mapping[str, object],
    candidate: Mapping[str, object],
    identity: Mapping[str, str],
) -> str:
    """Build a private, transient candidate prompt without persistence."""

    return json.dumps(
        {
            "kind": "behavior_candidate_response",
            "reference_policy_ref": identity["reference_policy_ref"],
            "candidate_policy_bundle_hash": candidate["policy_bundle_hash"],
            "policy_patches": candidate["patches"],
            "scenario": case["scenario"],
            "instruction": "Generate the Candidate assistant response for this private benchmark case only.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _checked_metrics(value: Mapping[str, object]) -> dict[str, float | int]:
    """Reject non-finite aggregate inputs before they become a receipt."""

    result: dict[str, float | int] = {}
    for name in _METRIC_NAMES:
        try:
            metric = float(value[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_metrics_invalid") from exc
        if not math.isfinite(metric) or metric < 0:
            raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_metrics_invalid")
        result[name] = metric
    try:
        hard_failures = int(value["hard_failure_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_metrics_invalid") from exc
    if hard_failures < 0:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_metrics_invalid")
    result["hard_failure_count"] = hard_failures
    return result


def _aggregate_candidate_runs(
    vault: PrivateBenchmarkVault,
    run_model: Callable[[str, str, Mapping[str, str]], Mapping[str, object]],
    candidate: Mapping[str, object],
    *,
    repetitions: int,
) -> OfflineCandidateEvaluation:
    if not isinstance(vault, PrivateBenchmarkVault) or not vault._private_cases:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_vault_unavailable")
    if not callable(run_model):
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_invalid")
    if not isinstance(repetitions, int) or repetitions < 3:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_repetitions_invalid")

    normalized_candidate = make_policy_bundle_candidate(candidate)
    frozen = vault.frozen
    if normalized_candidate["reference_policy_ref"] != frozen.reference_policy_ref:
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_candidate_reference_mismatch")

    identity = {
        "provider_ref": frozen.provider_ref,
        "model_ref": frozen.model_ref,
        "thinking_ref": frozen.thinking_ref,
        "reference_policy_ref": frozen.reference_policy_ref,
    }
    totals = {name: 0.0 for name in _METRIC_NAMES}
    latency_samples: list[float] = []
    hard_failure_count = 0
    run_count = 0
    for case in vault._private_cases:
        for _ in range(repetitions):
            candidate_result = _model_result(
                run_model("candidate", _candidate_prompt(case, normalized_candidate, identity), dict(identity))
            )
            judge_result = _model_result(
                run_model("judge", _judge_prompt(case, str(candidate_result["output"]), identity), dict(identity))
            )
            assessment = _judgment(judge_result["output"])
            metrics = _checked_metrics(
                {
                    "total_score": assessment["total_score"],
                    "reply_obligation_recall": 1.0 if assessment["reply_obligation_hit"] else 0.0,
                    "ambient_intrusion_rate": 1.0 if assessment["ambient_intrusion"] else 0.0,
                    "citation_completeness": 1.0 if assessment["citation_complete"] else 0.0,
                    "mean_cost_microunits": int(candidate_result["cost_microunits"])
                    + int(judge_result["cost_microunits"]),
                    "p95_latency_ms": int(candidate_result["latency_ms"]) + int(judge_result["latency_ms"]),
                    "mean_output_tokens": int(candidate_result["output_tokens"]),
                    "hard_failure_count": len(assessment["hard_failure_classes"]),
                }
            )
            for name in _METRIC_NAMES:
                totals[name] += float(metrics[name])
            latency_samples.append(float(metrics["p95_latency_ms"]))
            hard_failure_count += int(metrics["hard_failure_count"])
            run_count += 1

    evaluation = OfflineCandidateEvaluation(
        candidate_ref=str(normalized_candidate["candidate_id"]),
        reference_policy_ref=str(normalized_candidate["reference_policy_ref"]),
        policy_bundle_hash=str(normalized_candidate["policy_bundle_hash"]),
        benchmark_ref=frozen.benchmark_ref,
        benchmark_hash=frozen.benchmark_hash,
        provider_ref=frozen.provider_ref,
        model_ref=frozen.model_ref,
        thinking_ref=frozen.thinking_ref,
        case_count=frozen.case_count,
        runs_per_case=repetitions,
        campaign_budget=dict(frozen.campaign_budget),
        candidate_metrics={
            name: (
                sorted(latency_samples)[max(0, math.ceil(0.95 * len(latency_samples)) - 1)]
                if name == "p95_latency_ms"
                else totals[name] / run_count
            )
            for name in _METRIC_NAMES
        },
        hard_failure_count=hard_failure_count,
    )
    provenance = _EvaluationProvenance()
    _EVALUATION_PROVENANCE.add(provenance)
    object.__setattr__(evaluation, "_provenance", provenance)
    return evaluation


def run_private_reference_evaluation(
    vault: PrivateBenchmarkVault,
    run_model: Callable[[str, str, Mapping[str, str]], Mapping[str, object]],
    *,
    evaluator_ref: object,
    run_ref: object,
    signing_key_ref: object,
    signing_key: bytes,
    completed_at: object,
    repetitions: int = 3,
) -> dict[str, object]:
    """Run the frozen Reference and private Judge without retaining their bodies."""

    if not isinstance(vault, PrivateBenchmarkVault) or not callable(run_model):
        raise PrivateBehaviorEvaluatorError("behavior_private_evaluator_invalid")

    def runner(case: Mapping[str, object], identity: Mapping[str, str]) -> Mapping[str, object]:
        reference = _model_result(run_model("reference", _reference_prompt(case, identity), identity))
        judge = _model_result(run_model("judge", _judge_prompt(case, str(reference["output"]), identity), identity))
        assessment = _judgment(judge["output"])
        return {
            "total_score": assessment["total_score"],
            "reply_obligation_recall": 1.0 if assessment["reply_obligation_hit"] else 0.0,
            "ambient_intrusion_rate": 1.0 if assessment["ambient_intrusion"] else 0.0,
            "citation_completeness": 1.0 if assessment["citation_complete"] else 0.0,
            "mean_cost_microunits": int(reference["cost_microunits"]) + int(judge["cost_microunits"]),
            "p95_latency_ms": int(reference["latency_ms"]) + int(judge["latency_ms"]),
            "mean_output_tokens": int(reference["output_tokens"]),
            "hard_failure_count": len(assessment["hard_failure_classes"]),
        }

    summary = run_reference_baseline(vault, runner, repetitions=repetitions)
    return sign_reference_baseline_receipt(
        vault.frozen,
        summary,
        evaluator_ref=evaluator_ref,
        run_ref=run_ref,
        signing_key_ref=signing_key_ref,
        signing_key=signing_key,
        completed_at=completed_at,
    )


def run_private_candidate_evaluation(
    vault: PrivateBenchmarkVault,
    run_model: Callable[[str, str, Mapping[str, str]], Mapping[str, object]],
    candidate: Mapping[str, object],
    *,
    repetitions: int = 3,
) -> OfflineCandidateEvaluation:
    """Evaluate one candidate wholly inside the private evaluator.

    The only return value is a repeated, aggregate, body-free evaluation.  No
    case, Gold, Rubric, model reply, prompt, signing key, SQLite handle, or
    delivery-capable dependency crosses this API.
    """

    return _aggregate_candidate_runs(vault, run_model, candidate, repetitions=repetitions)


def run_private_candidate_evaluation_receipt(
    vault: PrivateBenchmarkVault,
    run_model: Callable[[str, str, Mapping[str, str]], Mapping[str, object]],
    candidate: Mapping[str, object],
    *,
    assistant_id: object,
    owner_authorization_ref: object,
    evaluator_ref: object,
    run_ref: object,
    signing_key_ref: object,
    signing_key: object,
    completed_at: object,
    repetitions: int = 3,
) -> dict[str, object]:
    """Evaluate and sign one Candidate wholly inside the private process.

    The only serialized result is an Ed25519-signed, body-free aggregate.  The
    Bridge receives a public verification key through its separately managed
    key resolver; it never receives ``signing_key``.
    """

    evaluation = run_private_candidate_evaluation(vault, run_model, candidate, repetitions=repetitions)
    return sign_candidate_evaluation_receipt(
        evaluation,
        assistant_id=assistant_id,
        owner_approval_ref=vault.frozen.owner_approval_ref,
        owner_authorization_ref=owner_authorization_ref,
        evaluator_ref=evaluator_ref,
        run_ref=run_ref,
        signing_key_ref=signing_key_ref,
        signing_key=signing_key,
        completed_at=completed_at,
    )


__all__ = [
    "OfflineCandidateEvaluation",
    "PrivateBehaviorEvaluatorError",
    "is_private_candidate_evaluation",
    "run_private_candidate_evaluation",
    "run_private_candidate_evaluation_receipt",
    "run_private_reference_evaluation",
]
