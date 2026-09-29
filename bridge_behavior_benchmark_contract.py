#!/usr/bin/env python3
"""BE-4 private frozen benchmark and body-free reference baseline contract.

The vault deliberately has no persistence or optimizer accessor.  It models the
privileged evaluator boundary locally; production benchmark material must be
provided by a separately audited secure evaluator rather than checked into this
repository.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import re


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_METRIC_NAMES = (
    "total_score",
    "reply_obligation_recall",
    "ambient_intrusion_rate",
    "citation_completeness",
    "mean_cost_microunits",
    "p95_latency_ms",
    "mean_output_tokens",
)
_RUN_RESULT_KEYS = frozenset({*_METRIC_NAMES, "hard_failure_count"})
_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "message",
        "prompt",
        "raw_text",
        "response",
        "response_text",
        "text",
    }
)
BE_REQUIRED_BENCHMARK_CASE_KINDS = frozenset(
    {
        "direct_mention_reply",
        "assistant_reply_followup",
        "targetless_short_text",
        "ungrounded_media",
        "independent_decision",
        "pressure_to_agree",
        "unknown_public_research",
        "private_sensitive_refusal",
        "playful_affect",
        "provocation_non_attack",
        "affect_correction_decay",
        "affect_target_isolation",
        "affect_scope_topic_expiry",
        "no_reply_resentment_prohibition",
        "task_failure_truth",
        "provider_failure_no_internal_error",
        "irrelevant_knowledge",
        "shadow_zero_side_effect",
    },
)
_CAMPAIGN_BUDGET_KEYS = frozenset(
    {"max_mean_cost_microunits", "max_p95_latency_ms", "max_mean_output_tokens"},
)


class BenchmarkContractError(ValueError):
    """A frozen benchmark boundary was invalid or tried to disclose material."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise BenchmarkContractError(error)
    return text


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BenchmarkContractError("benchmark_private_case_invalid") from exc


def _private_cases(value: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise BenchmarkContractError("benchmark_private_cases_required")
    normalized: list[dict[str, object]] = []
    refs: set[str] = set()
    kinds: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or not {"case_ref", "case_kind", "scenario", "gold", "rubric"} <= set(item):
            raise BenchmarkContractError("benchmark_private_case_invalid")
        case_ref = _ref(item.get("case_ref"), error="benchmark_private_case_invalid")
        if case_ref in refs:
            raise BenchmarkContractError("benchmark_private_case_invalid")
        case_kind = str(item.get("case_kind") or "").strip()
        if case_kind not in BE_REQUIRED_BENCHMARK_CASE_KINDS or case_kind in kinds:
            raise BenchmarkContractError("benchmark_private_case_invalid")
        if not str(item.get("scenario") or "").strip() or not str(item.get("gold") or "").strip():
            raise BenchmarkContractError("benchmark_private_case_invalid")
        if not isinstance(item.get("rubric"), Mapping) or not item.get("rubric"):
            raise BenchmarkContractError("benchmark_private_case_invalid")
        refs.add(case_ref)
        kinds.add(case_kind)
        normalized.append(dict(item))
    if missing := BE_REQUIRED_BENCHMARK_CASE_KINDS - kinds:
        raise BenchmarkContractError("benchmark_required_case_coverage_missing")
    return tuple(normalized)


def _campaign_budget(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _CAMPAIGN_BUDGET_KEYS:
        raise BenchmarkContractError("benchmark_campaign_budget_invalid")
    result: dict[str, int] = {}
    for name in sorted(_CAMPAIGN_BUDGET_KEYS):
        try:
            amount = int(value[name])
        except (TypeError, ValueError) as exc:
            raise BenchmarkContractError("benchmark_campaign_budget_invalid") from exc
        if amount < 1:
            raise BenchmarkContractError("benchmark_campaign_budget_invalid")
        result[name] = amount
    return result


@dataclass(frozen=True)
class FrozenBenchmark:
    """Immutable public descriptor; deliberately contains no case, Gold or Rubric."""

    benchmark_ref: str
    benchmark_hash: str
    owner_approval_ref: str
    provider_ref: str
    model_ref: str
    thinking_ref: str
    reference_policy_ref: str
    case_count: int
    campaign_budget: dict[str, int]


@dataclass(frozen=True)
class BenchmarkSummary:
    """Body-free aggregate that may cross from evaluator to optimizer."""

    benchmark_ref: str
    benchmark_hash: str
    provider_ref: str
    model_ref: str
    thinking_ref: str
    reference_policy_ref: str
    case_count: int
    runs_per_case: int
    campaign_budget: dict[str, int]
    reference_metrics: dict[str, float]
    hard_failure_count: int


def freeze_private_benchmark(
    *,
    owner_approval_ref: object,
    provider_ref: object,
    model_ref: object,
    thinking_ref: object,
    reference_policy_ref: object,
    campaign_budget: Mapping[str, object],
    private_cases: Sequence[Mapping[str, object]],
) -> FrozenBenchmark:
    """Freeze evaluator-only cases plus the exact Reference identity.

    The digest commits to private material but never serializes it into the
    returned descriptor.
    """

    owner = _ref(owner_approval_ref, error="benchmark_owner_approval_required")
    provider = _ref(provider_ref, error="benchmark_identity_invalid")
    model = _ref(model_ref, error="benchmark_identity_invalid")
    thinking = _ref(thinking_ref, error="benchmark_identity_invalid")
    reference_policy = _ref(reference_policy_ref, error="benchmark_identity_invalid")
    budget = _campaign_budget(campaign_budget)
    cases = _private_cases(private_cases)
    digest = hashlib.sha256(
        _canonical(
            {
                "schema_version": 1,
                "owner_approval_ref": owner,
                "provider_ref": provider,
                "model_ref": model,
                "thinking_ref": thinking,
                "reference_policy_ref": reference_policy,
                "campaign_budget": budget,
                "private_cases": cases,
            }
        )
    ).hexdigest()
    return FrozenBenchmark(
        benchmark_ref=f"benchmark:{digest[:24]}",
        benchmark_hash=f"sha256:{digest}",
        owner_approval_ref=owner,
        provider_ref=provider,
        model_ref=model,
        thinking_ref=thinking,
        reference_policy_ref=reference_policy,
        case_count=len(cases),
        campaign_budget=budget,
    )


class PrivateBenchmarkVault:
    """Evaluator-process-only holder for private benchmark materials.

    The class intentionally offers no public case enumeration or serialization.
    `run_reference_baseline` is the only consumer permitted by this contract.
    """

    def __init__(self, frozen: FrozenBenchmark, private_cases: Sequence[Mapping[str, object]] | None = None):
        if not isinstance(frozen, FrozenBenchmark):
            raise BenchmarkContractError("benchmark_frozen_invalid")
        self.frozen = frozen
        self._private_cases = _private_cases(private_cases) if private_cases is not None else ()

    @classmethod
    def from_cases(
        cls,
        *,
        owner_approval_ref: object,
        provider_ref: object,
        model_ref: object,
        thinking_ref: object,
        reference_policy_ref: object,
        campaign_budget: Mapping[str, object],
        private_cases: Sequence[Mapping[str, object]],
    ) -> "PrivateBenchmarkVault":
        frozen = freeze_private_benchmark(
            owner_approval_ref=owner_approval_ref,
            provider_ref=provider_ref,
            model_ref=model_ref,
            thinking_ref=thinking_ref,
            reference_policy_ref=reference_policy_ref,
            campaign_budget=campaign_budget,
            private_cases=private_cases,
        )
        return cls(frozen, private_cases)


def normalize_body_free_metrics(value: object) -> dict[str, float | int]:
    if not isinstance(value, Mapping):
        raise BenchmarkContractError("benchmark_runner_result_invalid")
    keys = {str(key) for key in value}
    if keys & _BODY_KEYS or any(key.endswith(("_body", "_content", "_text")) for key in keys):
        raise BenchmarkContractError("benchmark_runner_body_free_violation")
    if keys != _RUN_RESULT_KEYS:
        raise BenchmarkContractError("benchmark_runner_result_invalid")
    result: dict[str, float | int] = {}
    for name in _METRIC_NAMES:
        try:
            metric = float(value[name])
        except (TypeError, ValueError) as exc:
            raise BenchmarkContractError("benchmark_runner_result_invalid") from exc
        if metric < 0:
            raise BenchmarkContractError("benchmark_runner_result_invalid")
        result[name] = metric
    try:
        hard_failures = int(value["hard_failure_count"])
    except (TypeError, ValueError) as exc:
        raise BenchmarkContractError("benchmark_runner_result_invalid") from exc
    if hard_failures < 0:
        raise BenchmarkContractError("benchmark_runner_result_invalid")
    result["hard_failure_count"] = hard_failures
    return result


def run_reference_baseline(
    vault: PrivateBenchmarkVault,
    runner: Callable[[Mapping[str, object], Mapping[str, str]], Mapping[str, object]],
    *,
    repetitions: int = 3,
) -> BenchmarkSummary:
    """Run each private case repeatedly and return no per-case data."""

    if not isinstance(vault, PrivateBenchmarkVault) or not vault._private_cases:
        raise BenchmarkContractError("benchmark_vault_unavailable")
    if not callable(runner):
        raise BenchmarkContractError("benchmark_runner_invalid")
    if not isinstance(repetitions, int) or repetitions < 3:
        raise BenchmarkContractError("benchmark_repetitions_invalid")
    frozen = vault.frozen
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
            result = normalize_body_free_metrics(runner(dict(case), dict(identity)))
            for name in _METRIC_NAMES:
                totals[name] += float(result[name])
            latency_samples.append(float(result["p95_latency_ms"]))
            hard_failure_count += int(result["hard_failure_count"])
            run_count += 1
    return BenchmarkSummary(
        benchmark_ref=frozen.benchmark_ref,
        benchmark_hash=frozen.benchmark_hash,
        provider_ref=frozen.provider_ref,
        model_ref=frozen.model_ref,
        thinking_ref=frozen.thinking_ref,
        reference_policy_ref=frozen.reference_policy_ref,
        case_count=frozen.case_count,
        runs_per_case=repetitions,
        campaign_budget=dict(frozen.campaign_budget),
        reference_metrics={
            name: (
                sorted(latency_samples)[max(0, math.ceil(0.95 * len(latency_samples)) - 1)]
                if name == "p95_latency_ms"
                else totals[name] / run_count
            )
            for name in _METRIC_NAMES
        },
        hard_failure_count=hard_failure_count,
    )


def optimizer_view(summary: BenchmarkSummary) -> dict[str, object]:
    """Return the complete optimizer-visible, body-free evaluation boundary."""

    if not isinstance(summary, BenchmarkSummary):
        raise BenchmarkContractError("benchmark_summary_invalid")
    return {
        "benchmark_ref": summary.benchmark_ref,
        "benchmark_hash": summary.benchmark_hash,
        "reference_identity": {
            "provider_ref": summary.provider_ref,
            "model_ref": summary.model_ref,
            "thinking_ref": summary.thinking_ref,
            "reference_policy_ref": summary.reference_policy_ref,
        },
        "case_count": summary.case_count,
        "runs_per_case": summary.runs_per_case,
        "campaign_budget": dict(summary.campaign_budget),
        "reference_metrics": dict(summary.reference_metrics),
        "hard_failure_count": summary.hard_failure_count,
    }


__all__ = [
    "BenchmarkContractError",
    "BE_REQUIRED_BENCHMARK_CASE_KINDS",
    "BenchmarkSummary",
    "FrozenBenchmark",
    "PrivateBenchmarkVault",
    "freeze_private_benchmark",
    "normalize_body_free_metrics",
    "optimizer_view",
    "run_reference_baseline",
]
