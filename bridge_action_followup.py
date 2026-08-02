#!/usr/bin/env python3
"""Deterministic binding of natural-language follow-ups to recent actions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

from bridge_social_engine import get_group_policy

_VERIFY_HINTS = ("验证", "检查一下", "检查下", "确认生效", "确认一下", "核对", "验收", "看看是否生效")
_CONTINUE_HINTS = ("继续", "接着做", "按刚才", "基于刚才", "这个结果")
_POLICY_FIELDS = (
    "participation_mode", "enabled", "mention_only", "active_reply", "reply_probability",
    "cooldown_seconds", "quiet_start", "quiet_end", "timezone", "max_context", "allow_work",
    "allowed_work_senders", "meme_enabled", "quiet_gap_seconds", "burst_window_seconds",
    "burst_max_messages", "daily_reply_budget", "continuation_window_seconds",
    "max_auto_continuations",
)


def is_action_followup(message: str) -> bool:
    text = str(message or "").strip().lower()
    return bool(text) and any(hint in text for hint in (*_VERIFY_HINTS, *_CONTINUE_HINTS))


def _latest_receipt(recent_actions: list[dict] | None) -> dict | None:
    for item in recent_actions or []:
        receipt = item.get("receipt") if isinstance(item, Mapping) else None
        if isinstance(receipt, Mapping) and str(receipt.get("status") or "") in {"completed", "no_op"}:
            return dict(receipt)
    return None


def verify_recent_action(connect: Callable[[], sqlite3.Connection], *, message: str, recent_actions: list[dict] | None) -> dict | None:
    if not is_action_followup(message):
        return None
    receipt = _latest_receipt(recent_actions)
    if not receipt:
        return None
    action_type = str(receipt.get("action_type") or "")
    if action_type != "qq_group_policy_clone":
        return {
            "ok": True,
            "dispatch": "action_followup_status",
            "reply": "已识别为上一动作的后续请求，并找到服务端完成回执；当前领域的专用验证器尚未接入，本轮没有冒充执行新的验证动作。",
            "action_receipts": [{"action_type": "action.followup.verify", "status": "failed", "facts": {"bound_action_type": action_type, "source": "continuity_receipt", "reason": "domain_verifier_unavailable"}}],
            "verified_action": receipt,
        }
    target_group_id = str(receipt.get("target_id") or "")
    facts = receipt.get("facts") if isinstance(receipt.get("facts"), Mapping) else {}
    source_group_id = str(facts.get("source_group_id") or "")
    if not target_group_id or not source_group_id:
        return {"ok": True, "dispatch": "action_followup_failed", "reply": "上一动作有完成回执，但缺少策略对齐目标，无法安全验收。", "action_receipts": [{"action_type": "action.followup.verify", "status": "failed", "facts": {"reason": "target_missing"}}]}
    with connect() as conn:
        target = get_group_policy(conn, target_group_id)
        source = get_group_policy(conn, source_group_id)
    if not target or not source:
        return {"ok": True, "dispatch": "action_followup_failed", "reply": "上一动作的群策略对象缺失，验收未通过；没有把缺失对象说成已生效。", "action_receipts": [{"action_type": "action.followup.verify", "status": "failed", "facts": {"reason": "policy_missing"}}]}
    mismatches = [key for key in _POLICY_FIELDS if target.get(key) != source.get(key)]
    ok = not mismatches
    return {
        "ok": True,
        "dispatch": "action_followup_verified" if ok else "action_followup_failed",
        "reply": "已按上一动作的服务端回执重新核对，目标策略与模板策略一致，配置对齐验收通过。" if ok else "已按上一动作的服务端回执重新核对，但仍有配置差异，验收未通过。",
        "action_receipts": [{"action_type": "action.followup.verify", "status": "completed" if ok else "failed", "facts": {"bound_action_type": action_type, "policy_aligned": ok, "mismatch_count": len(mismatches)}}],
        "verified_action": receipt,
    }


def dispatch_action_followup(connect: Callable[[], sqlite3.Connection], continuity_kernel: object, *, user_id: str, source: str, trace_id: str, message: str, inbound_context: Mapping[str, object] | None = None, delivery_recipient_id: str = "") -> dict | None:
    recent = continuity_kernel.recent_action_context({"user_id": user_id, "source": source, "trace_id": trace_id, "message": message, "inbound_context": dict(inbound_context or {}), "delivery_recipient_id": delivery_recipient_id})
    result = verify_recent_action(connect, message=message, recent_actions=recent)
    if result is None:
        return None
    result.update({"mode": "work", "intent": "ops", "source": "action_followup_router", "recent_action_context": recent[:3]})
    return result


def dispatch_action_followup_context(connect: Callable[[], sqlite3.Connection], continuity_kernel: object, context: Mapping[str, object]) -> dict | None:
    return dispatch_action_followup(connect, continuity_kernel, user_id=str(context.get("user_id") or ""), source=str(context.get("source") or ""), trace_id=str(context.get("trace_id") or ""), message=str(context.get("message") or ""), inbound_context=context.get("inbound_context") if isinstance(context.get("inbound_context"), Mapping) else {}, delivery_recipient_id=str(context.get("delivery_recipient_id") or ""))


__all__ = ["dispatch_action_followup", "dispatch_action_followup_context", "is_action_followup", "verify_recent_action"]
