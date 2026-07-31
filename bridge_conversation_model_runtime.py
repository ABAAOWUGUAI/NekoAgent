"""Provider-neutral execution slice for one already-planned conversation turn."""

from __future__ import annotations

from collections.abc import Callable

from bridge_conversation_reply_runtime import call_openai_conversation_reply


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
) -> dict:
    """Run exactly one reply model without performing planning or persistence."""

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
        )
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
    return result


__all__ = ["run_conversation_model_reply"]
