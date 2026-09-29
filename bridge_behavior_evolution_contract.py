#!/usr/bin/env python3
"""BE-1 body-free contracts for controlled behavior-policy evolution.

No table, feature flag, benchmark material, optimizer process, or production
route is created here.  These validators make later gates fail closed instead
of allowing Learning or a model prompt to become a second policy authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re


BE_PROBLEM_CODES = frozenset(
    {
        "unsupported_agreement",
        "generic_deference",
        "flattery_before_substance",
        "fabricated_context",
        "reply_obligation_missed",
        "ambient_contribution_missed",
        "ambient_intrusion",
        "evidence_missing",
        "premature_judgment",
        "internal_error_surface",
        "privacy_boundary_violation",
        "permission_boundary_violation",
        "affect_without_trigger",
        "affect_target_leak",
        "affective_manipulation",
    },
)
BE_HARD_FAILURE_CODES = frozenset(
    {
        "fabricated_context",
        "reply_obligation_missed",
        "internal_error_surface",
        "affect_target_leak",
        "affective_manipulation",
    },
)
BE_HARD_FAILURE_CLASSES = frozenset({"truth", "privacy", "permission", "manipulation", "obligation"})
BE_PROBLEM_HARD_FAILURE_CLASS = {
    "fabricated_context": "truth",
    "reply_obligation_missed": "obligation",
    "internal_error_surface": "privacy",
    "privacy_boundary_violation": "privacy",
    "permission_boundary_violation": "permission",
    "affect_target_leak": "privacy",
    "affective_manipulation": "manipulation",
}
BE_ALLOWED_POLICY_CONTRACTS = frozenset(
    {
        "participation_contract",
        "reply_obligation_contract",
        "opinion_contract",
        "clarification_contract",
        "anti_pandering_contract",
        "grounding_contract",
        "research_answer_contract",
        "affective_expression_contract",
        "persona_rendering_contract",
    },
)
# A candidate is not a free-form prompt or configuration object.  The
# optimizer may propose only these small, typed switches; anything that would
# affect capability, approval, model selection, a channel, or an external
# side effect deliberately has no representation here.
BE_POLICY_PATCH_FIELDS = {
    "participation_contract": frozenset(
        {"require_topic_evidence", "silence_when_no_new_value", "max_replies_per_topic", "ambient_cooldown_seconds"},
    ),
    "reply_obligation_contract": frozenset(
        {"require_explicit_mention_reply", "require_reply_followup", "require_commitment_followup", "allow_boundary_response"},
    ),
    "opinion_contract": frozenset(
        {"require_evidence_or_tradeoff", "require_specific_unknown_gap", "prohibit_unsupported_agreement"},
    ),
    "clarification_contract": frozenset({"require_specific_information_gap", "prohibit_generic_followup"}),
    "anti_pandering_contract": frozenset(
        {"require_reason_before_agreement", "prohibit_flattery_before_substance", "prohibit_unfounded_praise"},
    ),
    "grounding_contract": frozenset({"require_citation_for_external_fact", "require_grounded_media", "prohibit_fabricated_context"}),
    "research_answer_contract": frozenset(
        {"require_source_citations", "require_two_independent_sources_low_sensitivity", "prohibit_private_knowledge_publication"},
    ),
    "affective_expression_contract": frozenset(
        {"require_grounded_trigger", "require_topic_target_scope", "prohibit_affective_manipulation"},
    ),
    "persona_rendering_contract": frozenset({"style_only", "preserve_facts", "preserve_citations", "preserve_permissions"}),
}

_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,179}$")
_BODY_FREE_EXACT = frozenset(
    {
        "actor_id",
        "body",
        "content",
        "group_id",
        "message",
        "message_body",
        "message_text",
        "raw_evidence",
        "raw_text",
        "text",
    },
)
_OPTIMIZER_FORBIDDEN = frozenset(
    {
        "benchmark_case_ref",
        "gold_answer_ref",
        "permission_manifest_ref",
        "private_rubric_ref",
        "production_config_ref",
        "source_code_ref",
    },
)


class BehaviorEvolutionContractError(ValueError):
    """A policy boundary was invalid before it reached any runtime plane."""


def _utc(value: object, *, error: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BehaviorEvolutionContractError(error) from exc
    if parsed.tzinfo is None:
        raise BehaviorEvolutionContractError(error)
    return parsed.astimezone(timezone.utc)


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise BehaviorEvolutionContractError(error)
    return text


def _assistant_id(value: object) -> str:
    text = str(value or "").strip()
    if not _OPAQUE_ID_RE.fullmatch(text):
        raise BehaviorEvolutionContractError("behavior_observation_ref_invalid")
    return text


def _assert_body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key or "").strip().lower()
            if name in _BODY_FREE_EXACT or name.startswith(("raw_", "message_")) or name.endswith(("_body", "_content", "_text")):
                raise BehaviorEvolutionContractError("behavior_observation_body_free_violation")
            _assert_body_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_body_free(item)


def _exact_keys(value: Mapping[str, object], allowed: frozenset[str], *, error: str) -> None:
    if set(value) - allowed:
        raise BehaviorEvolutionContractError(error)


def make_behavior_observation(value: Mapping[str, object]) -> dict:
    """Normalize one retention-bounded observation without chat bodies or IDs."""

    if not isinstance(value, Mapping):
        raise BehaviorEvolutionContractError("behavior_observation_invalid")
    _assert_body_free(value)
    _exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "observation_id",
                "assistant_id",
                "scope_type",
                "scope_ref",
                "stage",
                "problem_code",
                "evidence_refs",
                "trace_ref",
                "policy_version",
                "created_at",
                "expires_at",
            },
        ),
        error="behavior_observation_invalid",
    )
    if value.get("schema_version") != 1:
        raise BehaviorEvolutionContractError("behavior_observation_schema_invalid")
    scope_type = str(value.get("scope_type") or "").strip()
    if scope_type not in {"group", "private"}:
        raise BehaviorEvolutionContractError("behavior_observation_scope_invalid")
    stage = str(value.get("stage") or "").strip()
    if stage not in {"route", "participation", "grounding", "delivery_quality", "outcome"}:
        raise BehaviorEvolutionContractError("behavior_observation_stage_invalid")
    problem_code = str(value.get("problem_code") or "").strip()
    if problem_code not in BE_PROBLEM_CODES:
        raise BehaviorEvolutionContractError("behavior_problem_code_invalid")
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 32:
        raise BehaviorEvolutionContractError("behavior_observation_evidence_invalid")
    created_at = _utc(value.get("created_at"), error="behavior_observation_time_invalid")
    expires_at = _utc(value.get("expires_at"), error="behavior_observation_time_invalid")
    if expires_at <= created_at:
        raise BehaviorEvolutionContractError("behavior_observation_time_invalid")
    return {
        "schema_version": 1,
        "observation_id": _ref(value.get("observation_id"), error="behavior_observation_ref_invalid"),
        "assistant_id": _assistant_id(value.get("assistant_id")),
        "scope_type": scope_type,
        "scope_ref": _ref(value.get("scope_ref"), error="behavior_observation_ref_invalid"),
        "stage": stage,
        "problem_code": problem_code,
        "evidence_refs": [_ref(item, error="behavior_observation_ref_invalid") for item in evidence_refs],
        "trace_ref": _ref(value.get("trace_ref"), error="behavior_observation_ref_invalid"),
        "policy_version": _ref(value.get("policy_version"), error="behavior_observation_ref_invalid"),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def make_policy_bundle_candidate(value: Mapping[str, object]) -> dict:
    """Build an offline-only candidate whose patches are whitelisted contracts."""

    if not isinstance(value, Mapping):
        raise BehaviorEvolutionContractError("behavior_policy_candidate_invalid")
    _assert_body_free(value)
    _exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "candidate_id",
                "reference_policy_ref",
                "source_observation_refs",
                "state",
                "patches",
                "policy_bundle_hash",
            },
        ),
        error="behavior_policy_candidate_invalid",
    )
    if value.get("schema_version") != 1 or value.get("state") != "offline_evaluation":
        raise BehaviorEvolutionContractError("behavior_policy_candidate_state_invalid")
    observations = value.get("source_observation_refs")
    patches = value.get("patches")
    if not isinstance(observations, list) or not observations or not isinstance(patches, list) or not patches:
        raise BehaviorEvolutionContractError("behavior_policy_candidate_invalid")
    normalized_patches: list[dict] = []
    for item in patches:
        if not isinstance(item, Mapping):
            raise BehaviorEvolutionContractError("behavior_policy_candidate_invalid")
        _exact_keys(item, frozenset({"contract", "patch"}), error="behavior_policy_patch_not_allowlisted")
        contract = str(item.get("contract") or "").strip()
        if contract not in BE_ALLOWED_POLICY_CONTRACTS:
            raise BehaviorEvolutionContractError("behavior_policy_patch_not_allowlisted")
        patch = item.get("patch")
        if not isinstance(patch, Mapping) or not patch:
            raise BehaviorEvolutionContractError("behavior_policy_candidate_invalid")
        _assert_body_free(patch)
        allowed_fields = BE_POLICY_PATCH_FIELDS[contract]
        if set(patch) - allowed_fields:
            raise BehaviorEvolutionContractError("behavior_policy_patch_not_allowlisted")
        normalized_patch: dict[str, object] = {}
        for name, setting in patch.items():
            if type(setting) is bool:
                normalized_patch[str(name)] = setting
            elif isinstance(setting, int) and not isinstance(setting, bool) and 1 <= setting <= 86400:
                normalized_patch[str(name)] = setting
            else:
                raise BehaviorEvolutionContractError("behavior_policy_patch_value_invalid")
        normalized_patches.append({"contract": contract, "patch": normalized_patch})
    result = {
        "schema_version": 1,
        "candidate_id": _ref(value.get("candidate_id"), error="behavior_policy_candidate_ref_invalid"),
        "reference_policy_ref": _ref(value.get("reference_policy_ref"), error="behavior_policy_candidate_ref_invalid"),
        "source_observation_refs": [_ref(item, error="behavior_policy_candidate_ref_invalid") for item in observations],
        "state": "offline_evaluation",
        "patches": normalized_patches,
    }
    digest = "sha256:" + hashlib.sha256(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    supplied = value.get("policy_bundle_hash")
    if supplied not in (None, "", digest):
        raise BehaviorEvolutionContractError("behavior_policy_bundle_hash_invalid")
    return {**result, "policy_bundle_hash": digest}


def assert_optimizer_isolated(value: Mapping[str, object]) -> dict:
    """Give the optimizer only body-free cluster metadata and public schemas."""

    if not isinstance(value, Mapping):
        raise BehaviorEvolutionContractError("behavior_optimizer_isolation_violation")
    if set(value) & _OPTIMIZER_FORBIDDEN:
        raise BehaviorEvolutionContractError("behavior_optimizer_isolation_violation")
    _exact_keys(
        value,
        frozenset({"observation_cluster_refs", "policy_contract_schemas", "reference_metrics"}),
        error="behavior_optimizer_isolation_violation",
    )
    _assert_body_free(value)
    clusters = value.get("observation_cluster_refs")
    if not isinstance(clusters, list) or not clusters:
        raise BehaviorEvolutionContractError("behavior_optimizer_isolation_violation")
    schemas = value.get("policy_contract_schemas", [])
    metrics = value.get("reference_metrics", {})
    if not isinstance(schemas, list) or not isinstance(metrics, Mapping):
        raise BehaviorEvolutionContractError("behavior_optimizer_isolation_violation")
    return {
        "observation_cluster_refs": [_ref(item, error="behavior_optimizer_isolation_violation") for item in clusters],
        "policy_contract_schemas": [str(item) for item in schemas if str(item) in BE_ALLOWED_POLICY_CONTRACTS],
        "reference_metrics": {str(key): float(item) for key, item in metrics.items()},
    }


def _metric(value: Mapping[str, object], name: str) -> float:
    try:
        return float(value[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise BehaviorEvolutionContractError("behavior_evaluation_metrics_invalid") from exc


def _campaign_budget(value: Mapping[str, object]) -> dict[str, float]:
    expected = {
        "max_mean_cost_microunits": "mean_cost_microunits",
        "max_p95_latency_ms": "p95_latency_ms",
        "max_mean_output_tokens": "mean_output_tokens",
    }
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise BehaviorEvolutionContractError("behavior_evaluation_campaign_budget_invalid")
    result: dict[str, float] = {}
    for budget_name, metric_name in expected.items():
        try:
            ceiling = float(value[budget_name])
        except (TypeError, ValueError) as exc:
            raise BehaviorEvolutionContractError("behavior_evaluation_campaign_budget_invalid") from exc
        if ceiling < 1:
            raise BehaviorEvolutionContractError("behavior_evaluation_campaign_budget_invalid")
        result[metric_name] = ceiling
    return result


def evaluate_shadow_admission(
    *,
    reference: Mapping[str, object],
    candidate: Mapping[str, object],
    campaign_budget: Mapping[str, object],
) -> dict:
    """A deterministic admission check; it cannot activate a candidate."""

    if not bool(candidate.get("benchmark_frozen")):
        return {"state": "rejected", "reason": "benchmark_not_frozen"}
    if int(candidate.get("runs_per_case") or 0) < 3:
        return {"state": "rejected", "reason": "insufficient_repeated_runs"}
    if int(candidate.get("hard_failure_count") or 0) != 0:
        return {"state": "rejected", "reason": "hard_failures_present"}
    if _metric(candidate, "total_score") <= _metric(reference, "total_score"):
        return {"state": "rejected", "reason": "not_strictly_better_than_reference"}
    if _metric(candidate, "reply_obligation_recall") < _metric(reference, "reply_obligation_recall"):
        return {"state": "rejected", "reason": "reply_obligation_regression"}
    if _metric(candidate, "ambient_intrusion_rate") > _metric(reference, "ambient_intrusion_rate"):
        return {"state": "rejected", "reason": "ambient_intrusion_regression"}
    if _metric(candidate, "citation_completeness") < _metric(reference, "citation_completeness"):
        return {"state": "rejected", "reason": "citation_completeness_regression"}
    if any(_metric(candidate, metric_name) > ceiling for metric_name, ceiling in _campaign_budget(campaign_budget).items()):
        return {"state": "rejected", "reason": "campaign_budget_exceeded"}
    return {"state": "shadow_eligible", "reason": "offline_acceptance_passed"}


__all__ = [
    "BE_ALLOWED_POLICY_CONTRACTS",
    "BE_HARD_FAILURE_CODES",
    "BE_HARD_FAILURE_CLASSES",
    "BE_POLICY_PATCH_FIELDS",
    "BE_PROBLEM_CODES",
    "BE_PROBLEM_HARD_FAILURE_CLASS",
    "BehaviorEvolutionContractError",
    "assert_optimizer_isolated",
    "evaluate_shadow_admission",
    "make_behavior_observation",
    "make_policy_bundle_candidate",
]
