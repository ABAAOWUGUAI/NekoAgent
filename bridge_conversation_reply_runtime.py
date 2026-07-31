#!/usr/bin/env python3
"""Bounded resilience for user-visible conversation replies."""

from __future__ import annotations

from collections.abc import Callable

from bridge_social_reply import group_reply_style_issues


def call_openai_with_empty_retry(
    settings: dict,
    messages: list[dict],
    *,
    timeout: int,
    user_id: str,
    call_model: Callable[..., dict],
    record_model: Callable[..., None],
    empty_source: str,
    retry_instruction: str,
) -> dict:
    """Retry one empty final answer without ever emitting hidden reasoning."""

    result = call_model(settings, messages, timeout=timeout)
    if result.get("ok") or result.get("error_kind") != "empty":
        return result
    record_model(
        settings,
        result,
        source=empty_source,
        user_id=user_id,
    )
    retry_messages = [dict(item) for item in messages]
    retry_messages[0] = {
        **retry_messages[0],
        "content": (
            str(retry_messages[0].get("content") or "")
            + "\n\n"
            + retry_instruction
        ),
    }
    retry = call_model(settings, retry_messages, timeout=timeout)
    retry.update({
        "empty_retry_attempted": True,
        "initial_finish_reason": result.get("finish_reason") or "",
        "initial_reasoning_only": bool(result.get("reasoning_only")),
    })
    return retry


def call_openai_conversation_reply(
    settings: dict,
    messages: list[dict],
    *,
    timeout: int,
    user_id: str,
    call_model: Callable[..., dict],
    record_model: Callable[..., None],
) -> dict:
    result = call_openai_with_empty_retry(
        settings,
        messages,
        timeout=timeout,
        user_id=user_id,
        call_model=call_model,
        record_model=record_model,
        empty_source="assistant_chat_empty_initial",
        retry_instruction="输出协议：必须生成非空的最终回复正文；不要只生成思考过程。",
    )
    is_group = bool(messages and "群聊边界:" in str(messages[0].get("content") or ""))
    if not is_group:
        return result
    request = str(messages[-1].get("content") or "") if messages else ""
    recent_group_replies = [
        str(item.get("content") or "")
        for item in messages[-14:]
        if str(item.get("role") or "") == "assistant"
    ]
    return call_openai_group_style_retry(
        settings,
        messages,
        result,
        group_reply_style_issues(
            request,
            result.get("reply") or result.get("output") or "",
            recent_replies=recent_group_replies,
        ),
        timeout=timeout,
        user_id=user_id,
        call_model=call_model,
        record_model=record_model,
    )


def call_openai_group_style_retry(
    settings: dict,
    messages: list[dict],
    result: dict,
    issues: list[str],
    *,
    timeout: int,
    user_id: str,
    call_model: Callable[..., dict],
    record_model: Callable[..., None],
) -> dict:
    """Regenerate one group draft that failed the server-side naturalness gate."""

    if not issues or not result.get("ok"):
        return result
    record_model(
        settings,
        result,
        source="assistant_chat_group_style_initial",
        user_id=user_id,
    )
    retry_messages = [dict(item) for item in messages]
    retry_messages[0] = {
        **retry_messages[0],
        "content": (
            str(retry_messages[0].get("content") or "")
            + "\n\n"
            + "上一版草稿未通过群聊自然表达检查（"
            + "、".join(issues)
            + "）。保留原事实，只重写成一句自然群聊消息：直接接住话题里的具体人、"
              "物或动作，不复述上一条，不解释自己的表达，不用括号补充动作或心理，"
              "不用固定口头禅开场，不编造自己的经历或设定，也不要提到规则或改写。"
        ),
    }
    retry = call_model(settings, retry_messages, timeout=timeout)
    if not retry.get("ok"):
        result.update({
            "group_style_retry_attempted": True,
            "group_style_retry_failed": True,
            "group_style_retry_error_kind": retry.get("error_kind") or "",
        })
        return result
    retry.update({
        "group_style_retry_attempted": True,
        "group_style_initial_issues": list(issues),
    })
    return retry


__all__ = [
    "call_openai_conversation_reply",
    "call_openai_group_style_retry",
    "call_openai_with_empty_retry",
]
