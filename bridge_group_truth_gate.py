"""Group final truth gate, reply-plan enforcement and persona cadence budget.

The 2026-08-08 group defects were: media/sensory claims without evidence,
echoing unverified facts, target/topic mismatch, ``degraded`` results still
being sent, and mechanical signature-token overuse.  This module owns the
deterministic, server-side checks that decide whether a draft may be sent
(B3/B4) and how the signature budget is counted from confirmed projections
only (B5).  A model may reword a draft, but it may never upgrade a media
state or a claim type here.

Execution order used by the caller:

    raw draft
     -> normalize exact delivery text
     -> target/topic consistency check
     -> grounding/fact check
     -> safety and anti-sycophancy check
     -> persona cadence check
     -> passed: send
     -> failed: one controlled rewrite preserving evidence
     -> still failed: block/silent, record reason, do not enqueue Delivery
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from bridge_social_reply import group_reply_style_issues

# Stable issue taxonomy from the repair command (B4).
ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE = "media_claim_without_evidence"
ISSUE_FABRICATED_PERSONAL_EXPERIENCE = "fabricated_personal_experience"
ISSUE_UNSUPPORTED_SPECIFIC_FACT = "unsupported_specific_fact"
ISSUE_REPLY_TARGET_MISMATCH = "reply_target_mismatch"
ISSUE_TOPIC_ANCHOR_MISMATCH = "topic_anchor_mismatch"
ISSUE_SYCOPHANTIC_AGREEMENT = "sycophantic_agreement_without_basis"
ISSUE_SIGNATURE_OVERUSE = "persona_signature_overuse"

SEND_STATE_PASSED = "passed"
SEND_STATE_REWRITTEN_PASSED = "rewritten_passed"
SEND_STATE_BLOCKED = "blocked"
# Historical compatibility value that must never be treated as sendable.
SEND_STATE_DEGRADED = "degraded"

# Closed set of signature tokens (B5).  Server-configurable bounds live in
# ``signature_budget_limits``; the default is 4/10 and max 2 consecutive.
SIGNATURE_TOKENS = ("喵", "～")
DEFAULT_SIGNATURE_BUDGET_WINDOW = 10
DEFAULT_SIGNATURE_BUDGET_MAX = 4
DEFAULT_SIGNATURE_BUDGET_MAX_CONSECUTIVE = 2
SIGNATURE_MIN_WINDOW = 4
SIGNATURE_MAX_WINDOW = 40
SIGNATURE_MIN_MAX = 0
SIGNATURE_MAX_MAX = 10
SIGNATURE_MIN_CONSECUTIVE = 1
SIGNATURE_MAX_CONSECUTIVE = 5


def normalize_signature_budget(value: Mapping | None) -> dict:
    value = value if isinstance(value, Mapping) else {}
    window = value.get("window") if isinstance(value, Mapping) else None
    max_tokens = value.get("max_tokens") if isinstance(value, Mapping) else None
    max_consecutive = value.get("max_consecutive") if isinstance(value, Mapping) else None
    try:
        window = int(window)
    except (TypeError, ValueError):
        window = DEFAULT_SIGNATURE_BUDGET_WINDOW
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_SIGNATURE_BUDGET_MAX
    try:
        max_consecutive = int(max_consecutive)
    except (TypeError, ValueError):
        max_consecutive = DEFAULT_SIGNATURE_BUDGET_MAX_CONSECUTIVE
    return {
        "window": max(SIGNATURE_MIN_WINDOW, min(window, SIGNATURE_MAX_WINDOW)),
        "max_tokens": max(SIGNATURE_MIN_MAX, min(max_tokens, SIGNATURE_MAX_MAX)),
        "max_consecutive": max(
            SIGNATURE_MIN_CONSECUTIVE,
            min(max_consecutive, SIGNATURE_MAX_CONSECUTIVE),
        ),
    }


def _has_signature_token(text: str) -> bool:
    value = str(text or "").strip()
    return any(token in value for token in SIGNATURE_TOKENS)


def signature_budget_issues(
    draft: str,
    recent_confirmed: Sequence[Mapping],
    *,
    budget: Mapping | None = None,
) -> list[str]:
    """Return overuse issues for a draft against confirmed projections only.

    ``recent_confirmed`` is the rolling window of *confirmed* assistant group
    projections (post-ACK), never model drafts or failed deliveries.
    """

    limits = normalize_signature_budget(budget)
    issues: list[str] = []
    if not _has_signature_token(draft):
        return issues
    window = int(limits["window"])
    recent = [item for item in recent_confirmed if isinstance(item, Mapping)][-window:]
    used = sum(1 for item in recent if _has_signature_token(str(item.get("content") or "")))
    if used >= int(limits["max_tokens"]):
        issues.append(ISSUE_SIGNATURE_OVERUSE)
    consecutive = 0
    for item in reversed(recent):
        if _has_signature_token(str(item.get("content") or "")):
            consecutive += 1
            if consecutive >= int(limits["max_consecutive"]):
                issues.append(ISSUE_SIGNATURE_OVERUSE)
                break
        else:
            break
    return issues


def group_final_send_state(gate_value: object) -> str:
    """Map a historical ``group_style_gate`` value to a final send decision.

    ``degraded`` is no longer a soft warning; it is blocked.  ``not_applicable``
    and empty mean the style gate does not apply to this path (control/work/
    non-group), so a genuine reply is not blocked by a missing style value.
    """

    value = str(gate_value or "").strip().lower()
    if value in {SEND_STATE_PASSED, SEND_STATE_REWRITTEN_PASSED}:
        return value
    if value == SEND_STATE_DEGRADED:
        return SEND_STATE_BLOCKED
    if value == SEND_STATE_BLOCKED:
        return SEND_STATE_BLOCKED
    if value in {"provider_failed", "not_applicable", ""}:
        return SEND_STATE_PASSED if value != "provider_failed" else SEND_STATE_BLOCKED
    # Unknown style state: fail closed to blocked rather than silently sending.
    return SEND_STATE_BLOCKED


_MEDIA_CLAIM_PATTERNS = (
    re.compile(r"(?:这个|这张|这图|图中|图片|画面|杯子|玩偶|立牌|手办).{0,20}(?:是|像|颜色|造型|立体|材质)", re.IGNORECASE),
    re.compile(r"(?:我看到|看起来|看上去|颜色是|造型是)", re.IGNORECASE),
)
_SENSORY_CLAIM_PATTERNS = (
    re.compile(r"(?:我听到|听过|在循环|播放的是|这首歌是|声音.{0,6}(?:是|像))", re.IGNORECASE),
)
_EXPERIENCE_CLAIM_PATTERNS = (
    re.compile(r"(?:我研究过|我记得|我执行过|我们之前|我之前)", re.IGNORECASE),
)
_STRONG_AGREEMENT_PATTERNS = (
    re.compile(r"^(?:确实|没错|就是这样|太对了|完全同意|是的没错)", re.IGNORECASE),
)
_SPECIFIC_FACT_PATTERNS = (
    re.compile(r"\d+%|百分之[\d一二三四五六七八九十百]+\d*|¥|￥|\$\s?\d+|\d+\s*(?:元|块|万|亿|倍|家|条)"),
    re.compile(r"(?:违法|犯法|稳赚|必赔|板上钉钉|铁定|绝对会)", re.IGNORECASE),
)


def _media_claim_without_evidence(reply: str, envelope: Mapping) -> str | None:
    media = envelope.get("media") if isinstance(envelope.get("media"), Mapping) else {}
    visual_context = str(media.get("visual_context") or "none")
    observation = str(media.get("observation") or "none")
    if visual_context == "ready" and observation not in {"none", "deferred", "blocked"}:
        return None
    for pattern in _MEDIA_CLAIM_PATTERNS:
        if pattern.search(reply):
            return ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE
    for pattern in _SENSORY_CLAIM_PATTERNS:
        if pattern.search(reply):
            return ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE
    return None


def _fabricated_personal_experience(reply: str) -> str | None:
    for pattern in _EXPERIENCE_CLAIM_PATTERNS:
        if pattern.search(reply):
            return ISSUE_FABRICATED_PERSONAL_EXPERIENCE
    return None


def _unsupported_specific_fact(reply: str, envelope: Mapping) -> str | None:
    forbidden = envelope.get("forbidden_claim_types") if isinstance(envelope.get("forbidden_claim_types"), list) else []
    if "concrete_attribution" in forbidden:
        for pattern in _SPECIFIC_FACT_PATTERNS:
            if pattern.search(reply):
                return ISSUE_UNSUPPORTED_SPECIFIC_FACT
    return None


def _sycophantic_agreement(reply: str, envelope: Mapping) -> str | None:
    allowed = envelope.get("allowed_claim_types") if isinstance(envelope.get("allowed_claim_types"), list) else []
    if "agree_with_reference" in allowed:
        return None
    for pattern in _STRONG_AGREEMENT_PATTERNS:
        if pattern.search(reply) and not re.search(r"(?:参考|依据|我看到|根据)", reply):
            return ISSUE_SYCOPHANTIC_AGREEMENT
    return None


def _reply_target_mismatch(reply: str, envelope: Mapping) -> str | None:
    target = envelope.get("target") if isinstance(envelope.get("target"), Mapping) else {}
    if target.get("ambiguous"):
        # Ambiguous target: a draft that names a specific addressee or a
        # strong stance as if it knows the target is a mismatch.
        if re.search(r"(?:甲|乙|丙|你说的)", reply):
            return ISSUE_REPLY_TARGET_MISMATCH
    return None


def group_final_truth_issues(
    reply: str,
    grounding_envelope: Mapping | None = None,
    *,
    recent_confirmed: Sequence[Mapping] | None = None,
    signature_budget: Mapping | None = None,
) -> list[str]:
    """Return the ordered truth-gate issues for one draft.

    Deterministic rules are the minimum guarantee; a model judge may only be
    an additive reviewer and must fail closed.
    """

    envelope = grounding_envelope if isinstance(grounding_envelope, Mapping) else {}
    issues: list[str] = []
    media_issue = _media_claim_without_evidence(reply, envelope)
    if media_issue:
        issues.append(media_issue)
    experience_issue = _fabricated_personal_experience(reply)
    if experience_issue:
        issues.append(experience_issue)
    fact_issue = _unsupported_specific_fact(reply, envelope)
    if fact_issue:
        issues.append(fact_issue)
    target_issue = _reply_target_mismatch(reply, envelope)
    if target_issue:
        issues.append(target_issue)
    sycophancy_issue = _sycophantic_agreement(reply, envelope)
    if sycophancy_issue:
        issues.append(sycophancy_issue)
    if recent_confirmed is not None:
        cadence_issues = signature_budget_issues(
            reply,
            recent_confirmed,
            budget=signature_budget,
        )
        issues.extend(issue for issue in cadence_issues if issue not in issues)
    return issues


def apply_group_safety_gate(result: dict, payload: dict, conversation_frame: dict | None = None, reply: str = "") -> str:
    """Cancel an uninvited targeted judgement before the final truth gate."""

    if (
        reply
        and str((conversation_frame or {}).get("attention") or "") == "active_continuation"
        and "uninvited_targeted_judgement" in group_reply_style_issues(
            str(payload.get("message") or ""), reply, uninvited=True,
        )
    ):
        result.update({
            "reply": "",
            "output": "",
            "group_safety_blocked": True,
            "group_safety_reason": "uninvited_targeted_judgement",
        })
        return ""
    return reply


def apply_group_final_truth_gate(result: dict, conversation_frame: dict | None = None) -> str:
    """Apply the B4 final truth gate to one draft result in place.

    Mutates ``result`` by clearing ``reply``/``output`` and recording
    ``group_truth_blocked``/``group_truth_send_state``/``group_truth_issues``
    when the draft must not be sent.  Returns the possibly-emptied reply text.
    """

    reply = str(result.get("reply") or "").strip()
    if not reply:
        return reply
    style_gate = str(result.get("group_style_gate") or "")
    final_send_state = group_final_send_state(style_gate)
    grounding_envelope = (
        conversation_frame.get("grounding_envelope")
        if isinstance(conversation_frame, dict)
        else None
    )
    recent_confirmed = [
        item
        for item in (result.get("recent_confirmed") or [])
        if isinstance(item, dict)
    ]
    truth_issues = group_final_truth_issues(
        reply,
        grounding_envelope,
        recent_confirmed=recent_confirmed,
    )
    if final_send_state in {"blocked", "degraded"} or truth_issues:
        result.update({
            "reply": "",
            "output": "",
            "group_truth_blocked": True,
            "group_truth_send_state": final_send_state,
            "group_truth_issues": truth_issues,
            "group_style_gate": "blocked",
        })
        return ""
    return reply


__all__ = [
    "DEFAULT_SIGNATURE_BUDGET_MAX",
    "DEFAULT_SIGNATURE_BUDGET_MAX_CONSECUTIVE",
    "DEFAULT_SIGNATURE_BUDGET_WINDOW",
    "ISSUE_FABRICATED_PERSONAL_EXPERIENCE",
    "ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE",
    "ISSUE_REPLY_TARGET_MISMATCH",
    "ISSUE_SIGNATURE_OVERUSE",
    "ISSUE_SYCOPHANTIC_AGREEMENT",
    "ISSUE_TOPIC_ANCHOR_MISMATCH",
    "ISSUE_UNSUPPORTED_SPECIFIC_FACT",
    "SEND_STATE_BLOCKED",
    "SEND_STATE_DEGRADED",
    "SEND_STATE_PASSED",
    "SEND_STATE_REWRITTEN_PASSED",
    "SIGNATURE_TOKENS",
    "apply_group_final_truth_gate",
    "apply_group_safety_gate",
    "group_final_send_state",
    "group_final_truth_issues",
    "normalize_signature_budget",
    "signature_budget_issues",
]
