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
    conversation_scope: str = "private",
    group_context: dict | None = None,
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
    scope = str(conversation_scope or "private").strip().lower()
    if scope not in {"private", "group", "work"}:
        scope = "private"
    result["conversation_scope"] = scope
    if scope != "group":
        result.setdefault("group_style_gate", "not_applicable")
        return result
    request = str(messages[-1].get("content") or "") if messages else ""
    group_context = group_context if isinstance(group_context, dict) else {}
    uninvited = bool(group_context.get("uninvited_group_action"))
    recent_group_replies = [
        str(item.get("content") or "")
        for item in messages[-14:]
        if str(item.get("role") or "") == "assistant"
    ]
    final = call_openai_group_style_retry(
        settings,
        messages,
        result,
        group_reply_style_issues(
            request,
            result.get("reply") or result.get("output") or "",
            recent_replies=recent_group_replies,
            uninvited=uninvited,
        ),
        timeout=timeout,
        user_id=user_id,
        call_model=call_model,
        record_model=record_model,
        request=request,
        recent_replies=recent_group_replies,
        uninvited=uninvited,
    )
    final["conversation_scope"] = scope
    return final


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
    request: str = "",
    recent_replies: list[str] | None = None,
    uninvited: bool = False,
) -> dict:
    """Regenerate one group draft that failed the server-side naturalness gate."""

    if not result.get("ok"):
        result.update({
            "group_style_retry_attempted": False,
            "group_style_initial_issues": [],
            "group_style_final_issues": [],
            "group_style_gate": "provider_failed",
        })
        return result
    if not issues:
        result.update({
            "group_style_gate": "passed",
            "group_style_retry_attempted": False,
            "group_style_retry_failed": False,
            "group_style_initial_issues": [],
            "group_style_final_issues": [],
        })
        return result
    record_model(
        settings,
        result,
        source="assistant_chat_group_style_initial",
        user_id=user_id,
    )
    retry_messages = [dict(item) for item in messages]
    retry_instruction = (
        "上一版草稿未通过群聊自然表达检查（"
        + "、".join(issues)
        + "）。保留原事实，只重写成符合本轮 Expression Plan 的一到两句自然群聊消息："
          "直接接住话题里的具体人、物或动作，不复述上一条，不解释自己的表达，"
          "不用括号补充动作或心理，不用固定口头禅开场，不编造自己的经历或设定，"
          "也不要提到规则或改写。"
    )
    # Keep the reusable system prefix byte-for-byte stable.  A retry belongs
    # to the volatile current turn; appending it to the system message would
    # create a new cache prefix and make the naturalness repair itself reduce
    # the cache hit rate we are trying to measure.
    # Keep the original current-user packet byte-for-byte unchanged as well;
    # the extra user turn is a retry-only instruction and never becomes
    # conversation history or a cache replay packet.
    retry_messages.append({"role": "user", "content": retry_instruction})
    retry = call_model(settings, retry_messages, timeout=timeout)
    if not retry.get("ok"):
        result.update({
            "group_style_retry_attempted": True,
            "group_style_retry_failed": True,
            "group_style_retry_error_kind": retry.get("error_kind") or "",
            "group_style_gate": "degraded",
            "group_style_final_issues": list(issues),
        })
        return result
    final_issues = group_reply_style_issues(
        request,
        retry.get("reply") or retry.get("output") or "",
        recent_replies=recent_replies or [],
        uninvited=uninvited,
    )
    retry.update({
        "group_style_retry_attempted": True,
        "group_style_initial_issues": list(issues),
        "group_style_final_issues": list(final_issues),
        "group_style_gate": "passed" if not final_issues else "degraded",
    })
    return retry


__all__ = [
    "call_openai_conversation_reply",
    "call_openai_group_style_retry",
    "call_openai_with_empty_retry",
]
