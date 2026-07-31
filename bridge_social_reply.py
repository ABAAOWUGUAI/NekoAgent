#!/usr/bin/env python3
"""User-facing daily and group reply normalization."""

from __future__ import annotations

import re

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
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        text = "\n".join(paragraphs[:1])
        limit = min(limit, 180)
    text = guard_casual_identity_reply(request, text)
    return text[: max(1, int(limit or 3600))].strip()


__all__ = ["group_reply_style_issues", "normalize_social_reply"]
