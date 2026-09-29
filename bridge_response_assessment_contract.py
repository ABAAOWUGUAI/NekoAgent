#!/usr/bin/env python3
"""BE-1 contract for fact-first, natural Assistant reply assessment.

This is deliberately an internal decision object, never a visible reply
template.  It binds a later renderer to the actual topic, evidence, stance
and communicative act without storing a conversation body or granting an
action, delivery or approval right.
"""

from __future__ import annotations

from collections.abc import Mapping
import re


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_BODY_FIELDS = frozenset(
    {"body", "content", "message", "message_body", "message_text", "raw_text", "reply_text", "text"}
)
_SCOPE_TYPES = frozenset({"group", "private"})
_REPLY_OBLIGATIONS = frozenset({"required", "ambient_optional", "correct_silence"})
_SPEECH_ACTS = frozenset(
    {
        "acknowledge",
        "answer",
        "boundary",
        "challenge",
        "clarify",
        "coordinate",
        "evaluate",
        "explain",
        "humor",
        "research_answer",
    }
)
_GROUNDING = frozenset({"grounded", "partial", "unknown", "not_needed"})
_STANCE_KINDS = frozenset({"none", "support", "oppose", "mixed", "tentative", "uncertain", "preference"})
_RESEARCH_DISPOSITIONS = frozenset({"not_needed", "research_candidate", "research_blocked"})
_TASK_CONTINUATIONS = frozenset({"not_applicable", "preserve_due_work"})
_BOUNDARY_TRIGGERS = frozenset({"visible_insult_or_mockery", "visible_disrespect", "visible_hostility"})
_BOUNDARY_AFFECTS = frozenset({"hurt", "annoyed", "disappointed", "disagreeing"})
_BOUNDARY_STYLES = frozenset({"gentle", "firm", "sharp"})


class ResponseAssessmentContractError(ValueError):
    """A reply decision would become fabricated, manipulative, or non-natural."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise ResponseAssessmentContractError(error)
    return text


def _assert_body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key or "").strip().lower()
            if name in _BODY_FIELDS or name.startswith(("raw_", "message_")) or name.endswith(("_body", "_content", "_text")):
                raise ResponseAssessmentContractError("response_assessment_body_free_violation")
            _assert_body_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_body_free(item)


def _exact_keys(value: Mapping[str, object], allowed: frozenset[str], *, error: str) -> None:
    if set(value) - allowed:
        raise ResponseAssessmentContractError(error)


def _unit_interval(value: object, *, error: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ResponseAssessmentContractError(error) from exc
    if not 0 <= result <= 1:
        raise ResponseAssessmentContractError(error)
    return result


def _stance(value: object, *, speech_act: str, grounding_status: str) -> dict:
    if not isinstance(value, Mapping):
        raise ResponseAssessmentContractError("response_assessment_stance_invalid")
    _exact_keys(value, frozenset({"kind", "confidence", "basis_refs", "missing_fact_refs"}), error="response_assessment_stance_invalid")
    kind = str(value.get("kind") or "").strip()
    if kind not in _STANCE_KINDS:
        raise ResponseAssessmentContractError("response_assessment_stance_invalid")
    confidence = _unit_interval(value.get("confidence"), error="response_assessment_stance_invalid")
    basis = value.get("basis_refs")
    missing = value.get("missing_fact_refs")
    if not isinstance(basis, list) or not isinstance(missing, list) or len(basis) > 32 or len(missing) > 32:
        raise ResponseAssessmentContractError("response_assessment_stance_invalid")
    normalized = {
        "kind": kind,
        "confidence": confidence,
        "basis_refs": [_ref(item, error="response_assessment_stance_invalid") for item in basis],
        "missing_fact_refs": [_ref(item, error="response_assessment_stance_invalid") for item in missing],
    }
    if speech_act in {"evaluate", "challenge"} and kind not in {"none", "uncertain"} and not normalized["basis_refs"]:
        raise ResponseAssessmentContractError("response_assessment_stance_basis_required")
    if grounding_status == "unknown" and (speech_act in {"answer", "evaluate", "explain", "research_answer"} or kind not in {"none", "uncertain"}):
        raise ResponseAssessmentContractError("response_assessment_unknown_fact_conclusion")
    if grounding_status == "unknown" and kind != "uncertain":
        raise ResponseAssessmentContractError("response_assessment_unknown_fact_conclusion")
    return normalized


def make_response_assessment(value: Mapping[str, object]) -> dict:
    """Normalize a fact-first reply assessment without forcing a visible format."""

    if not isinstance(value, Mapping):
        raise ResponseAssessmentContractError("response_assessment_invalid")
    _assert_body_free(value)
    _exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "scope_type",
                "scope_ref",
                "topic_ref",
                "topic_revision",
                "target_ref",
                "reply_obligation",
                "speech_act",
                "grounding_status",
                "stance",
                "research_disposition",
                "task_continuation",
                "policy_version",
            }
        ),
        error="response_assessment_invalid",
    )
    if value.get("schema_version") != 1:
        raise ResponseAssessmentContractError("response_assessment_schema_invalid")
    scope_type = str(value.get("scope_type") or "").strip()
    if scope_type not in _SCOPE_TYPES:
        raise ResponseAssessmentContractError("response_assessment_scope_invalid")
    try:
        revision = int(value.get("topic_revision"))
    except (TypeError, ValueError) as exc:
        raise ResponseAssessmentContractError("response_assessment_topic_invalid") from exc
    if revision < 0:
        raise ResponseAssessmentContractError("response_assessment_topic_invalid")
    obligation = str(value.get("reply_obligation") or "").strip()
    speech_act = str(value.get("speech_act") or "").strip()
    grounding_status = str(value.get("grounding_status") or "").strip()
    research = str(value.get("research_disposition") or "").strip()
    continuation = str(value.get("task_continuation") or "").strip()
    if obligation not in _REPLY_OBLIGATIONS or speech_act not in _SPEECH_ACTS or grounding_status not in _GROUNDING:
        raise ResponseAssessmentContractError("response_assessment_invalid")
    if research not in _RESEARCH_DISPOSITIONS or continuation not in _TASK_CONTINUATIONS:
        raise ResponseAssessmentContractError("response_assessment_invalid")
    if grounding_status == "unknown" and research == "not_needed":
        raise ResponseAssessmentContractError("response_assessment_unknown_fact_conclusion")
    stance = _stance(value.get("stance"), speech_act=speech_act, grounding_status=grounding_status)
    return {
        "schema_version": 1,
        "scope_type": scope_type,
        "scope_ref": _ref(value.get("scope_ref"), error="response_assessment_ref_invalid"),
        "topic_ref": _ref(value.get("topic_ref"), error="response_assessment_ref_invalid"),
        "topic_revision": revision,
        "target_ref": _ref(value.get("target_ref"), error="response_assessment_ref_invalid"),
        "reply_obligation": obligation,
        "speech_act": speech_act,
        "grounding_status": grounding_status,
        "stance": stance,
        "research_disposition": research,
        "task_continuation": continuation,
        "policy_version": _ref(value.get("policy_version"), error="response_assessment_ref_invalid"),
    }


def make_affective_boundary_response(value: Mapping[str, object]) -> dict:
    """Permit a grounded, sharp boundary while preserving every due work obligation."""

    if not isinstance(value, Mapping):
        raise ResponseAssessmentContractError("affective_boundary_invalid")
    _assert_body_free(value)
    _exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "scope_type",
                "scope_ref",
                "topic_ref",
                "topic_revision",
                "target_ref",
                "trigger_kind",
                "trigger_evidence_refs",
                "affect",
                "intensity",
                "response_style",
                "task_continuation",
                "expires_after_turns",
                "policy_version",
            }
        ),
        error="affective_boundary_invalid",
    )
    if value.get("schema_version") != 1 or str(value.get("scope_type") or "").strip() not in _SCOPE_TYPES:
        raise ResponseAssessmentContractError("affective_boundary_invalid")
    try:
        revision = int(value.get("topic_revision"))
        expiry = int(value.get("expires_after_turns"))
    except (TypeError, ValueError) as exc:
        raise ResponseAssessmentContractError("affective_boundary_invalid") from exc
    if revision < 0 or not 1 <= expiry <= 3:
        raise ResponseAssessmentContractError("affective_boundary_invalid")
    trigger = str(value.get("trigger_kind") or "").strip()
    affect = str(value.get("affect") or "").strip()
    style = str(value.get("response_style") or "").strip()
    if trigger not in _BOUNDARY_TRIGGERS or affect not in _BOUNDARY_AFFECTS or style not in _BOUNDARY_STYLES:
        raise ResponseAssessmentContractError("affective_boundary_invalid")
    if value.get("task_continuation") != "preserve_due_work":
        raise ResponseAssessmentContractError("affective_boundary_due_work_required")
    triggers = value.get("trigger_evidence_refs")
    if not isinstance(triggers, list) or not 1 <= len(triggers) <= 32:
        raise ResponseAssessmentContractError("affective_boundary_trigger_required")
    return {
        "schema_version": 1,
        "scope_type": str(value["scope_type"]),
        "scope_ref": _ref(value.get("scope_ref"), error="affective_boundary_ref_invalid"),
        "topic_ref": _ref(value.get("topic_ref"), error="affective_boundary_ref_invalid"),
        "topic_revision": revision,
        "target_ref": _ref(value.get("target_ref"), error="affective_boundary_ref_invalid"),
        "trigger_kind": trigger,
        "trigger_evidence_refs": [_ref(item, error="affective_boundary_ref_invalid") for item in triggers],
        "affect": affect,
        "intensity": _unit_interval(value.get("intensity"), error="affective_boundary_invalid"),
        "response_style": style,
        "task_continuation": "preserve_due_work",
        "expires_after_turns": expiry,
        "policy_version": _ref(value.get("policy_version"), error="affective_boundary_ref_invalid"),
    }


__all__ = [
    "ResponseAssessmentContractError",
    "make_affective_boundary_response",
    "make_response_assessment",
]
