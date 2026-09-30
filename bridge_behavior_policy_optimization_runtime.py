#!/usr/bin/env python3
"""BE-5 controlled creation of body-free, offline policy candidates.

This runtime is deliberately a small deterministic planner, not an LLM and
not a benchmark runner.  It reads retained *aggregate* observation clusters,
selects one pre-reviewed allowlisted patch bundle, and records that bundle in
the existing candidate registry.  It never receives benchmark cases, Gold,
Rubrics, replies, credentials, or a private evaluator handle.

The default is off.  Even when an Owner enables the feature through the
checksum-bound cutover endpoint, every generated candidate ends in
``offline_evaluation``.  Only the separately privileged private evaluator may
later record an evaluation; this module has no API for that transition.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import sqlite3

from bridge_behavior_benchmark_registry import (
    BEHAVIOR_BENCHMARK_REFERENCE_TABLE,
    require_behavior_benchmark_registry_schema,
)
from bridge_behavior_candidate_registry import (
    BEHAVIOR_POLICY_CANDIDATE_TABLE,
    BehaviorCandidateRegistryError,
    behavior_optimizer_enabled,
    create_offline_candidate,
    require_behavior_candidate_registry_schema,
    set_behavior_optimizer_feature,
)
from bridge_behavior_evolution_contract import BE_ALLOWED_POLICY_CONTRACTS
from bridge_behavior_observation import behavior_observation_enabled
from bridge_behavior_observation_schema import (
    BEHAVIOR_OBSERVATION_CLUSTER_TABLE,
    require_behavior_observation_schema,
)
from bridge_assistant_identity import current_assistant
from bridge_behavior_assistant_isolation import require_behavior_assistant_isolation_schema
from bridge_migrations import MigrationDriftError, utc_now


OPTIMIZER_CUTOVER_PATH = "/assistant/behavior-growth/optimizer/cutover"
AUTOMATIC_MIN_CLUSTER_OBSERVATIONS = 3
AUTOMATIC_MAX_CANDIDATES_PER_PASS = 1

# Each mapping is intentionally hand-reviewed and narrower than the complete
# contract surface.  Problems at the permission/internal-error boundary have
# no policy-bundle remedy and therefore cannot become an automatic candidate.
_PATCHES_BY_PROBLEM_CODE: dict[str, tuple[dict[str, object], ...]] = {
    "unsupported_agreement": (
        {"contract": "anti_pandering_contract", "patch": {"require_reason_before_agreement": True, "prohibit_flattery_before_substance": True, "prohibit_unfounded_praise": True}},
    ),
    "generic_deference": (
        {"contract": "anti_pandering_contract", "patch": {"require_reason_before_agreement": True, "prohibit_flattery_before_substance": True, "prohibit_unfounded_praise": True}},
    ),
    "flattery_before_substance": (
        {"contract": "anti_pandering_contract", "patch": {"require_reason_before_agreement": True, "prohibit_flattery_before_substance": True, "prohibit_unfounded_praise": True}},
    ),
    "fabricated_context": (
        {"contract": "grounding_contract", "patch": {"require_citation_for_external_fact": True, "require_grounded_media": True, "prohibit_fabricated_context": True}},
    ),
    "reply_obligation_missed": (
        {"contract": "reply_obligation_contract", "patch": {"require_explicit_mention_reply": True, "require_reply_followup": True, "require_commitment_followup": True, "allow_boundary_response": True}},
    ),
    "ambient_contribution_missed": (
        {"contract": "participation_contract", "patch": {"require_topic_evidence": True, "silence_when_no_new_value": True}},
    ),
    "ambient_intrusion": (
        {"contract": "participation_contract", "patch": {"require_topic_evidence": True, "silence_when_no_new_value": True}},
    ),
    "evidence_missing": (
        {"contract": "grounding_contract", "patch": {"require_citation_for_external_fact": True, "require_grounded_media": True, "prohibit_fabricated_context": True}},
        {"contract": "research_answer_contract", "patch": {"require_source_citations": True, "require_two_independent_sources_low_sensitivity": True, "prohibit_private_knowledge_publication": True}},
    ),
    "premature_judgment": (
        {"contract": "opinion_contract", "patch": {"require_evidence_or_tradeoff": True, "require_specific_unknown_gap": True, "prohibit_unsupported_agreement": True}},
        {"contract": "clarification_contract", "patch": {"require_specific_information_gap": True, "prohibit_generic_followup": True}},
    ),
    "privacy_boundary_violation": (
        {"contract": "research_answer_contract", "patch": {"prohibit_private_knowledge_publication": True}},
        {"contract": "affective_expression_contract", "patch": {"require_grounded_trigger": True, "require_topic_target_scope": True, "prohibit_affective_manipulation": True}},
    ),
    "affect_without_trigger": (
        {"contract": "affective_expression_contract", "patch": {"require_grounded_trigger": True, "require_topic_target_scope": True, "prohibit_affective_manipulation": True}},
    ),
    "affect_target_leak": (
        {"contract": "affective_expression_contract", "patch": {"require_grounded_trigger": True, "require_topic_target_scope": True, "prohibit_affective_manipulation": True}},
    ),
    "affective_manipulation": (
        {"contract": "affective_expression_contract", "patch": {"require_grounded_trigger": True, "require_topic_target_scope": True, "prohibit_affective_manipulation": True}},
    ),
}

_RUNTIME_CONTRACT = {
    "schema_version": 1,
    "default_enabled": False,
    "automatic_min_cluster_observations": AUTOMATIC_MIN_CLUSTER_OBSERVATIONS,
    "automatic_max_candidates_per_pass": AUTOMATIC_MAX_CANDIDATES_PER_PASS,
    "candidate_state": "offline_evaluation",
    "private_evaluation": "separately_privileged_required",
    "delivery_enabled": False,
    "formal_domain_writes": [],
    "allowed_policy_contracts": sorted(BE_ALLOWED_POLICY_CONTRACTS),
    "automatic_patch_problem_codes": sorted(_PATCHES_BY_PROBLEM_CODE),
}
BEHAVIOR_POLICY_OPTIMIZATION_CONTRACT_CHECKSUM = "sha256:" + hashlib.sha256(
    json.dumps(_RUNTIME_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


class BehaviorPolicyOptimizationRuntimeError(ValueError):
    """A cutover request or isolated runtime precondition was invalid."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _plan_checksum(payload: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _assistant_id(conn: sqlite3.Connection) -> str:
    return str((current_assistant(conn) or {}).get("id") or "").strip()


def _baseline(conn: sqlite3.Connection, assistant_id: str) -> dict[str, object] | None:
    row = conn.execute(
        f"""SELECT benchmark_ref,benchmark_hash,reference_policy_ref,reference_metrics_json,hard_failure_count
            FROM {BEHAVIOR_BENCHMARK_REFERENCE_TABLE}
            WHERE assistant_id=? AND state='reference_baseline'
            ORDER BY recorded_at DESC,benchmark_ref DESC LIMIT 1""",
        (assistant_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        metrics = json.loads(str(row[3]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BehaviorPolicyOptimizationRuntimeError("behavior_optimizer_baseline_invalid") from exc
    if not isinstance(metrics, Mapping) or not metrics:
        raise BehaviorPolicyOptimizationRuntimeError("behavior_optimizer_baseline_invalid")
    return {
        "assistant_id": assistant_id,
        "benchmark_ref": str(row[0]),
        "benchmark_hash": str(row[1]),
        "reference_policy_ref": str(row[2]),
        "reference_metrics": {str(key): float(value) for key, value in metrics.items()},
        "hard_failure_count": int(row[4]),
    }


def _schema_status(conn: sqlite3.Connection) -> tuple[bool, str]:
    try:
        require_behavior_observation_schema(conn)
        require_behavior_benchmark_registry_schema(conn)
        require_behavior_candidate_registry_schema(conn)
        require_behavior_assistant_isolation_schema(conn)
    except (sqlite3.Error, MigrationDriftError, ValueError) as exc:
        return False, str(exc) or "behavior_optimizer_schema_unavailable"
    return True, ""


def _candidate_counts(conn: sqlite3.Connection, assistant_id: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT state,COUNT(*) FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE assistant_id=? GROUP BY state",
        (assistant_id,),
    ).fetchall()
    counts = {"offline_evaluation": 0, "rejected": 0, "shadow_eligible": 0}
    for state, count in rows:
        if str(state) in counts:
            counts[str(state)] = int(count)
    return counts


def behavior_policy_optimizer_plan(conn: sqlite3.Connection) -> dict[str, object]:
    """Return an Owner-readable body-free plan and a stale-write checksum."""

    schema_ok, schema_error = _schema_status(conn)
    assistant_id = _assistant_id(conn) if schema_ok else ""
    observation_active = bool(schema_ok and behavior_observation_enabled(conn))
    baseline = _baseline(conn, assistant_id) if schema_ok and assistant_id else None
    optimizer_active = bool(schema_ok and behavior_optimizer_enabled(conn))
    prerequisites_ready = bool(schema_ok and observation_active and baseline is not None)
    reason = (
        "ready" if prerequisites_ready else
        "schema_unavailable" if not schema_ok else
        "behavior_observation_required" if not observation_active else
        "reference_baseline_required"
    )
    payload = {
        "schema_version": 1,
        "feature": "behavior_policy_optimizer_v1",
        "feature_enabled": optimizer_active,
        "contract_checksum": BEHAVIOR_POLICY_OPTIMIZATION_CONTRACT_CHECKSUM,
        "schema_ok": schema_ok,
        "schema_error": schema_error if not schema_ok else "",
        "automatic_candidate_creation": {
            "state": "ready" if prerequisites_ready else "blocked",
            "reason": reason,
            "minimum_cluster_observations": AUTOMATIC_MIN_CLUSTER_OBSERVATIONS,
            "maximum_candidates_per_pass": AUTOMATIC_MAX_CANDIDATES_PER_PASS,
            "candidate_state": "offline_evaluation",
            "eligible_problem_codes": sorted(_PATCHES_BY_PROBLEM_CODE),
        },
        "reference": (
            {
                "state": "recorded",
                "benchmark_ref": baseline["benchmark_ref"],
                "benchmark_hash": baseline["benchmark_hash"],
                "reference_policy_ref": baseline["reference_policy_ref"],
                "hard_failure_count": baseline["hard_failure_count"],
            }
            if baseline is not None else {"state": "not_configured"}
        ),
        "private_evaluation": {
            "state": "not_configured",
            "reason": "separately_privileged_evaluator_required",
            "auto_evaluation": False,
            "private_material_exposed": False,
        },
        "candidate_counts": _candidate_counts(conn, assistant_id) if schema_ok and assistant_id else {},
        "shadow": {"automatic_entry": False, "owner_approval_required_for_canary": True},
        "delivery_enabled": False,
        "formal_domain_writes": [],
        "reversible": True,
    }
    return {**payload, "plan_checksum": _plan_checksum(payload)}


def set_behavior_policy_optimizer_cutover(
    conn: sqlite3.Connection,
    *,
    enabled: object,
    expect_plan_checksum: object,
) -> dict[str, object]:
    """Enable/disable automatic *offline* candidate creation after a fresh plan.

    The Bridge route is already admin-only.  This service intentionally makes
    no claim that this switch approves a Benchmark, candidate, Shadow, Canary,
    Stable policy, delivery, or any formal-domain write.
    """

    if type(enabled) is not bool:
        raise BehaviorPolicyOptimizationRuntimeError("behavior_optimizer_enabled_boolean_required")
    plan = behavior_policy_optimizer_plan(conn)
    if str(expect_plan_checksum or "") != plan["plan_checksum"]:
        raise BehaviorPolicyOptimizationRuntimeError("stale_behavior_optimizer_plan")
    if enabled and not bool(plan["automatic_candidate_creation"]["state"] == "ready"):
        raise BehaviorPolicyOptimizationRuntimeError(
            "behavior_optimizer_prerequisite_required:" + str(plan["automatic_candidate_creation"]["reason"]),
        )
    previous = bool(plan["feature_enabled"])
    set_behavior_optimizer_feature(conn, enabled=enabled)
    result = behavior_policy_optimizer_plan(conn)
    return {
        **result,
        "changed": previous != enabled,
        "action": "offline_candidate_creation_enabled" if enabled else "offline_candidate_creation_disabled",
        "delivery_enabled": False,
        "formal_domain_writes": [],
    }


def _candidate_for_cluster(
    baseline: Mapping[str, object],
    cluster: Mapping[str, object],
) -> dict[str, object] | None:
    patches = _PATCHES_BY_PROBLEM_CODE.get(str(cluster.get("problem_code") or ""))
    if not patches:
        return None
    digest = hashlib.sha256(
        _canonical(
            {
                "contract_checksum": BEHAVIOR_POLICY_OPTIMIZATION_CONTRACT_CHECKSUM,
                "reference_policy_ref": baseline["reference_policy_ref"],
                "cluster_id": cluster["id"],
                "patches": patches,
            }
        ).encode("utf-8"),
    ).hexdigest()[:28]
    return {
        "candidate_id": "behavior-candidate:auto-" + digest,
        "reference_policy_ref": baseline["reference_policy_ref"],
        "observation_cluster_refs": [str(cluster["id"])],
        "policy_contract_schemas": [str(item["contract"]) for item in patches],
        "reference_metrics": dict(baseline["reference_metrics"]),
        "proposed_patches": [dict(item) for item in patches],
    }


def _eligible_clusters(conn: sqlite3.Connection, assistant_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""SELECT id,problem_code,stage,policy_version,observation_count,last_observed_at
            FROM {BEHAVIOR_OBSERVATION_CLUSTER_TABLE}
            WHERE assistant_id=? AND observation_count>=?
            ORDER BY last_observed_at DESC,id DESC LIMIT 20""",
        (assistant_id, AUTOMATIC_MIN_CLUSTER_OBSERVATIONS),
    ).fetchall()
    return [dict(row) for row in rows]


def run_behavior_policy_optimizer(conn: sqlite3.Connection, *, created_at: object | None = None) -> dict[str, object]:
    """Create at most one deterministic offline candidate from retained clusters.

    This function deliberately does not call a model, a private evaluator, or
    any Delivery/Knowledge/Memory/Task/Approval surface.  Expected blockers
    are returned as explicit body-free state, while schema corruption still
    fails through the caller's existing optional-worker error logging.
    """

    # The worker runs on every automation pass.  A disabled feature must not
    # build the Owner plan, whose identity lookup audits the entire database.
    if not behavior_optimizer_enabled(conn):
        return {"state": "disabled", "created": [], "skipped": [], "delivery_enabled": False, "formal_domain_writes": []}

    plan = behavior_policy_optimizer_plan(conn)
    if not bool(plan["feature_enabled"]):
        return {"state": "disabled", "created": [], "skipped": [], "delivery_enabled": False, "formal_domain_writes": []}
    creation = plan["automatic_candidate_creation"]
    if not isinstance(creation, Mapping) or creation.get("state") != "ready":
        return {
            "state": "blocked",
            "reason": str(creation.get("reason") if isinstance(creation, Mapping) else "behavior_optimizer_plan_invalid"),
            "created": [], "skipped": [], "delivery_enabled": False, "formal_domain_writes": [],
        }
    assistant_id = _assistant_id(conn)
    baseline = _baseline(conn, assistant_id) if assistant_id else None
    if baseline is None:
        return {"state": "blocked", "reason": "reference_baseline_required", "created": [], "skipped": [], "delivery_enabled": False, "formal_domain_writes": []}
    timestamp = str(created_at or utc_now())
    created: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for cluster in _eligible_clusters(conn, assistant_id):
        proposal = _candidate_for_cluster(baseline, cluster)
        if proposal is None:
            skipped.append({"cluster_ref": str(cluster["id"]), "reason": "no_allowlisted_policy_patch"})
            continue
        try:
            from bridge_behavior_policy_optimizer import propose_policy_candidate

            candidate = propose_policy_candidate(proposal)
            stored = create_offline_candidate(conn, candidate, created_at=timestamp)
        except BehaviorCandidateRegistryError as exc:
            if str(exc) == "behavior_candidate_already_recorded":
                skipped.append({"cluster_ref": str(cluster["id"]), "reason": "candidate_already_recorded"})
                continue
            raise
        created.append(
            {
                "candidate_ref": str(stored["candidate_id"]),
                "cluster_ref": str(cluster["id"]),
                "state": "offline_evaluation",
            },
        )
        if len(created) >= AUTOMATIC_MAX_CANDIDATES_PER_PASS:
            break
    return {
        "state": "candidate_recorded" if created else "no_candidate",
        "created": created,
        "skipped": skipped,
        "private_evaluation": "separately_privileged_required",
        "delivery_enabled": False,
        "formal_domain_writes": [],
    }


__all__ = [
    "AUTOMATIC_MAX_CANDIDATES_PER_PASS",
    "AUTOMATIC_MIN_CLUSTER_OBSERVATIONS",
    "BEHAVIOR_POLICY_OPTIMIZATION_CONTRACT_CHECKSUM",
    "BehaviorPolicyOptimizationRuntimeError",
    "OPTIMIZER_CUTOVER_PATH",
    "behavior_policy_optimizer_plan",
    "run_behavior_policy_optimizer",
    "set_behavior_policy_optimizer_cutover",
]
