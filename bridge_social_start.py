#!/usr/bin/env python3
"""Evidence assembly and fail-closed adjudication for social topic starts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_group_context_frame import normalize_group_visible_text
from bridge_qq_access_service import check_private_chat_access, get_qq_access_settings
from bridge_social_opportunity import (
    add_topic_candidate,
    create_opportunity,
    decide_opportunity,
    normalize_social_decision,
)


def _clip(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _load(value: object, fallback):
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _cjk_bigrams(value: object) -> set[str]:
    compact = re.sub(r"[^\u3400-\u9fff]", "", str(value or ""))
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


_GENERIC_GROUNDING_BIGRAMS = {
    "用户", "对方", "我们", "大家", "今天", "明天", "刚才", "现在",
    "当前", "最近", "继续", "回应", "共同", "对话", "事情", "这个",
    "那个", "时候", "一下", "有点", "真的", "可以", "还是", "已经",
}


def _grounding_evidence_bigrams(prepared: dict) -> set[str]:
    situation = prepared.get("situation") if isinstance(prepared.get("situation"), dict) else {}
    evidence_parts = [
        _clip(item.get("content"), 800)
        for item in situation.get("recent_conversation") or []
        if isinstance(item, dict) and _clip(item.get("content"), 800)
    ] + [
        _clip(item.get("content"), 300)
        for item in situation.get("governed_memories") or []
        if isinstance(item, dict) and _clip(item.get("content"), 300)
    ]
    return _cjk_bigrams("。".join(evidence_parts)) - _GENERIC_GROUNDING_BIGRAMS


def _decision_metadata_is_grounded(prepared: dict, topic: str, why_now: str) -> bool:
    """Require the Agent's free-form topic/reason to retain a concrete anchor.

    The candidate authorizes a bounded evidence bundle; it does not make any
    topic text true.  Requiring a salient phrase from that bundle prevents an
    otherwise grounded message from carrying a fabricated topic into the Plan.
    """

    evidence = _grounding_evidence_bigrams(prepared)
    decision = _cjk_bigrams(f"{topic}。{why_now}") - _GENERIC_GROUNDING_BIGRAMS
    return bool(evidence and decision and evidence & decision)


_SAFE_RELATIONAL_CLAUSE = re.compile(
    r"^(?:"
    r"(?:请)?记得(?:照顾好自己|好好休息|早点休息|按时吃饭|注意安全)|"
    r"(?:回来|到时候|有结果(?:了)?)(?:再)?告诉我(?:结果|怎么样)?|"
    r"(?:照顾好自己|慢慢来|早点休息|注意安全|别太累|别太着急|加油)|"
    r"(?:要不要|愿不愿意|想不想)(?:聊聊|说说)"
    r")$"
)


def _message_is_grounded(prepared: dict, message: str) -> bool:
    """Conservatively reject output clauses that introduce unseen facts.

    This is a deterministic final guard, not a semantic quality score.  It
    allows ordinary emotional/conversational framing while requiring every
    substantive clause to share concrete language with the current bounded
    Situation or a governed memory.  Uncertain paraphrases fail closed rather
    than becoming confident invented facts in a proactive message.
    """

    evidence_bigrams = _grounding_evidence_bigrams(prepared)
    if not evidence_bigrams:
        return False
    cleaned = str(message or "")
    for scaffold in (
        "你刚才说", "群成员刚才说", "大家刚才说", "你说", "刚才",
        "我还想听听", "我也想听听", "我想听听", "我想问问", "想问问",
        "谢谢你", "感谢你", "我真的很感动", "我很感动", "我挺感动",
        "我真的很开心", "我很开心", "我挺开心", "我替你高兴",
        "我有点担心", "我很担心", "我有点难过", "我很难过",
    ):
        cleaned = cleaned.replace(scaffold, "")
    # Conjunctions start a new predicate unit.  Without this boundary,
    # "谢谢你帮我看天气还买了电脑" inherits overlap from the first true action
    # and smuggles the second, unseen action through the lexical guard.
    clauses = re.split(r"[。！？!?；;，,：:]|而且|并且|另外|同时|还(?:给)?|又", cleaned)
    for clause in clauses:
        compact = re.sub(r"[^\u3400-\u9fff]", "", clause)
        compact = re.sub(r"^(?:你|我|我们|大家|群里|用户|对方)+", "", compact)
        compact = re.sub(r"(?:吗|呢|呀|啊|啦|吧)+$", "", compact)
        if len(compact) < 3:
            continue
        if _SAFE_RELATIONAL_CLAUSE.fullmatch(compact):
            continue
        overlap = len(_cjk_bigrams(compact) & evidence_bigrams)
        required = 1 if len(compact) <= 6 else 2
        if overlap < required:
            return False
    # A message made only of validated emotional framing is adjudicated by the
    # emotion evidence contract below; do not relabel that failure here.
    return True


def _independent_question_is_safe(topic: str, message: str) -> bool:
    """Allow a hypothetical invitation, never an unobserved personal claim."""
    topic, message = str(topic or "").strip(), str(message or "").strip()
    if not (4 <= len(topic) <= 100 and 8 <= len(message) <= 120):
        return False
    if not message.endswith(("？", "?")) or message.count("？") + message.count("?") != 1:
        return False
    if not re.match(r"^(?:如果|假如|要是|你会|你更想|你觉得|想不想|有没有兴趣)", message):
        return False
    if re.search(r"你(?:昨天|刚才|上次|之前|已经|正在|总是)|我(?:昨天|刚才|上次|在线下|已经)|帮你(?:查|做|修)|(?:新闻|报道|现实中)", message):
        return False
    return bool(_cjk_bigrams(topic) & _cjk_bigrams(message))


def _utc(value: object | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("social_start_time_invalid")
    return parsed.astimezone(timezone.utc)


def _created_at(item: dict) -> datetime | None:
    try:
        return _utc(item.get("created_at"))
    except (TypeError, ValueError):
        return None


_RESPECT_PAUSE = re.compile(
    r"(?:不急(?!(?:着)?(?:做|处理|上线|修|改|解决|完成|实现))|"
    r"不用(?:现在)?(?:回|回复|回答)|慢慢想|先睡|睡吧|早点休息|明天再说|先休息)",
    re.IGNORECASE,
)


def _waiting_state(history: list[dict]) -> str:
    """Project the assistant's latest pause promise from durable dialogue.

    A promise to let the user sleep or answer later is an interaction fact.  It
    suppresses a social nudge until that user actually returns; elapsed timer
    time alone must not turn care into pressure.
    """

    last_pause = -1
    for index, item in enumerate(history):
        if item.get("role") == "assistant" and _RESPECT_PAUSE.search(_clip(item.get("content"), 800)):
            last_pause = index
    if last_pause < 0:
        return "open"
    if any(item.get("role") != "assistant" and _clip(item.get("content"), 800) for item in history[last_pause + 1:]):
        return "user_returned"
    return "respect_user_pause"


def _admissible_memories(memories: list[dict], *, assistant_id: str, user_id: str, now: datetime) -> list[dict]:
    result: list[dict] = []
    for raw in memories[:6]:
        item = dict(raw or {})
        content = _clip(item.get("content"), 300)
        if not content or str(item.get("sensitivity") or "normal") == "sensitive":
            continue
        if str(item.get("retention_class") or "") == "metadata_only" or str(item.get("body_redacted_at") or ""):
            continue
        if item.get("assistant_id") and str(item.get("assistant_id")) != assistant_id:
            continue
        if item.get("subject_actor_ref") and str(item.get("subject_actor_ref")) != user_id:
            continue
        expires = str(item.get("expires_at") or "").strip()
        if expires:
            try:
                if _utc(expires) <= now:
                    continue
            except ValueError:
                continue
        result.append({
            "id": _clip(item.get("id"), 120),
            "kind": _clip(item.get("kind"), 40),
            "content": content,
            "updated_at": _clip(item.get("updated_at") or item.get("created_at"), 80),
        })
    return result


def _load_private_affect(
    conn: sqlite3.Connection,
    *,
    assistant_id: str,
    user_id: str,
    now: datetime,
) -> dict:
    try:
        from bridge_assistant_affect_runtime import load_assistant_affect_influence

        row = conn.execute(
            """SELECT id,latest_inbound_sequence FROM conversation_threads
               WHERE assistant_id=? AND channel_type='qq_private'
                 AND external_thread_ref=? AND status='active'
               ORDER BY updated_at DESC,id LIMIT 1""",
            (assistant_id, user_id),
        ).fetchone()
        if not row:
            return {"active": False}
        return load_assistant_affect_influence(
            conn,
            assistant_id=assistant_id,
            scope_type="private",
            scope_id=str(row[0]),
            target_id=user_id,
            turn_revision=int(row[1] or 0),
            now=now,
        )
    except (sqlite3.Error, TypeError, ValueError):
        return {"active": False}


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
    ).fetchone()
    if not row:
        raise ValueError("active_assistant_missing")
    return str(row[0])


def private_reply_obligation_reason(conn: sqlite3.Connection, policy: dict) -> str:
    """A social start must not replace an outstanding direct private reply."""
    if str(policy.get("policy_kind") or "") != "social":
        return ""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "conversation_response_cycles" not in tables:
        return ""  # Pre-coordinator installations retain their existing path.
    try:
        thread = conn.execute(
            """SELECT id,latest_inbound_sequence FROM conversation_threads
               WHERE legacy_user_id=? AND assistant_id=? AND channel_type='qq_private'
                 AND status='active' ORDER BY updated_at DESC,id LIMIT 1""",
            (_clip(policy.get("user_id"), 80), _clip(policy.get("assistant_id"), 80)),
        ).fetchone()
        if not thread:
            return ""
        held = conn.execute(
            """SELECT 1 FROM conversation_response_cycles WHERE thread_id=?
               AND blocks_thread=1
               AND state IN ('pending','processing','commit_ready','retryable','manual_hold')
               LIMIT 1""", (thread[0],),
        ).fetchone()
        if held:
            return "private_reply_pending"
        queued = conn.execute(
            """SELECT 1 FROM conversation_response_cycles c WHERE c.thread_id=?
               AND c.through_inbound_sequence>=?
               AND c.state='outbox_queued' AND NOT EXISTS (
                   SELECT 1 FROM conversation_messages m
                   WHERE m.thread_id=c.thread_id AND m.response_cycle_id=c.id
                     AND m.role='assistant' AND m.delivery_projection_state='channel_acked'
               ) LIMIT 1""", (thread[0], int(thread[1] or 0)),
        ).fetchone()
        if queued:
            return "private_reply_pending"
        handled = conn.execute(
            "SELECT COALESCE(MAX(through_inbound_sequence),0) FROM conversation_response_cycles WHERE thread_id=?",
            (thread[0],),
        ).fetchone()[0]
        if int(thread[1] or 0) > int(handled or 0):
            return "private_reply_pending"
    except sqlite3.Error:
        return "private_reply_state_unavailable"
    return ""


def owner_social_start_preflight(conn: sqlite3.Connection, policy: dict) -> dict:
    """Fail closed before reading conversation bodies, secrets, or a model.

    Private starts require the same current QQ ``chat`` authorization as an
    ordinary private turn, plus an explicitly enabled and authorized social
    policy.  Existing group starts keep their distinct, explicit group
    authorization and must also remain bound to the current allowlist and live
    send session.  This preserves the platform's one SocialOpportunity spine
    without turning a timer into permission to speak.
    """

    policy_kind = str(policy.get("policy_kind") or "")
    if policy_kind not in {"social", "group_social"}:
        return {"allowed": False, "reason": "social_policy_kind_invalid", "assistant_id": ""}
    try:
        assistant_id = _active_assistant_id(conn)
    except (sqlite3.Error, ValueError):
        return {"allowed": False, "reason": "active_assistant_missing", "assistant_id": ""}
    bound_assistant = _clip(policy.get("assistant_id"), 80)
    if bound_assistant and bound_assistant != assistant_id:
        return {"allowed": False, "reason": "assistant_policy_stale", "assistant_id": assistant_id}
    if "enabled" in policy and not bool(policy.get("enabled")):
        return {"allowed": False, "reason": "policy_disabled", "assistant_id": assistant_id}
    if "authorized" in policy and not bool(policy.get("authorized")):
        return {"allowed": False, "reason": "policy_disabled", "assistant_id": assistant_id}
    user_id = _clip(policy.get("user_id"), 80)
    if policy_kind == "group_social":
        group_id = user_id.removeprefix("group:") if user_id.startswith("group:") else ""
        if not group_id or not _clip(policy.get("send_session"), 300):
            return {"allowed": False, "reason": "group_session_required", "assistant_id": assistant_id}
        try:
            access = get_qq_access_settings(conn)
        except (sqlite3.Error, ValueError):
            access = {}
        settings = dict(access.get("settings") or {})
        group_allowed = any(
            str(item.get("group_id") or "") == group_id and bool(item.get("enabled"))
            for item in (access.get("group_allowlist") or [])
        )
        if not (
            access.get("feature_enabled")
            and settings.get("channel_enabled")
            and settings.get("group_chat_enabled")
            and group_allowed
        ):
            return {"allowed": False, "reason": "group_not_authorized", "assistant_id": assistant_id}
        try:
            group_policy = conn.execute(
                """SELECT enabled,participation_mode,session
                   FROM group_policies WHERE group_id=?""",
                (group_id,),
            ).fetchone()
        except sqlite3.Error:
            group_policy = None
        if not group_policy or not bool(group_policy[0]) or str(group_policy[1] or "") != "natural_participation" or not _clip(group_policy[2], 300):
            return {
                "allowed": False,
                "reason": "group_participation_ineligible",
                "assistant_id": assistant_id,
            }
        if _clip(group_policy[2], 300) != _clip(policy.get("send_session"), 300):
            return {"allowed": False, "reason": "group_session_stale", "assistant_id": assistant_id}
        return {"allowed": True, "reason": "group_authorized", "assistant_id": assistant_id}
    try:
        access = check_private_chat_access(
            conn,
            user_id,
            requested_action="chat",
        )
    except (sqlite3.Error, ValueError):
        access = {"allowed": False}
    if not access.get("allowed"):
        return {
            "allowed": False,
            "reason": "private_not_authorized",
            "assistant_id": assistant_id,
        }
    pending_reason = private_reply_obligation_reason(conn, {**policy, "assistant_id": assistant_id})
    if pending_reason:
        return {"allowed": False, "reason": pending_reason, "assistant_id": assistant_id}
    return {"allowed": True, "reason": "private_authorized", "assistant_id": assistant_id}


def _group_relationship_projection(
    conn: sqlite3.Connection,
    *,
    group_id: str,
) -> dict:
    """Derive bounded group familiarity without copying member-private state."""

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN sender_id<>'bot' THEN 1 ELSE 0 END) AS member_messages,
            SUM(CASE WHEN sender_id='bot' THEN 1 ELSE 0 END) AS assistant_turns,
            COUNT(DISTINCT CASE WHEN sender_id<>'bot' THEN sender_id END) AS participants,
            MAX(created_at) AS last_activity_at
        FROM group_messages
        WHERE group_id=? AND created_at>=?
        """,
        (group_id, cutoff),
    ).fetchone()
    item = dict(row) if row else {}
    member_messages = int(item.get("member_messages") or 0)
    assistant_turns = int(item.get("assistant_turns") or 0)
    participants = int(item.get("participants") or 0)
    if member_messages >= 30 and assistant_turns >= 3:
        familiarity = "established"
    elif member_messages >= 10 and assistant_turns >= 1:
        familiarity = "familiar"
    else:
        familiarity = "new"
    if member_messages >= 50 or participants >= 5:
        style = "lively"
    elif member_messages >= 10:
        style = "conversational"
    else:
        style = "natural"
    return {
        "preferred_address": "",
        "interaction_style": style,
        "familiarity_context": familiarity,
        "blocked_topics": [],
        "version": 0,
        "projection": "group_participation_30d",
        "member_messages": member_messages,
        "assistant_turns": assistant_turns,
        "participant_count": participants,
        "last_activity_at": str(item.get("last_activity_at") or ""),
    }


def prepare_start_opportunity(
    conn: sqlite3.Connection,
    policy: dict,
    *,
    history: list[dict],
    memories: list[dict],
    assistant_affect: dict | None = None,
    now: object | None = None,
) -> dict:
    current_time = _utc(now)
    assistant_id = _clip(policy.get("assistant_id"), 80) or _active_assistant_id(conn)
    user_id = _clip(policy.get("user_id"), 80)
    if not user_id:
        raise ValueError("social_start_user_required")
    is_group = (
        _clip(policy.get("policy_kind"), 40) == "group_social"
        and user_id.startswith("group:")
        and bool(user_id[6:])
    )
    subject_type = "qq_group" if is_group else "private_user"
    subject_id = user_id[6:] if is_group else user_id
    thread_id = f"qq:group:{subject_id}" if is_group else f"qq:private:{subject_id}"
    relationship_row = None if is_group else conn.execute(
        """SELECT * FROM relationship_states
           WHERE assistant_id=? AND user_id=? AND scope_type='private_user'
           ORDER BY updated_at DESC LIMIT 1""",
        (assistant_id, user_id),
    ).fetchone()
    relationship = (
        _group_relationship_projection(conn, group_id=subject_id)
        if is_group
        else (dict(relationship_row) if relationship_row else {})
    )
    allowed_topics = _load(relationship.get("allowed_topics_json"), [])
    blocked_topics = [_clip(item, 120) for item in _load(relationship.get("blocked_topics_json"), [])]
    seed = f"{assistant_id}|{subject_type}|{subject_id}|{policy.get('next_check_at') or ''}|{policy.get('policy_version') or 1}"
    opportunity_id = "start-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    opportunity = create_opportunity(
        conn,
        assistant_id=assistant_id,
        kind="start",
        subject_type=subject_type,
        subject_id=subject_id,
        thread_id=thread_id,
        trigger_type="authorized_social_timer",
        trigger_ref=str(policy.get("next_check_at") or ""),
        policy_snapshot={
            "policy_version": int(policy.get("policy_version") or 1),
            "initiative_mode": str(policy.get("initiative_mode") or "balanced"),
            "allowed_intents": [item for item in str(policy.get("allowed_intents") or "").split(",") if item],
            "consecutive_unanswered": int(policy.get("consecutive_unanswered") or 0),
        },
        relationship_version=int(relationship.get("version") or 0),
        opportunity_id=opportunity_id,
    )

    def blocked(text: str) -> bool:
        return any(topic and topic in text for topic in blocked_topics)

    # Waiting/promise state is durable interaction truth, not prompt context.
    # Evaluate it before the seven-day body window so time alone cannot revoke
    # an explicit "sleep / answer later" commitment.  A later user message in
    # the same durable tail is the only normal transition back to user_returned.
    waiting_state = (
        "open"
        if is_group
        else _waiting_state([dict(item or {}) for item in history[-12:]])
    )
    seven_days_ago = current_time - timedelta(days=7)
    visible_history = []
    for raw in history[-12:]:
        item = dict(raw or {})
        content = _clip(item.get("content"), 800)
        created = _created_at(item)
        if not content or created is None or created < seven_days_ago or blocked(content):
            continue
        if is_group:
            content = _clip(normalize_group_visible_text(content), 800)
        visible_history.append({**item, "content": content})
    admissible_memories = [] if is_group else _admissible_memories(
        memories,
        assistant_id=assistant_id,
        user_id=user_id,
        now=current_time,
    )
    affect = dict(assistant_affect or {}) if assistant_affect is not None else (
        {"active": False} if is_group else _load_private_affect(
            conn, assistant_id=assistant_id, user_id=user_id, now=current_time,
        )
    )
    affect = {
        key: affect.get(key)
        for key in (
            "active", "style_only", "primary_affect", "effective_intensity",
            "tone", "sentence_length", "expression_guidance", "turns_elapsed",
            "state_id", "source_event_id", "evidence_ref", "updated_at",
        )
        if key in affect
    }
    roles = {str(item.get("role") or "") for item in visible_history}
    mutual_context = {"user", "assistant"}.issubset(roles)
    historical_mutual = {"user", "assistant"}.issubset(
        {str(item.get("role") or "") for item in history[-12:]}
    )
    # Memory, Relationship and Affect enrich a shared situation.  None of them
    # may manufacture the time-sensitive reason to contact somebody alone.
    has_material = bool(visible_history) if is_group else mutual_context
    candidates = []
    if has_material and waiting_state != "respect_user_pause":
        # Assistant output is part of visible continuity, but it is not new
        # external evidence.  Basing the candidate identity on bot rows lets a
        # just-ACKed proactive message mint a new topic key and bypass the
        # cooldown.  Only user evidence and governed memory revisions advance
        # the situation fingerprint.
        evidence_refs = [
            _clip(item.get("id") or item.get("created_at"), 160)
            for item in visible_history
            if str(item.get("role") or "") == "user"
        ] + [str(item.get("id") or "") for item in admissible_memories]
        evidence_refs = [item for item in evidence_refs if item]
        fingerprint = hashlib.sha256("\0".join(evidence_refs).encode("utf-8")).hexdigest()
        freshness = max(
            [str(item.get("created_at") or "") for item in visible_history]
            + [str(item.get("updated_at") or "") for item in admissible_memories]
            + [current_time.isoformat() if affect.get("active") else ""],
        )
        candidates.append(add_topic_candidate(conn, opportunity["id"], {
            "source_type": "conversation",
            "source_id": f"situation:{fingerprint}",
            "scope_type": subject_type,
            "scope_id": subject_id,
            # Persist no private body here.  The model sees the bounded
            # in-memory Situation below and must bind its derived topic to this
            # evidence bundle.
            "summary": "当前受控情境具备可由 Assistant 自主判断的话题材料",
            "freshness": freshness,
            "why_relevant": "近期双方交流、受治理记忆或有依据的 Assistant Affect 仍处于同一关系上下文",
            "risk": "low" if mutual_context else "medium",
        }))
    if (
        not is_group and historical_mutual and waiting_state != "respect_user_pause"
        and bool(policy.get("enabled")) and bool(policy.get("authorized"))
    ):
        last_user_ref = next(
            (_clip(item.get("id") or item.get("created_at"), 160)
             for item in reversed(history[-12:]) if item.get("role") == "user"), ""
        )
        candidates.append(add_topic_candidate(conn, opportunity["id"], {
            "source_type": "relationship",
            "source_id": "independent:" + hashlib.sha256(
                f"{assistant_id}|{user_id}|{last_user_ref}".encode("utf-8")
            ).hexdigest()[:24],
            "scope_type": subject_type, "scope_id": subject_id,
            "summary": "可自主提出一个具体、无现实事实前提的轻松新问题",
            "freshness": current_time.isoformat(),
            "why_relevant": "已授权私聊且此前双方确有交流；是否联系仍由本轮模型和硬闸门决定",
            "risk": "low",
        }))
    recent_proactive_topics = []
    if not is_group:
        recent_proactive_topics = [
            {"message": _clip(item[0], 160), "responded": bool(item[1]),
             "created_at": _clip(item[2], 80)}
            for item in conn.execute(
                """SELECT message,responded_at,decision_at FROM proactive_events
                   WHERE user_id=? AND action='send' ORDER BY decision_at DESC LIMIT 4""",
                (user_id,),
            ).fetchall()
        ]
    learned_preferences: dict[str, list[str]] = {"prefer": [], "avoid": []}
    if not is_group:
        from bridge_learning_service import active_private_topic_constraints
        learned_preferences = active_private_topic_constraints(conn, user_id=user_id)
        blocked_topics.extend(learned_preferences["avoid"])
    situation = {
        "schema_version": 1,
        "assistant_id": assistant_id,
        "thread_id": thread_id,
        "channel_scope": "group_proactive" if is_group else "private_proactive",
        "topic_origin": "agent_derived",
        "linear_stages": [
            "observe", "affect", "intend", "gate", "express", "deliver", "wait", "feedback",
        ],
        "recent_conversation": visible_history,
        "source_refs": [
            {
                "id": _clip(item.get("id") or item.get("created_at"), 160),
                "role": "assistant" if item.get("role") == "assistant" else "user",
                "created_at": _clip(item.get("created_at"), 80),
            }
            for item in visible_history
        ],
        "governed_memories": admissible_memories,
        "applied_topic_preferences": learned_preferences["prefer"],
        "recent_proactive_topics": recent_proactive_topics,
        "assistant_affect": affect or {"active": False},
        "waiting_state": waiting_state,
        "topic_constraints": {
            "allowed_directions": [_clip(item, 120) for item in allowed_topics[:3]],
            "blocked_topics": blocked_topics,
        },
    }
    return {
        "opportunity": opportunity,
        "candidates": candidates,
        "relationship": {
            "preferred_address": relationship.get("preferred_address") or "",
            "interaction_style": relationship.get("interaction_style") or "natural",
            "familiarity_context": relationship.get("familiarity_context") or "new",
            "blocked_topics": blocked_topics,
            "version": int(relationship.get("version") or 0),
            **(
                {
                    "projection": relationship.get("projection") or "",
                    "member_messages": int(relationship.get("member_messages") or 0),
                    "assistant_turns": int(relationship.get("assistant_turns") or 0),
                    "participant_count": int(relationship.get("participant_count") or 0),
                    "last_activity_at": relationship.get("last_activity_at") or "",
                }
                if is_group
                else {}
            ),
        },
        "subject_type": subject_type,
        "subject_id": subject_id,
        "situation": situation,
    }


def finalize_start_decision(conn: sqlite3.Connection, prepared: dict, model_value: dict) -> dict:
    opportunity_id = prepared["opportunity"]["id"]
    requested_send = str(model_value.get("action") or "").lower() == "send"
    if not prepared["candidates"]:
        requested_send = False
        model_value = {**model_value, "reason": "no_admissible_situation"}
    reason_override = ""
    if requested_send:
        candidate_id = _clip(model_value.get("topic_candidate_id"), 80)
        candidates_by_id = {
            str(item.get("id") or ""): item for item in prepared.get("candidates") or []
        }
        candidate_ids = set(candidates_by_id)
        independent = (
            prepared.get("subject_type") == "private_user"
            and str(candidates_by_id.get(candidate_id, {}).get("source_id") or "").startswith("independent:")
        )
        topic = _clip(model_value.get("topic") or model_value.get("derived_topic"), 240)
        why_now = _clip(model_value.get("why_now"), 800)
        blocked_topics = list((prepared.get("relationship") or {}).get("blocked_topics") or [])
        if not candidate_id or candidate_id not in candidate_ids:
            requested_send, reason_override = False, "topic_evidence_invalid"
        elif not topic or not why_now or any(
            item and (item.casefold() in topic.casefold() or item.casefold() in _clip(model_value.get("message"), 600).casefold())
            for item in blocked_topics
        ):
            requested_send, reason_override = False, "derived_topic_invalid"
        else:
            emotion = model_value.get("emotion_expression")
            message = _clip(model_value.get("message"), 600)
            visible_emotions = {
                kind
                for kind, pattern in {
                    "gratitude": r"(?:谢谢|感谢|感激|感动)",
                    "joy": r"(?:我|替你).{0,6}(?:开心|高兴|欣慰)",
                    "sadness": r"我.{0,6}(?:难过|难受|伤心)",
                    "concern": r"我.{0,6}(?:担心|放心不下)",
                }.items()
                if re.search(pattern, message)
            }
            emotion_kind = (
                str(emotion.get("kind") or "none").strip().lower()
                if isinstance(emotion, dict)
                else "none"
            )
            if visible_emotions and emotion_kind not in visible_emotions:
                requested_send, reason_override = False, "emotion_evidence_invalid"
            elif emotion_kind != "none":
                user_refs = {
                    str(item.get("id") or item.get("created_at") or ""): _clip(item.get("content"), 4000)
                    for item in (prepared.get("situation") or {}).get("recent_conversation") or []
                    if str(item.get("role") or "") == "user"
                }
                evidence_ref = _clip(emotion.get("evidence_ref"), 160)
                evidence_text = user_refs.get(evidence_ref, "")
                evidence_quote = _clip(emotion.get("evidence_quote"), 80)
                compact_quote = re.sub(r"\s+", "", evidence_quote)
                compact_evidence = re.sub(r"\s+", "", evidence_text)
                compact_message = re.sub(r"\s+", "", message)
                reason = _clip(emotion.get("reason"), 300)
                signal_patterns = {
                    "gratitude": r"(?:帮|替|给|送|做|写|修|改|找|准备|计划|安排|提供|分享|陪|支持|照顾|记得|坚持|努力|花了|攒钱|解决|处理|接入|部署|上线|认真)",
                    "joy": r"(?:成功|完成|通过|做到|解决|好消息|拿到|实现|开心|高兴|进展|上线|发布|恢复|找到了|达成)",
                    "sadness": r"(?:难过|难受|痛|失败|生病|受伤|失去|离开|不舒服|崩|累|睡不)",
                    "concern": r"(?:怕|担心|焦虑|压力|生病|受伤|不舒服|危险|风险|麻烦|问题|睡不)",
                }
                evidence_bigrams = {
                    token[index:index + 2]
                    for token in re.findall(r"[\u3400-\u9fff]{2,}", compact_evidence)
                    for index in range(len(token) - 1)
                }
                reason_bigrams = {
                    token[index:index + 2]
                    for token in re.findall(r"[\u3400-\u9fff]{2,}", re.sub(r"\s+", "", reason))
                    for index in range(len(token) - 1)
                }
                if (
                    not evidence_text
                    or not reason
                    or len(compact_quote) < 4
                    or compact_quote not in compact_evidence
                    or compact_quote not in compact_message
                    or not re.search(signal_patterns.get(emotion_kind, r"(?!)"), evidence_text)
                    or len(evidence_bigrams & reason_bigrams) < 2
                    or not _message_is_grounded(prepared, reason)
                ):
                    requested_send, reason_override = False, "emotion_evidence_invalid"
            if requested_send and independent and not _independent_question_is_safe(topic, message):
                requested_send, reason_override = False, "independent_topic_invalid"
            if requested_send and not independent and not _decision_metadata_is_grounded(prepared, topic, why_now):
                requested_send, reason_override = False, "derived_topic_invalid"
            if requested_send and not independent and not _message_is_grounded(prepared, message):
                requested_send, reason_override = False, "situation_grounding_invalid"
    if not requested_send:
        social = decide_opportunity(conn, opportunity_id, {
            "action": "silent", "reason_code": reason_override or model_value.get("reason") or "default_silent",
            "confidence": model_value.get("confidence", 1.0),
        })
        return {
            **social,
            "action": "skip", "intent": "silence", "reason": social["reason_code"],
            "message": "", "topic_key": "", "next_check_minutes": model_value.get("next_check_minutes", 60),
        }
    emotion = model_value.get("emotion_expression") if isinstance(model_value.get("emotion_expression"), dict) else {}
    emotion_kind = str(emotion.get("kind") or "none").strip().lower()
    emotion_ref = _clip(emotion.get("evidence_ref"), 160)
    emotion_reason = _clip(emotion.get("reason"), 300)
    plan_affect_kind = {
        "gratitude": "happy", "joy": "happy", "sadness": "sad",
        "concern": "comfort", "amusement": "playful",
    }.get(emotion_kind, "neutral")
    # This is a validated plan proposal, not yet a persisted Plan.  The
    # authoritative automation writer may still reject the send because of an
    # intent, review, or cooldown gate.  It persists the Plan only after those
    # final gates accept the same message, preventing orphan `planned` rows.
    interaction_plan = {
        "schema_version": 2,
        "summary_mode": "daily",
        "primary_intent": "chat",
        "confidence": model_value.get("confidence", 0.5),
        "reason": _clip(model_value.get("why_now"), 500),
        "affect": {
            "expression_present": plan_affect_kind != "neutral",
            "kind": plan_affect_kind,
            "confidence": model_value.get("confidence", 0.5),
            "intensity": "medium" if plan_affect_kind != "neutral" else "low",
        },
        "intents": [{
            "id": "intent-1", "type": "chat", "confidence": model_value.get("confidence", 0.5),
            "objective": _clip(model_value.get("topic") or model_value.get("derived_topic"), 240),
            "requires_tools": False, "risk_level": "none",
        }],
        "reply_parts": [{
            "id": "reply-1", "type": "social_ack",
            "text": _clip(model_value.get("message"), 600), "styleable": True,
        }],
        "actions": [{
            "id": "action-1", "type": "respond", "intent_id": "intent-1",
            "objective": "发送本次主动社交表达", "requires_tools": False,
            "risk_level": "none", "depends_on": [],
        }],
        "approval_requests": [], "memory_candidates": [],
        "research": {"subject": "", "deliverable": "none"},
        "delivery": {"mode": "INLINE"},
    }
    # This remains a proposal until the scheduler claim and final access gate
    # are accepted.  ``record_proactive_decision`` finalizes the opportunity
    # and event in one transaction, so a late user message or authorization
    # change cannot leave a false "reply decided" orphan.
    social = normalize_social_decision(conn, opportunity_id, {
        "action": "reply", "reason_code": model_value.get("reason") or "authorized_topic_now",
        "why_now": model_value.get("why_now"), "topic_candidate_id": model_value.get("topic_candidate_id"),
        "approach": model_value.get("approach"), "confidence": model_value.get("confidence", 0.5),
        "meme_intent": model_value.get("meme_intent"),
    })
    evidence_id = next(
        str(item.get("source_id") or "")
        for item in prepared["candidates"]
        if str(item.get("id") or "") == str(social.get("topic_candidate_id") or "")
    )
    # One unchanged evidence situation may yield only one proactive start per
    # cooldown.  A model cannot evade that limit by renaming or paraphrasing
    # its derived topic while the user evidence is unchanged.
    chosen = next(
        item for item in prepared["candidates"]
        if str(item.get("id") or "") == str(social.get("topic_candidate_id") or "")
    )
    independent = (
        prepared.get("subject_type") == "private_user"
        and str(chosen.get("source_id") or "").startswith("independent:")
    )
    topic_key = hashlib.sha256(
        (
            f"independent|{prepared['subject_id']}|{_clip(model_value.get('topic'), 240)}"
            if independent else evidence_id
        ).encode("utf-8")
    ).hexdigest()
    return {
        **social,
        "action": "send", "intent": _clip(model_value.get("intent"), 40),
        "reason": social["reason_code"], "message": _clip(model_value.get("message"), 600),
        "topic_key": topic_key,
        "next_check_minutes": model_value.get("next_check_minutes", 60),
        "topic_origin": "independent_curiosity" if independent else "agent_derived",
        "interaction_plan": interaction_plan,
        "derived_topic": _clip(model_value.get("topic") or model_value.get("derived_topic"), 240),
        "emotion_expression": model_value.get("emotion_expression") if isinstance(model_value.get("emotion_expression"), dict) else {},
        "evidence_snapshot": {
            "source_refs": list((prepared.get("situation") or {}).get("source_refs") or []),
            "waiting_state": str((prepared.get("situation") or {}).get("waiting_state") or "open"),
            "topic_origin": "independent_curiosity" if independent else "agent_derived",
            "affect_expression": {
                "kind": emotion_kind,
                "evidence_ref": emotion_ref,
                "evidence_grounded": emotion_kind != "none",
                "reason_sha256": (
                    hashlib.sha256(emotion_reason.encode("utf-8")).hexdigest()
                    if emotion_reason
                    else ""
                ),
            },
        },
    }


def finalize_start_failure(
    conn: sqlite3.Connection,
    prepared: dict,
    error_kind: str,
) -> dict:
    """Close a fail-closed start opportunity when no model decision exists."""

    return decide_opportunity(
        conn,
        prepared["opportunity"]["id"],
        {
            "action": "silent",
            "reason_code": f"decision_unavailable:{_clip(error_kind, 80)}",
            "confidence": 0,
        },
    )


def reconcile_stale_start_opportunities(
    conn: sqlite3.Connection,
    *,
    max_age_minutes: int = 30,
) -> int:
    """Close opportunities orphaned by a crash or an older failed runtime."""

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=max(5, min(int(max_age_minutes or 30), 1440)))
    ).isoformat()
    rows = conn.execute(
        """
        SELECT id FROM social_opportunities
        WHERE kind='start' AND status='open' AND created_at<=?
        ORDER BY created_at
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        decide_opportunity(
            conn,
            str(row[0]),
            {
                "action": "silent",
                "reason_code": "decision_unavailable:stale_open",
                "confidence": 0,
            },
        )
    return len(rows)


__all__ = [
    "finalize_start_decision",
    "finalize_start_failure",
    "prepare_start_opportunity",
    "owner_social_start_preflight",
    "reconcile_stale_start_opportunities",
]
