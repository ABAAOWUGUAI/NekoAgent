"""Unified deterministic request routing contract.

The router only proposes and arbitrates action candidates. Domain executors
remain the authority for permissions, approvals, writes, and evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
    """Normalize only bounded lexical variants; raw inbound remains audited."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"\s+", " ", text)
    for source, target in {"准人": "准入", "准入名单": "准入列表", "白名单": "准入列表"}.items():
        text = text.replace(source, target)
    return re.sub(r"准入\s+列表", "准入列表", text)


def _automation_candidate(action: Mapping[str, object]) -> RouteCandidate:
    kind = str(action.get("action_type") or "")
    operation = {"automation_create": "mutate", "automation_update": "mutate", "automation_disable": "mutate", "automation_run_now": "execute", "automation_create_clarification": "clarify"}.get(kind, "mutate")
    return RouteCandidate("automation", kind, operation, True, 1.0, dict(action), "deterministic.automation")


def _qq_candidate(action: Mapping[str, object]) -> RouteCandidate:
    kind = str(action.get("action_type") or "")
    operation = "read" if kind.endswith(("_list", "_status_read", "_diagnose")) else ("clarify" if kind.endswith("_clarification") else "mutate")
    return RouteCandidate("qq", kind, operation, True, 1.0, dict(action), "deterministic.qq")


def resolve_request(message: str, history: list[dict] | None = None, *, current_group_id: str = "") -> dict:
    """Resolve candidates only; never execute or authorize an action."""

    original = str(message or "").strip()
    normalized = normalize_request_text(original)
    candidates: list[RouteCandidate] = []
    automation = parse_automation_action(normalized, history, current_group_id=current_group_id)
    if automation:
        candidates.append(_automation_candidate(automation))
    qq = parse_qq_admin_action(normalized, history, current_group_id=current_group_id)
    if qq:
        candidates.append(_qq_candidate(qq))
    domains = {item.domain for item in candidates}
    status = "unresolved" if not candidates else ("resolved" if len(candidates) == 1 else ("mixed" if len(domains) > 1 else "ambiguous"))
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
    if not isinstance(decision, Mapping):
        return {}
    candidates = decision.get("candidates") if isinstance(decision.get("candidates"), list) else []
    return {
        "route_schema_version": int(decision.get("schema_version") or ROUTER_SCHEMA_VERSION),
        "route_status": str(decision.get("status") or "unresolved"),
        "route_operation": str(decision.get("operation") or ""),
        "route_candidate_count": len(candidates),
        "route_action_types": [str(item.get("action_type") or "") for item in candidates if isinstance(item, Mapping)][:8],
        "route_evidence_required": bool(decision.get("evidence_required")),
    }


def initial_route_disposition(message: str, history: list[dict] | None = None, *, current_group_id: str = "") -> tuple[dict, dict | None]:
    decision = resolve_request(message, history, current_group_id=current_group_id)
    if decision["status"] == "mixed":
        # Explicit cross-domain actions are composable work in one message.
        # The dispatcher builds one Interaction Plan instead of forcing the
        # Owner to choose a domain or silently dropping a candidate.
        if not any(
            isinstance(item, dict) and str(item.get("operation") or "") == "clarify"
            for item in decision.get("candidates") or []
        ):
            return decision, None
    if decision["status"] not in {"mixed", "ambiguous"}:
        return decision, None
    labels = "、".join(str(item.get("action_type") or "") for item in decision["candidates"] if isinstance(item, dict))
    return decision, {"ok": True, "dispatch": "route_clarification", "reply": f"这条消息包含多个需要分别确认的操作（{labels or '多个动作'}）。我不会只执行其中一个；请把要先做的动作说明清楚。", "route_decision": decision, "route_metadata": route_metadata(decision)}


def route_execution_missing_result(decision: Mapping[str, object] | None) -> dict | None:
    if not isinstance(decision, Mapping) or decision.get("status") != "resolved" or not decision.get("evidence_required"):
        return None
    candidate = next((item for item in decision.get("candidates") or [] if isinstance(item, dict)), {})
    return {"ok": True, "dispatch": "route_execution_missing", "reply": "我识别到了这项可核验操作，但本轮没有拿到服务端回执，因此不会用历史内容代替实时结果。", "action_receipts": [{"action_type": str(candidate.get("action_type") or "route.resolve"), "status": "failed", "facts": {"reason": "route_executor_no_receipt"}}], "route_decision": dict(decision), "route_metadata": route_metadata(decision)}


__all__ = ["ROUTER_SCHEMA_VERSION", "RouteCandidate", "initial_route_disposition", "normalize_request_text", "resolve_request", "route_execution_missing_result", "route_metadata"]
