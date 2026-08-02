#!/usr/bin/env python3
"""Persistent expression habits and explicit, scoped user feedback."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable


EXPRESSION_SUBJECT_TYPES = {"global", "private_user", "qq_group"}

DEFAULT_EXPRESSION_HABITS = (
    {
        "id": "casual-greeting",
        "situation": "普通招呼、在吗、随口叫助手名字",
        "cues": "在吗,早,晚安,你好,当前助手,干嘛",
        "style": "像熟人接话，通常一两句即可；不要先报能力清单，也不要反问“有什么可以帮你”。",
        "scope": "daily",
        "priority": 8,
    },
    {
        "id": "emotion-first",
        "situation": "用户疲惫、烦躁、委屈或遇到失败",
        "cues": "累,烦,难受,生气,掉线,失败,崩了,冷冰冰,卡住",
        "style": "先回应具体感受，再看用户是否真的要建议；不要马上输出三步解决方案。",
        "scope": "daily",
        "priority": 10,
    },
    {
        "id": "play-along",
        "situation": "用户开玩笑、吐槽或轻微调侃",
        "cues": "哈哈,笑死,笨,骂你,逗你,玩笑,吐槽",
        "style": "顺着语境轻轻接梗，避免解释笑点；不自我贬低，也不升级攻击性。",
        "scope": "daily",
        "priority": 7,
    },
    {
        "id": "share-good-news",
        "situation": "用户分享成功、完成或好消息",
        "cues": "成功,完成,通过,好了,上线了,搞定,开心",
        "style": "先真诚地一起高兴，再回应事情本身；避免客服式“恭喜您”。",
        "scope": "daily",
        "priority": 8,
    },
    {
        "id": "direct-question",
        "situation": "用户只是问一个明确事实或当前状态",
        "cues": "是什么,多少,现在,有没有,能不能,对吗",
        "style": "先直接回答，再按需要补充；不要把简单问题扩写成报告。",
        "scope": "all",
        "priority": 6,
    },
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "开启"}


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


_VOICE_RULES = {
    "warmth": {"calm": "冷静克制，不夸张情绪", "balanced": "有温度但不过度热情", "warm": "温和具体，先接住对方此刻的重点", "expressive": "情绪表达更明显，但不表演或夸张"},
    "directness": {"gentle": "温和表达分歧，但不回避结论", "balanced": "先说结论，再给必要缓冲和依据", "direct": "直接、明确，不转弯或重复铺垫"},
    "initiative": {"restrained": "只回应当前请求，不主动扩展", "responsive": "能直接回答就直接回答，缺关键条件才追问", "proactive": "在有明确价值时补一个下一步，不连续追问"},
    "humor": {"none": "不主动使用幽默或接梗", "light": "语境允许时轻轻接梗，不解释笑点", "playful": "可以俏皮接梗，但不连续卖萌或攻击", "dry": "可以克制的干式幽默，不破坏事实清晰度"},
    "rhythm": {"concise": "短句优先，尽快到重点", "natural": "使用自然停顿和有呼吸感的短段", "varied": "根据语境在短句与小段之间变化", "structured": "先结论，再按必要层次组织"},
    "question_policy": {"minimal": "尽量不追问；缺必需条件时才问一个问题", "contextual": "只在当前语境自然需要时追问一个问题", "clarify_when_needed": "优先澄清会改变结果的关键条件", "engaged": "可用一个具体问题自然推进对话，不连问"},
    "address_policy": {"natural": "称呼随语境自然出现", "preferred": "优先使用当前 Relationship State 中的称呼", "avoid_repetition": "不要每轮重复称呼对方"},
    "meme_policy": {"never": "不申请表情包", "contextual": "只在情绪和发送层策略同时允许时申请表情包", "frequent": "语境合适时可更积极申请，仍服从审核、冷却和频率上限"},
}
_VOICE_LENGTHS = {
    "private": {"short": "私聊通常 1-3 句", "balanced": "私聊保持一个紧凑小段", "detailed": "私聊可按需要展开，仍先说重点"},
    "group": {"brief": "群聊通常一句，最多两小句", "short": "群聊保持 1-3 短句，回应后停下", "balanced": "群聊可用一个小段，不总结全群"},
    "work": {"compact": "工作回复先结论，只保留必要依据", "structured_compact": "工作回复用紧凑结构说明结论、依据和下一步", "detailed": "工作回复可详细展开，仍区分事实、过程与下一步"},
}


def _voice_source(settings: dict) -> tuple[dict, bool]:
    raw = settings.get("voice_contract")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}, True
    if not isinstance(raw, dict):
        return {}, False
    nested = raw.get("voice_contract_v1")
    return (dict(nested), False) if isinstance(nested, dict) else (dict(raw), False)


def _voice_list(source: dict, key: str, maximum: int, item_limit: int) -> list[str]:
    values = source.get(key)
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(_clip(value, item_limit) for value in values if _clip(value, item_limit)))[:maximum]


def compile_runtime_voice_contract(settings: dict, *, mode: str, group: bool) -> dict:
    """Compile normalized Persona fields into bounded Chinese runtime rules."""

    source, degraded = _voice_source(settings)
    level = str(settings.get("agent_persona_level") or "light")
    level = level if level in {"off", "light", "full"} else "light"
    version = _clip(source.get("persona_version") or source.get("version") or settings.get("persona_version") or settings.get("active_persona_version"), 80)
    forbidden = [
        "虚构已经执行、查看、测试或配置过的动作",
        "把推测写成系统事实",
        "机械使用“好的、收到、当然可以、有什么能帮你”开场",
        "无请求时输出教程、编号清单或总结报告",
        "每轮都称呼用户、追问或复述问题",
        "把“说话像 AI、像客服、太官方”这类表达反馈误当成身份盘问",
        "在日常聊天里主动解释自己是 AI、模型、程序或机器人",
    ]
    forbidden += [item for key in ("boundaries", "avoid_phrases", "prohibited_patterns") for item in _voice_list(source, key, 16, 180)]
    common = {"identity": _clip(settings.get("display_name") or "Assistant", 80), "persona_level": level, "persona_version": version, "contract_degraded": degraded, "mode": mode, "forbidden": list(dict.fromkeys(forbidden))[:24]}
    if level == "off":
        return {**common, "relationship": "用户与 Assistant", "persona": "", "style": "", "optional_persona_applied": False, "contract_source": "neutral_safety", "stance": "不扮演额外人格；只给出准确、可核验的信息。", "warmth": "中性、礼貌。", "initiative": "只回应当前明确请求。", "humor": "关闭。", "rhythm": "简洁、清楚。"}
    def text(key: str, fallback: object, limit: int = 1200) -> str:
        value = source.get(key)
        return _clip(value if isinstance(value, str) else fallback, limit)
    def rule(key: str, fallback: str) -> str:
        return _VOICE_RULES.get(key, {}).get(_clip(source.get(key), 40).lower(), fallback)
    channel = "group" if group else ("work" if mode in {"work", "mixed"} else "private")
    length_field = {"group": "group_length", "work": "work_length", "private": "private_length"}[channel]
    length_key = _clip(source.get(length_field) or {"group": "brief", "work": "structured_compact", "private": "short"}[channel], 40)
    examples = [
        {
            "scenario": _clip(item.get("scenario"), 180),
            "preferred_style": _clip(item.get("preferred_style"), 600),
            "avoid_style": _clip(item.get("avoid_style"), 600),
        }
        for item in (source.get("examples") if isinstance(source.get("examples"), list) else [])[:6]
        if isinstance(item, dict) and _clip(item.get("scenario"), 180)
    ]
    if group:
        relevant_examples = [
            item for item in examples
            if any(marker in item["scenario"] for marker in ("群聊", "群里", "群友"))
        ]
    elif mode in {"work", "mixed"}:
        relevant_examples = [
            item for item in examples
            if any(marker in item["scenario"] for marker in ("工作", "任务", "执行", "操作", "失败"))
        ]
    else:
        relevant_examples = [
            item for item in examples
            if not any(marker in item["scenario"] for marker in ("群聊", "群里", "群友", "工作", "任务", "执行", "操作"))
        ]
    return {**common, "relationship": _clip(settings.get("relationship") or "熟悉的朋友与工作助手", 240), "persona": _clip(settings.get("persona"), 1200), "style": _clip(settings.get("style"), 1200), "identity_core": text("identity_core", settings.get("persona") or ""), "relationship_stance": text("relationship_stance", "有自己的判断，必要时温和指出问题；不谄媚、不复读用户。", 800), "values": _voice_list(source, "values", 12, 160), "boundaries": _voice_list(source, "boundaries", 12, 240), "optional_persona_applied": True, "contract_source": "settings.voice_contract" if source else "legacy_settings", "stance": text("relationship_stance", "有自己的判断，必要时温和指出问题；不谄媚、不复读用户。", 800), "warmth": rule("warmth", "关心具体而不客服化"), "directness": rule("directness", "先说结论，再补必要依据"), "directness_key": _clip(source.get("directness") or "balanced", 40), "initiative": rule("initiative", "缺关键条件时才追问"), "humor": rule("humor", "语境允许时轻轻接梗"), "rhythm": rule("rhythm", "自然短句和小段"), "question_policy": rule("question_policy", "缺必需条件时才追问一个问题"), "question_policy_key": _clip(source.get("question_policy") or "contextual", 40), "address_policy": rule("address_policy", "称呼随语境自然出现"), "private_length": _clip(source.get("private_length") or "short", 40), "group_length": _clip(source.get("group_length") or "brief", 40), "work_length": _clip(source.get("work_length") or "structured_compact", 40), "length_rule": _VOICE_LENGTHS[channel].get(length_key, "先说重点"), "work_continuity": text("work_continuity", "区分计划、执行中、完成和失败；只有可验证结果才能表述为已完成。", 800), "meme_policy": rule("meme_policy", "表情包由审核后的发送层按语境选择"), "meme_policy_key": _clip(source.get("meme_policy") or "contextual", 40), "group_stance": _clip(source.get("group_stance") or "observant", 40), "group_reaction_style": _clip(source.get("group_reaction_style") or "specific", 40), "group_sentence_rhythm": _clip(source.get("group_sentence_rhythm") or "one_beat", 40), "group_ending_policy": _clip(source.get("group_ending_policy") or "drop", 40), "preferred_phrases": [] if group else _voice_list(source, "preferred_phrases", 16, 120), "avoid_phrases": _voice_list(source, "avoid_phrases", 16, 120), "examples": (relevant_examples or examples)[:3]}


def _slug(value: object, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    return (text or fallback)[:64]


def ensure_social_experience_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS expression_habits (
            id TEXT PRIMARY KEY, situation TEXT NOT NULL, cues TEXT NOT NULL DEFAULT '',
            style TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'daily', enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 5, use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS expression_feedback (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL CHECK(subject_type IN ('global','private_user','qq_group')),
            subject_id TEXT NOT NULL DEFAULT '', source_message TEXT NOT NULL,
            feedback_type TEXT NOT NULL CHECK(feedback_type IN ('prefer','avoid')),
            learned_style TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.95,
            created_at TEXT NOT NULL
        )""",
    )
    habit_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(expression_habits)")}
    for name, definition in {
        "subject_type": "TEXT NOT NULL DEFAULT 'global'",
        "subject_id": "TEXT NOT NULL DEFAULT ''",
        "origin": "TEXT NOT NULL DEFAULT 'manual'",
        "confidence": "REAL NOT NULL DEFAULT 1.0",
        "feedback_count": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in habit_columns:
            conn.execute(f"ALTER TABLE expression_habits ADD COLUMN {name} {definition}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS group_policies (
            group_id TEXT PRIMARY KEY, group_name TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 0, mention_only INTEGER NOT NULL DEFAULT 1,
            active_reply INTEGER NOT NULL DEFAULT 0, reply_probability REAL NOT NULL DEFAULT 0.2,
            cooldown_seconds INTEGER NOT NULL DEFAULT 180, quiet_start TEXT NOT NULL DEFAULT '23:30',
            quiet_end TEXT NOT NULL DEFAULT '08:30', timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            max_context INTEGER NOT NULL DEFAULT 40, allow_work INTEGER NOT NULL DEFAULT 0,
            allowed_work_senders TEXT NOT NULL DEFAULT '', meme_enabled INTEGER NOT NULL DEFAULT 0,
            participation_mode TEXT NOT NULL DEFAULT '', quiet_gap_seconds INTEGER NOT NULL DEFAULT 8,
            burst_window_seconds INTEGER NOT NULL DEFAULT 12,
            burst_max_messages INTEGER NOT NULL DEFAULT 6,
            daily_reply_budget INTEGER NOT NULL DEFAULT 20,
            continuation_window_seconds INTEGER NOT NULL DEFAULT 120,
            max_auto_continuations INTEGER NOT NULL DEFAULT 2,
            last_reply_at TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0,
            reply_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
    )
    group_policy_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(group_policies)")
    }
    for name, definition in {
        "participation_mode": "TEXT NOT NULL DEFAULT ''",
        "quiet_gap_seconds": "INTEGER NOT NULL DEFAULT 8",
        "burst_window_seconds": "INTEGER NOT NULL DEFAULT 12",
        "burst_max_messages": "INTEGER NOT NULL DEFAULT 6",
        "daily_reply_budget": "INTEGER NOT NULL DEFAULT 20",
        "continuation_window_seconds": "INTEGER NOT NULL DEFAULT 120",
        "max_auto_continuations": "INTEGER NOT NULL DEFAULT 2",
    }.items():
        if name not in group_policy_columns:
            conn.execute(f"ALTER TABLE group_policies ADD COLUMN {name} {definition}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            sender_id TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL, is_mention INTEGER NOT NULL DEFAULT 0,
            decision TEXT NOT NULL DEFAULT '', decision_reason TEXT NOT NULL DEFAULT '',
            replied INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        )""",
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expression_habits_enabled ON expression_habits(enabled, scope, priority)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expression_habits_subject "
        "ON expression_habits(subject_type,subject_id,enabled,priority)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expression_feedback_subject "
        "ON expression_feedback(subject_type,subject_id,created_at DESC)",
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_group_messages_group ON group_messages(group_id, id)")


def seed_expression_habits(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for item in DEFAULT_EXPRESSION_HABITS:
        conn.execute(
            """INSERT OR IGNORE INTO expression_habits(
                id, situation, cues, style, scope, enabled, priority,
                use_count, last_used_at, created_at, updated_at,
                subject_type,subject_id,origin,confidence,feedback_count
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 0, '', ?, ?, 'global', '', 'system', 1.0, 0)""",
            (
                item["id"], item["situation"], item["cues"], item["style"],
                item["scope"], item["priority"], now, now,
            ),
        )


def list_expression_habits(conn: sqlite3.Connection, *, enabled: str = "") -> list[dict]:
    where = ""
    params: list[object] = []
    if enabled in {"0", "1"}:
        where = "WHERE enabled = ?"
        params.append(int(enabled))
    rows = conn.execute(
        f"SELECT * FROM expression_habits {where} ORDER BY enabled DESC, priority DESC, use_count DESC, id",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_expression_habit(conn: sqlite3.Connection, payload: dict) -> dict:
    situation = _clip(payload.get("situation"), 240)
    style = _clip(payload.get("style"), 1000)
    if not situation:
        raise ValueError("situation_required")
    if not style:
        raise ValueError("style_required")
    habit_id = _slug(payload.get("id") or situation, "expression")
    scope = str(payload.get("scope") or "daily").strip().lower()
    if scope not in {"daily", "group", "all"}:
        raise ValueError("invalid_scope")
    subject_type = str(payload.get("subject_type") or "global").strip()
    subject_id = _clip(payload.get("subject_id"), 160)
    if subject_type not in EXPRESSION_SUBJECT_TYPES:
        raise ValueError("invalid_expression_subject")
    if subject_type != "global" and not subject_id:
        raise ValueError("expression_subject_id_required")
    if subject_type == "global":
        subject_id = ""
    now = utc_now()
    enabled = 1 if truthy(payload.get("enabled", "1")) else 0
    try:
        priority = max(1, min(int(payload.get("priority") or 5), 20))
    except (TypeError, ValueError):
        priority = 5
    conn.execute(
        """INSERT INTO expression_habits(
            id, situation, cues, style, scope, enabled, priority,
            use_count, last_used_at, created_at, updated_at,
            subject_type,subject_id,origin,confidence,feedback_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            situation=excluded.situation,cues=excluded.cues,style=excluded.style,scope=excluded.scope,
            enabled=excluded.enabled,priority=excluded.priority,subject_type=excluded.subject_type,
            subject_id=excluded.subject_id,origin=excluded.origin,confidence=excluded.confidence,
            updated_at=excluded.updated_at""",
        (
            habit_id, situation, _clip(payload.get("cues"), 500), style, scope, enabled, priority,
            now, now, subject_type, subject_id, _clip(payload.get("origin") or "manual", 40),
            max(0.0, min(float(payload.get("confidence") or 1.0), 1.0)),
            max(0, int(payload.get("feedback_count") or 0)),
        ),
    )
    return dict(conn.execute("SELECT * FROM expression_habits WHERE id = ?", (habit_id,)).fetchone())


def select_expression_habits(
    conn: sqlite3.Connection,
    *,
    message: str,
    social_cues: dict | None = None,
    scope: str = "daily",
    subject_type: str = "global",
    subject_id: str = "",
    limit: int = 3,
) -> list[dict]:
    if subject_type not in EXPRESSION_SUBJECT_TYPES:
        subject_type = "global"
    subject_id = str(subject_id or "").strip()
    rows = conn.execute(
        """SELECT * FROM expression_habits
        WHERE enabled = 1 AND (scope = ? OR scope = 'all')
          AND (subject_type='global' OR (subject_type=? AND subject_id=?))
        ORDER BY CASE WHEN subject_type='global' THEN 0 ELSE 1 END DESC,
                 priority DESC, use_count ASC, updated_at DESC""",
        (scope, subject_type, subject_id),
    ).fetchall()
    query = f"{message or ''} {json.dumps(social_cues or {}, ensure_ascii=False)}".lower()
    scored: list[tuple[int, dict]] = []
    for row in rows:
        item = dict(row)
        cues = [part.strip().lower() for part in re.split(r"[,，\s]+", item.get("cues") or "") if part.strip()]
        matches = sum(1 for cue in cues if cue in query)
        scoped_bonus = 1000 if item.get("subject_type") != "global" else 0
        score = scoped_bonus + matches * 100 + int(item.get("priority") or 0)
        if matches or item.get("scope") == "all" or item.get("origin") == "user_feedback":
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], -int(pair[1].get("use_count") or 0)), reverse=True)
    selected = [item for _, item in scored[: max(1, min(int(limit or 3), 5))]]
    if selected:
        now = utc_now()
        conn.executemany(
            "UPDATE expression_habits SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
            [(now, item["id"]) for item in selected],
        )
    return selected


def detect_expression_feedback(message: str) -> dict | None:
    """Recognize only explicit user corrections; never infer a preference silently."""

    text = " ".join(str(message or "").split()).strip()
    if len(text) < 4 or len(text) > 500:
        return None
    expression_words = ("说话", "表达", "回复", "回答", "语气", "口吻", "开场", "结尾", "列表", "卖萌", "人格", "口癖")
    prefer_patterns = (
        r"(?:以后|今后).{0,8}(?:说话|表达|回复|回答).+",
        r"我(?:希望|喜欢)你.{0,20}(?:说|回复|回答|表达).+",
    )
    avoid_patterns = (
        r"(?:不要|别|禁止)(?:再|总是|老是)?[^。！？]{1,120}",
        r"你(?:说话|表达|回复|回答|语气|口吻).{0,12}(?:太|很|有点|过于)[^。！？]{1,80}",
    )
    feedback_type = ""
    if any(re.search(pattern, text) for pattern in prefer_patterns):
        feedback_type = "prefer"
    elif any(word in text for word in expression_words) and any(re.search(pattern, text) for pattern in avoid_patterns):
        feedback_type = "avoid"
    elif any(word in text.lower() for word in ("像ai", "像 ai", "机器人话", "客服话")) and any(
        word in text for word in expression_words
    ):
        feedback_type = "avoid"
    if not feedback_type:
        return None
    normalized = text.lower()
    if any(word in normalized for word in ("简短", "简洁", "短一点", "直接一点", "别啰嗦", "太长", "冗长")):
        preference_code = "brief_direct"
        learned = "默认简短直接；除非被要求，不展开成说明书。"
    elif any(word in normalized for word in ("像ai", "像 ai", "机器人话", "客服话", "太正式", "生硬")):
        preference_code = "natural_conversational"
        learned = "避免客服式套话和自我说明；使用自然、克制的口语表达。"
    elif any(word in normalized for word in ("表情", "emoji", "颜文字")):
        preference_code = "emoji_restraint"
        learned = "表情只在语境需要时使用，避免连续或模板化表情。"
    elif any(word in normalized for word in ("引用", "接谁", "接话")):
        preference_code = "clear_group_target"
        learned = "群消息需要明确指向时，优先引用或点明正在回应的内容。"
    else:
        preference_code = "explicit_expression_preference"
        learned = "遵循已确认的表达偏好，避免重复触发同类纠正。"
    return {
        "feedback_type": feedback_type,
        "preference_code": preference_code,
        "style": learned,
        "confidence": 0.95,
    }


def record_expression_feedback(
    conn: sqlite3.Connection,
    *,
    message: str,
    subject_type: str,
    subject_id: str,
    scope_override: str = "",
) -> dict | None:
    detected = detect_expression_feedback(message)
    if not detected:
        return None
    subject_type = str(subject_type or "").strip()
    subject_id = str(subject_id or "").strip()
    if subject_type not in {"private_user", "qq_group"} or not subject_id:
        raise ValueError("expression_feedback_subject_invalid")
    digest = hashlib.sha256(f"{subject_type}\0{subject_id}\0{detected['style']}".encode("utf-8")).hexdigest()[:18]
    habit_id = f"feedback-{digest}"
    event_id = "expression-feedback-" + uuid.uuid4().hex
    scope = str(scope_override or ("group" if subject_type == "qq_group" else "daily")).strip()
    if scope not in {"daily", "group", "all"}:
        raise ValueError("expression_feedback_scope_invalid")
    now = utc_now()
    conn.execute(
        """INSERT INTO expression_feedback(
            id,subject_type,subject_id,source_message,feedback_type,learned_style,confidence,created_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (event_id, subject_type, subject_id, message[:500], detected["feedback_type"], detected["style"], detected["confidence"], now),
    )
    conn.execute(
        """INSERT INTO expression_habits(
            id,situation,cues,style,scope,enabled,priority,use_count,last_used_at,
            created_at,updated_at,subject_type,subject_id,origin,confidence,feedback_count
        ) VALUES(?,?,'',?,?,1,18,0,'',?,?,?,?,? ,?,1)
        ON CONFLICT(id) DO UPDATE SET
            style=excluded.style,enabled=1,updated_at=excluded.updated_at,
            confidence=max(expression_habits.confidence,excluded.confidence),
            feedback_count=expression_habits.feedback_count+1""",
        (
            habit_id, "用户明确提出的表达偏好", detected["style"], scope,
            now, now, subject_type, subject_id, "user_feedback", detected["confidence"],
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM expression_habits WHERE id=?", (habit_id,)).fetchone())


def expression_profile(conn: sqlite3.Connection, *, subject_type: str, subject_id: str) -> dict:
    habits = conn.execute(
        """SELECT * FROM expression_habits
        WHERE enabled=1 AND (subject_type='global' OR (subject_type=? AND subject_id=?))
        ORDER BY CASE WHEN subject_type='global' THEN 0 ELSE 1 END DESC,priority DESC,updated_at DESC""",
        (subject_type, str(subject_id or "")),
    ).fetchall()
    return {
        "subject_type": subject_type,
        "subject_id": str(subject_id or ""),
        "habits": [dict(row) for row in habits],
        "learned_count": sum(1 for row in habits if str(row["origin"]) == "user_feedback"),
    }


def hydrate_expression_context(
    db_connect: Callable[[], sqlite3.Connection],
    *,
    message: str,
    social_cues: dict,
    user_id: str,
    group: dict | None = None,
    allow_group_feedback: bool = False,
) -> dict:
    """Persist explicit feedback and select scoped habits without blocking chat."""

    group_info = group or {}
    group_id = str(group_info.get("group_id") or "").strip()
    sender_id = str(group_info.get("sender_id") or "").strip()
    subject_type = "qq_group" if group_info and allow_group_feedback else "private_user"
    subject_id = group_id if subject_type == "qq_group" else (sender_id or str(user_id or "").strip())
    try:
        with db_connect() as conn:
            from bridge_learning_service import (
                capture_expression_candidate,
                learning_feature_enabled,
            )
            if learning_feature_enabled(conn):
                learned = capture_expression_candidate(
                    conn,
                    message=message,
                    user_id=str(user_id or ""),
                    thread_id=str(group_info.get("thread_id") or ""),
                    source_message_id=str(group_info.get("message_id") or ""),
                    group=group_info,
                    allow_group_feedback=allow_group_feedback,
                )
            else:
                learned = record_expression_feedback(
                    conn,
                    message=message,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    # A group member may teach only their own cross-channel expression
                    # preference.  Group-wide learning requires an explicit admin grant.
                    scope_override="group" if subject_type == "qq_group" else ("all" if group_info else "daily"),
                )
            if group_info:
                group_habits = select_expression_habits(
                    conn,
                    message=message,
                    social_cues=social_cues,
                    scope="group",
                    subject_type="qq_group",
                    subject_id=group_id,
                    limit=3,
                )
                sender_habits = select_expression_habits(
                    conn,
                    message=message,
                    social_cues=social_cues,
                    scope="group",
                    subject_type="private_user",
                    subject_id=sender_id,
                    limit=3,
                ) if sender_id else []
                habits = []
                seen: set[str] = set()
                for item in [*sender_habits, *group_habits]:
                    habit_id = str(item.get("id") or "")
                    if habit_id and habit_id not in seen:
                        habits.append(item)
                        seen.add(habit_id)
                    if len(habits) >= 5:
                        break
            else:
                habits = select_expression_habits(
                    conn,
                    message=message,
                    social_cues=social_cues,
                    scope="daily",
                    subject_type=subject_type,
                    subject_id=subject_id,
                )
            if learning_feature_enabled(conn):
                from bridge_learning_service import record_context_trace
                record_context_trace(
                    conn,
                    thread_id=str(group_info.get("thread_id") or ""),
                    message_id=str(group_info.get("message_id") or ""),
                    domain="expression",
                    source_type="expression_context",
                    source_id=str((learned or {}).get("id") or (learned or {}).get("candidate_id") or ""),
                    decision="applied" if habits else "observed",
                    detail={"habit_count": len(habits), "candidate_status": str((learned or {}).get("status") or "")},
                )
        return {"learned_feedback": learned, "habits": habits}
    except (sqlite3.Error, ValueError):
        return {"learned_feedback": None, "habits": []}
