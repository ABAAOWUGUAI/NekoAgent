#!/usr/bin/env python3
"""Text-model contract for deterministic, reviewed local meme attachments."""

from __future__ import annotations

import re
from typing import Callable


MANUAL_MEME_HINTS = ("表情包", "发图", "发张图", "来张图", "图片回复")
ATTACHMENT_DENIAL_HINTS = (
    "发不了图", "不能发图", "无法发图", "不能发送图片", "无法发送图片", "只能发文字",
)
UNSUPPORTED_IMAGE_CREATION_HINTS = ("现打", "现画", "现场生成", "马上生成图片", "给你画一张")


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def manual_meme_request(message: str) -> bool:
    return any(hint in str(message or "") for hint in MANUAL_MEME_HINTS)


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
    allow_work = work_enabled and bool(policy.get("work_emoji_enabled"))
    allowed = enabled and ((mode == "work" and allow_work) or (mode != "work" and allow_daily))
    context = {
        "requested": requested,
        "planned": False,
        "kind": "local_reviewed_meme",
        "vision_used": False,
        "generation_supported": False,
        "reason": "not_requested_or_policy_disabled",
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
                meme, diagnostics = select_meme(
                    conn,
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
        },
    )
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
    "manual_meme_request",
    "mark_failed_attachment",
    "prepare_meme_attachment",
]
