#!/usr/bin/env python3
"""Controlled group-expression variation without storing new message content."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping


GROUP_EXPRESSION_DEFAULTS = {
    "group_stance": "observant",
    "group_reaction_style": "specific",
    "group_sentence_rhythm": "one_beat",
    "group_ending_policy": "drop",
}

_SENTENCE_SPLIT = re.compile(r"[。！？!?]+")
_GROUP_SHAPES = {
    "clean_drop": "直接接住一个具体点，说完自然收住。",
    "reaction_then_point": "先给很短的即时反应，再落到一个具体点；不要写成总结。",
    "point_then_reaction": "先点出当前最关键的细节，再给一句自然反应。",
    "two_short_beats": "用两句短节奏：一拍接住，第二拍补一个具体观察；不要解释套路。",
    "single_hook": "只接住当前话题，留一个与语境有关的轻钩子；不是泛泛追问。",
}


def group_expression_signature(contract: Mapping[str, object] | None) -> dict[str, str]:
    """Return only allowlisted group-expression settings with neutral defaults."""

    source = contract or {}
    return {
        key: str(source.get(key) or fallback).strip().lower()
        for key, fallback in GROUP_EXPRESSION_DEFAULTS.items()
    }


def reply_shape(value: object) -> str:
    """Classify a reply from punctuation/length only; callers need not retain text."""

    text = str(value or "").strip()
    if not text:
        return "empty"
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    if text.endswith(("?", "？")) and len(parts) <= 1:
        return "single_hook"
    if len(parts) == 2 and max(map(len, parts)) <= 28:
        return "two_short_beats"
    if len(parts) >= 3 or len(text) > 64:
        return "long_or_multi"
    return "clean_drop"


def recent_reply_shapes(history: Iterable[Mapping[str, object]] | None, *, limit: int = 4) -> list[str]:
    """Derive a bounded ephemeral shape history from already-selected chat history."""

    shapes = [
        reply_shape(item.get("content"))
        for item in (history or [])
        if str(item.get("role") or "") == "assistant" and str(item.get("content") or "").strip()
    ]
    return shapes[-max(1, min(int(limit or 4), 8)):]


def choose_group_reply_shape(
    *,
    social_action: str,
    signature: Mapping[str, object] | None,
    recent_shapes: Iterable[str] | None = None,
) -> tuple[str, str]:
    """Choose an allowed response form and its visible-plan instruction.

    This is deterministic and only varies *form*. It never grants a send, changes
    the social action, or writes a separate memory/learning record.
    """

    normalized = group_expression_signature(signature)
    action = str(social_action or "reply").strip().lower()
    rhythm = normalized["group_sentence_rhythm"]
    ending = normalized["group_ending_policy"]
    if action in {"ack", "repair", "follow_up", "topic_start"}:
        candidates = ["clean_drop"] if action != "follow_up" else ["single_hook"]
    elif rhythm == "one_beat":
        candidates = ["clean_drop", "point_then_reaction"]
    elif rhythm == "two_beats":
        candidates = ["two_short_beats", "reaction_then_point"]
    else:
        candidates = ["reaction_then_point", "point_then_reaction", "two_short_beats"]
    if ending in {"contextual_hook", "varied"} and action in {"ack_add", "reply", "bridge_topic"}:
        candidates.append("single_hook")
    previous = [str(shape) for shape in recent_shapes or [] if str(shape)]
    available = [shape for shape in candidates if shape != (previous[-1] if previous else "")]
    if not available:
        available = candidates
    seed = f"{action}|{normalized['group_stance']}|{normalized['group_reaction_style']}|{rhythm}|{ending}|{','.join(previous[-3:])}"
    index = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(available)
    selected = available[index]
    stance = {
        "observant": "先抓住当前明确细节，不评判群成员。",
        "quick_witted": "反应快但不抢戏；幽默必须落在当前具体点上。",
        "direct_playful": "可以直接又轻快，但不嘲弄、不贴标签。",
    }.get(normalized["group_stance"], "先抓住当前明确细节，不评判群成员。")
    reaction = {
        "specific": "不用泛泛的‘有意思/确实’，要让回应能对应这段话。",
        "dry": "可以轻微冷幽默，但没有具体切入点就不硬接。",
        "playful": "可以轻快接梗，但不堆表情或固定口头禅。",
    }.get(normalized["group_reaction_style"], "不用泛泛的‘有意思/确实’，要让回应能对应这段话。")
    return selected, "；".join((stance, reaction, _GROUP_SHAPES[selected]))


def repeated_reply_shape_issue(reply: object, recent_replies: Iterable[object] | None) -> bool:
    """Flag only sustained identical forms, avoiding one-off false positives."""

    current = reply_shape(reply)
    previous = [reply_shape(item) for item in recent_replies or [] if str(item or "").strip()]
    return current != "empty" and len(previous) >= 3 and previous[-3:] == [current, current, current]


__all__ = [
    "GROUP_EXPRESSION_DEFAULTS",
    "choose_group_reply_shape",
    "group_expression_signature",
    "recent_reply_shapes",
    "repeated_reply_shape_issue",
    "reply_shape",
]
