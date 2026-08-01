#!/usr/bin/env python3
"""Agent mode policy for the Codex QQ bridge.

This module keeps the fast-changing assistant behavior rules out of the
HTTP/server orchestration file.  It handles:
- work / daily / mixed mode decisions
- work-session lifecycle and exit policy
- Maibot-inspired social expression settings
- acceptance criteria and lightweight quality checks
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any


AGENT_MODE_DEFAULTS = {
    "agent_mode_autodetect": "1",
    "agent_default_mode": "auto",
    "agent_low_confidence_behavior": "previous",
    "agent_work_exit_policy": "auto",
    "agent_work_ttl_minutes": "30",
    "agent_work_max_turns": "6",
    "agent_daily_expression_level": "light",
    "agent_daily_emoji_mode": "manual",
    "agent_work_emoji_enabled": "0",
    "agent_mixed_mode_enabled": "1",
}

AGENT_MODE_SETTING_KEYS = set(AGENT_MODE_DEFAULTS)
AGENT_MODE_BOOLEAN_KEYS = {
    "agent_mode_autodetect",
    "agent_work_emoji_enabled",
    "agent_mixed_mode_enabled",
}
AGENT_MODE_NUMERIC_KEYS = {
    "agent_work_ttl_minutes": (5, 240, 30),
    "agent_work_max_turns": (1, 30, 6),
}
AGENT_MODE_CHOICES = {
    "agent_default_mode": {"auto", "work", "daily"},
    "agent_low_confidence_behavior": {"previous", "work", "daily", "ask"},
    "agent_work_exit_policy": {"auto", "explicit", "ttl"},
    "agent_daily_expression_level": {"off", "light", "rich"},
    "agent_daily_emoji_mode": {"off", "manual", "auto"},
}

WORK_INTENTS = {"ops", "code", "research", "analysis", "memory", "automation"}
TOOL_DEFAULT_INTENTS = {"ops", "code", "research", "automation"}
MODE_VALUES = {"work", "daily", "mixed"}
LIFECYCLE_VALUES = {"none", "start", "continue", "finish"}
SOCIAL_EMOTIONS = {"neutral", "happy", "sad", "tired", "annoyed", "playful", "curious", "comfort"}
REPLY_LENGTH_VALUES = {"short", "medium", "long"}
MEME_INTENT_VALUES = {"none", "optional", "strong"}

ALWAYS_FRESH_HINTS = (
    "天气",
    "气象",
    "台风",
    "暴雨",
    "降雨",
    "降雪",
    "预报",
    "股价",
    "币价",
    "汇率",
    "油价",
    "航班",
    "比分",
)
FRESH_TOPIC_HINTS = (
    "新闻",
    "政策",
    "法规",
    "价格",
    "版本",
    "发布",
    "上市",
    "比赛",
    "赛程",
    "榜单",
)
FRESH_TIME_HINTS = (
    "最新",
    "最近",
    "当前",
    "现在",
    "今天",
    "今晚",
    "明天",
    "什么时候",
    "何时",
    "到达",
    "靠近",
    "实时",
)
FRESH_FOLLOWUP_HINTS = (
    "到时候",
    "那时候",
    "这时候",
    "它现在",
    "它会",
    "它什么时候",
    "它的影响",
    "这个台风",
    "这场雨",
    "这边",
    "我这里",
    "会有什么影响",
    "影响多大",
    "严重吗",
)
FRESH_CANCEL_HINTS = ("不用查", "别查", "停止查询", "取消查询", "不需要查")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def truthy_setting(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "开启"}


def _normalize_number(value: object, minimum: int, maximum: int, default: int) -> str:
    try:
        number = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        number = default
    return str(max(minimum, min(number, maximum)))


def normalize_agent_policy_setting(key: str, value: object, defaults: dict[str, str]) -> str:
    if key in AGENT_MODE_BOOLEAN_KEYS:
        return "1" if truthy_setting(value) else "0"
    if key in AGENT_MODE_NUMERIC_KEYS:
        return _normalize_number(value, *AGENT_MODE_NUMERIC_KEYS[key])
    raw = str(value or "").strip()
    choices = AGENT_MODE_CHOICES.get(key)
    if choices and raw not in choices:
        return defaults.get(key, AGENT_MODE_DEFAULTS.get(key, ""))
    return raw or defaults.get(key, AGENT_MODE_DEFAULTS.get(key, ""))


def build_agent_policy(settings: dict[str, Any]) -> dict[str, Any]:
    def get(key: str, default: str = "") -> str:
        return str(settings.get(key, default) or default)

    ttl = int(normalize_agent_policy_setting("agent_work_ttl_minutes", get("agent_work_ttl_minutes"), AGENT_MODE_DEFAULTS))
    max_turns = int(normalize_agent_policy_setting("agent_work_max_turns", get("agent_work_max_turns"), AGENT_MODE_DEFAULTS))
    return {
        "language": get("agent_language", "zh-CN"),
        "detail_level": get("agent_detail_level", "standard"),
        "persona_level": get("agent_persona_level", "light"),
        "technical_mode": get("agent_technical_mode", "professional"),
        "summarize_tools": truthy_setting(get("agent_summarize_tools", "1")),
        "disclose_fallback": truthy_setting(get("agent_disclose_fallback", "1")),
        "self_check": truthy_setting(get("agent_self_check", "1")),
        "clarify_when_uncertain": truthy_setting(get("agent_clarify_when_uncertain", "1")),
        "confirm_risky_ops": truthy_setting(get("agent_confirm_risky_ops", "1")),
        "quality_log_enabled": truthy_setting(get("agent_quality_log_enabled", "1")),
        "mode_autodetect": truthy_setting(get("agent_mode_autodetect", "1")),
        "default_mode": normalize_agent_policy_setting("agent_default_mode", get("agent_default_mode"), AGENT_MODE_DEFAULTS),
        "low_confidence_behavior": normalize_agent_policy_setting(
            "agent_low_confidence_behavior",
            get("agent_low_confidence_behavior"),
            AGENT_MODE_DEFAULTS,
        ),
        "work_exit_policy": normalize_agent_policy_setting(
            "agent_work_exit_policy",
            get("agent_work_exit_policy"),
            AGENT_MODE_DEFAULTS,
        ),
        "work_ttl_minutes": ttl,
        "work_max_turns": max_turns,
        "daily_expression_level": normalize_agent_policy_setting(
            "agent_daily_expression_level",
            get("agent_daily_expression_level"),
            AGENT_MODE_DEFAULTS,
        ),
        "daily_emoji_mode": normalize_agent_policy_setting(
            "agent_daily_emoji_mode",
            get("agent_daily_emoji_mode"),
            AGENT_MODE_DEFAULTS,
        ),
        "work_emoji_enabled": truthy_setting(get("agent_work_emoji_enabled", "0")),
        "mixed_mode_enabled": truthy_setting(get("agent_mixed_mode_enabled", "1")),
    }


def requires_fresh_external_data(message: str, history: list[dict[str, Any]] | None = None) -> bool:
    """Return whether a turn must use current external information.

    The history branch is intentionally limited to explicit follow-up language so
    an old weather/news discussion does not force unrelated later chat into a
    research task.
    """

    text = (message or "").strip().lower()
    if not text or any(hint in text for hint in FRESH_CANCEL_HINTS):
        return False
    if any(hint in text for hint in ALWAYS_FRESH_HINTS):
        return True
    if any(hint in text for hint in ("\u8054\u7f51", "\u5b9e\u65f6\u6570\u636e", "\u6700\u65b0\u6570\u636e", "\u67e5\u6700\u65b0", "\u5f53\u524d\u6570\u636e")):
        return True
    if any(hint in text for hint in FRESH_TOPIC_HINTS) and any(hint in text for hint in FRESH_TIME_HINTS):
        return True
    if history and any(hint in text for hint in FRESH_FOLLOWUP_HINTS):
        recent = "\n".join(str(item.get("content") or "") for item in history[-8:]).lower()
        if any(hint in recent for hint in (*ALWAYS_FRESH_HINTS, *FRESH_TOPIC_HINTS)):
            return True
    return False


def enforce_fresh_data_route(
    decision: dict[str, Any],
    message: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = dict(decision)
    if not requires_fresh_external_data(message, history):
        return result
    result.update(
        {
            "mode": "work",
            "intent": "research",
            "confidence": max(_safe_float(result.get("confidence"), 0.5), 0.95),
            "need_tools": True,
            "fresh_data_required": True,
            "execution_lane": "invoke_capability",
            "required_capability": "external.current_data",
            "reason": "本轮涉及天气、价格、新闻或其他时效信息，必须使用当前外部数据核验。",
            "source": "freshness_rule",
        },
    )
    return result


def detect_agent_intent(message: str) -> str:
    text = (message or "").lower()
    if any(word in text for word in ("定时任务", "定时计划", "定时提醒", "每天", "每日", "每周", "每隔")):
        return "automation"
    if any(word in text for word in ("记住", "以后记得", "偏好", "习惯")):
        return "memory"
    if any(word in text for word in ("服务器", "docker", "日志", "端口", "nginx", "部署", "重启", "安装", "排查", "报错", "systemctl")):
        return "ops"
    if any(word in text for word in ("代码", "开发", "修复", "bug", "重构", "测试", "git", "仓库", "项目", "上线")):
        return "code"
    if requires_fresh_external_data(message) or any(
        word in text for word in ("github", "热门", "搜索", "查询", "资料", "新闻", "最新", "榜", "文档", "说明")
    ):
        return "research"
    if any(word in text for word in ("为什么", "怎么", "如何", "帮我分析", "方案", "建议", "评估")):
        return "analysis"
    return "chat"


def is_meta_conversation(message: str) -> bool:
    text = (message or "").strip().lower()
    language_meta = any(word in text for word in ("中文回复", "英文回复", "用中文", "用英文", "回复我"))
    model_meta = any(word in text for word in ("模型", "provider", "deepseek", "codex", "gpt")) and any(
        word in text for word in ("切换", "现在用", "正在用", "不是已经", "为什么", "改成", "换成")
    )
    behavior_meta = any(word in text for word in ("你刚才", "你的回复", "回复太", "别这样回复", "不要这样回复"))
    return language_meta or model_meta or behavior_meta


def intent_label(intent: str) -> str:
    return {
        "memory": "记忆与偏好",
        "automation": "定时计划",
        "ops": "服务器运维",
        "code": "代码/项目任务",
        "research": "资料查询",
        "analysis": "分析建议",
        "chat": "日常聊天",
    }.get(intent, intent or "未知")


def mode_label(mode: str) -> str:
    return {
        "work": "工作模式",
        "daily": "日常聊天",
        "mixed": "混合模式",
    }.get(mode, mode or "未知")


def _safe_float(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(number, 1.0))


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _session_active(session: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not session:
        return False
    expires_at = str(session.get("expires_at") or "").strip()
    if not expires_at:
        return True
    try:
        expires = datetime.fromisoformat(expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return expires > (now or utc_now())


def fallback_mode_decision(
    message: str,
    previous_session: dict[str, Any] | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    intent = detect_agent_intent(message)
    text = (message or "").strip()
    previous_mode = str(previous_session.get("mode") or "") if previous_session else ""
    explicit_finish = any(word in text for word in ("先这样", "结束工作", "不用继续", "回到日常", "随便聊聊", "不工作了"))
    explicit_work = any(word in text for word in ("工作模式", "认真点", "开发", "排查", "上线", "部署"))
    explicit_daily = any(word in text for word in ("日常模式", "轻松点", "陪我聊", "随便聊"))
    mode = "daily"
    lifecycle = "none"
    confidence = 0.68
    reason = "fallback 规则判断。"

    if explicit_finish or explicit_daily:
        mode = "daily"
        lifecycle = "finish" if previous_mode == "work" else "none"
        confidence = 0.86
        reason = "用户表达了结束工作或轻松聊天的倾向。"
    elif explicit_work or intent in WORK_INTENTS:
        mode = "work"
        lifecycle = "continue" if previous_mode == "work" and _session_active(previous_session) else "start"
        confidence = 0.82
        reason = f"消息包含明确任务目标，识别为{intent_label(intent)}。"
    elif previous_mode == "work" and _session_active(previous_session) and policy.get("low_confidence_behavior") == "previous":
        mode = "work"
        lifecycle = "continue"
        confidence = 0.55
        reason = "上一轮仍处于有效工作会话，低置信度时沿用工作模式。"

    emotion = "neutral"
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
    explicit_meme = any(word in text for word in ("表情包", "发图", "来张图", "图片回复"))
    return {
        "mode": mode,
        "intent": intent,
        "confidence": confidence,
        "reason": reason,
        "work_lifecycle": lifecycle,
        "end_work": lifecycle == "finish",
        "allow_emoji": mode == "daily" and policy.get("daily_emoji_mode") != "off",
        "need_tools": intent in TOOL_DEFAULT_INTENTS,
        "response_style": "structured" if mode == "work" else "casual",
        "emotion": emotion,
        "emotion_confidence": 0.86 if emotion != "neutral" else 0.0,
        "reply_length": "medium" if mode == "work" else "short",
        "meme_intent": "strong" if explicit_meme else "none",
        "engagement": "respond",
        "source": "fallback",
    }


def _history_lines(history: list[dict[str, Any]], limit: int = 8) -> list[str]:
    selected = history[-limit:] if history else []
    lines = []
    for item in selected:
        role = "用户" if item.get("role") == "user" else "助手"
        content = _clip(item.get("content"), 300)
        if content:
            lines.append(f"{role}: {content}")
    return lines or ["(无近期对话)"]


def build_mode_decision_messages(
    settings: dict[str, Any],
    user_id: str,
    message: str,
    history: list[dict[str, Any]],
    previous_session: dict[str, Any] | None,
    policy: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 QQ AI 助手的模式判断器。你只输出 JSON，不输出解释文字。\n"
                "目标：判断本轮应使用 work（日常任务/开发/运维/资料/文档/分析）、daily（闲聊/情绪/陪伴）、"
                "mixed（先接住情绪再处理任务）哪一种模式。\n"
                "必须让模型自己判断，不要只按关键词机械分类。\n"
                "work 模式要专业、结构化、禁用无关表情；daily 模式可以自然、有轻度人格和表情包；mixed 模式先短暂回应情绪，再进入工作。\n"
                "还要判断工作生命周期：none/start/continue/finish。finish 表示用户在结束工作、验收完成、转回闲聊或不想继续。\n"
                "need_tools 只在必须联网、读取文件、调用终端或执行其他外部工具时为 true；工作模式本身不等于必须使用工具。\n"
                "同时判断用户当前情绪和适合的回复长度；表情包不是每次都发，只有能替代或强化情绪表达时才设为 optional/strong。\n"
                "只返回 JSON，字段：mode, confidence, intent, reason, work_lifecycle, end_work, allow_emoji, need_tools, "
                "response_style, emotion(neutral/happy/sad/tired/annoyed/playful/curious/comfort), "
                "reply_length(short/medium/long), meme_intent(none/optional/strong), engagement(respond/quiet)。"
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"QQ 用户: {user_id}",
                    f"助手名字: {settings.get('display_name')}",
                    f"日常表达强度: {policy.get('daily_expression_level')}",
                    f"日常表情策略: {policy.get('daily_emoji_mode')}",
                    f"工作模式允许表情: {bool(policy.get('work_emoji_enabled'))}",
                    f"上一会话: {json.dumps(previous_session or {}, ensure_ascii=False)}",
                    "近期对话:",
                    *_history_lines(history),
                    f"本轮消息: {message}",
                ],
            ),
        },
    ]


def build_mode_decision_prompt(
    settings: dict[str, Any],
    user_id: str,
    message: str,
    history: list[dict[str, Any]],
    previous_session: dict[str, Any] | None,
    policy: dict[str, Any],
) -> str:
    messages = build_mode_decision_messages(settings, user_id, message, history, previous_session, policy)
    return "\n\n".join([messages[0]["content"], messages[1]["content"], "请只输出 JSON。"])


def parse_mode_decision(raw_text: str, fallback: dict[str, Any]) -> dict[str, Any]:
    text = (raw_text or "").strip()
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
    result = dict(fallback)
    if isinstance(data, dict):
        result.update(data)
        result["source"] = "model"
    return result


def finalize_mode_decision(
    decision: dict[str, Any],
    previous_session: dict[str, Any] | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    result = dict(decision)
    mode = str(result.get("mode") or "daily").strip().lower()
    if mode not in MODE_VALUES:
        mode = "daily"
    if mode == "mixed" and not policy.get("mixed_mode_enabled"):
        mode = "work"
    lifecycle = str(result.get("work_lifecycle") or "none").strip().lower()
    if lifecycle not in LIFECYCLE_VALUES:
        lifecycle = "none"
    confidence = _safe_float(result.get("confidence"), 0.5)
    previous_mode = str(previous_session.get("mode") or "") if previous_session else ""
    previous_active = _session_active(previous_session)
    low_behavior = str(policy.get("low_confidence_behavior") or "previous")
    message = str(result.get("message") or "")

    if is_meta_conversation(message):
        mode = "daily"
        lifecycle = "finish" if previous_mode == "work" else "none"
        confidence = max(confidence, 0.92)
        result.update(
            {
                "intent": "chat",
                "need_tools": False,
                "end_work": previous_mode == "work",
                "reason": "本轮是在调整助手回复、语言或模型认知，不创建后台工作任务。",
                "source": "meta_rule",
            },
        )

    if not policy.get("mode_autodetect"):
        default_mode = str(policy.get("default_mode") or "daily")
        if default_mode != "auto":
            mode = default_mode
            confidence = max(confidence, 0.99)
            result["reason"] = f"自动判断关闭，使用默认{mode_label(mode)}。"

    if confidence < 0.55:
        if low_behavior == "previous" and previous_mode in MODE_VALUES and previous_active:
            mode = previous_mode
            result["reason"] = f"{result.get('reason') or ''} 置信度偏低，沿用上一会话模式。".strip()
        elif low_behavior in {"work", "daily"}:
            mode = low_behavior
            result["reason"] = f"{result.get('reason') or ''} 置信度偏低，按后台策略选择{mode_label(mode)}。".strip()
        elif low_behavior == "ask":
            mode = "mixed" if policy.get("mixed_mode_enabled") else "work"
            result["needs_mode_clarification"] = True

    end_work = bool(result.get("end_work")) or lifecycle == "finish"
    if previous_mode == "work" and previous_active and mode == "daily" and confidence < 0.62 and not end_work:
        mode = "work"
        lifecycle = "continue"
        result["reason"] = f"{result.get('reason') or ''} 上一工作会话仍有效，日常判断置信度不足，继续工作模式。".strip()
    if previous_mode == "work" and end_work:
        mode = "daily"
        lifecycle = "finish"
    if mode == "work" and lifecycle == "none":
        lifecycle = "continue" if previous_mode == "work" and previous_active else "start"

    intent = str(result.get("intent") or detect_agent_intent(str(result.get("message") or ""))).strip()
    if intent not in {"memory", "ops", "code", "research", "analysis", "automation", "chat"}:
        intent = detect_agent_intent(str(result.get("reason") or ""))
    if mode == "daily" and intent in WORK_INTENTS:
        intent = "chat"

    allow_emoji = bool(result.get("allow_emoji"))
    if mode == "work":
        allow_emoji = bool(policy.get("work_emoji_enabled"))
    elif mode == "mixed":
        allow_emoji = False
    else:
        allow_emoji = policy.get("daily_emoji_mode") != "off"

    emotion = str(result.get("emotion") or "neutral").strip().lower()
    if emotion not in SOCIAL_EMOTIONS:
        emotion = "neutral"
    reply_length = str(result.get("reply_length") or ("medium" if mode == "work" else "short")).strip().lower()
    if reply_length not in REPLY_LENGTH_VALUES:
        reply_length = "medium" if mode == "work" else "short"
    meme_intent = str(result.get("meme_intent") or "none").strip().lower()
    if meme_intent not in MEME_INTENT_VALUES or not allow_emoji:
        meme_intent = "none"

    result.update(
        {
            "mode": mode,
            "mode_label": mode_label(mode),
            "intent": intent,
            "intent_label": intent_label(intent),
            "confidence": confidence,
            "work_lifecycle": lifecycle,
            "end_work": end_work,
            "allow_emoji": allow_emoji,
            "need_tools": bool(result.get("need_tools")),
            "response_style": result.get("response_style") or ("structured" if mode == "work" else "casual"),
            "emotion": emotion,
            "reply_length": reply_length,
            "meme_intent": meme_intent,
            "engagement": str(result.get("engagement") or "respond").strip().lower(),
            "source": result.get("source") or "model",
        },
    )
    return result


def next_session_state(
    user_id: str,
    decision: dict[str, Any],
    previous_session: dict[str, Any] | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    now = utc_now()
    previous_turns = int(previous_session.get("turn_count") or 0) if previous_session else 0
    previous_work_turns = int(previous_session.get("work_turns") or 0) if previous_session else 0
    mode = str(decision.get("mode") or "daily")
    lifecycle = str(decision.get("work_lifecycle") or "none")
    ended_reason = ""
    if lifecycle == "finish" or decision.get("end_work"):
        ended_reason = "model_finish"
    if mode == "work":
        work_turns = previous_work_turns + 1
        if policy.get("work_exit_policy") == "auto" and work_turns >= int(policy.get("work_max_turns") or 6):
            ended_reason = "max_work_turns"
        expires_at = now + timedelta(minutes=int(policy.get("work_ttl_minutes") or 30))
    else:
        work_turns = 0
        expires_at = now + timedelta(minutes=15)
    return {
        "user_id": user_id or "default",
        "mode": "daily" if ended_reason == "max_work_turns" else mode,
        "intent": decision.get("intent") or "chat",
        "confidence": float(decision.get("confidence") or 0),
        "reason": _clip(decision.get("reason"), 800),
        "source": decision.get("source") or "model",
        "work_lifecycle": lifecycle,
        "turn_count": previous_turns + 1,
        "work_turns": 0 if ended_reason == "max_work_turns" else work_turns,
        "expires_at": expires_at.isoformat(),
        "ended_reason": ended_reason,
        "updated_at": now.isoformat(),
    }


def agent_policy_lines(policy: dict[str, Any]) -> list[str]:
    detail_label = {
        "brief": "简洁",
        "standard": "标准",
        "detailed": "详细",
    }.get(policy.get("detail_level"), "标准")
    persona_label = {
        "off": "关闭角色化",
        "light": "轻度角色化",
        "full": "明显角色化",
    }.get(policy.get("persona_level"), "轻度角色化")
    technical_label = {
        "professional": "专业准确优先",
        "balanced": "专业与亲切平衡",
        "friendly": "友好解释优先",
    }.get(policy.get("technical_mode"), "专业准确优先")
    return [
        f"- 默认语言: {'简体中文' if policy.get('language') == 'zh-CN' else '跟随用户语言'}。",
        f"- 回复详细度: {detail_label}；角色化强度: {persona_label}；技术场景: {technical_label}。",
        "- 工具、接口、命令行结果必须先理解和整理，再用适合 QQ 私聊的方式回复。",
        "- 如果数据来自缓存、fallback、近似搜索或失败降级，必须主动说明可信度和限制。",
        "- 回答前按本轮验收标准自检；如果达不到，说明缺口并给出下一步。",
        "- 涉及删除、重启、改权限、开放端口、密钥、成本或生产风险时，先请求确认。",
    ]


def mode_policy_lines(decision: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    mode = decision.get("mode")
    lines = [
        f"- 当前模式: {mode_label(str(mode))}；判断置信度: {float(decision.get('confidence') or 0):.2f}。",
        f"- 判断理由: {decision.get('reason') or '无'}",
        f"- 工作生命周期: {decision.get('work_lifecycle') or 'none'}；允许表情: {'是' if decision.get('allow_emoji') else '否'}。",
    ]
    if mode == "work":
        lines.extend(
            [
                "- 工作模式下优先解决问题，结构化输出结论、步骤、风险和验证。",
                "- 工作模式下禁止无关撒娇、口癖堆叠和主动表情包；只在确有帮助时保持一点自然语气。",
                "- 当用户表达“先这样/不用继续/回到日常/谢谢已经解决”时，结束工作模式并自然降温。",
            ],
        )
    elif mode == "mixed":
        lines.extend(
            [
                "- 混合模式下先用一句话接住情绪，再切入任务处理。",
                "- 不要把陪聊语气扩散到执行步骤里，任务部分仍按工作标准完成。",
            ],
        )
    else:
        expression = policy.get("daily_expression_level")
        lines.extend(
            [
                f"- 日常模式下可以自然陪聊，表达强度: {expression}。",
                "- 不要强行把闲聊变成任务清单；如果用户提出明确任务，再切回工作或混合模式。",
            ],
        )
        if policy.get("daily_emoji_mode") != "off":
            lines.append("- 日常模式允许表情包表达，但要遵守模型意图、资产审核、冷却、去重和每日上限。")
    return lines


def acceptance_criteria(
    intent: str,
    message: str,
    policy: dict[str, Any],
    mode_decision: dict[str, Any] | None = None,
) -> list[str]:
    mode = (mode_decision or {}).get("mode", "work" if intent in WORK_INTENTS else "daily")
    text = message or ""
    criteria = [
        "直接回应用户本轮目标，不只复述工具结果。",
        "默认使用简体中文，英文技术名词可保留但要解释清楚。",
    ]
    if policy.get("self_check"):
        criteria.append("回复前自检是否满足用户原始要求；不满足时说明限制或给下一步。")
    if mode == "daily":
        criteria.extend(
            [
                "按日常聊天回应，不强行输出任务清单。",
                "可以有自然人格感，但不要装真人或越界暧昧。",
            ],
        )
    if mode == "mixed":
        criteria.append("先简短接住情绪，再进入工作处理。")
    if any(word in text for word in ("详细", "说明", "分析", "解释", "为什么")) or policy.get("detail_level") == "detailed":
        criteria.append("需要给出足够细节、判断依据和可执行结论。")
    if intent == "research":
        criteria.extend(
            [
                "说明信息来源、时效性和不确定性。",
                "如果使用降级数据源或结果质量差，要主动提醒用户。",
            ],
        )
    if (mode_decision or {}).get("fresh_data_required"):
        criteria.extend(
            [
                "使用当前可获得的权威来源，并明确数据或公告时间。",
                "不得用历史同名事件代替当前事件；权威实时来源不可用时要明确说明，禁止猜测确定结论。",
            ],
        )
    if intent == "ops":
        criteria.extend(
            [
                "区分现象、判断、风险和下一步操作。",
                "涉及重启、删除、暴露端口、密钥或成本时先提醒风险。",
            ],
        )
    if intent == "code":
        criteria.extend(
            [
                "说明需求理解、改动范围、验证方式和剩余风险。",
                "不要假装已经修改或测试未实际执行的内容。",
            ],
        )
    if intent == "memory":
        criteria.append("明确说明已记住什么，避免保存含糊或敏感信息。")
    if policy.get("clarify_when_uncertain"):
        criteria.append("需求关键条件缺失且无法安全假设时，只问一个最关键问题。")
    return criteria


def quality_check_response(
    *,
    request: str,
    response: str,
    result: dict[str, Any],
    intent: str,
    criteria: list[str],
    policy: dict[str, Any],
    mode_decision: dict[str, Any] | None = None,
    now_text: str = "",
) -> dict[str, Any]:
    issues: list[str] = []
    response = (response or "").strip()
    request = request or ""
    mode_decision = mode_decision or {}
    mode = mode_decision.get("mode", "work" if intent in WORK_INTENTS else "daily")
    if not response:
        issues.append("empty_response")
    if result.get("ok") is False:
        issues.append(f"provider_{result.get('error_kind') or 'error'}")
    if policy.get("language") == "zh-CN" and response and not re.search(r"[\u4e00-\u9fff]", response):
        issues.append("language_mismatch")
    wants_detail = any(word in request for word in ("详细", "说明", "分析", "解释", "为什么"))
    if (wants_detail or policy.get("detail_level") == "detailed") and len(response) < 180 and mode != "daily":
        issues.append("too_brief_for_requested_detail")
    fallback = bool(result.get("fallback") or result.get("source_quality") in {"fallback", "cache"})
    if fallback and policy.get("disclose_fallback") and not any(
        word in response.lower() for word in ("fallback", "降级", "缓存", "近似", "不确定")
    ):
        issues.append("fallback_not_disclosed")
    if intent in WORK_INTENTS and len(response) >= 80:
        if not any(word in response for word in ("建议", "下一步", "风险", "验证", "来源", "原因", "可以", "已")):
            issues.append("weak_actionability")
    if mode == "work" and not policy.get("work_emoji_enabled") and re.search(r"\[emoji|表情包|😊|😂|🥲|喵", response):
        issues.append("work_mode_social_leak")
    if "tool_raw_output" in result:
        issues.append("raw_tool_output_leaked")
    if result.get("action_truth_guarded"):
        issues.append("ungrounded_action_claim")
    status = "passed"
    if (
        "empty_response" in issues
        or "ungrounded_action_claim" in issues
        or any(item.startswith("provider_") for item in issues)
    ):
        status = "failed"
    elif issues:
        status = "warn"
    return {
        "status": status,
        "issues": issues,
        "intent": intent,
        "intent_label": intent_label(intent),
        "mode": mode,
        "mode_label": mode_label(str(mode)),
        "mode_decision": mode_decision,
        "criteria": criteria,
        "fallback": fallback,
        "checked_at": now_text or iso_now(),
    }
