#!/usr/bin/env python3
"""BE-5 offline-only optimizer over the BE-1 whitelisted policy contracts.

This module may create candidates and consume a body-free decision, but it is
not a benchmark runner.  Private cases, Gold, Rubrics, temporary replies and
signing material are confined to ``bridge_behavior_private_evaluator``.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import re

from bridge_behavior_benchmark_contract import (
    BenchmarkSummary,
)
from bridge_behavior_evolution_contract import (
    BehaviorEvolutionContractError,
    assert_optimizer_isolated,
    evaluate_shadow_admission,
    make_policy_bundle_candidate,
)
from bridge_behavior_private_evaluator import OfflineCandidateEvaluation, is_private_candidate_evaluation


_OPTIMIZER_INPUT_KEYS = frozenset(
    {
        "candidate_id",
        "reference_policy_ref",
        "observation_cluster_refs",
        "policy_contract_schemas",
        "reference_metrics",
        "proposed_patches",
    }
)
_FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "benchmark_case_ref",
        "gold_answer_ref",
        "permission_manifest_ref",
        "private_rubric_ref",
        "production_config_ref",
        "source_code_ref",
    }
)
_PRIVATE_MATERIAL_KEYS = frozenset(
    {
        "benchmark_case_ref",
        "case",
        "case_ref",
        "case_set",
        "gold",
        "gold_answer",
        "gold_answer_ref",
        "private_cases",
        "private_rubric",
        "private_rubric_ref",
        "rubric",
        "scenario",
        "signing_key",
        "signing_key_ref",
    }
)
_METRICS = frozenset(
    {
        "total_score",
        "reply_obligation_recall",
        "ambient_intrusion_rate",
        "citation_completeness",
        "mean_cost_microunits",
        "p95_latency_ms",
        "mean_output_tokens",
    }
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAMPAIGN_BUDGET_KEYS = frozenset(
    {"max_mean_cost_microunits", "max_p95_latency_ms", "max_mean_output_tokens"}
)


def _assert_private_material_free(value: object) -> None:
    """Keep private benchmark material out of optimizer-owned candidates."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key or "").strip().lower() in _PRIVATE_MATERIAL_KEYS:
                raise BehaviorEvolutionContractError("behavior_optimizer_isolation_violation")
            _assert_private_material_free(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_private_material_free(nested)


def _optimizer_input(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BehaviorEvolutionContractError("behavior_optimizer_isolation_violation")
    _assert_private_material_free(value)
    # Invoke the shared fail-closed check before accepting candidate-specific
    # fields, so a private evaluator handle cannot be smuggled in any shape.
    if set(value) & _FORBIDDEN_INPUT_KEYS:
        assert_optimizer_isolated(dict(value))
    if set(value) != _OPTIMIZER_INPUT_KEYS:
        raise BehaviorEvolutionContractError("behavior_optimizer_isolation_violation")
    isolated = assert_optimizer_isolated(
        {
            "observation_cluster_refs": value.get("observation_cluster_refs"),
            "policy_contract_schemas": value.get("policy_contract_schemas", []),
            "reference_metrics": value.get("reference_metrics", {}),
        }
    )
    return {
        "candidate_id": value.get("candidate_id"),
        "reference_policy_ref": value.get("reference_policy_ref"),
        "observation_cluster_refs": isolated["observation_cluster_refs"],
        "proposed_patches": value.get("proposed_patches"),
    }


def propose_policy_candidate(optimizer_input: Mapping[str, object]) -> dict[str, object]:
    """Normalize one non-authoritative candidate with no activation surface."""

    value = _optimizer_input(optimizer_input)
    return make_policy_bundle_candidate(
        {
            "schema_version": 1,
            "candidate_id": value["candidate_id"],
            "reference_policy_ref": value["reference_policy_ref"],
            "source_observation_refs": value["observation_cluster_refs"],
            "state": "offline_evaluation",
            "patches": value["proposed_patches"],
        }
    )


def evaluate_candidate(*_args: object, **_kwargs: object) -> None:
    """Reject the former unsafe optimizer-side benchmark entrypoint.

    Kept as a fail-closed compatibility error so callers cannot accidentally
    hand private benchmark material to the optimizer.  Use
    ``run_private_candidate_evaluation`` in the separately privileged
    evaluator process instead.
    """

    raise BehaviorEvolutionContractError("behavior_private_evaluator_required")


def _finite_metrics(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != _METRICS:
        raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid")
    result: dict[str, float] = {}
    for name in _METRICS:
        try:
            metric = float(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid") from exc
        if not math.isfinite(metric) or metric < 0:
            raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid")
        result[name] = metric
    return result


def _campaign_budget(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _CAMPAIGN_BUDGET_KEYS:
        raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid")
    normalized: dict[str, int] = {}
    for name in _CAMPAIGN_BUDGET_KEYS:
        raw = value[name]
        if isinstance(raw, bool):
            raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid")
        try:
            amount = int(raw)
        except (TypeError, ValueError) as exc:
            raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid") from exc
        if amount < 1 or str(amount) != str(raw).strip():
            raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid")
        normalized[name] = amount
    return normalized


def _trusted_evaluation(candidate: object) -> OfflineCandidateEvaluation:
    if not isinstance(candidate, OfflineCandidateEvaluation) or not is_private_candidate_evaluation(candidate):
        raise BehaviorEvolutionContractError("behavior_candidate_evaluation_untrusted")
    if (
        not _SHA256_RE.fullmatch(candidate.policy_bundle_hash)
        or not _SHA256_RE.fullmatch(candidate.benchmark_hash)
        or candidate.case_count < 1
        or candidate.runs_per_case < 3
        or isinstance(candidate.hard_failure_count, bool)
        or not isinstance(candidate.hard_failure_count, int)
        or candidate.hard_failure_count < 0
    ):
        raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid")
    _finite_metrics(candidate.candidate_metrics)
    _campaign_budget(candidate.campaign_budget)
    return candidate


def decide_candidate(reference: BenchmarkSummary, candidate: OfflineCandidateEvaluation) -> dict[str, object]:
    """Decide only offline-to-Shadow eligibility; no runtime policy is changed."""

    if not isinstance(reference, BenchmarkSummary):
        raise BehaviorEvolutionContractError("behavior_candidate_evaluation_invalid")
    candidate = _trusted_evaluation(candidate)
    reference_metrics = _finite_metrics(reference.reference_metrics)
    reference_budget = _campaign_budget(reference.campaign_budget)
    if (
        candidate.benchmark_ref != reference.benchmark_ref
        or candidate.benchmark_hash != reference.benchmark_hash
        or candidate.provider_ref != reference.provider_ref
        or candidate.model_ref != reference.model_ref
        or candidate.thinking_ref != reference.thinking_ref
        or candidate.reference_policy_ref != reference.reference_policy_ref
        or candidate.case_count != reference.case_count
        or candidate.campaign_budget != reference_budget
    ):
        raise BehaviorEvolutionContractError("behavior_candidate_benchmark_mismatch")
    result = evaluate_shadow_admission(
        reference=reference_metrics,
        candidate={
            **_finite_metrics(candidate.candidate_metrics),
            "benchmark_frozen": True,
            "runs_per_case": candidate.runs_per_case,
            "hard_failure_count": candidate.hard_failure_count,
            "mean_cost_microunits": candidate.candidate_metrics["mean_cost_microunits"],
            "p95_latency_ms": candidate.candidate_metrics["p95_latency_ms"],
            "mean_output_tokens": candidate.candidate_metrics["mean_output_tokens"],
        },
        campaign_budget=reference_budget,
    )
    return {
        "candidate_ref": candidate.candidate_ref,
        "reference_policy_ref": candidate.reference_policy_ref,
        "state": result["state"],
        "reason": result["reason"],
        "benchmark_ref": candidate.benchmark_ref,
        "benchmark_hash": candidate.benchmark_hash,
        "candidate_metrics": dict(candidate.candidate_metrics),
        "campaign_budget": dict(candidate.campaign_budget),
        "hard_failure_count": candidate.hard_failure_count,
        "delivery_enabled": False,
        "formal_domain_writes": [],
    }


def restore_reference(candidate: OfflineCandidateEvaluation) -> dict[str, object]:
    """Emit an audit-friendly receipt without mutating Reference or production."""

    candidate = _trusted_evaluation(candidate)
    return {
        "candidate_ref": candidate.candidate_ref,
        "reference_policy_ref": candidate.reference_policy_ref,
        "state": "restored",
        "reference_modified": False,
        "production_modified": False,
    }


__all__ = [
    "OfflineCandidateEvaluation",
    "decide_candidate",
    "evaluate_candidate",
    "propose_policy_candidate",
    "restore_reference",
]
