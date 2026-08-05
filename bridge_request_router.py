#!/usr/bin/env python3
"""Unified request routing contract for natural-language assistant turns.

This module deliberately does not execute actions or grant capabilities.  It
collects bounded deterministic candidates from domain recognizers, classifies
their operation/evidence requirements, and gives the Bridge one arbitration
point before any domain executor or generic chat model runs.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import re
import unicodedata
from typing import Mapping

from bridge_automation_actions import parse_automation_action
from bridge_qq_admin_actions import parse_qq_admin_action


ROUTER_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RouteCandidate:
    domain: str
    action_type: str
    operation: str
    evidence_required: bool
    confidence: float
    parameters: dict
    source: str

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_request_text(value: object) -> str:
    """Apply bounded, versioned lexical normalization only for routing.

    The original inbound text remains the audit source.  This layer handles
    Unicode width/spacing and a small controlled alias table, rather than
    accumulating one-off branches in each domain parser.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"\s+", " ", text)
    # Common speech/input variants observed in Owner turns.  Keep this table
    # domain-neutral and bounded; semantic resolution remains domain-owned.
    aliases = {
        "准人": "准入",
        "准入名单": "准入列表",
        "白名单": "准入列表",
    }
    for source, target in aliases.items():
        text = text.replace(source, target)
    text = re.sub(r"准入\s+列表", "准入列表", text)
    text = re.sub(r"白名单\s+列表", "白名单列表", text)
    return text


def _candidate_from_automation(action: Mapping[str, object]) -> RouteCandidate:
    action_type = str(action.get("action_type") or "")
    operation = {
        "automation_create": "mutate",
        "automation_update": "mutate",
        "automation_disable": "mutate",
        "automation_run_now": "execute",
        "automation_create_clarification": "clarify",
    }.get(action_type, "mutate")
    return RouteCandidate(
        domain="automation",
        action_type=action_type,
        operation=operation,
        evidence_required=True,
        confidence=1.0,
        parameters=dict(action),
        source="deterministic.automation",
    )


def _candidate_from_qq(action: Mapping[str, object]) -> RouteCandidate:
    action_type = str(action.get("action_type") or "")
    operation = "read" if action_type.endswith(("_list", "_status_read", "_diagnose")) else (
        "clarify" if action_type.endswith("_clarification") else "mutate"
    )
    return RouteCandidate(
        domain="qq",
        action_type=action_type,
        operation=operation,
        evidence_required=True,
        confidence=1.0,
        parameters=dict(action),
        source="deterministic.qq",
    )


def resolve_request(
    message: str,
    history: list[dict] | None = None,
    *,
    current_group_id: str = "",
) -> dict:
    """Return a route decision without executing or authorising anything."""

    original = str(message or "").strip()
    normalized = normalize_request_text(original)
    candidates: list[RouteCandidate] = []
    automation = parse_automation_action(normalized, history, current_group_id=current_group_id)
    if automation:
        candidates.append(_candidate_from_automation(automation))
    qq = parse_qq_admin_action(normalized, history, current_group_id=current_group_id)
    if qq:
        candidates.append(_candidate_from_qq(qq))

    # Deterministic recognizers are authoritative only when they agree on one
    # executable domain.  Multiple domains are preserved as a mixed plan so a
    # future executor can run them compositionally; current dispatch must not
    # silently choose one and discard the other.
    domains = {item.domain for item in candidates}
    if not candidates:
        status = "unresolved"
    elif len(candidates) == 1:
        status = "resolved"
    else:
        status = "mixed" if len(domains) > 1 else "ambiguous"
    return {
        "schema_version": ROUTER_SCHEMA_VERSION,
        "status": status,
        "original_length": len(original),
        "normalized_length": len(normalized),
        "candidates": [item.as_dict() for item in candidates],
        "evidence_required": any(item.evidence_required for item in candidates),
        "operation": candidates[0].operation if len(candidates) == 1 else "mixed",
    }


def route_metadata(decision: Mapping[str, object] | None) -> dict:
    """Expose only bounded route metadata for persistence/diagnostics."""

    if not isinstance(decision, Mapping):
        return {}
    candidates = decision.get("candidates") if isinstance(decision.get("candidates"), list) else []
    return {
        "route_schema_version": int(decision.get("schema_version") or ROUTER_SCHEMA_VERSION),
        "route_status": str(decision.get("status") or "unresolved"),
        "route_operation": str(decision.get("operation") or ""),
        "route_candidate_count": len(candidates),
        "route_action_types": [
            str(item.get("action_type") or "")
            for item in candidates
            if isinstance(item, Mapping)
        ][:8],
        "route_evidence_required": bool(decision.get("evidence_required")),
    }


def initial_route_disposition(
    message: str,
    history: list[dict] | None = None,
    *,
    current_group_id: str = "",
) -> tuple[dict, dict | None]:
    """Resolve an inbound request and fail closed when it needs composition."""

    decision = resolve_request(message, history, current_group_id=current_group_id)
    if decision.get("status") == "mixed":
        # Different explicit domains are composable work in one message.  The
        # route dispatcher will build one Interaction Plan and execute each
        # server-owned candidate; do not force the Owner to choose a domain.
        candidates = decision.get("candidates") or []
        if not any(
            isinstance(item, dict) and str(item.get("operation") or "") == "clarify"
            for item in candidates
        ):
            return decision, None
    if decision.get("status") not in {"mixed", "ambiguous"}:
        return decision, None
    candidates = decision.get("candidates") or []
    labels = "、".join(
        str(item.get("action_type") or "")
        for item in candidates
        if isinstance(item, dict)
    )
    return decision, {
        "ok": True,
        "dispatch": "route_clarification",
        "reply": (
            "这条消息包含多个需要分别确认的操作（"
            f"{labels or '多个动作'}）。我不会只执行其中一个；请把要先做的动作说明清楚。"
        ),
        "route_decision": decision,
        "route_metadata": route_metadata(decision),
    }


def route_execution_missing_result(decision: Mapping[str, object] | None) -> dict | None:
    """Prevent an evidence-required route from falling through to chat."""

    if not isinstance(decision, Mapping) or decision.get("status") != "resolved":
        return None
    if not decision.get("evidence_required"):
        return None
    candidate = next(
        (item for item in decision.get("candidates") or [] if isinstance(item, dict)),
        {},
    )
    action_type = str(candidate.get("action_type") or "route.resolve")
    return {
        "ok": True,
        "dispatch": "route_execution_missing",
        "reply": "我识别到了这项可核验操作，但本轮没有拿到服务端回执，因此不会用历史内容代替实时结果。",
        "action_receipts": [{
            "action_type": action_type,
            "status": "failed",
            "facts": {"reason": "route_executor_no_receipt"},
        }],
        "route_decision": dict(decision),
        "route_metadata": route_metadata(decision),
    }


__all__ = [
    "ROUTER_SCHEMA_VERSION",
    "RouteCandidate",
    "normalize_request_text",
    "initial_route_disposition",
    "resolve_request",
    "route_execution_missing_result",
    "route_metadata",
]
