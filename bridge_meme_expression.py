"""Bounded post-reply expression choice using reviewed meme metadata only."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

from bridge_meme_selection import candidate_pool, select_and_reserve_meme
from bridge_meme_attachment import manual_meme_request, record_meme_funnel


# Source-page prose and an approval flag do not attest to visible image content.
# These method values are review attestations, not a substitute for image audit.
VISUAL_REVIEW_METHODS = frozenset({
    "codex_visual_review", "manual_visual_review", "vlm_visual_review",
})
GENERIC_TAGS = frozenset({
    "daily", "happy", "comfort", "playful", "curious", "sad", "angry",
    "awkward", "surprise", "boundary", "solicitation", "work",
    "可见内容已核对", "二创参考", "私用", "接梗",
})


def _anchored_candidates(candidates: list[dict], message: str, reply: str) -> list[dict]:
    """Offer only visually reviewed assets with a specific current-turn anchor.

    This is a conservative cheap retrieval stage, not a second social decision.
    The expression model still decides whether to send a matching image.
    """
    # The already checked reply, not just the incoming topic, must carry the
    # asset's meaning. Otherwise a passing mention in the group would make
    # every unrelated text reply pay for another model call.
    context = reply.casefold()
    offered: list[dict] = []
    for item in candidates:
        if (str(item.get("description_method") or "") not in VISUAL_REVIEW_METHODS
                or not str(item.get("description") or "").strip()):
            continue
        # Boundary/solicitation assets require an explicit request; the
        # automatic path must not turn a generic group reply into a taunt.
        if str(item.get("emotion") or "").casefold() in {"boundary", "solicitation"}:
            continue
        tags = [tag.strip().casefold() for tag in
                re.split(r"[,，;；]", str(item.get("tags") or ""))]
        if not any(tag and tag not in GENERIC_TAGS
                   and (len(tag) >= 2 or tag == "喵") and tag in context
                   for tag in tags):
            continue
        offered.append(item)
        if len(offered) == 16:
            break
    return offered


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _plain(reason: str) -> tuple[dict, None]:
    return {"planned": False, "delivery_form": "text", "reason": reason,
            "funnel_stage": "expression", "funnel_reason": reason,
            "decision_source": "expression_model"}, None


def _can_replace_reply(reply: str) -> bool:
    # An image must not erase a substantive answer or a task commitment.
    text = reply.strip()
    return (
        0 < len(text) <= 28 and "\n" not in text
        and not any(char in text for char in "?？:：0123456789`/")
        and "http" not in text.lower()
        and not any(word in text for word in ("因为", "答案", "证据", "已经完成", "我会", "我来处理"))
    )


def _choice_json_body(value: object) -> str:
    """Remove only a complete JSON code fence; never accept surrounding prose."""

    raw = str(value or "")
    fenced = re.fullmatch(
        r"```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```",
        raw.strip(), flags=re.IGNORECASE,
    )
    return fenced.group("body").strip() if fenced else raw


def _model_reply(settings: dict, messages: list[dict], timeout: int, *,
                 call_openai: Callable | None, run_codex: Callable | None,
                 default_cwd: str | Path) -> dict:
    if str(settings.get("chat_provider") or "") == "openai-compatible":
        if not callable(call_openai):
            return {"ok": False}
        return dict(call_openai(settings, messages, timeout=timeout) or {})
    if not callable(run_codex):
        return {"ok": False}
    prompt = "\n\n".join(f"[{item['role'].upper()}]\n{item['content']}" for item in messages)
    return dict(run_codex(prompt, cwd=default_cwd, timeout=timeout,
                          settings_override=settings) or {})


def choose_meme_expression(
    *, db_connect: Callable, settings: dict, policy: dict, group_policy: dict | None,
    scope: str, message: str, reply: str, mode: str, intent: str, user_id: str,
    session: str, persona_meme_policy: str, visual_ready: bool,
    deadline_monotonic: float | None, call_openai: Callable | None,
    run_codex: Callable | None, default_cwd: str | Path,
    record_model: Callable | None = None,
    approved: bool = True,
) -> tuple[dict, dict | None]:
    """Choose text, image-only or text+image for one already-approved social turn.

    No additional participation, Outbox entry, image-model call or unreviewed
    asset is created here. Any uncertain state leaves the checked text intact.
    """
    if not approved or scope not in {"group", "private"} or not reply.strip():
        return _plain("reply_unavailable")
    if mode != "daily" or intent != "chat":
        return _plain("non_social_turn")
    if manual_meme_request(message):
        return _plain("explicit_request_uses_existing_path")
    if not visual_ready:
        return _plain("visual_observation_unavailable")
    if persona_meme_policy == "never" or (scope == "group" and not _enabled((group_policy or {}).get("meme_enabled"))):
        return _plain("scope_policy_disabled")
    if not (_enabled(settings.get("meme_enabled")) and _enabled(settings.get("meme_daily_enabled"))):
        return _plain("global_policy_disabled")
    if str(policy.get("daily_emoji_mode") or "manual") != "auto":
        return _plain("auto_policy_disabled")
    remaining = float(deadline_monotonic or 0) - time.monotonic()
    if remaining < 4:
        return _plain("deadline_insufficient")
    try:
        with db_connect() as conn:
            candidates, diagnostics = candidate_pool(
                conn, text=message, mode=mode, intent=intent, emotion_hint="",
                user_id=user_id, allow_recent_reuse=False, include_all_candidates=True,
            )
    except Exception:
        return _plain("candidate_query_failed")
    if not candidates:
        return _plain(str(diagnostics.get("reason_code") or "no_eligible_asset"))
    candidates = _anchored_candidates(candidates, message, reply)
    if not candidates:
        return _plain("no_reviewed_relevant_asset")
    # A bounded, context-anchored catalogue; never image bytes or file paths.
    catalogue = [{
        "id": str(item["id"]), "name": str(item.get("name") or "")[:64],
        "description": str(item.get("description") or "")[:140],
        "emotion": str(item.get("emotion") or "")[:30],
        "tags": str(item.get("tags") or "")[:100],
        "intent": str(item.get("intent") or "")[:40],
    } for item in candidates]
    messages = [
        {"role": "system", "content": (
            "你只决定本轮已通过检查的社交回复如何表达。只返回严格 JSON："
            '{"form":"text|sticker_only|text_sticker","asset_id":""}。'
            "可不发图；只有素材描述与当前语境贴切才选 ID。"
            "需要事实回答、解释、安慰、任务承诺或对象不明时选 text；"
            "图片不可替代必要文字。不要臆测图片视觉细节。")},
        {"role": "user", "content": json.dumps({
            "scope": scope, "current_message": str(message or "")[:500],
            "checked_reply": reply[:500], "assets": catalogue,
        }, ensure_ascii=False)},
    ]
    timeout = min(8, max(1, int(remaining - 1)))
    record_meme_funnel({"funnel_stage": "choice_invoked", "funnel_reason": "eligible_catalogue",
                        "decision_source": "expression_model"}, scope=scope)
    try:
        result = _model_reply(settings, messages, timeout, call_openai=call_openai,
                              run_codex=run_codex, default_cwd=default_cwd)
        if callable(record_model):
            record_model(settings, result, source="meme_expression", user_id=user_id)
    except Exception:
        return _plain("expression_model_failed")
    if time.monotonic() >= float(deadline_monotonic):
        return _plain("expression_deadline_exhausted")
    if not result.get("ok"):
        return _plain("expression_model_failed")
    try:
        choice = json.loads(_choice_json_body(result.get("reply") or result.get("output")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return _plain("expression_invalid_json")
    if not isinstance(choice, dict) or set(choice) != {"form", "asset_id"}:
        return _plain("expression_invalid_schema")
    form, asset_id = choice["form"], choice["asset_id"]
    if form == "text" and asset_id == "":
        return _plain("model_chose_text")
    if form not in {"sticker_only", "text_sticker"} or not isinstance(asset_id, str):
        return _plain("expression_invalid_choice")
    if asset_id not in {item["id"] for item in catalogue}:
        return _plain("expression_asset_not_offered")
    if form == "sticker_only" and not _can_replace_reply(reply):
        return _plain("reply_requires_visible_text")
    try:
        with db_connect() as conn:
            meme, _ = select_and_reserve_meme(
                conn, text=message, mode=mode, intent=intent, emotion_hint="",
                user_id=user_id, session=session, allow_recent_reuse=False,
                preferred_meme_id=asset_id,
            )
    except Exception:
        return _plain("expression_selection_failed")
    if not meme:
        return _plain("expression_asset_unavailable")
    return {"planned": True, "delivery_form": form, "reason": "reviewed_asset_selected",
            "funnel_stage": "selected", "funnel_reason": form,
            "decision_source": "expression_model"}, meme


__all__ = ["choose_meme_expression"]
