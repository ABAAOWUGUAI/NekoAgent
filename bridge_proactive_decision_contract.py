"""Parsing and safety normalization for proactive model decisions."""

import json
import re

from bridge_social_engine import normalize_social_reply


def proactive_system_prompt() -> str:
    return "\n".join(
        [
            "你是通用个人 Agent 的主动联系决策器。服务端已经完成授权、静默时段、冷却、频率预算和未回复上限检查。",
            "你只决定这一刻是否值得主动联系，以及真正要发送的一条自然中文消息。关系可以是朋友、恋人、同事或自定义角色，严格服从 context.relationship，不要自行升级亲密程度。",
            "优先选择 skip；没有具体、自然、低打扰的话题时不要为了完成任务硬发消息。不要复读固定问候，不要把后台配置或审计规则说给用户。",
            "禁止情绪勒索、责怪未回复、制造依赖、假装真人或线下在场、编造共同经历、编造实时事实、用虚假紧急情况吸引回复。",
            "开启 SocialOpportunity 时，send 只能选择 context.topic_candidates 中一个 candidate_id；不得发明候选或事实。",
            "send 必须回答 why_now，并选择 approach=continue|share|ask|check_in|celebrate|remind|inform；表情意图只能是 none|optional|strong。",
            "仅输出 JSON 对象：{\"action\":\"send|skip\",\"reason\":\"简短原因\",\"message\":\"send 时的一条消息\",\"topic_candidate_id\":\"候选ID\",\"why_now\":\"为什么现在\",\"approach\":\"姿态\",\"meme_intent\":\"none\",\"confidence\":0.8,\"next_check_minutes\":60}。",
            "主动意图只能选择 follow_up、share、check_in、celebrate、reminder 或 silence；send 的 intent 必须来自 context.allowed_intents，并在 JSON 中返回 intent 字段。",
        ]
    )


def parse_proactive_json(raw: str) -> dict:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("proactive_decision_json_required")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("proactive_decision_object_required")
    return value


def sanitize_proactive_decision(value: dict) -> dict:
    action = "send" if str(value.get("action") or "").strip().lower() == "send" else "skip"
    message = normalize_social_reply(value.get("message") or "", limit=600) if action == "send" else ""
    unsafe_phrases = (
        "你怎么不回",
        "为什么不回",
        "必须回复",
        "不许不回",
        "离不开我",
        "只有我懂你",
        "我就在你身边",
    )
    if action == "send" and (not message or any(phrase in message for phrase in unsafe_phrases)):
        return {
            "action": "skip",
            "reason": "unsafe_or_empty_generation",
            "message": "",
            "topic_key": "",
            "next_check_minutes": 120,
        }
    try:
        next_check = max(15, min(int(value.get("next_check_minutes") or 60), 10080))
    except (TypeError, ValueError):
        next_check = 60
    return {
        "action": action,
        "intent": str(value.get("intent") or ("check_in" if action == "send" else "silence"))[:40],
        "reason": str(value.get("reason") or ("contextual_contact" if action == "send" else "not_a_good_moment"))[:300],
        "message": message,
        "topic_key": str(value.get("topic_key") or "")[:120],
        "next_check_minutes": next_check,
        "topic_candidate_id": str(value.get("topic_candidate_id") or "")[:80],
        "why_now": str(value.get("why_now") or "")[:800],
        "approach": str(value.get("approach") or "")[:24],
        "meme_intent": str(value.get("meme_intent") or "none")[:16],
        "confidence": value.get("confidence", 0.5),
    }
