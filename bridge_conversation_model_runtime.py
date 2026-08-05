"""Provider-neutral execution slice for one already-planned conversation turn."""

from __future__ import annotations

from collections.abc import Callable

from bridge_conversation_reply_runtime import call_openai_conversation_reply
from bridge_prompt_cache_contract import provider_cache_replay_metadata
from bridge_social_reply import group_reply_style_issues


def run_conversation_model_reply(
    provider: str,
    settings: dict,
    user_id: str,
    message: str,
    memories: list[dict],
    history: list[dict],
    *,
    intent: str,
    criteria: list[str],
    policy: dict,
    mode_decision: dict,
    social_context: dict,
    attachment_context: dict,
    timeout: int,
    build_messages: Callable[..., list[dict]],
    format_prompt: Callable[..., str],
    call_model: Callable[..., dict],
    record_model: Callable[..., None],
    run_codex: Callable[..., dict],
    cwd,
) -> tuple[dict, dict]:
    """Run exactly one reply model without performing planning or persistence."""

    if provider == "openai-compatible":
        messages = build_messages(
            settings, user_id, message, memories, history,
            intent=intent, criteria=criteria, policy=policy,
            mode_decision=mode_decision, social_context=social_context,
            attachment_context=attachment_context,
        )
        group_context = social_context.get("group") if isinstance(social_context, dict) else None
        conversation_scope = (
            "group" if group_context else
            "work" if str((mode_decision or {}).get("mode") or "daily") in {"work", "mixed"} else
            "private"
        )
        return call_openai_conversation_reply(
            settings, messages, timeout=max(20, min(int(timeout or 180), 300)),
            user_id=user_id, call_model=call_model, record_model=record_model,
            conversation_scope=conversation_scope, group_context=group_context,
        ), provider_cache_replay_metadata(messages)
    prompt = format_prompt(
        user_id, message, memories, history,
        intent=intent, criteria=criteria, policy=policy,
        mode_decision=mode_decision, social_context=social_context,
        attachment_context=attachment_context,
    )
    result = run_codex(
        prompt, cwd=cwd,
        timeout=max(30, min(int(timeout or 180), 600)),
        settings_override=settings,
    )
    result["provider"] = "codex"
    group_context = social_context.get("group") if isinstance(social_context, dict) else None
    conversation_scope = (
        "group" if group_context else
        "work" if str((mode_decision or {}).get("mode") or "daily") in {"work", "mixed"} else
        "private"
    )
    result["conversation_scope"] = conversation_scope
    if conversation_scope != "group":
        result.setdefault("group_style_gate", "not_applicable")
        return result, {}

    group_context = group_context if isinstance(group_context, dict) else {}
    uninvited = bool(group_context.get("uninvited_group_action"))
    recent_group_replies = [
        str(item.get("content") or "")
        for item in history[-14:]
        if isinstance(item, dict) and str(item.get("role") or "") == "assistant"
    ]
    request = str(message or "")
    issues = group_reply_style_issues(
        request,
        result.get("reply") or result.get("output") or "",
        recent_replies=recent_group_replies,
        uninvited=uninvited,
    )
    if not result.get("ok"):
        result.update({
            "group_style_retry_attempted": False,
            "group_style_initial_issues": [],
            "group_style_final_issues": [],
            "group_style_gate": "provider_failed",
        })
        return result, {}
    if not issues:
        result.update({
            "group_style_retry_attempted": False,
            "group_style_retry_failed": False,
            "group_style_initial_issues": [],
            "group_style_final_issues": [],
            "group_style_gate": "passed",
        })
        return result, {}

    record_model(settings, result, source="assistant_chat_group_style_initial", user_id=user_id)
    retry_prompt = (
        prompt
        + "\n\n上一版草稿未通过群聊自然表达检查（"
        + "、".join(issues)
        + "）。保留原事实，只重写成符合本轮 Expression Plan 的一到两句自然群聊消息："
          "直接接住话题里的具体人、物或动作，不复述上一条，不解释自己的表达，"
          "不用括号补充动作或心理，不用固定口头禅开场，不编造自己的经历或设定，"
          "也不要提到规则或改写。"
    )
    retry = run_codex(
        retry_prompt,
        cwd=cwd,
        timeout=max(30, min(int(timeout or 180), 600)),
        settings_override=settings,
    )
    if not retry.get("ok"):
        result.update({
            "group_style_retry_attempted": True,
            "group_style_retry_failed": True,
            "group_style_retry_error_kind": retry.get("error_kind") or "",
            "group_style_initial_issues": list(issues),
            "group_style_final_issues": list(issues),
            "group_style_gate": "degraded",
        })
        return result, {}
    final_issues = group_reply_style_issues(
        request,
        retry.get("reply") or retry.get("output") or "",
        recent_replies=recent_group_replies,
        uninvited=uninvited,
    )
    retry.update({
        "provider": "codex",
        "conversation_scope": conversation_scope,
        "group_style_retry_attempted": True,
        "group_style_initial_issues": list(issues),
        "group_style_final_issues": list(final_issues),
        "group_style_gate": "passed" if not final_issues else "degraded",
    })
    return retry, {}


__all__ = ["run_conversation_model_reply"]
