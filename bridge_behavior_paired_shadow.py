#!/usr/bin/env python3
"""Pure, zero-send BE-6 paired Shadow evaluator.

The real group path gives this module one *temporary* body-free projection of
the inbound turn.  Reference and Candidate are compiled independently and
then evaluated through the same deterministic route/quality functions.  It is
not a reply generator: it cannot call a model, network, Delivery, Knowledge,
Memory, Task, or Approval surface.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json

from bridge_behavior_combined_shadow import (
    CombinedShadowContractError,
    run_combined_shadow,
)
from bridge_behavior_evolution_contract import (
    BE_ALLOWED_POLICY_CONTRACTS,
    BE_POLICY_PATCH_FIELDS,
)
from bridge_response_assessment_contract import make_response_assessment


_ATTENTION = frozenset({"explicit_mention", "reply_to_assistant", "ambient_optional", "unknown"})
_GROUNDING = frozenset({"grounded", "not_required", "unknown", "blocked"})
_RESEARCH = frozenset({"not_needed", "research_answer", "research_blocked"})
_BODY_KEYS = frozenset({"body", "content", "message", "message_text", "raw_evidence", "raw_text", "text"})


class PairedShadowError(ValueError):
    """The ephemeral paired evaluation was not safe to run."""


def _body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key or "").lower()
            if name in _BODY_KEYS or name.startswith(("raw_", "message_")) or name.endswith(("_body", "_content", "_text")):
                raise PairedShadowError("paired_shadow_body_free_violation")
            _body_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _body_free(item)


def _ref(value: object, *, error: str) -> str:
    result = str(value or "").strip()
    if ":" not in result or len(result) > 300:
        raise PairedShadowError(error)
    return result


def _temporary_context(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise PairedShadowError("paired_shadow_context_invalid")
    _body_free(value)
    expected = {
        "scope_ref", "source_event_ref", "trace_ref", "attention", "topic_active",
        "reference_should_reply", "grounding_status", "research_disposition", "task_continuation",
    }
    if set(value) != expected:
        raise PairedShadowError("paired_shadow_context_invalid")
    attention = str(value.get("attention") or "")
    grounding = str(value.get("grounding_status") or "")
    research = str(value.get("research_disposition") or "")
    if attention not in _ATTENTION or grounding not in _GROUNDING or research not in _RESEARCH:
        raise PairedShadowError("paired_shadow_context_invalid")
    if type(value.get("topic_active")) is not bool or type(value.get("reference_should_reply")) is not bool:
        raise PairedShadowError("paired_shadow_context_invalid")
    continuation = str(value.get("task_continuation") or "")
    if continuation not in {"not_applicable", "preserve_due_work"}:
        raise PairedShadowError("paired_shadow_context_invalid")
    return {
        "scope_ref": _ref(value.get("scope_ref"), error="paired_shadow_context_invalid"),
        "source_event_ref": _ref(value.get("source_event_ref"), error="paired_shadow_context_invalid"),
        "trace_ref": _ref(value.get("trace_ref"), error="paired_shadow_context_invalid"),
        "attention": attention,
        "topic_active": value["topic_active"],
        "reference_should_reply": value["reference_should_reply"],
        "grounding_status": grounding,
        "research_disposition": research,
        "task_continuation": continuation,
    }


def _compile_policy(*, kind: str, reference_policy_ref: object, patches: object) -> dict[str, object]:
    policy_ref = _ref(reference_policy_ref, error="paired_shadow_policy_invalid")
    if kind == "reference":
        if patches not in (None, [], ()):
            raise PairedShadowError("paired_shadow_policy_invalid")
        normalized: list[dict[str, object]] = []
    elif kind == "candidate":
        if not isinstance(patches, list) or not patches:
            raise PairedShadowError("paired_shadow_policy_invalid")
        normalized = []
        for item in patches:
            if not isinstance(item, Mapping) or set(item) != {"contract", "patch"}:
                raise PairedShadowError("paired_shadow_policy_invalid")
            contract = str(item.get("contract") or "")
            patch = item.get("patch")
            if contract not in BE_ALLOWED_POLICY_CONTRACTS or not isinstance(patch, Mapping) or not patch:
                raise PairedShadowError("paired_shadow_policy_invalid")
            if set(patch) - BE_POLICY_PATCH_FIELDS[contract]:
                raise PairedShadowError("paired_shadow_policy_invalid")
            clean: dict[str, object] = {}
            for name, setting in patch.items():
                if type(setting) is bool:
                    clean[str(name)] = setting
                elif isinstance(setting, int) and not isinstance(setting, bool) and 1 <= setting <= 86400:
                    clean[str(name)] = setting
                else:
                    raise PairedShadowError("paired_shadow_policy_invalid")
            normalized.append({"contract": contract, "patch": clean})
    else:
        raise PairedShadowError("paired_shadow_policy_invalid")
    digest = hashlib.sha256(json.dumps({"kind": kind, "reference_policy_ref": policy_ref, "patches": normalized}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"kind": kind, "policy_ref": policy_ref, "compiler_hash": "sha256:" + digest, "patches": normalized}


def _enabled(policy: Mapping[str, object], name: str) -> bool:
    return any(bool(patch.get("patch", {}).get(name)) for patch in policy["patches"] if isinstance(patch, Mapping))


def _route_and_assess(context: Mapping[str, object], policy: Mapping[str, object]) -> dict[str, object]:
    """Run a deterministic, body-free subset of route and quality assessment.

    Candidate patches can only make the temporary route more conservative.  A
    direct reply/commitment obligation always wins, so Shadow cannot learn a
    candidate that suppresses a promised response.
    """

    obligation = str(context["attention"]) in {"explicit_mention", "reply_to_assistant"}
    should_reply = bool(context["reference_should_reply"])
    if not obligation and _enabled(policy, "silence_when_no_new_value") and not bool(context["topic_active"]):
        should_reply = False
    if _enabled(policy, "require_citation_for_external_fact") and str(context["grounding_status"]) == "unknown":
        should_reply = False
    if obligation:
        should_reply = bool(context["reference_should_reply"])
    action = "reply" if should_reply else "silent"
    # The paired context distinguishes ``not_required`` (the inbound did not
    # make an external-fact claim) from ``unknown``.  The established BE-1
    # assessment vocabulary calls that same body-free state ``not_needed``.
    assessment_grounding = "not_needed" if context["grounding_status"] == "not_required" else context["grounding_status"]
    assessment = make_response_assessment({
        "schema_version": 1,
        "scope_type": "group",
        "scope_ref": context["scope_ref"],
        "topic_ref": context["source_event_ref"],
        "topic_revision": 0,
        "target_ref": context["trace_ref"],
        "reply_obligation": "required" if obligation else "ambient_optional",
        "speech_act": "answer" if should_reply else "clarify",
        "grounding_status": assessment_grounding,
        "stance": {"kind": "uncertain" if assessment_grounding == "unknown" else "none", "confidence": 1.0, "basis_refs": [], "missing_fact_refs": []},
        "research_disposition": context["research_disposition"],
        "task_continuation": context["task_continuation"],
        "policy_version": policy["policy_ref"],
    })
    return {
        "route_action": action,
        "quality_assessment": {
            "reply_obligation": assessment["reply_obligation"],
            "speech_act": assessment["speech_act"],
            "grounding_status": assessment["grounding_status"],
            "research_disposition": assessment["research_disposition"],
            "task_continuation": assessment["task_continuation"],
        },
        "hard_failure_count": int(obligation and not should_reply),
    }


def run_paired_shadow_context(
    event: Mapping[str, object],
    reference: Mapping[str, object],
    candidate: Mapping[str, object],
    temporary_context: Mapping[str, object],
) -> dict[str, object]:
    """Return one body-free receipt for the same temporary inbound context.

    This function is deliberately pure.  Callers must recheck admission and
    bind Reference/Candidate in the registry before calling it.
    """

    context = _temporary_context(temporary_context)
    reference_policy_ref = _ref(reference.get("reference_policy_ref"), error="paired_shadow_reference_invalid")
    if str(candidate.get("reference_policy_ref") or "") != reference_policy_ref:
        raise PairedShadowError("paired_shadow_reference_binding_mismatch")
    reference_policy = _compile_policy(kind="reference", reference_policy_ref=reference_policy_ref, patches=[])
    candidate_policy = _compile_policy(kind="candidate", reference_policy_ref=reference_policy_ref, patches=candidate.get("patches"))
    normalized_reference = {key: reference[key] for key in ("benchmark_ref", "benchmark_hash", "reference_metrics", "hard_failure_count")}
    normalized_candidate = {key: candidate[key] for key in ("candidate_ref", "state", "benchmark_ref", "benchmark_hash", "candidate_metrics", "hard_failure_count")}
    record = run_combined_shadow(event, normalized_reference, normalized_candidate)
    record["paired_execution"] = {
        "schema_version": 1,
        "execution_mode": "temporary_inbound_deterministic_no_model",
        "reference": {**reference_policy, **_route_and_assess(context, reference_policy)},
        "candidate": {**candidate_policy, **_route_and_assess(context, candidate_policy)},
    }
    # ``run_combined_shadow`` already proves Delivery/formal writes are empty;
    # this second body-free guard covers the pair evidence added afterwards.
    _body_free(record)
    return record


__all__ = ["PairedShadowError", "run_paired_shadow_context"]
