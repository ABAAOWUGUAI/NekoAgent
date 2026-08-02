#!/usr/bin/env python3
"""Stable, cache-friendly model messages for group engagement decisions."""

from __future__ import annotations

import json

from bridge_group_context_frame import (
    audit_group_conversation_frame,
    group_context_lines,
    normalize_group_context_limit,
)


def build_group_decision_messages(
    policy: dict, history: list[dict], current: dict,
    conversation_frame: dict | None = None,
) -> list[dict[str, str]]:
    """Build a stable protocol prefix followed by chronological group turns.

    Provider KV cache reuse depends on prefix stability.  The protocol and the
    group configuration are emitted before prior turns; volatile candidate
    metadata remains in the final message.  This does not authorize delivery:
    the caller must still apply every server-owned group policy and Outbox ACK.
    """

    current_id = current.get("id")
    prior_history = [
        item for item in history
        if current_id is None or item.get("id") != current_id
    ]
    recent = prior_history[-8:]
    assistant_turns = sum(1 for item in recent if str(item.get("sender_id") or "") == "bot")
    unique_speakers = len({str(item.get("sender_id") or "") for item in recent if item.get("sender_id")})
    context_limit = normalize_group_context_limit(policy.get("max_context"))
    frame = audit_group_conversation_frame(conversation_frame)
    stable_system = {
        "role": "system",
        "content": (
            "你是 QQ 群聊发言决策器，只输出 JSON。决定 AI 此刻是否应该发言，而不是判断能不能回答。"
            "[助手/self] 是你自己以前说过的话，绝不能把它误判为群成员之间的对话。"
            "被明确 @ 时通常应该回复。未被 @ 时，助手仍可参与当前群话题，但必须先找到可追溯的切入点和新增价值。"
            "成员正常互聊不是自动沉默理由，也不是插话理由。这个候选已通过服务端节奏筛选："
            "若存在具体话题锚点且能用一句话接住、补一个小观点或问一个贴题问题，优先选择非 silent。"
            "只有敏感交流、纯确认词、无可读锚点、会重复已有人接住的内容或只能泛泛回应时，选择 silent。"
            "统一会话框架的 active_continuation 只是候选，不是回复义务；它同样要受时效、连续轮数、密度、预算和当前价值约束。"
            "主动参与强度只调节同等候选的证据门槛，绝不能绕过这些规则。"
            "先选择 social_action：silent/ack/ack_add/follow_up/reply/bridge_topic/topic_start/repair。"
            "ack 只简短承接；ack_add 承接后只补一个新点；follow_up 只问一个锚定问题；reply 只回应当前一件事；"
            "bridge_topic 必须说清与当前话题的关联；topic_start 只可基于当前群已有共同上下文且话题明显停住；repair 直接修正自己刚才的具体误解。"
            "输出字段：should_reply(boolean), confidence(0-1), reason, social_action, emotion, reply_length(short/medium), "
            "meme_intent(none/optional/strong), mode(daily/work/mixed), intent(chat/analysis/research/code/ops), "
            "why_now, topic_candidate_id。"
            "\nServer candidate contract: this candidate already passed server-side group access, safety, cooldown, density and budget preflight. "
            "You are not the permission judge. For a non-sensitive, non-acknowledgement message with a concrete topic anchor, choose one non-silent "
            "social_action and a short reason code; reserve silent for the enumerated safety, duplicate or no-anchor cases."
        ),
    }
    stable_context = "\n".join(
        [
            f"群配置：{policy.get('group_name') or policy.get('group_id')}",
            f"主动参与强度（影响候选节奏和证据门槛，不是逐条回复概率）：{float(policy.get('reply_probability') or 0.2):.2f}",
            "最后一条成员消息是当前候选；请只返回 JSON 决策，不复述上下文。",
        ],
    )
    history_messages: list[dict[str, str]] = []
    for item in prior_history[-context_limit:]:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        is_assistant = str(item.get("sender_id") or "") == "bot"
        history_messages.append({
            "role": "assistant" if is_assistant else "user",
            "content": f"[助手/self] {content}" if is_assistant else f"[成员] {content}",
        })
    current_packet = "\n".join(
        [
            "当前候选消息：",
            f"[成员] {str(current.get('content') or '').strip()}",
            "服务端候选元数据：",
            json.dumps({
                "active_continuation": bool(frame.get("active_continuation")),
                "assistant_turns_last_8": assistant_turns,
                "unique_speakers": unique_speakers,
                "server_candidate": "preflight_passed",
            }, ensure_ascii=False, sort_keys=True),
            "如有具体锚点，优先选择 ack_add、follow_up、reply 或 bridge_topic 之一。",
        ],
    )
    return [
        stable_system,
        {"role": "user", "content": stable_context},
        *history_messages,
        {"role": "user", "content": current_packet},
    ]


__all__ = ["build_group_decision_messages"]
