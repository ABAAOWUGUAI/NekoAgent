#!/usr/bin/env python3
"""Factual Interaction Plan projection for deterministic QQ control actions."""

from __future__ import annotations

from collections.abc import Mapping


def build_qq_control_mode_decision(action: Mapping[str, object]) -> dict:
    """Describe a server-owned QQ control result without granting LLM authority."""

    action_type = str(action.get("action_type") or "qq_control")
    plan = {
        "schema_version": 1,
        "summary_mode": "work",
        "primary_intent": "ops",
        "confidence": 1.0,
        "reason": "命中 Bridge 受支持的确定性 QQ 管理动作。",
        "intents": [{
            "id": "intent-1",
            "type": "ops",
            "confidence": 1.0,
            "objective": "执行或查询 QQ 群准入状态",
            "requires_tools": False,
            "risk_level": "low" if action_type.endswith(("read", "diagnose")) else "medium",
        }],
        "reply_parts": [],
        "actions": [{
            "id": "action-1",
            "type": "respond",
            "intent_id": "intent-1",
            "objective": f"返回 {action_type} 的结构化事实与回执",
            "requires_tools": False,
            "risk_level": "none",
        }],
        "approval_requests": [],
        "memory_candidates": [],
    }
    return {
        "mode": "work",
        "intent": "ops",
        "confidence": 1.0,
        "reason": plan["reason"],
        "work_lifecycle": "none",
        "end_work": False,
        "allow_emoji": False,
        "need_tools": False,
        "response_style": "structured",
        "emotion": "neutral",
        "reply_length": "medium",
        "meme_intent": "none",
        "engagement": "respond",
        "source": "qq_control_router",
        "interaction_plan": plan,
    }


__all__ = ["build_qq_control_mode_decision"]
