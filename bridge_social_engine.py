#!/usr/bin/env python3
"""Natural conversation and controlled group-chat policy.

The bridge keeps transport and task orchestration in ``codex_qq_bridge``.
This module owns the user-facing social layer: expression habits, a compact
daily-chat prompt, group policies, and bounded group context.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bridge_social_experience import (
    compile_runtime_voice_contract,
    detect_expression_feedback,
    ensure_social_experience_tables,
    expression_profile,
    list_expression_habits,
    record_expression_feedback,
    seed_expression_habits,
    select_expression_habits,
    upsert_expression_habit,
)
from bridge_group_message_store import (
    group_context,
    group_recent_turn_metadata,
    record_group_message,
)
from bridge_group_context_frame import (
    acknowledgement_only,
    audit_group_conversation_frame,
    group_context_lines,
    normalize_group_context_limit,
)
from bridge_group_policy_store import get_group_policy, list_group_policies, upsert_group_policy
from bridge_conversation_participation_contract import (
    GroupParticipationMode,
    group_mode_from_legacy,
)
from bridge_social_identity import (
    GROUP_NATURAL_PROMPT_LINES,
    PUBLIC_IDENTITY_PROMPT_LINES,
)
from bridge_social_reply import normalize_social_reply
from bridge_response_modality import voice_modality_prompt_lines


DEFAULT_TIMEZONE = "Asia/Shanghai"


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        if name in {"UTC", "Etc/UTC"}:
            return timezone.utc
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        raise
EMOTIONS = {"neutral", "happy", "sad", "tired", "annoyed", "playful", "curious", "comfort"}
REPLY_LENGTHS = {"short", "medium", "long"}
MEME_INTENTS = {"none", "optional", "strong"}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "开启"}


def normalize_group_inbound(payload: dict) -> tuple[str, bool]:
    is_mention = truthy(payload.get("is_mention"))
    message = str(payload.get("message") or "").strip()
    return (message or "@" if is_mention else message), is_mention


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _slug(value: object, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    return (text or fallback)[:64]


def normalize_social_cues(decision: dict | None, message: str = "") -> dict:
    source = decision or {}
    emotion = str(source.get("emotion") or "neutral").strip().lower()
    if emotion not in EMOTIONS:
        emotion = "neutral"
    reply_length = str(source.get("reply_length") or "short").strip().lower()
    if reply_length not in REPLY_LENGTHS:
        reply_length = "short"
    meme_intent = str(source.get("meme_intent") or "none").strip().lower()
    if meme_intent not in MEME_INTENTS:
        meme_intent = "none"
    text = str(message or "")
    if emotion == "neutral":
        if any(word in text for word in ("累", "困", "不想动")):
            emotion = "tired"
        elif any(word in text for word in ("烦", "生气", "气死", "卡住")):
            emotion = "annoyed"
        elif any(word in text for word in ("难受", "委屈", "失败", "掉线")):
            emotion = "comfort"
        elif any(word in text for word in ("哈哈", "笑死", "逗你")):
            emotion = "playful"
        elif any(word in text for word in ("成功", "完成", "搞定", "开心")):
            emotion = "happy"
    return {
        "emotion": emotion,
        "reply_length": reply_length,
        "meme_intent": meme_intent,
        "engagement": _clip(source.get("engagement") or "respond", 32),
    }


def _memory_lines(memories: list[dict]) -> list[str]:
    lines = [f"- {_clip(item.get('content'), 300)}" for item in memories if _clip(item.get("content"), 300)]
    return lines or ["- 暂无与本轮直接相关的长期记忆。"]


def _habit_lines(habits: list[dict]) -> list[str]:
    lines = [
        f"- 情境“{_clip(item.get('situation'), 120)}”时：{_clip(item.get('style'), 500)}"
        for item in habits
    ]
    return lines or ["- 根据当前语境自然接话，不套用固定开场白。"]


def relationship_context_lines(relationship: dict | None) -> list[str]:
    """Compile a scoped Relationship State without exposing its subject id."""

    item = relationship or {}
    if not item.get("applied"):
        return []
    lines = ["Current Relationship State:"]
    preferred = _clip(item.get("preferred_address"), 80)
    if preferred:
        lines.append(f"- 对当前对象自然使用称呼“{preferred}”；不需每轮重复。")
    lines.append(
        f"- 互动方式：{_clip(item.get('interaction_style') or 'natural', 30)}；"
        f"熟悉程度：{_clip(item.get('familiarity_context') or 'new', 30)}。",
    )
    allowed = [_clip(value, 80) for value in item.get("allowed_topics") or [] if _clip(value, 80)]
    blocked = [_clip(value, 80) for value in item.get("blocked_topics") or [] if _clip(value, 80)]
    if allowed:
        lines.append("- 可在语境合适时提及：" + "、".join(allowed[:8]) + "。")
    if blocked:
        lines.append("- 不主动提起：" + "、".join(blocked[:8]) + "。")
    return lines


def build_voice_contract(
    settings: dict,
    *,
    mode_decision: dict | None = None,
    group_context: dict | None = None,
) -> dict:
    """Create a stable Voice Contract without turning Persona into facts."""

    return compile_runtime_voice_contract(
        settings,
        mode=str((mode_decision or {}).get("mode") or "daily"),
        group=bool(group_context),
)

STRUCTURED_SOCIAL_DECISION_MAX_TOKENS = 700


# This is a server-owned action vocabulary.  The engagement model can propose
# one action, but cannot invent a send path, bypass the guardrails, or turn a
# free-form rationale into a policy decision.  ``approach`` remains only as a
# compatibility projection for the existing SocialOpportunity evidence model.
GROUP_SOCIAL_ACTIONS = {
    "silent",
    "ack",
    "ack_add",
    "follow_up",
    "reply",
    "bridge_topic",
    "topic_start",
    "repair",
}
GROUP_SOCIAL_ACTION_APPROACH = {
    "ack": "light_join",
    "ack_add": "light_join",
    "follow_up": "ask",
    "reply": "inform",
    "bridge_topic": "share",
    "topic_start": "ask",
    "repair": "inform",
}
LEGACY_GROUP_APPROACH_ACTION = {
    "light_join": "ack_add",
    "continue": "reply",
    "share": "bridge_topic",
    "ask": "follow_up",
    "inform": "reply",
}


def normalize_group_social_action(
    value: object,
    *,
    approach: object = "",
    should_reply: bool = False,
) -> str:
    """Return one bounded social action without trusting model free text."""

    action = str(value or "").strip().lower()
    if action in GROUP_SOCIAL_ACTIONS:
        return action
    legacy = str(approach or "").strip().lower()
    if legacy in LEGACY_GROUP_APPROACH_ACTION:
        return LEGACY_GROUP_APPROACH_ACTION[legacy]
    return "reply" if should_reply else "silent"


def plan_expression(
    message: str,
    *,
    social_cues: dict | None = None,
    mode_decision: dict | None = None,
    group_context: dict | None = None,
    voice_contract: dict | None = None,
) -> dict:
    """Return a bounded per-turn expression plan used by prompts and QA."""

    cues = normalize_social_cues(social_cues or mode_decision, message)
    mode = str((mode_decision or {}).get("mode") or "daily")
    group = bool(group_context)
    emotion = cues["emotion"]
    if emotion in {"tired", "sad", "annoyed", "comfort"}:
        purpose, tone = "先接住具体感受，再回应事情", "温和、具体、不急着教育"
    elif emotion == "happy":
        purpose, tone = "一起确认好消息，再接事情本身", "真诚、轻快、不夸张"
    elif emotion == "playful":
        purpose, tone = "顺着当下语境接梗", "轻松、有分寸"
    elif mode in {"work", "mixed"}:
        purpose, tone = "先交代结论或当前真实状态", "可靠、直接、保留人格但不表演"
    else:
        purpose, tone = "回应这句话本身", "自然、熟悉、不端着"
    contract = voice_contract or {}
    social_action = normalize_group_social_action(
        (mode_decision or {}).get("social_action")
        or (mode_decision or {}).get("participation_action"),
        approach=(mode_decision or {}).get("approach"),
        should_reply=bool((mode_decision or {}).get("should_reply")),
    ) if group else ""
    if group:
        length_key = str(contract.get("group_length") or "").strip()
        sentence_limit = {"brief": 1, "short": 2, "balanced": 3}.get(length_key, 1)
    elif mode in {"work", "mixed"}:
        length_key = str(contract.get("work_length") or "").strip()
        sentence_limit = {"compact": 4, "structured_compact": 6, "detailed": 10}.get(
            length_key,
            {"short": 3, "medium": 5, "long": 9}[cues["reply_length"]],
        )
    else:
        length_key = str(contract.get("private_length") or "").strip()
        sentence_limit = {"short": 3, "balanced": 5, "detailed": 9}.get(
            length_key,
            {"short": 3, "medium": 5, "long": 9}[cues["reply_length"]],
        )
    structure = ["直接回应"]
    if mode == "mixed" and emotion in {"tired", "sad", "annoyed", "comfort"}:
        structure = ["具体情绪回应", "事实或结论", "明确下一步"]
    elif mode in {"work", "mixed"}:
        structure = ["事实或结论", "必要依据", "明确下一步"]
    elif emotion in {"tired", "sad", "annoyed", "comfort"}:
        structure = ["具体情绪回应", "用户真正问到的内容"]
    follow_up = str(contract.get("question_policy") or "").strip() or (
        "只有缺少关键条件或自然需要时才问一个问题，否则陈述结束"
    )
    directness = str(contract.get("directness") or "").strip()
    if directness:
        tone = f"{tone}；{directness}"
    if group:
        group_action_plan = {
            "ack": (
                "只给一个轻量承接，不把它扩成新话题",
                1,
                ["简短承接"],
                "不追问，陈述结束",
            ),
            "ack_add": (
                "先接住，再补充一个当前话题里没有重复过的小点",
                2,
                ["简短承接", "一条新增观察"],
                "不连续追问，陈述结束",
            ),
            "follow_up": (
                "只对当前可追溯的细节问一个问题",
                1,
                ["点明正在接的内容", "一个具体问题"],
                "只问一个问题，等待对方回应",
            ),
            "reply": (
                "只回应当前最相关的一件事，不复述全群",
                sentence_limit,
                ["直接回应", "一条必要补充"],
                "回应后停下，不抢下一轮",
            ),
            "bridge_topic": (
                "用当前话题与前文的明确关联自然转接",
                min(sentence_limit, 2),
                ["点出关联", "一条自然延续"],
                "没有明确关联就不要转话题",
            ),
            "topic_start": (
                "只在当前群已有共同上下文且对话明显停住时，抛出低门槛问题",
                1,
                ["一条和当前群相关的低门槛问题"],
                "不钓鱼、不发送无关的新话题",
            ),
            "repair": (
                "直接改正刚才的具体误解，不写客服式道歉",
                min(sentence_limit, 2),
                ["指出自己刚才错在何处", "给出修正"],
                "修正后停止辩解",
            ),
        }.get(social_action)
        if group_action_plan:
            purpose, sentence_limit, structure, follow_up = group_action_plan
    return {
        "purpose": purpose,
        "tone": tone,
        "sentence_limit": sentence_limit,
        "structure": structure,
        "follow_up": follow_up,
        "group_turn": "回应后停下，不抢下一轮" if group else "允许自然结束，不强行续聊",
        "social_action": social_action,
        "meme_intent": cues["meme_intent"],
    }


def voice_contract_lines(contract: dict) -> list[str]:
    if not contract.get("optional_persona_applied", True):
        return [
            f"- 身份：{contract.get('identity')}。",
            "- 可选人格已关闭；不注入人设、亲密关系、口癖或表达偏好。",
            f"- 事实与安全：{contract.get('stance')}",
            "- 禁止：" + "；".join(contract.get("forbidden") or []),
        ]
    lines = [
        f"- 身份：{contract.get('identity')}；关系：{contract.get('relationship')}。",
        f"- 身份核心：{contract.get('identity_core')}",
        f"- 立场：{contract.get('stance')}",
        "- 价值：" + "；".join(contract.get("values") or []),
        "- 边界：" + "；".join(contract.get("boundaries") or []),
        f"- 温度：{contract.get('warmth')}",
        f"- 直接程度（directness={contract.get('directness_key')}）：{contract.get('directness')}",
        f"- 主动程度：{contract.get('initiative')}",
        f"- 幽默：{contract.get('humor')}",
        f"- 节奏与长度：{contract.get('rhythm')}；{contract.get('length_rule')}。",
        (
            "- 场景长度："
            f"private_length={contract.get('private_length')}；"
            f"group_length={contract.get('group_length')}；"
            f"work_length={contract.get('work_length')}。"
        ),
        f"- 追问策略（question_policy={contract.get('question_policy_key')}）：{contract.get('question_policy')}",
        f"- 称呼：{contract.get('address_policy')}",
        f"- 工作连续性：{contract.get('work_continuity')}",
        f"- 表情包：{contract.get('meme_policy')}",
        "- 禁止：" + "；".join(contract.get("forbidden") or []),
    ]
    preferred = contract.get("preferred_phrases") or []
    avoided = contract.get("avoid_phrases") or []
    if preferred:
        lines.append("- 偏好表达：" + "；".join(preferred))
    if avoided:
        lines.append("- 避免表达：" + "；".join(avoided))
    for example in (contract.get("examples") or [])[:3]:
        lines.append(
            f"- 场景示例“{example.get('scenario')}”：建议 {example.get('preferred_style')}"
            + (f"；避免 {example.get('avoid_style')}" if example.get("avoid_style") else ""),
        )
    return [line for line in lines if not line.endswith("：") and not line.endswith("：None")]


def expression_plan_lines(plan: dict) -> list[str]:
    lines = [
        f"- 这一轮要做到：{plan.get('purpose')}",
        f"- 语气：{plan.get('tone')}；最多约 {plan.get('sentence_limit')} 句。",
        f"- 组织顺序：{' → '.join(plan.get('structure') or ['直接回应'])}。",
        f"- 追问：{plan.get('follow_up')}。",
        f"- 收尾：{plan.get('group_turn')}。",
    ]
    if plan.get("social_action"):
        lines.insert(0, f"- 群聊动作：{plan.get('social_action')}。");
    return lines


def attachment_capability_lines(context: dict | None) -> list[str]:
    """Describe an output attachment without pretending the model can see it."""

    item = context or {}
    if not item.get("requested") and not item.get("planned"):
        return voice_modality_prompt_lines(item.get("response_modality"))
    if item.get("planned"):
        label = _clip(item.get("asset_label") or "已审核本地表情包", 120)
        emotion = _clip(item.get("emotion") or "日常", 40)
        return [
            "本轮附件事实：",
            "- 发送层将在这段文字后附加一张已审核的本地表情包。",
            f"- 可用元数据：名称“{label}”，情绪标签“{emotion}”；你没有查看图片像素，不得描述未提供的视觉细节。",
            "- 只写一句与附件协调的自然配文；不得声称发不了图、只能发文字，或正在现画、现打、生成图片。",
        ] + voice_modality_prompt_lines(item.get("response_modality"))
    return [
        "本轮附件事实：",
        "- 用户请求了图片，但发送层没有找到当前可用的已审核本地表情包，本轮不会附图。",
        "- 不得声称已经发图或正在现画、现打、生成图片；应简短说明暂时没有合适的已审核表情包。",
    ] + voice_modality_prompt_lines(item.get("response_modality"))


def build_daily_system_prompt(
    settings: dict,
    memories: list[dict],
    *,
    mode_decision: dict | None = None,
    habits: list[dict] | None = None,
    group_context: dict | None = None,
    attachment_context: dict | None = None,
    voice_contract: dict | None = None,
    expression_plan: dict | None = None,
    relationship_context: dict | None = None,
) -> str:
    cues = normalize_social_cues(mode_decision)
    contract = voice_contract or build_voice_contract(
        settings,
        mode_decision=mode_decision,
        group_context=group_context,
    )
    turn_plan = expression_plan or plan_expression(
        "",
        social_cues=cues,
        mode_decision=mode_decision,
        group_context=group_context,
        voice_contract=contract,
    )
    local_now = datetime.now().astimezone().isoformat(timespec="seconds")
    location = "QQ群聊" if group_context else "QQ 私聊"
    persona_enabled = bool(contract.get("optional_persona_applied", True))
    identity_lines = [f"你的名字是 {contract.get('identity') or 'Assistant'}。"]
    expression_lines: list[str] = []
    habit_lines: list[str] = []
    if persona_enabled:
        identity_lines.extend(
            [
                f"你和用户的关系设定是：{contract.get('relationship') or '熟悉的朋友与工作助手'}。",
                f"人设底色：{contract.get('persona') or ''}",
                f"总体表达：{contract.get('style') or ''}",
            ],
        )
        expression_lines = [
            "",
            "本轮 Expression Plan：",
            *expression_plan_lines(turn_plan),
            *( ["", *relationship_context_lines(relationship_context)] if relationship_context_lines(relationship_context) else [] ),
        ]
        habit_lines = ["", "可采用的表达习惯：", *_habit_lines(habits or [])]
    lines = [
        f"你正在{location}中回复消息。只输出真正要发送的中文消息，不输出分析、模式标签或 JSON。",
        f"当前服务器本地时间：{local_now}。涉及最新天气、新闻、价格或其他时效事实时，不得依赖记忆猜测，应转为实时核验。",
        *identity_lines,
        "",
        "稳定 Voice Contract：",
        *voice_contract_lines(contract),
        *expression_lines,
        "",
        "自然聊天原则：",
        "- 先理解对方此刻是在分享、吐槽、提问、求安慰还是认真办事，再接这一句话本身。",
        "- 不要默认以“收到、好的、当然可以、有什么能帮你”开头；也不要每轮都叫对方名字。",
        "- 用户没有求建议时，不要强行给任务清单；简单一句能接住，就不要写成小报告。",
        "- 不必每次用问题结尾，也不要为了显得热情连续追问。",
        "- 可以有轻微口语、省略和幽默，但不要故意错别字、夸张撒娇或机械堆口癖。",
        "- 不声称自己是真人，不编造共同经历；可以像熟悉的人一样有连续性和立场。",
        *PUBLIC_IDENTITY_PROMPT_LINES,
        "- 技术事实仍要准确；没做过的检查和操作不能说成已经完成。",
        "- 普通聊天回复没有控制面权限。没有 Bridge 提供的动作回执时，不得声称正在或已经修改配置、登录服务器、查看日志、重启服务、部署或测试。",
        (
            f"- 本轮情绪线索：{cues['emotion']}；长度以本轮 Expression Plan（约 "
            f"{turn_plan.get('sentence_limit')} 句）和当前场景 Voice Contract 为准。"
        ),
        *habit_lines,
        "",
        "与本轮相关的长期记忆：",
        *_memory_lines(memories),
    ]
    attachment_lines = attachment_capability_lines(attachment_context)
    if attachment_lines:
        lines.extend(["", *attachment_lines])
    if group_context:
        rhythm = (
            group_context.get("expression_rhythm")
            if isinstance(group_context.get("expression_rhythm"), dict)
            else {}
        )
        rhythm_lines = [(
            "- 最近群成员消息的非内容节奏："
            f"样本 {int(rhythm.get('sample_count') or 0)} 条，"
            f"中位长度约 {int(rhythm.get('median_length') or 0)} 字，"
            f"本轮按 {rhythm.get('target') or 'brief'} 控制；"
            "只模仿长度与节奏，不照抄成员原话。"
        )] if int(rhythm.get("sample_count") or 0) else []
        lines.extend(
            [
                "",
                "群聊边界：",
                f"- 群名：{group_context.get('group_name') or group_context.get('group_id') or '未知'}。",
                f"- 当前发言者：{group_context.get('sender_name') or group_context.get('sender_id') or '群成员'}。",
                "- 只回应当前话题，不替群成员下结论，不暴露私聊记忆、服务器密钥或后台配置。",
                "- 未被明确点名时，不评价群成员、不催促、不调侃对方能力；没有新增价值就保持沉默。",
                "- 群聊回复比私聊更短；不要抢话、总结全群或把每条消息都变成任务。",
                "- 默认只发一句；不要用括号补充动作、心理或旁白，也不要连续复用“好家伙、笑死、哈哈、懂了、草”等固定开场。",
                "- 不为显得像群友而编造自己的经历、爱好、身体反应或群内身份；有态度即可，不需要表演人设。",
                *rhythm_lines,
                *GROUP_NATURAL_PROMPT_LINES,
            ],
        )
    return "\n".join(lines)


def _parse_iso(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _inside_quiet_hours(policy: dict, now_utc: datetime) -> bool:
    try:
        zone = _timezone(str(policy.get("timezone") or DEFAULT_TIMEZONE))
        start_h, start_m = [int(part) for part in str(policy.get("quiet_start") or "23:30").split(":", 1)]
        end_h, end_m = [int(part) for part in str(policy.get("quiet_end") or "08:30").split(":", 1)]
    except (ValueError, TypeError):
        return False
    local = now_utc.astimezone(zone)
    current = local.hour * 60 + local.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


def group_hard_gate(
    policy: dict | None,
    *,
    is_mention: bool,
    continuation_candidate: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str]:
    if not policy or not int(policy.get("enabled") or 0):
        return False, "group_disabled"
    if is_mention:
        return True, "direct_mention"
    explicit_mode = str(policy.get("participation_mode") or "").strip() if policy else ""
    if explicit_mode:
        mode = group_mode_from_legacy(policy)
        if mode is GroupParticipationMode.MENTIONS_ONLY:
            return False, "mention_required"
        if mode is GroupParticipationMode.DISABLED:
            return False, "group_disabled"
        # Directed and natural modes are model-engagement candidates.  The
        # deterministic engine has already handled direct/reply/continuation.
        allow_model = mode in {
            GroupParticipationMode.DIRECTED_CONTEXT,
            GroupParticipationMode.NATURAL_PARTICIPATION,
        }
    else:
        allow_model = bool(int(policy.get("active_reply") or 0))
    if int(policy.get("mention_only") or 0):
        return False, "mention_required"
    if not allow_model:
        return False, "active_reply_disabled"
    current = now or datetime.now(timezone.utc)
    if _inside_quiet_hours(policy, current):
        return False, "quiet_hours"
    last_reply = _parse_iso(policy.get("last_reply_at"))
    cooldown = max(15, int(policy.get("cooldown_seconds") or 180))
    if (
        last_reply
        and current - last_reply < timedelta(seconds=cooldown)
        and not continuation_candidate
    ):
        return False, "cooldown"
    return True, "model_decision_required"

def build_group_decision_messages(
    policy: dict, history: list[dict], current: dict,
    conversation_frame: dict | None = None,
) -> list[dict[str, str]]:
    current_id = current.get("id")
    prior_history = [
        item for item in history
        if current_id is None or item.get("id") != current_id
    ]
    recent = prior_history[-8:]
    assistant_turns = sum(1 for item in recent if str(item.get("sender_id") or "") == "bot")
    unique_speakers = len({str(item.get("sender_id") or "") for item in recent if item.get("sender_id")})
    context_limit = normalize_group_context_limit(policy.get("max_context"))
    context_lines = group_context_lines(prior_history, limit=context_limit)
    frame = audit_group_conversation_frame(conversation_frame)
    return [
        {
            "role": "system",
            "content": (
                "你是 QQ 群聊发言决策器，只输出 JSON。决定 AI 此刻是否应该发言，而不是判断能不能回答。[助手/self] 是你自己以前说过的话，绝不能把它误判为群成员之间的对话。"
                "被明确 @ 时通常应该回复。未被 @ 时，助手仍可参与当前群话题，但必须先找到可追溯的切入点和新增价值。"
                "成员正常互聊不是自动沉默理由，也不是插话理由；没有新增价值、会打断敏感交流、已经有人接住、连续刷存在感、纯确认词或只能泛泛回应时，选择 silent。"
                "统一会话框架的 active_continuation 只是候选，不是回复义务；它同样要受时效、连续轮数、密度、预算和当前价值约束。"
                "主动参与强度只调节同等候选的证据门槛，绝不能绕过这些规则。"
                "先选择 social_action：silent/ack/ack_add/follow_up/reply/bridge_topic/topic_start/repair。"
                "ack 只简短承接；ack_add 承接后只补一个新点；follow_up 只问一个锚定问题；reply 只回应当前一件事；"
                "bridge_topic 必须说清与当前话题的关联；topic_start 只可基于当前群已有共同上下文且话题明显停住；repair 直接修正自己刚才的具体误解。"
                "输出字段：should_reply(boolean), confidence(0-1), reason, social_action, emotion, reply_length(short/medium), "
                "meme_intent(none/optional/strong), mode(daily/work/mixed), intent(chat/analysis/research/code/ops), "
                "why_now, topic_candidate_id。"
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"群：{policy.get('group_name') or policy.get('group_id')}",
                    f"主动参与强度（仅影响证据门槛）：{float(policy.get('reply_probability') or 0.2):.2f}",
                    f"是否被 @：{bool(current.get('is_mention'))}",
                    f"最近 8 条中助手已发言：{assistant_turns} 次；参与者：{unique_speakers} 人",
                    f"统一群会话框架：{json.dumps(frame, ensure_ascii=False, sort_keys=True)}",
                    "最近群聊：",
                    *(context_lines or ["(无上下文)"]),
                    f"当前消息：{current.get('sender_name') or current.get('sender_id')}: {current.get('content')}",
                ],
            ),
        },
    ]


def apply_group_turn_policy(
    policy: dict, history: list[dict], current: dict, decision: dict,
    conversation_frame: dict | None = None,
    *,
    rhythm_history: list[dict] | None = None,
) -> dict:
    """Apply deterministic rhythm constraints after the model decision.

    The model judges conversational value. This layer owns repeat suppression,
    acknowledgement silence and assistant turn density across providers.
    """
    result = dict(decision or {})
    message = str(current.get("content") or "").strip()
    is_mention = bool(current.get("is_mention"))
    recent = list((rhythm_history if rhythm_history is not None else history)[-8:])
    assistant_turns = sum(1 for item in recent if str(item.get("sender_id") or "") == "bot")
    repeated = any(
        str(item.get("sender_id") or "") == str(current.get("sender_id") or "")
        and str(item.get("content") or "").strip() == message
        for item in recent[:-1]
    )
    is_acknowledgement = acknowledgement_only(message)
    frame = conversation_frame or {}
    signals = {
        "direct_mention": is_mention,
        "assistant_turns_last_8": assistant_turns,
        "acknowledgement_only": is_acknowledgement,
        "repeated_message": repeated,
        "active_continuation": bool(frame.get("active_continuation")),
        "continuation_assistant_turns": int(frame.get("continuation_assistant_turns") or 0),
    }
    if is_mention:
        result.update({"should_reply": True, "turn_policy": signals})
        return result
    if repeated:
        result.update({"should_reply": False, "reason": "duplicate_group_message", "turn_policy": signals})
        return result
    if is_acknowledgement:
        result.update({"should_reply": False, "reason": "acknowledgement_does_not_need_reply", "turn_policy": signals})
        return result
    if frame.get("active_continuation"):
        try:
            max_auto_continuations = max(
                1,
                min(int(policy.get("max_auto_continuations") or 2), 3),
            )
        except (TypeError, ValueError):
            max_auto_continuations = 2
        if int(frame.get("continuation_assistant_turns") or 0) >= max_auto_continuations:
            result.update({"should_reply": False, "reason": "auto_continuation_limit", "turn_policy": signals})
            return result
    if (
        assistant_turns >= 2
        and float(result.get("confidence") or 0) < 0.9
    ):
        result.update({"should_reply": False, "reason": "assistant_turn_density", "turn_policy": signals})
        return result
    result["turn_policy"] = signals
    return result


def parse_group_decision(raw: object, *, is_mention: bool = False) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    confidence = data.get("confidence", 0.5 if is_mention else 0.0)
    try:
        confidence = max(0.0, min(float(confidence), 1.0))
    except (TypeError, ValueError):
        confidence = 0.5 if is_mention else 0.0
    requested_reply = bool(data.get("should_reply")) or is_mention
    social_action = normalize_group_social_action(
        data.get("social_action"),
        approach=data.get("approach"),
        should_reply=requested_reply,
    )
    should_reply = bool(requested_reply and social_action != "silent")
    cues = normalize_social_cues(data)
    mode = str(data.get("mode") or "daily").strip().lower()
    if mode not in {"daily", "work", "mixed"}:
        mode = "daily"
    intent = str(data.get("intent") or "chat").strip().lower()
    if intent not in {"chat", "analysis", "research", "code", "ops"}:
        intent = "chat"
    # A model's free-form rationale is useful only while evaluating this turn.
    # It must not become ``reason_code``: reason codes are a server-owned
    # taxonomy used by policy, aggregates, and the Owner console.
    model_reason = _clip(data.get("reason"), 500)
    return {
        **data,
        **cues,
        "should_reply": should_reply,
        "social_action": social_action,
        "approach": GROUP_SOCIAL_ACTION_APPROACH.get(social_action, ""),
        "group_action_plan": {
            "schema_version": 1,
            "action": social_action,
        },
        "confidence": confidence,
        "reason": (
            "direct_mention" if is_mention
            else ("model_engagement_approved" if should_reply else "model_engagement_declined")
        ),
        "model_reason": model_reason,
        "mode": mode,
        "intent": intent,
    }


def mark_group_decision(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    group_id: str,
    decision: dict,
    replied: bool,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE group_messages
        SET decision = ?, decision_reason = ?, replied = ? WHERE id = ?
        """,
        (
            "reply" if decision.get("should_reply") else "silent",
            _clip(decision.get("reason"), 500),
            1 if replied else 0,
            int(message_id),
        ),
    )
    if replied:
        conn.execute(
            """
            UPDATE group_policies
            SET last_reply_at = ?, reply_count = reply_count + 1, updated_at = ?
            WHERE group_id = ?
            """,
            (now, now, str(group_id or "").strip()),
        )
