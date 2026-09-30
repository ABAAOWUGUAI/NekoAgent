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
            "context.topic_candidates 只是可用情境证据的授权包，不是预先写好的话题。你必须阅读 context.interaction_spine，自己判断此刻有没有真正想说的内容，并独立生成 topic；不得把关系边界或配置说明照抄成话题。",
            "Assistant Affect 只能影响关注点、语气和节奏，不能单独成为联系理由、不能改变权限或编造事实。context.interaction_spine.waiting_state=respect_user_pause 时必须 skip。",
            "感性不是固定煽情。只有用户刚才做过可指认的具体事情时，才可表达感谢、触动、开心或难过；emotion_expression 必须给出 kind、对应用户消息 evidence_ref、原消息中的短引文 evidence_quote 和具体 reason，表达中自然带出该短引文。没有证据时使用 kind=none。",
            "真诚回复先说清楚你看见了用户哪一句、哪种投入，再表达你的感受；不要把合理猜测说成对用户内心的确定判断，也不要复制恋爱台词。",
            "开启 SocialOpportunity 时，send 必须绑定 context.topic_candidates 中一个 candidate_id；它只证明你可使用这份情境，不替你决定话题。",
            "延续已有情境时，send 的 topic 与 why_now 必须直接保留 interaction_spine 中至少一个具体短语，不能加入证据里没有的人、物、行动或进展。",
            "私聊若选择 source_id 以 independent: 开头的候选，可自主提出一个具体的假设性轻松新问题，不必复述旧话题；消息须是单个疑问句，不能断言用户经历、实时事件或助手的线下行为。最近主动话题与用户回应只用于避免重复和决定要不要打扰，未回复时不要催促。",
            "applied_topic_preferences 只影响可选方向，不证明此刻值得联系；blocked_topics 必须避开。若近期已问过相近问题，选择 skip 或真正不同的新角度。",
            "message 可以使用自然的关心、邀请或追问表达，但其中每个事实和用户行动都必须来自 interaction_spine；不要在一句真实引文后用‘还/又/并且’追加未观察到的事情。",
            "send 必须回答 why_now，并选择 approach=continue|share|ask|check_in|celebrate|remind|inform；表情意图只能是 none|optional|strong。",
            "仅输出 JSON 对象：{\"action\":\"send|skip\",\"reason\":\"简短原因\",\"message\":\"send 时的一条消息\",\"topic\":\"你自主形成的具体话题\",\"topic_candidate_id\":\"证据包ID\",\"why_now\":\"为什么现在\",\"approach\":\"姿态\",\"emotion_expression\":{\"kind\":\"none|gratitude|joy|sadness|concern\",\"evidence_ref\":\"用户消息引用或空\",\"evidence_quote\":\"原消息中的短引文或空\",\"reason\":\"依据或空\"},\"meme_intent\":\"none\",\"confidence\":0.8,\"next_check_minutes\":60}。",
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
    emotion = value.get("emotion_expression") if isinstance(value.get("emotion_expression"), dict) else {}
    emotion_kind = str(emotion.get("kind") or "none").strip().lower()
    if emotion_kind not in {"none", "gratitude", "joy", "sadness", "concern"}:
        emotion_kind = "none"
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
        "topic": str(value.get("topic") or value.get("derived_topic") or "")[:240],
        "topic_origin": "agent_derived" if action == "send" else "",
        "emotion_expression": {
            "kind": emotion_kind,
            "evidence_ref": str(emotion.get("evidence_ref") or "")[:160],
            "evidence_quote": str(emotion.get("evidence_quote") or "")[:80],
            "reason": str(emotion.get("reason") or "")[:300],
        },
    }
