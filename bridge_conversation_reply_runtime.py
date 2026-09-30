#!/usr/bin/env python3
"""Bounded resilience for user-visible conversation replies."""

from __future__ import annotations

import time
from collections.abc import Callable

from bridge_social_reply import (
    group_reply_style_issues_for_delivery,
    normalize_group_reply_for_delivery,
)


def remaining_timeout(
    requested: int,
    deadline: float | None,
    clock: Callable[[], float],
) -> int:
    """Bound one provider attempt by the current absolute-deadline budget."""

    requested_timeout = int(requested)
    if deadline is None:
        return requested_timeout
    return max(0, min(requested_timeout, int(deadline - clock())))


def _with_reply_attempt_metadata(
    result: dict,
    *,
    attempt_count: int,
    retry_suppressed: bool,
    deadline_outcome: str,
) -> dict:
    result.update({
        "reply_attempt_count": attempt_count,
        "retry_suppressed_by_deadline": retry_suppressed,
        "deadline_outcome": deadline_outcome,
    })
    return result


def _deadline_exhausted_result(settings: dict) -> dict:
    return {
        "ok": False,
        "reply": "",
        "output": "",
        "error": "reply_deadline_exhausted",
        "error_kind": "deadline_exhausted",
        "retryable": False,
        "finish_reason": "",
        "reasoning_only": False,
        "usage": {},
        "provider": str(settings.get("chat_provider") or "openai-compatible"),
        "model": str(settings.get("chat_model") or ""),
        "reply_attempt_count": 0,
        "retry_suppressed_by_deadline": False,
        "deadline_outcome": "expired_before_attempt",
    }


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
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    minimum_retry_seconds: int = 12,
) -> dict:
    """Retry one empty or transient final answer without emitting hidden reasoning.

    A read timeout or a retryable HTTP status (429/5xx) from the provider is
    transient the same way an empty final answer is: a single bounded retry
    recovers most busy- or flaky-provider calls before the durable worker
    retry budget is spent.  Nothing here loops; at most one retry happens.
    """

    initial_timeout = remaining_timeout(timeout, deadline_monotonic, clock)
    if deadline_monotonic is not None and initial_timeout <= 0:
        return _deadline_exhausted_result(settings)
    result = call_model(settings, messages, timeout=initial_timeout)
    deadline_outcome = (
        "not_configured"
        if deadline_monotonic is None
        else "attempted_within_deadline"
    )
    if result.get("ok"):
        return _with_reply_attempt_metadata(
            result,
            attempt_count=1,
            retry_suppressed=False,
            deadline_outcome=deadline_outcome,
        )
    transient = bool(
        str(result.get("error_kind") or "") in {"empty", "timeout"}
        or result.get("retryable")
    )
    if not transient:
        return _with_reply_attempt_metadata(
            result,
            attempt_count=1,
            retry_suppressed=False,
            deadline_outcome=deadline_outcome,
        )
    retry_timeout = remaining_timeout(timeout, deadline_monotonic, clock)
    if deadline_monotonic is not None and (
        retry_timeout <= 0
        or retry_timeout < int(minimum_retry_seconds)
    ):
        return _with_reply_attempt_metadata(
            result,
            attempt_count=1,
            retry_suppressed=True,
            deadline_outcome="retry_suppressed",
        )
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
    retry = call_model(settings, retry_messages, timeout=retry_timeout)
    retry.update({
        "empty_retry_attempted": True,
        "initial_finish_reason": result.get("finish_reason") or "",
        "initial_reasoning_only": bool(result.get("reasoning_only")),
    })
    return _with_reply_attempt_metadata(
        retry,
        attempt_count=2,
        retry_suppressed=False,
        deadline_outcome=deadline_outcome,
    )


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
    group_reply_finalizer: Callable[[str, dict], tuple[str, dict]] | None = None,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
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
        deadline_monotonic=deadline_monotonic,
        clock=clock,
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
    expression_plan = group_context.get("expression_plan")
    recent_group_replies = [
        str(item.get("content") or "")
        for item in messages[-14:]
        if str(item.get("role") or "") == "assistant"
    ]
    initial_delivery_reply, initial_issues, initial_delivery_metadata = group_reply_style_issues_for_delivery(
        request,
        result.get("reply") or result.get("output") or "",
        recent_replies=recent_group_replies,
        uninvited=uninvited,
        expression_plan=expression_plan,
        candidate=result,
        finalizer=group_reply_finalizer,
    )
    final = call_openai_group_style_retry(
        settings,
        messages,
        result,
        initial_issues,
        timeout=timeout,
        user_id=user_id,
        call_model=call_model,
        record_model=record_model,
        request=request,
        recent_replies=recent_group_replies,
        uninvited=uninvited,
        expression_plan=expression_plan,
        delivery_reply=initial_delivery_reply,
        delivery_metadata=initial_delivery_metadata,
        group_reply_finalizer=group_reply_finalizer,
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
    expression_plan: dict | None = None,
    delivery_reply: str | None = None,
    delivery_metadata: dict | None = None,
    group_reply_finalizer: Callable[[str, dict], tuple[str, dict]] | None = None,
) -> dict:
    """Regenerate one group draft that failed the server-side naturalness gate."""

    initial_delivery_metadata = dict(delivery_metadata or {})
    if delivery_reply is None:
        initial_delivery_reply, _fallback_issues, initial_delivery_metadata = group_reply_style_issues_for_delivery(
            request,
            result.get("reply") or result.get("output") or "",
            recent_replies=recent_replies,
            uninvited=uninvited,
            expression_plan=expression_plan,
            candidate=result,
            finalizer=group_reply_finalizer,
        )
    else:
        initial_delivery_reply = delivery_reply
    if not result.get("ok"):
        result.update({
            "reply": initial_delivery_reply,
            "output": initial_delivery_reply,
            **initial_delivery_metadata,
            "group_style_retry_attempted": False,
            "group_style_initial_issues": [],
            "group_style_final_issues": [],
            "group_style_gate": "provider_failed",
        })
        return result
    if not issues:
        result.update({
            "reply": initial_delivery_reply,
            "output": initial_delivery_reply,
            **initial_delivery_metadata,
        })
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
          "直接接住该话题中的具体对象或动作，不复述上一条，不解释自己的表达，"
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
            "reply": initial_delivery_reply,
            "output": initial_delivery_reply,
            **initial_delivery_metadata,
        })
        result.update({
            "group_style_retry_attempted": True,
            "group_style_retry_failed": True,
            "group_style_retry_error_kind": retry.get("error_kind") or "",
            "group_style_initial_issues": list(issues),
            "group_style_gate": "degraded",
            "group_style_final_issues": list(issues),
        })
        return result
    final_delivery_reply, final_issues, final_delivery_metadata = group_reply_style_issues_for_delivery(
        request,
        retry.get("reply") or retry.get("output") or "",
        recent_replies=recent_replies or [],
        uninvited=uninvited,
        expression_plan=expression_plan,
        candidate=retry,
        finalizer=group_reply_finalizer,
    )
    retry.update({
        "reply": final_delivery_reply,
        "output": final_delivery_reply,
        **final_delivery_metadata,
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
    "remaining_timeout",
]
