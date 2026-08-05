"""Provider-neutral execution slice for one already-planned conversation turn."""

from __future__ import annotations

from collections.abc import Callable

from bridge_conversation_reply_runtime import call_openai_conversation_reply
from bridge_prompt_cache_contract import provider_cache_replay_metadata
from bridge_social_reply import group_reply_style_issues_for_delivery


def _codex_prompt_turn_envelope(
    stable_prefix: str,
    current_user_packet: str,
    retry_instruction: str = "",
) -> str:
    """Serialize explicit turn boundaries for Codex's raw-stdin executor.

    Codex CLI has no role-message API here.  The original prompt therefore
    remains a byte-for-byte prefix, while UTF-8 byte-length frames make the
    current and optional retry-only user turns representable and auditable.
    """

    def frame(label: str, value: str) -> str:
        payload = str(value)
        return (
            f"<|{label} bytes={len(payload.encode('utf-8'))}|>\n"
            f"{payload}\n"
            f"<|/{label}|>"
        )

    envelope = (
        str(stable_prefix)
        + "\n\n<|codex_prompt_turn_envelope_v1|>\n"
        + frame("current_user", str(current_user_packet))
    )
    if retry_instruction:
        envelope += "\n" + frame("retry_user", retry_instruction)
    return envelope


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
    group_reply_finalizer: Callable[[str, dict], tuple[str, dict]] | None = None,
) -> tuple[dict, dict]:
    """Run exactly one reply model without performing planning or persistence."""

    group_context = social_context.get("group") if isinstance(social_context, dict) else None
    if isinstance(group_context, dict) and isinstance(social_context.get("expression_plan"), dict):
        group_context = {**group_context, "expression_plan": social_context["expression_plan"]}
    conversation_scope = (
        "group" if group_context else
        "work" if str((mode_decision or {}).get("mode") or "daily") in {"work", "mixed"} else
        "private"
    )
    if provider == "openai-compatible":
        messages = build_messages(
            settings, user_id, message, memories, history,
            intent=intent, criteria=criteria, policy=policy,
            mode_decision=mode_decision, social_context=social_context,
            attachment_context=attachment_context,
        )
        return call_openai_conversation_reply(
            settings, messages, timeout=max(20, min(int(timeout or 180), 300)),
            user_id=user_id, call_model=call_model, record_model=record_model,
            conversation_scope=conversation_scope, group_context=group_context,
            group_reply_finalizer=group_reply_finalizer,
        ), provider_cache_replay_metadata(messages)
    prompt = format_prompt(
        user_id, message, memories, history,
        intent=intent, criteria=criteria, policy=policy,
        mode_decision=mode_decision, social_context=social_context,
        attachment_context=attachment_context,
    )
    codex_prompt = (
        _codex_prompt_turn_envelope(prompt, message)
        if conversation_scope == "group" else prompt
    )
    result = run_codex(
        codex_prompt, cwd=cwd,
        timeout=max(30, min(int(timeout or 180), 600)),
        settings_override=settings,
    )
    result["provider"] = "codex"
    result["conversation_scope"] = conversation_scope
    if conversation_scope != "group":
        result.setdefault("group_style_gate", "not_applicable")
        return result, {}

    group_context = group_context if isinstance(group_context, dict) else {}
    uninvited = bool(group_context.get("uninvited_group_action"))
    expression_plan = group_context.get("expression_plan")
    recent_group_replies = [
        str(item.get("content") or "")
        for item in history[-14:]
        if isinstance(item, dict) and str(item.get("role") or "") == "assistant"
    ]
    request = str(message or "")
    initial_delivery_reply, issues, initial_delivery_metadata = group_reply_style_issues_for_delivery(
        request,
        result.get("reply") or result.get("output") or "",
        recent_replies=recent_group_replies,
        uninvited=uninvited,
        expression_plan=expression_plan,
        candidate=result,
        finalizer=group_reply_finalizer,
    )
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
        return result, {}
    if not issues:
        result.update({
            "reply": initial_delivery_reply,
            "output": initial_delivery_reply,
            **initial_delivery_metadata,
        })
        result.update({
            "group_style_retry_attempted": False,
            "group_style_retry_failed": False,
            "group_style_initial_issues": [],
            "group_style_final_issues": [],
            "group_style_gate": "passed",
        })
        return result, {}

    record_model(settings, result, source="assistant_chat_group_style_initial", user_id=user_id)
    retry_instruction = (
        "上一版草稿未通过群聊自然表达检查（"
        + "、".join(issues)
        + "）。保留原事实，只重写成符合本轮 Expression Plan 的一到两句自然群聊消息："
          "直接接住该话题中的具体对象或动作，不复述上一条，不解释自己的表达，"
          "不用括号补充动作或心理，不用固定口头禅开场，不编造自己的经历或设定，"
          "也不要提到规则或改写。"
    )
    retry_prompt = _codex_prompt_turn_envelope(prompt, message, retry_instruction)
    retry = run_codex(
        retry_prompt,
        cwd=cwd,
        timeout=max(30, min(int(timeout or 180), 600)),
        settings_override=settings,
    )
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
            "group_style_final_issues": list(issues),
            "group_style_gate": "degraded",
        })
        return result, {}
    final_delivery_reply, final_issues, final_delivery_metadata = group_reply_style_issues_for_delivery(
        request,
        retry.get("reply") or retry.get("output") or "",
        recent_replies=recent_group_replies,
        uninvited=uninvited,
        expression_plan=expression_plan,
        candidate=retry,
        finalizer=group_reply_finalizer,
    )
    retry.update({
        "reply": final_delivery_reply,
        "output": final_delivery_reply,
        **final_delivery_metadata,
        "provider": "codex",
        "conversation_scope": conversation_scope,
        "group_style_retry_attempted": True,
        "group_style_initial_issues": list(issues),
        "group_style_final_issues": list(final_issues),
        "group_style_gate": "passed" if not final_issues else "degraded",
    })
    return retry, {}


__all__ = ["run_conversation_model_reply"]
