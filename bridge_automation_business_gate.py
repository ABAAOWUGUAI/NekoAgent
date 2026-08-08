"""Server-owned business-verdict and internal-prose gates for automation.

The 2026-08-08 defect was a ``turn.completed`` turn whose only output was an
internal sandbox/limitation note being marked ``done/ok=1`` and then pushed as
"已完成".  This module separates:

- ``automation_leak_gate`` — blocks internal runtime/tooling/sandbox prose from
  ever being treated as a business result (stable Chinese failure, stable
  error code, no raw prose echoed).
- ``evaluate_automation_business_verdict`` — verifies that a capability result
  actually satisfies its business contract (structured evidence, item count,
  stable repo identity, source URL, dedupe) before it may be called success.

No model decides a capability, a permission, or a verdict here.  The module is
importable by both the Bridge and the cross-database reconciler without a
circular import.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Closed-set internal-prose signatures.  Matching is deliberately
# conservative: only prose that reads as an internal runtime/tooling note is
# blocked, never a normal user-facing sentence that happens to mention a word.
_INTERNAL_PROSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"bubblewrap", re.IGNORECASE),
    re.compile(r"restrictnamespaces", re.IGNORECASE),
    re.compile(r"no permissions? to create (?:a )?new namespace", re.IGNORECASE),
    re.compile(r"\bnamespace\b", re.IGNORECASE),
    re.compile(r"looking at my (?:function list|tools)|my (?:available )?tools?\b", re.IGNORECASE),
    re.compile(r"tool[_ -]?search", re.IGNORECASE),
    re.compile(r"given (?:these|those) limitations", re.IGNORECASE),
    re.compile(r"the sandbox(?:['’]s)? (?:command runner|executor).{0,40}(?:broken|unavailable|cannot)", re.IGNORECASE),
    re.compile(r"execution limited", re.IGNORECASE),
)

# Stable user-safe error codes; the message layer maps them to Chinese.
LEAK_BLOCKED_ERROR_KIND = "no_business_evidence"


def automation_leak_gate(text: object) -> dict:
    """Return ``ok=False`` when ``text`` is internal runtime/tooling prose.

    A blocked result carries a stable ``error_kind`` and a short ``reason``;
    the caller renders a Chinese, sanitised failure instead of echoing the raw
    prose.
    """

    value = str(text or "").strip()
    if not value:
        return {"ok": False, "reason": "empty_output", "error_kind": LEAK_BLOCKED_ERROR_KIND}
    for pattern in _INTERNAL_PROSE_PATTERNS:
        if pattern.search(value):
            return {
                "ok": False,
                "reason": "internal_runtime_prose",
                "error_kind": LEAK_BLOCKED_ERROR_KIND,
            }
    return {"ok": True, "reason": "", "error_kind": ""}


def _stable_repo_identity(item: Mapping) -> str:
    return str(item.get("repo") or item.get("name") or "").strip()


def _item_source_url(item: Mapping) -> str:
    return str(item.get("url") or item.get("html_url") or "").strip()


# Closed-set topic keywords for the GitHub AI / AI Agent contract.  Matching is
# deliberately conservative: only clearly AI/agent signal counts, so the "10 条
# AI/AI Agent 热门项目" business contract is actually enforced rather than
# reported as success on generic trending repos.
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ai-agent": (
        "ai agent", "agent", "llm", "autonomous", "langchain", "llamaindex",
        "copilot", "mcp", "assistant", "chatbot", "chat bot", "rag", "genai",
        "multiagent", "multi-agent", "智能体", "代理", "大模型", "模型",
        "gpt", "claude", "ollama", "openai", "anthropic",
    ),
    "ai": (
        "ai", "artificial intelligence", "llm", "machine learning", "deep learning",
        "neural", "transformer", "gpt", "claude", "diffusion", "genai", "rag",
        "model", "大模型", "人工智能", "机器学习", "模型",
    ),
}


def _item_topic_relevant(item: Mapping, topic: str) -> bool:
    """Deterministic topic-relevance check over repo identity + description."""
    keywords = _TOPIC_KEYWORDS.get(topic)
    if not keywords:
        return True  # no topic requested -> any repo satisfies the contract
    text = " ".join(
        (
            str(item.get("repo") or item.get("name") or ""),
            str(item.get("description") or ""),
        )
    ).lower()
    return any(keyword in text for keyword in keywords)


def evaluate_automation_business_verdict(
    capability_id: str,
    result: Mapping,
    contract_arguments: Mapping | None = None,
) -> dict:
    """Verify a capability result satisfies its fixed business contract.

    Only ``github.trending.read`` is currently bound to a structured verdict.
    A generic ``agent_task`` has no typed contract here and is not called
    "success" by this function; it must prove itself through the task worker
    path (leak gate + reconcile checks).
    """

    args = dict(contract_arguments or {})
    if str(capability_id or "") != "github.trending.read":
        return {
            "passed": False,
            "status": "blocked",
            "reason": "unsupported_capability_for_verdict",
            "error_kind": "business_verdict_unsupported",
        }
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(
        not isinstance(item, Mapping) for item in evidence
    ):
        return {
            "passed": False,
            "status": "blocked",
            "reason": "evidence_missing",
            "error_kind": "github_trending_evidence_missing",
        }
    output = result.get("output")
    if not isinstance(output, Mapping):
        return {
            "passed": False,
            "status": "blocked",
            "reason": "output_not_structured",
            "error_kind": "github_trending_output_invalid",
        }
    items = output.get("items")
    if not isinstance(items, list):
        return {
            "passed": False,
            "status": "blocked",
            "reason": "output_items_missing",
            "error_kind": "github_trending_output_invalid",
        }
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    if len(items) < limit:
        return {
            "passed": False,
            "status": "blocked",
            "reason": f"insufficient_items_{len(items)}_of_{limit}",
            "error_kind": "github_trending_insufficient_items",
        }
    topic = str(args.get("topic") or "").strip().lower()
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            return {
                "passed": False,
                "status": "blocked",
                "reason": "item_not_structured",
                "error_kind": "github_trending_item_invalid",
            }
        identity = _stable_repo_identity(item)
        if not identity:
            return {
                "passed": False,
                "status": "blocked",
                "reason": "item_missing_repo_identity",
                "error_kind": "github_trending_item_invalid",
            }
        if not _item_source_url(item):
            return {
                "passed": False,
                "status": "blocked",
                "reason": "item_missing_source_url",
                "error_kind": "github_trending_item_invalid",
            }
        key = identity.lower()
        if key in seen:
            return {
                "passed": False,
                "status": "blocked",
                "reason": "duplicate_repo",
                "error_kind": "github_trending_duplicate_item",
            }
        seen.add(key)
        if not _item_topic_relevant(item, topic):
            return {
                "passed": False,
                "status": "blocked",
                "reason": f"item_off_topic:{identity}",
                "error_kind": "github_trending_topic_mismatch",
            }
    return {"passed": True, "status": "passed", "reason": "", "error_kind": ""}


__all__ = [
    "LEAK_BLOCKED_ERROR_KIND",
    "automation_leak_gate",
    "evaluate_automation_business_verdict",
]
