#!/usr/bin/env python3
"""Natural-language schedule objective extraction.

The schedule parser owns time/frequency detection; this module only removes
schedule scaffolding while preserving the business objective and delivery
constraints expressed in the same sentence.
"""

from __future__ import annotations

import re


_DAILY_CLAUSE_RE = re.compile(
    r"(?:每天|每日|每晚|每早)\s*"
    r"(?:(?:凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
    r"(?:[01]?\d|2[0-3])\s*(?:[:：点时])\s*(?:[0-5]?\d)?\s*分?",
)
_SCHEDULE_WRAPPER_RE = re.compile(
    r"(?:，|,|；|;)\s*(?:这个|这项|该|当前)?(?:需求|任务|计划)\s*"
    r"(?:做成|设置成|设为|安排为|改成|改为)?\s*"
    r"(?:一个)?(?:定时任务|定时计划|定时提醒)(?:\s*(?:给到我|发给我|推送给我|提醒我))?\s*$",
)

# Server-owned, closed-set source aliases.  ``githu`` is the historical real
# misspelling from the 2026-08-06/08 incident; an alias only normalises when it
# also appears in an explicit GitHub trending context below, so ordinary text
# mentioning the string alone is never coerced.
_SOURCE_ALIASES: dict[str, str] = {"githu": "github"}

# Only normalise a source alias when GitHub trending/topic context is present,
# so a bare misspelling elsewhere does not route to a capability.
_GITHUB_CONTEXT_RE = re.compile(
    r"(?:github|githu|gh)\s*"
    r"(?:[^，。；;,]{0,20}?)?"
    r"(?:trending|趋势|热门|热榜|话题|top|排行榜|项目|仓库)",
    re.IGNORECASE,
)
_GITHUB_TRENDING_WORDS = (
    "trending",
    "趋势",
    "热门",
    "热榜",
    "话题",
    "top",
    "排行榜",
    "项目",
    "仓库",
    "repo",
    "github 热榜",
)


def normalise_source_alias(text: str) -> str:
    """Normalise a closed-set source alias only under explicit GitHub context.

    Returns ``github`` when the text mentions GitHub directly or a known alias
    (``githu``) together with GitHub trending context.  Returns ``""`` when the
    alias appears without trending context, so ordinary text is never coerced
    into the GitHub capability.
    """

    lowered = str(text or "").lower()
    if "github" in lowered:
        return "github"
    for alias, canonical in _SOURCE_ALIASES.items():
        if alias not in lowered:
            continue
        if any(token in lowered for token in _GITHUB_TRENDING_WORDS):
            return canonical
    return ""


def _normalise_source(text: str, lowered: str) -> str:
    return normalise_source_alias(text)


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def extract_instruction(text: str) -> str:
    value = str(text or "").strip()
    # Both natural word orders are valid: “做一个定时任务，每天……” and
    # “每天……，这个需求做成定时任务给到我”。
    leading = re.match(
        r"^(?:.*?)(?:定时任务|定时计划|定时提醒)\s*[，,：:]?\s*(?:要求)?\s*",
        value,
    )
    if leading and not any(
        token in value[:leading.end()] for token in ("天气", "github", "价格", "汇报")
    ):
        value = value[leading.end():]
    else:
        value = _SCHEDULE_WRAPPER_RE.sub("", value, count=1)
    value = re.sub(r"^(?:呢|呀|吧)\s*[,，:：]?\s*", "", value, count=1)
    value = value.replace("点钟", "点")
    value = _DAILY_CLAUSE_RE.sub("", value, count=1)
    value = re.sub(r"^(?:当前助手|助手)\s*", "", value)
    value = re.sub(r"^(?:请|麻烦|帮我|给我|要求)\s*", "", value)
    value = re.sub(r"^(?:给我)?推送\s*", "获取并整理", value)
    value = re.sub(r"[，,。；;\s]+$", "", value).strip(" ，,：:")
    return _clip(value, 4000)


def extract_parameters(instruction: str) -> dict:
    """Extract stable constraints without asking a model to infer them."""

    text = str(instruction or "")
    parameters: dict[str, object] = {}
    count = re.search(r"(?:每天|每日|前|取|要|共)\s*(\d+)\s*(?:条|个|项)", text)
    if count:
        parameters["item_limit"] = max(1, min(int(count.group(1)), 100))
    lowered = text.lower()
    normalised_source = _normalise_source(text, lowered)
    if normalised_source:
        parameters["source"] = normalised_source
    if "AI" in text.upper() or "aiagent" in lowered or "ai agent" in lowered:
        parameters["topic"] = "ai_agent"
    if "天气" in text:
        parameters["topic"] = "weather"
        if any(token in text for token in ("汇报", "给到我", "发给我")):
            parameters["delivery_format"] = "conversation"
        horizon = re.search(r"(?:未来|接下来)\s*(\d{1,3})\s*小时", text)
        if horizon:
            parameters["forecast_horizon_hours"] = max(1, min(int(horizon.group(1)), 168))
        if any(token in text for token in ("下雨", "降雨", "降水")):
            parameters["include_precipitation"] = True
    if any(token in text for token in ("不允许出现重复", "不要重复", "去重")):
        parameters["dedupe_policy"] = "job_history"
    if any(token in text for token in ("聊天记录", "聊天方式", "发送消息")):
        parameters["delivery_format"] = "conversation"
    if any(token in text for token in ("中文", "简体中文", "使用中文", "必须是中文")):
        parameters["output_language"] = "zh-CN"
    return parameters


__all__ = ["extract_instruction", "extract_parameters", "normalise_source_alias"]
