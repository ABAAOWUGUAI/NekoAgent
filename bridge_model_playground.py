"""Isolated model validation orchestration for the web console."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


def _playground_error(result: dict) -> str:
    """Keep the actionable tail of an executor error without echoing full output."""

    detail = str(result.get("error") or "").strip()
    if not detail and not result.get("ok"):
        detail = str(result.get("output") or "").strip()
    return detail[-2000:]


def run_model_playground(
    payload: dict,
    timeout: int,
    *,
    settings: dict,
    model_item: dict,
    default_cwd: str,
    run_codex: Callable[..., dict],
    run_transport: Callable[..., dict],
    record_usage: Callable[..., None],
) -> dict:
    """Run one model probe without writing conversation, QQ, goal, or task state."""

    # This timestamp belongs to the actual server-side request, not model discovery
    # or a cached health result. It lets the console state the freshness boundary
    # without storing a discovered model before the Owner explicitly saves it.
    validation_started_at = datetime.now(timezone.utc).isoformat()
    system_prompt = str(payload.get("system_prompt") or "你是一个简洁、可靠的验证助手。").strip()
    user_prompt = str(payload.get("user_prompt") or payload.get("message") or "").strip()
    if not user_prompt:
        raise ValueError("playground_prompt_required")
    if len(system_prompt) > 8000 or len(user_prompt) > 12000:
        raise ValueError("playground_prompt_too_long")
    try:
        temperature = max(0.0, min(float(payload.get("temperature", 0.7)), 2.0))
        max_tokens = max(1, min(int(payload.get("max_tokens", 900)), 8192))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_playground_parameters") from exc
    settings.update({"chat_temperature": str(temperature), "chat_max_tokens": str(max_tokens)})
    timeout = max(20, min(int(timeout or 90), 300))
    if str(settings.get("chat_provider") or "codex") == "codex":
        result = run_codex(
            f"系统指令:\n{system_prompt}\n\n用户输入:\n{user_prompt}",
            cwd=default_cwd,
            timeout=timeout,
            settings_override=settings,
        )
        result["provider"] = "codex"
        result["provider_label"] = str(model_item.get("provider_name") or "Codex ChatGPT")
        # A custom Codex provider is a stable executor alias. Its real upstream
        # model is selected by the executor profile and can change independently
        # of the alias row, so validation must report what was actually invoked.
        if str(settings.get("model_transport") or "") == "codex_cli_custom_provider":
            result["model"] = str(settings.get("codex_model") or "")
        else:
            result["model"] = str(model_item.get("model") or settings.get("codex_model") or "")
    else:
        result = run_transport(
            settings,
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            timeout=timeout,
        )
    record_usage(settings, result, source="validation_lab", user_id="web-console")
    return {
        "ok": bool(result.get("ok")),
        "reply": str(result.get("reply") or result.get("output") or "").strip(),
        "provider": str(result.get("provider") or ""),
        "provider_label": str(result.get("provider_label") or model_item.get("provider_name") or ""),
        "model": str(result.get("model") or model_item.get("model") or ""),
        "validated_at": validation_started_at,
        "duration": result.get("duration"),
        "usage": result.get("usage") or {},
        "error_kind": str(result.get("error_kind") or ""),
        "error": _playground_error(result),
        # These fields are protocol facts, never provider response content.
        # They let the console separate an exhausted response budget from a
        # reasoning-only result or an incompatible OpenAI-compatible shape.
        "finish_reason": str(result.get("finish_reason") or "")[:40],
        "reasoning_only": bool(result.get("reasoning_only")),
        "response_shape": str(result.get("response_shape") or "")[:40],
        # A completed probe may legitimately fail because of the selected
        # model or its upstream. Preserve the classification so the console
        # can distinguish a configuration correction from a retry later.
        "retryable": bool(result.get("retryable")),
        "owner_action_required": bool(result.get("owner_action_required")),
    }
