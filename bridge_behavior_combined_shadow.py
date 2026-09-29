#!/usr/bin/env python3
"""BE-6 body-free Combined Shadow, with a structural zero-side-effect proof."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_METRICS = frozenset(
    {"total_score", "reply_obligation_recall", "ambient_intrusion_rate", "citation_completeness"}
)
_BODY_KEYS = frozenset(
    {"body", "content", "message", "message_text", "raw_evidence", "raw_text", "text"}
)


class CombinedShadowContractError(ValueError):
    """Raised if a Combined Shadow would leak content or affect formal state."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise CombinedShadowContractError(error)
    return text


def _body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key or "").lower()
            if normalized in _BODY_KEYS or normalized.startswith(("raw_", "message_")) or normalized.endswith(("_body", "_content", "_text")):
                raise CombinedShadowContractError("combined_shadow_body_free_violation")
            _body_free(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _body_free(item)


def _metrics(value: object, *, error: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != _METRICS:
        raise CombinedShadowContractError(error)
    normalized: dict[str, float] = {}
    for name in _METRICS:
        try:
            metric = float(value[name])
        except (TypeError, ValueError) as exc:
            raise CombinedShadowContractError(error) from exc
        if metric < 0:
            raise CombinedShadowContractError(error)
        normalized[name] = metric
    return normalized


def _event(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CombinedShadowContractError("combined_shadow_event_invalid")
    _body_free(value)
    allowed = {"schema_version", "scope_type", "scope_ref", "admitted_scope", "source_event_ref", "trace_ref", "evidence_refs"}
    if set(value) != allowed or value.get("schema_version") != 1 or value.get("scope_type") != "group":
        raise CombinedShadowContractError("combined_shadow_event_invalid")
    if value.get("admitted_scope") is not True:
        raise CombinedShadowContractError("combined_shadow_scope_not_admitted")
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise CombinedShadowContractError("combined_shadow_event_invalid")
    return {
        "scope_type": "group",
        "scope_ref": _ref(value.get("scope_ref"), error="combined_shadow_event_invalid"),
        "source_event_ref": _ref(value.get("source_event_ref"), error="combined_shadow_event_invalid"),
        "trace_ref": _ref(value.get("trace_ref"), error="combined_shadow_event_invalid"),
        "evidence_refs": [_ref(item, error="combined_shadow_event_invalid") for item in evidence_refs],
    }


def _reference(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CombinedShadowContractError("combined_shadow_reference_invalid")
    _body_free(value)
    expected = {"benchmark_ref", "benchmark_hash", "reference_metrics", "hard_failure_count"}
    if set(value) != expected:
        raise CombinedShadowContractError("combined_shadow_reference_invalid")
    try:
        hard_failures = int(value["hard_failure_count"])
    except (TypeError, ValueError) as exc:
        raise CombinedShadowContractError("combined_shadow_reference_invalid") from exc
    if hard_failures < 0 or not str(value.get("benchmark_hash") or "").startswith("sha256:"):
        raise CombinedShadowContractError("combined_shadow_reference_invalid")
    return {
        "benchmark_ref": _ref(value.get("benchmark_ref"), error="combined_shadow_reference_invalid"),
        "benchmark_hash": str(value["benchmark_hash"]),
        "reference_metrics": _metrics(value.get("reference_metrics"), error="combined_shadow_reference_invalid"),
        "hard_failure_count": hard_failures,
    }


def _candidate(value: Mapping[str, object], reference: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CombinedShadowContractError("combined_shadow_candidate_not_eligible")
    _body_free(value)
    expected = {"candidate_ref", "state", "benchmark_ref", "benchmark_hash", "candidate_metrics", "hard_failure_count"}
    if set(value) != expected or value.get("state") != "shadow_eligible":
        raise CombinedShadowContractError("combined_shadow_candidate_not_eligible")
    if value.get("benchmark_ref") != reference["benchmark_ref"] or value.get("benchmark_hash") != reference["benchmark_hash"]:
        raise CombinedShadowContractError("combined_shadow_benchmark_mismatch")
    try:
        hard_failures = int(value["hard_failure_count"])
    except (TypeError, ValueError) as exc:
        raise CombinedShadowContractError("combined_shadow_candidate_not_eligible") from exc
    if hard_failures != 0:
        raise CombinedShadowContractError("combined_shadow_candidate_not_eligible")
    return {
        "candidate_ref": _ref(value.get("candidate_ref"), error="combined_shadow_candidate_not_eligible"),
        "candidate_metrics": _metrics(value.get("candidate_metrics"), error="combined_shadow_candidate_not_eligible"),
        "hard_failure_count": hard_failures,
    }


def run_combined_shadow(event: Mapping[str, object], reference: Mapping[str, object], candidate: Mapping[str, object]) -> dict[str, object]:
    """Compare an admitted event locally; this function owns no outbox or DB handle."""

    normalized_event = _event(event)
    normalized_reference = _reference(reference)
    normalized_candidate = _candidate(candidate, normalized_reference)
    token = json.dumps(
        {
            "event": normalized_event,
            "benchmark_hash": normalized_reference["benchmark_hash"],
            "candidate_ref": normalized_candidate["candidate_ref"],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    record = {
        "combined_shadow_ref": "combined-shadow:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:24],
        "scope_ref": normalized_event["scope_ref"],
        "source_event_ref": normalized_event["source_event_ref"],
        "trace_ref": normalized_event["trace_ref"],
        "evidence_refs": normalized_event["evidence_refs"],
        "benchmark_ref": normalized_reference["benchmark_ref"],
        "benchmark_hash": normalized_reference["benchmark_hash"],
        "candidate_ref": normalized_candidate["candidate_ref"],
        "reference_hard_failure_count": normalized_reference["hard_failure_count"],
        "candidate_hard_failure_count": normalized_candidate["hard_failure_count"],
        "metric_delta": {
            name: normalized_candidate["candidate_metrics"][name] - normalized_reference["reference_metrics"][name]
            for name in sorted(_METRICS)
        },
        "delivery_count": 0,
        "formal_writes": [],
        "side_effects": [],
        "state": "shadow_only",
    }
    assert_combined_shadow_zero_effect(record)
    return record


def assert_combined_shadow_zero_effect(record: Mapping[str, object]) -> None:
    """Verify the only valid Combined Shadow receipt is strictly inert."""

    if not isinstance(record, Mapping):
        raise CombinedShadowContractError("combined_shadow_side_effect_violation")
    _body_free(record)
    if (
        record.get("state") != "shadow_only"
        or record.get("delivery_count") != 0
        or record.get("formal_writes") != []
        or record.get("side_effects") != []
    ):
        raise CombinedShadowContractError("combined_shadow_side_effect_violation")


__all__ = [
    "CombinedShadowContractError",
    "assert_combined_shadow_zero_effect",
    "run_combined_shadow",
]
