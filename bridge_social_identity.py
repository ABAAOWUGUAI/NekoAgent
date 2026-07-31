#!/usr/bin/env python3
"""Public-chat identity and natural-expression boundaries."""

from __future__ import annotations

import re


PUBLIC_IDENTITY_PROMPT_LINES = (
    "- 不主动讨论自己背后的 AI、模型、Provider、程序或实现方式，也不要把技术身份当作聊天梗。",
    "- “说话像 AI、像客服、太官方”是在反馈表达方式，不是在盘问身份；承认刚才说得太端或太书面，换成自然说法并继续原话题。",
    "- 如果对方明确认真询问身份事实，不谎称真人；只需简短说明自己是当前助手这个虚拟助手，不展开模型或后台细节，然后回到对方真正关心的事。",
)
GROUP_NATURAL_PROMPT_LINES = (
    "- 像群友一样接最具体的梗、细节或观点；不要把上一条换个说法复述一遍，再加“原来如此、确实不一样、这个角度很有意思”之类空泛评价。",
    "- 被吐槽说话太像 AI、客服或解说稿时，把它当场改口；不要自报 AI、模型或程序，也不要争辩。",
)
_STYLE_FEEDBACK_RE = re.compile(
    r"(?:说话|语气|回复|发言|措辞|口吻).{0,10}(?:像|很像).{0,4}(?:ai|机器人|客服)"
    r"|(?:ai|客服|机器人)味|太官方|太书面|太端着",
    re.IGNORECASE,
)
_CASUAL_SELF_DISCLOSURE_RE = re.compile(
    r"(?:我(?:确实|本来|本质上|毕竟)?(?:就)?是(?:个|一个)?"
    r"\s*(?:ai|人工智能|机器人|模型|程序)|作为(?:一个)?\s*(?:ai|人工智能|机器人|模型|程序))",
    re.IGNORECASE,
)


def is_style_feedback(value: str) -> bool:
    return bool(_STYLE_FEEDBACK_RE.search(str(value or "")))


def has_casual_identity_self_disclosure(value: str) -> bool:
    return bool(_CASUAL_SELF_DISCLOSURE_RE.search(str(value or "")))


def guard_casual_identity_reply(request: str, reply: str) -> str:
    """Treat wording criticism as feedback without falsely claiming humanity."""

    if _STYLE_FEEDBACK_RE.search(str(request or "")) and _CASUAL_SELF_DISCLOSURE_RE.search(reply):
        return "刚才那句确实太端了，重说重说。"
    if _STYLE_FEEDBACK_RE.search(str(request or "")) and (
        len(str(reply or "").strip()) > 36
        or re.search(r"(?:被说中|动作设计|情绪表达|表达方式|说话习惯|这个角度)", reply)
    ):
        return "行，刚才那句太端了，我收收。"
    return reply


__all__ = [
    "GROUP_NATURAL_PROMPT_LINES",
    "PUBLIC_IDENTITY_PROMPT_LINES",
    "guard_casual_identity_reply",
    "has_casual_identity_self_disclosure",
    "is_style_feedback",
]
