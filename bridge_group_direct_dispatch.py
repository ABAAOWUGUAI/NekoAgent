#!/usr/bin/env python3
"""Direct QQ group-turn shortcuts after deterministic participation accepts an event."""

from __future__ import annotations

import re
from collections.abc import Callable

from bridge_conversation_participation_engine import deterministic_acknowledgement
from bridge_group_context_frame import DEFAULT_GROUP_CONTEXT_LIMIT, group_model_history
from bridge_inbound_media import inbound_media_notice
from bridge_qq_admin_actions import parse_qq_admin_action
from bridge_qq_participation_shadow import complete_group_dispatch


def prepare_direct_group_turn(
    *,
    connect,
    event,
    deterministic_decision,
    decision: dict,
    group_id: str,
    payload: dict,
    classifier_settings: dict,
    current: dict,
    context_items: list[dict],
    message: str,
    fallback_settings: dict,
    timeout: int,
    get_role_settings,
    planner,
    agent_policy,
    conversation_frame: dict | None = None,
) -> dict:
    assistant_name = str(fallback_settings.get("display_name") or "助手")
    attachments = payload.get("attachments")
    media_notice = inbound_media_notice(
        get_role_settings("conversation_reply", fallback_settings),
        attachments,
        vision_settings=(
            get_role_settings("vision_caption", fallback_settings)
            if isinstance(attachments, list) and attachments
            else None
        ),
        suppress_repeated_notice=bool((conversation_frame or {}).get("media_gate_active")),
    )
    if media_notice is not None:
        return {"result": _complete(
            connect, media_notice, event, deterministic_decision, decision,
            group_id, payload, classifier_settings, current, assistant_name,
            conversation_frame,
        )}
    acknowledgement = deterministic_acknowledgement(
        event,
        assistant_name=assistant_name,
        conversation_frame=conversation_frame,
    )
    if acknowledgement:
        result = {"ok": True, "dispatch": "deterministic_ack", "reply": acknowledgement}
        return {"result": _complete(
            connect, result, event, deterministic_decision, decision,
            group_id, payload, classifier_settings, current, assistant_name,
            conversation_frame,
        )}
    history = group_model_history(
        context_items[:-1],
        limit=int((conversation_frame or {}).get("context_limit") or DEFAULT_GROUP_CONTEXT_LIMIT),
    )
    planned, mode_session = planner.decide(
        user_id=f"group:{group_id}",
        message=message,
        settings=fallback_settings,
        policy=agent_policy(fallback_settings),
        history=history,
        timeout=min(int(timeout or 60), 90),
    )
    return {
        "result": None,
        "mode_session": mode_session,
        "group_history": history,
        "decision": {
            **planned,
            "should_reply": True,
            "confidence": 1.0,
            "reason": deterministic_decision.reason.value,
            "deterministic": True,
            "participation_action": deterministic_decision.action.value,
            "group_conversation_frame": conversation_frame or {},
        },
    }


def apply_group_work_boundary(
    decision: dict,
    *,
    policy: dict,
    sender_id: str,
    intent_label,
) -> tuple[dict, str, str, bool]:
    mode = str(decision.get("mode") or "daily")
    intent = str(decision.get("intent") or "chat")
    allowed = {
        item.strip()
        for item in re.split(r"[,，\s]+", str(policy.get("allowed_work_senders") or ""))
        if item.strip()
    }
    work_allowed = bool(int(policy.get("allow_work") or 0)) and sender_id in allowed
    if mode in {"work", "mixed"} and not work_allowed:
        mode, intent = "daily", "chat"
        decision.update({
            "mode": mode,
            "intent": intent,
            "reason": f"{decision.get('reason') or ''} 群内工作执行未对该成员授权。".strip(),
            "meme_intent": "none" if not int(policy.get("meme_enabled") or 0) else decision.get("meme_intent"),
        })
    decision.update({
        "mode": mode,
        "mode_label": "工作模式" if mode == "work" else ("混合模式" if mode == "mixed" else "日常聊天"),
        "intent": intent,
        "intent_label": intent_label(intent),
        "allow_emoji": bool(int(policy.get("meme_enabled") or 0)) and decision.get("meme_intent") != "none",
        "need_tools": mode in {"work", "mixed"} and work_allowed,
        "work_lifecycle": "start" if mode in {"work", "mixed"} else "none",
    })
    return decision, mode, intent, work_allowed


def dispatch_group_control_action(
    assistant_dispatch,
    decision: dict,
    conversation_frame: dict,
    group_history: list[dict],
    group_id: str,
    sender_id: str,
    message: str,
    payload: dict,
    session: str,
    timeout: int,
    continuity_turn_id: str = "",
) -> tuple[dict, dict, bool] | None:
    action = parse_qq_admin_action(
        message, group_history, current_group_id=group_id,
    )
    if action is None:
        return None
    result = assistant_dispatch(
        user_id=sender_id,
        message=message,
        timeout=timeout,
        trace_id=str(payload.get("trace_id") or ""),
        force="chat",
        delivery_recipient_id=f"group:{group_id}",
        delivery_session=session,
        inbound_context={
            "history": group_history,
            "attachments": list(payload.get("attachments") or []),
            "group_id": group_id,
            "sender_id": sender_id,
            "_external_message_id": str(payload.get("_external_message_id") or ""),
            "_continuity_turn_id": continuity_turn_id,
        },
    )
    if isinstance(result.get("mode_decision"), dict):
        decision = {
            **result["mode_decision"],
            "should_reply": True,
            "confidence": 1.0,
            "reason": str(decision.get("reason") or "explicit_control_action"),
            "deterministic": bool(decision.get("deterministic")),
            "participation_action": decision.get("participation_action") or "reply",
            "group_conversation_frame": conversation_frame,
        }
    return result, decision, bool(result.get("reply"))


def run_admitted_group_turn(
    kernel,
    *,
    payload: dict,
    group_id: str,
    sender_id: str,
    message: str,
    timeout: int,
    operation: Callable[[str], dict],
) -> dict:
    """Create the single post-access Turn for one QQ group inbound event."""
    return kernel.execute_turn(
        {
            "user_id": sender_id,
            "message": message,
            "timeout": timeout,
            "trace_id": str(payload.get("trace_id") or ""),
            "source": "qq_group",
            "delivery_recipient_id": f"group:{group_id}",
            "inbound_context": {
                "group_id": group_id,
                "sender_id": sender_id,
                "_external_message_id": str(payload.get("_external_message_id") or ""),
            },
        },
        operation,
    )


def _complete(
    connect,
    result: dict,
    event,
    deterministic_decision,
    decision: dict,
    group_id: str,
    payload: dict,
    classifier_settings: dict,
    current: dict,
    assistant_name: str,
    conversation_frame: dict | None,
) -> dict:
    with connect() as conn:
        replied = complete_group_dispatch(
            conn,
            event=event,
            deterministic_decision=deterministic_decision,
            decision=decision,
            group_id=group_id,
            payload=payload,
            classifier_settings=classifier_settings,
            current=current,
            result=result,
            assistant_name=assistant_name,
            conversation_frame=conversation_frame,
        )
    result.update(
        {
            "should_reply": replied,
            "group_decision": decision,
            "engagement_decision_id": str(
                result.get("engagement_decision_id")
                or getattr(deterministic_decision, "decision_id", "")
                or current.get("engagement_decision_id")
                or ""
            ),
            "assistant_name": str(result.get("assistant_name") or assistant_name),
        },
    )
    return result


__all__ = [
    "apply_group_work_boundary",
    "dispatch_group_control_action",
    "prepare_direct_group_turn",
    "run_admitted_group_turn",
]
