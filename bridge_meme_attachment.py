#!/usr/bin/env python3
"""Text-model contract for deterministic, reviewed local meme attachments."""

from __future__ import annotations

import re
import json
import hashlib
from typing import Callable


MANUAL_MEME_HINTS = ("表情包", "发图", "发张图", "来张图", "图片回复")
CONTROLLED_PLAYFUL_HINTS = ("哈哈", "笑死", "好笑", "逗你", "绝了")
CONTROLLED_HAPPY_HINTS = ("好耶", "开心", "庆祝", "太棒", "牛啊")
CONTROLLED_SERIOUS_HINTS = (
    "求助", "帮我", "怎么办", "撑不住", "崩溃", "难受", "害怕", "生病",
    "报错", "修复", "代码", "项目", "工作", "任务", "解释", "分析", "为什么",
    "自杀", "轻生", "伤害自己", "急救", "医院", "死亡", "抑郁", "被打", "吵架",
    "停下", "别发", "不要发", "闭嘴",
)
CONTROLLED_FACT_HINTS = ("已经", "完成", "成功", "处理", "查到", "修好", "提交", "数据", "因为", "原因", "需要")
CONTROLLED_HOSTILE_HINTS = ("蠢", "废物", "垃圾", "有病", "滚开", "闭嘴")
ATTACHMENT_DENIAL_HINTS = (
    "发不了图", "不能发图", "无法发图", "不能发送图片", "无法发送图片", "只能发文字",
)
UNSUPPORTED_IMAGE_CREATION_HINTS = ("现打", "现画", "现场生成", "马上生成图片", "给你画一张")


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def manual_meme_request(message: str) -> bool:
    return any(hint in str(message or "") for hint in MANUAL_MEME_HINTS)


def controlled_meme_eligibility(
    *,
    scope: str,
    message: str,
    reply: str,
    mode_decision: dict,
    quality_status: str,
    social_cues: dict | None = None,
    has_inbound_media: bool = False,
) -> dict:
    """Body-free, conservative attachment decision after an approved short reply.

    This does not turn a declined or failed response into a send. The model's
    original decision remains authoritative for conversation/quality records.
    """

    plan = mode_decision or {}
    cues = social_cues or plan
    request = str(message or "").strip()
    caption = str(reply or "").strip()

    def deny(reason: str) -> dict:
        return {"eligible": False, "reason": reason, "emotion": ""}

    if scope not in {"group", "private"} or quality_status != "passed" or not caption:
        return deny("reply_not_approved")
    if has_inbound_media:
        return deny("inbound_media_requires_text")
    if (
        manual_meme_request(request)
        or str(plan.get("meme_intent") or "none") == "strong"
        or str(cues.get("meme_intent") or "none") == "strong"
    ):
        return deny("existing_meme_path")
    if str(plan.get("mode") or "daily") != "daily" or str(plan.get("intent") or "chat") != "chat":
        return deny("not_daily_social")
    if str(plan.get("reply_length") or "short") != "short":
        return deny("not_short_reply")
    if scope == "group" and (
        plan.get("should_reply") is False
        or str(plan.get("social_action") or "reply") not in {"reply", "echo_reaction", "ack_add", "follow_up"}
    ):
        return deny("not_short_social_action")
    if any(hint in request for hint in CONTROLLED_SERIOUS_HINTS):
        return deny("serious_or_task_context")
    if (
        len(caption) > 32
        or "\n" in caption
        or any(character in caption for character in "?？:：@`/0123456789")
        or "http" in caption.lower()
        or any(hint in caption for hint in CONTROLLED_FACT_HINTS)
        or any(hint in caption for hint in CONTROLLED_HOSTILE_HINTS)
    ):
        return deny("caption_not_social")
    emotion = str(cues.get("emotion") or plan.get("emotion") or "neutral").strip().lower()
    if emotion == "neutral":
        if any(hint in request for hint in CONTROLLED_PLAYFUL_HINTS):
            emotion = "playful"
        elif any(hint in request for hint in CONTROLLED_HAPPY_HINTS):
            emotion = "happy"
    if emotion not in {"happy", "playful"}:
        return deny("emotion_not_light")
    matching_hints = CONTROLLED_PLAYFUL_HINTS if emotion == "playful" else CONTROLLED_HAPPY_HINTS
    if not any(hint in request for hint in matching_hints):
        return deny("no_current_light_anchor")
    return {"eligible": True, "reason": "controlled_conversion", "emotion": emotion}


def record_meme_funnel(context: dict, *, scope: str) -> None:
    """Emit a body-free stage marker; Outbox and ACK remain separate evidence."""
    try:
        event = {
            "event": "assistant_meme_funnel",
            "scope": scope,
            "stage": str(context.get("funnel_stage") or "selector"),
            "reason": str(context.get("funnel_reason") or context.get("reason") or "unknown"),
            "planned": bool(context.get("planned")),
        }
        if context.get("decision_source"):
            event["decision_source"] = _clip(context.get("decision_source"), 30)
        decision_id = str(context.get("decision_id") or "").strip()
        if decision_id:
            event["decision_ref"] = hashlib.sha256(decision_id.encode("utf-8")).hexdigest()[:12]
        print("assistant_meme_funnel " + json.dumps(event, ensure_ascii=True, sort_keys=True), flush=True)
    except Exception:
        pass  # Observability must never change delivery.


def prepare_meme_attachment(
    *,
    db_connect: Callable,
    settings: dict,
    policy: dict,
    message: str,
    mode_decision: dict,
    social_cues: dict,
    user_id: str,
    intent: str,
    choose_meme: Callable | None = None,
    selection_runtime: tuple | None = None,
    require_exact_match: bool = False,
) -> tuple[dict, dict | None]:
    """Reserve a reviewed local asset before asking a text-only model for copy."""

    requested = manual_meme_request(message)
    truthy = {"1", "true", "yes", "on"}
    enabled = str(settings.get("meme_enabled") or "0").lower() in truthy
    daily_enabled = str(settings.get("meme_daily_enabled") or "0").lower() in truthy
    work_enabled = str(settings.get("meme_work_enabled") or "0").lower() in truthy
    mode = str(mode_decision.get("mode") or "daily")
    emoji_mode = str(policy.get("daily_emoji_mode") or "manual")
    meme_intent = str(social_cues.get("meme_intent") or "none")
    allow_daily = daily_enabled and ((emoji_mode == "auto" and meme_intent == "strong") or requested)
    allow_work = work_enabled and bool(policy.get("work_emoji_enabled")) and (requested or meme_intent == "strong")
    allowed = enabled and ((mode == "work" and allow_work) or (mode != "work" and allow_daily))
    context = {
        "requested": requested,
        "planned": False,
        "kind": "local_reviewed_meme",
        "vision_used": False,
        "generation_supported": False,
        "reason": "not_requested_or_policy_disabled",
        "funnel_stage": "policy" if not enabled else "decision",
        "funnel_reason": "global_policy_disabled" if not enabled else "intent_or_mode_not_allowed",
    }
    if not allowed:
        return context, None
    select_meme, vision_settings, call_model, record_model = selection_runtime or (None, None, None, None)
    diagnostics = {}
    try:
        with db_connect() as conn:
            session_row = conn.execute(
                "SELECT session FROM qq_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            session = str(session_row["session"] if session_row else "")
            if callable(select_meme):
                selector_kwargs = dict(
                    text=message,
                    mode=mode,
                    intent=intent,
                    emotion_hint=str(social_cues.get("emotion") or ""),
                    user_id=user_id,
                    session=session,
                    allow_recent_reuse=requested,
                    vision_settings=vision_settings,
                    call_model=call_model,
                    record_model=record_model,
                )
                if require_exact_match:
                    selector_kwargs["exact_match_only"] = True
                meme, diagnostics = select_meme(
                    conn,
                    **selector_kwargs,
                )
            else:
                if not callable(choose_meme):
                    raise RuntimeError("meme_selector_missing")
                meme = choose_meme(
                    conn,
                    text=message,
                    mode=mode,
                    intent=intent,
                    increment_usage=False,
                    user_id=user_id,
                    session=session,
                    emotion_hint=str(social_cues.get("emotion") or ""),
                    allow_recent_reuse=requested,
                )
                diagnostics = {}
    except Exception:
        meme = None
        diagnostics = {"reason_code": "selection_error"}
    if not meme:
        context["reason"] = str((diagnostics or {}).get("reason_code") or "no_approved_asset")
        context["funnel_stage"] = "selector"
        context["funnel_reason"] = context["reason"]
        context["selection_diagnostics"] = diagnostics or {}
        return context, None
    context.update(
        {
            "planned": True,
            "reason": "reviewed_asset_selected",
            "asset_label": _clip(meme.get("name") or "已审核本地表情包", 120),
            "emotion": _clip(meme.get("selected_emotion") or meme.get("emotion") or "daily", 40),
            "selection_method": _clip(meme.get("selection_method") or "deterministic", 40),
            "selection_reason": _clip(meme.get("selection_reason"), 120),
            "selection_diagnostics": meme.get("selection_diagnostics") or {},
            "funnel_stage": "selected",
            "funnel_reason": "reviewed_asset_selected",
        },
    )
    return context, meme


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "启用"}


def _group_attachment_context(
    reason: str,
    *,
    stage: str,
    funnel_reason: str,
    decision: dict | None = None,
) -> tuple[dict, None]:
    plan = decision or {}
    return {
        "requested": False,
        "planned": False,
        "kind": "local_reviewed_meme",
        "vision_used": False,
        "generation_supported": False,
        "group_meme": True,
        "reason": reason,
        # Body-free diagnostics: the source message is deliberately excluded.
        "funnel_stage": stage,
        "funnel_reason": funnel_reason,
        "social_action": _clip(plan.get("social_action"), 40),
        "meme_intent": _clip(plan.get("meme_intent") or "none", 20),
        "decision_emotion": _clip(plan.get("emotion") or "neutral", 20),
    }, None


def prepare_group_meme_attachment(
    *,
    db_connect: Callable,
    settings: dict,
    policy: dict,
    group_policy: dict,
    message: str,
    decision: dict,
    user_id: str,
    persona_meme_policy: str = "contextual",
    selection_runtime: tuple | None = None,
    require_exact_match: bool = False,
) -> tuple[dict, dict | None]:
    """Reserve a reviewed meme for an approved group attachment decision.

    SinglePlan or a post-truth controlled conversion can request the attachment,
    but neither creates a second delivery or model path. This adapter keeps the global policy,
    per-group switch, Persona switch, cooldowns and approved-asset selection in
    the existing attachment plane, with deterministic selection only.
    """

    if not _truthy((group_policy or {}).get("meme_enabled")):
        return _group_attachment_context(
            "group_policy_disabled",
            stage="policy",
            funnel_reason="group_policy_disabled",
            decision=decision,
        )
    action = str((decision or {}).get("social_action") or "").strip().lower()
    if action != "meme_reaction":
        return _group_attachment_context(
            "group_action_not_meme_reaction",
            stage="decision",
            funnel_reason="decision_not_meme_reaction",
            decision=decision,
        )
    if str((decision or {}).get("meme_intent") or "none").strip().lower() != "strong":
        return _group_attachment_context(
            "group_meme_intent_not_strong",
            stage="intent",
            funnel_reason="intent_not_strong",
            decision=decision,
        )
    persona_key = str(persona_meme_policy or "contextual").strip().lower()
    if persona_key == "never":
        return _group_attachment_context(
            "persona_meme_disabled",
            stage="persona",
            funnel_reason="persona_policy_disabled",
            decision=decision,
        )

    attachment_policy = dict(policy or {})
    if persona_key == "frequent":
        attachment_policy["daily_emoji_mode"] = "auto"
    select_meme = selection_runtime[0] if selection_runtime and callable(selection_runtime[0]) else None
    context, meme = prepare_meme_attachment(
        db_connect=db_connect,
        settings=dict(settings or {}),
        policy=attachment_policy,
        message=message,
        mode_decision={"mode": str((decision or {}).get("mode") or "daily")},
        social_cues={
            "meme_intent": "strong",
            "emotion": str((decision or {}).get("emotion") or ""),
        },
        user_id=user_id,
        intent=str((decision or {}).get("intent") or "chat"),
        # Group reaction selection must not spend another model call after the
        # single-plan decision has already completed.
        selection_runtime=(select_meme, None, None, None),
        require_exact_match=require_exact_match,
    )
    context.update({
        "group_meme": True,
        "social_action": action,
        "meme_intent": "strong",
        "decision_emotion": _clip((decision or {}).get("emotion") or "neutral", 20),
        "funnel_stage": "selected" if context.get("planned") else "selector",
        "funnel_reason": str(context.get("reason") or "selection_unknown"),
    })
    return context, meme


def align_reply_with_attachment(reply: str, context: dict | None) -> str:
    """Make final text consistent with the deterministic attachment plan."""

    text = str(reply or "").strip()
    item = context or {}
    if not item.get("requested") and not item.get("planned"):
        return text
    if item.get("planned"):
        parts = [part.strip() for part in re.split(r"(?<=[。！？!?])|\n+", text) if part.strip()]
        safe = [
            part for part in parts
            if not any(hint in part for hint in ATTACHMENT_DENIAL_HINTS)
            and not any(hint in part for hint in UNSUPPORTED_IMAGE_CREATION_HINTS)
        ]
        return "".join(safe).strip() or "给你挑了一张～"
    diagnostics = item.get("selection_diagnostics") or {}
    excluded = diagnostics.get("excluded") or {}
    if diagnostics.get("approved_count") == 0:
        return "当前没有已审核、可发送的表情包，我先用文字陪你。"
    if excluded.get("invalid"):
        return "已审核的表情包文件目前不可用，需要修复资产后才能发送。"
    if excluded.get("recent"):
        return "可用表情都在防重复窗口内，暂时不重复发送；稍后再试或明确让我重发。"
    if excluded.get("cooldown"):
        return "可用表情还在冷却时间内，暂时不发送，避免刷屏。"
    if excluded.get("daily_limit"):
        return "可用表情今天已达到发送上限，暂时不再发送。"
    if item.get("reason") == "selection_error":
        return "表情选择服务暂时异常，本轮不附图。"
    return "暂时没有符合当前条件的已审核表情包，我先用文字陪你。"


def mark_failed_attachment(
    *,
    db_connect: Callable,
    mark_delivery: Callable,
    meme: dict | None,
    error: object,
) -> None:
    selection_id = str((meme or {}).get("selection_id") or "").strip()
    if not selection_id:
        return
    try:
        with db_connect() as conn:
            mark_delivery(conn, selection_id, status="failed", error=_clip(error, 1000))
    except Exception:
        return


__all__ = [
    "align_reply_with_attachment",
    "controlled_meme_eligibility",
    "manual_meme_request",
    "mark_failed_attachment",
    "prepare_meme_attachment",
]
