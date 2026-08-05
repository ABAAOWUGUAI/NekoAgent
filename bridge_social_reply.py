#!/usr/bin/env python3
"""User-facing daily and group reply normalization."""

from __future__ import annotations

from collections.abc import Callable
import re

from bridge_group_expression import repeated_reply_shape_issue
from bridge_social_identity import (
    guard_casual_identity_reply,
    has_casual_identity_self_disclosure,
    is_style_feedback,
)


GENERIC_OPENINGS = (
    "好的，", "好的。", "收到，", "收到。",
    "当然可以，", "当然可以。", "没问题，", "没问题。",
)

_GROUP_FORMULA_RE = re.compile(
    r"(?:原来(?:还有)?这层|这个角度(?:很|挺|确实)|可以看出|由此可见|不得不说|"
    r"效果确实|确实不一样|这说明|"
    r"难怪.{0,30}原来|"
    r"这种(?:细节|设计|处理).{0,8}(?:有意思|讲究|厉害|到位))"
)
_GROUP_ABSTRACT_TERMS = (
    "渊源",
    "执念",
    "肢体语言",
    "动作设计",
    "情绪表达",
    "这个角度",
    "细节控",
    "这种细节",
    "讲究",
    "太有意思",
    "让人佩服",
    "效果确实",
)
_GROUP_PARENTHETICAL_RE = re.compile(r"[\(（][^()（）\r\n]{0,32}[\)）]")
_GROUP_STOCK_OPENERS = (
    "好家伙",
    "笑死",
    "哈哈",
    "懂了",
    "草",
    "原来如此",
    "确实",
)
_UNINVITED_TARGETED_JUDGEMENT_RE = re.compile(
    r"(?:你|你们).{0,16}(?:跟不上|看不懂|没看懂|不懂|不会|太慢|菜|离谱)",
)
_GENERIC_ASSISTANT_FRAME_RE = re.compile(
    r"^(?:我理解|听起来|感谢(?:你)?(?:的)?分享|很高兴|作为(?:一个)?(?:AI|助手)|希望(?:这些|这).{0,12}(?:有帮助|帮到你))",
)
_GROUP_RELATIONSHIP_ROLE_RE = re.compile(
    r"(?:尊敬的当前助手|主人(?:大人)?|爸爸(?=[，。！？!?~～\s]|$))",
)
_INTERNAL_DIAGNOSTIC_RE = re.compile(
    r"(?:本次请求没有成功完成|并在\s*Web\s*控制台查看|assistant_chat_group_style_initial|ActionReceipt)",
    re.IGNORECASE,
)
_GROUP_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])")
_ANCHOR_STOP_TERMS = {"这个", "那个", "这里", "那里", "什么", "怎么", "可以", "已经", "终于"}


def _bounded_anchor_terms(value: object) -> tuple[str, ...]:
    """Return a small set of literal topic markers without semantic inference."""

    terms: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", str(value or "")[:160]):
        if re.fullmatch(r"[\u4e00-\u9fff]+", run):
            terms.extend(run[index:index + 2] for index in range(len(run) - 1))
        else:
            terms.append(run.lower())
    return tuple(term for term in dict.fromkeys(terms) if term not in _ANCHOR_STOP_TERMS)[:48]


def _looks_generic_without_anchor(reply: object, anchor_text: object) -> bool:
    text = str(reply or "").strip()
    if not _GENERIC_ASSISTANT_FRAME_RE.search(text):
        return False
    terms = _bounded_anchor_terms(anchor_text)
    return bool(terms) and not _has_anchor_terms(text, anchor_text)


def _has_anchor_terms(value: object, anchor_text: object) -> bool:
    terms = _bounded_anchor_terms(anchor_text)
    text = str(value or "").lower()
    return bool(terms) and any(term in text for term in terms)


def _reply_opener(value: object) -> str:
    text = str(value or "").strip()
    for opener in _GROUP_STOCK_OPENERS:
        if text.startswith(opener):
            return opener
    return ""


def group_reply_style_issues(
    request: str,
    reply: str,
    *,
    recent_replies: list[str] | None = None,
    uninvited: bool = False,
    expression_plan: dict | None = None,
    source_reply: str | None = None,
) -> list[str]:
    """Return bounded, explainable reasons a group draft sounds like narration."""

    text = str(reply or "").strip()
    if not text:
        return []
    issues: list[str] = []
    if len(text) > 64:
        issues.append("too_long_for_group")
    if _GROUP_PARENTHETICAL_RE.search(text):
        issues.append("parenthetical_stage_direction")
    if _GROUP_FORMULA_RE.search(text):
        issues.append("summary_formula")
    abstract_hits = sum(term in text for term in _GROUP_ABSTRACT_TERMS)
    if abstract_hits >= 2:
        issues.append("abstract_narration")
    opener = _reply_opener(text)
    recent_openers = {
        item_opener
        for item in (recent_replies or [])
        if (item_opener := _reply_opener(item))
    }
    if opener and opener in recent_openers:
        issues.append("repeated_stock_opener")
    if _INTERNAL_DIAGNOSTIC_RE.search(text):
        issues.append("internal_state_leak")
    if _GENERIC_ASSISTANT_FRAME_RE.search(text):
        issues.append("generic_assistant_frame")
    plan = expression_plan if isinstance(expression_plan, dict) else {}
    if plan.get("topic_anchor_required") and _looks_generic_without_anchor(
        text,
        plan.get("topic_anchor_text"),
    ):
        issues.append("missing_topic_anchor")
    if (
        plan.get("topic_anchor_required")
        and source_reply is not None
        and _has_anchor_terms(source_reply, plan.get("topic_anchor_text"))
        and not _has_anchor_terms(text, plan.get("topic_anchor_text"))
    ):
        issues.append("topic_anchor_lost_after_normalization")
    if _GROUP_RELATIONSHIP_ROLE_RE.search(text):
        issues.append("relationship_role_leak")
    if repeated_reply_shape_issue(text, recent_replies):
        issues.append("repeated_reply_shape")
    if uninvited and _UNINVITED_TARGETED_JUDGEMENT_RE.search(text):
        issues.append("uninvited_targeted_judgement")
    if is_style_feedback(request) and (
        len(text) > 36
        or abstract_hits >= 1
        or has_casual_identity_self_disclosure(text)
        or re.search(r"(?:被说中|我改|表达方式|说话习惯)", text)
    ):
        issues.append("style_feedback_not_natural")
    return issues


def normalize_social_reply(
    value: object,
    *,
    limit: int = 3600,
    group: bool = False,
    request: str = "",
    max_sentences: int | None = None,
) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^(?:回复|回答|发送内容)\s*[：:]\s*", "", text)
    if len(text) > 12:
        for opening in GENERIC_OPENINGS:
            if text.startswith(opening):
                text = text[len(opening) :].lstrip()
                break
    text = re.sub(
        r"\n*(?:如果你愿意|如果你需要)，?我(?:也|还)?可以(?:继续)?(?:帮你)?(?:展开|处理|看看|说明).{0,24}[。！!]?$",
        "",
        text,
    ).strip()
    if group:
        text = re.sub(r"(?m)^#{1,6}\s+", "", text)
        text = _GROUP_PARENTHETICAL_RE.sub("", text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        if max_sentences is not None:
            sentence_limit = max(1, min(int(max_sentences), 3))
            units = []
            for paragraph in (part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()):
                units.extend(part.strip() for part in _GROUP_SENTENCE_SPLIT_RE.split(paragraph) if part.strip())
            text = "".join(units[:sentence_limit])
        limit = min(limit, 240 if (max_sentences or 0) >= 2 else 180)
        if _INTERNAL_DIAGNOSTIC_RE.search(text):
            return "这次没接好，先不把后台提示刷出来。"
    text = guard_casual_identity_reply(request, text)
    return text[: max(1, int(limit or 3600))].strip()


def normalize_group_reply_for_delivery(
    value: object,
    *,
    request: str = "",
    expression_plan: dict | None = None,
) -> str:
    """Return the exact group text that the existing send path will normalize."""

    plan = expression_plan if isinstance(expression_plan, dict) else {}
    try:
        sentence_limit = int(plan.get("sentence_limit"))
    except (TypeError, ValueError):
        sentence_limit = None
    return normalize_social_reply(
        value,
        group=True,
        request=request,
        max_sentences=sentence_limit,
    )


def group_reply_style_issues_for_delivery(
    request: str,
    reply: str,
    *,
    recent_replies: list[str] | None = None,
    uninvited: bool = False,
    expression_plan: dict | None = None,
    candidate: dict | None = None,
    finalizer: Callable[[str, dict], tuple[str, dict]] | None = None,
) -> tuple[str, list[str], dict]:
    """Inspect the exact finalized delivery text, retaining raw diagnostics."""

    normalized_reply = normalize_group_reply_for_delivery(
        reply,
        request=request,
        expression_plan=expression_plan,
    )
    final_reply, final_metadata = (
        finalizer(normalized_reply, candidate or {})
        if finalizer is not None else (normalized_reply, {})
    )
    final_reply = str(final_reply or "").strip()
    final_metadata = dict(final_metadata) if isinstance(final_metadata, dict) else {}
    raw_issues = group_reply_style_issues(
        request,
        reply,
        recent_replies=recent_replies,
        uninvited=uninvited,
        expression_plan=expression_plan,
    )
    final_issues = group_reply_style_issues(
        request,
        final_reply,
        recent_replies=recent_replies,
        uninvited=uninvited,
        expression_plan=expression_plan,
        source_reply=reply,
    )
    issues = [issue for issue in raw_issues if issue == "internal_state_leak"]
    issues.extend(issue for issue in final_issues if issue not in issues)
    return final_reply, issues, final_metadata


__all__ = [
    "group_reply_style_issues",
    "group_reply_style_issues_for_delivery",
    "normalize_group_reply_for_delivery",
    "normalize_social_reply",
]
