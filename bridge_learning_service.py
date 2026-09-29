#!/usr/bin/env python3
"""Policy-driven learning signals, candidates, applications and trace."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

from bridge_assistant_identity import current_assistant
from bridge_assistant_identity_schema import DEFAULT_OWNER_ACTOR_ID
from bridge_learning_schema import (
    LEARNING_FEATURE_FLAG,
    LOW_RISK_LEARNING_FEATURE_FLAG,
    OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG,
)
from bridge_migrations import utc_now


SENSITIVE_RE = re.compile(
    r"(?i)(api[_ -]?key|token|password|passwd|secret|private[_ -]?key|cookie|"
    r"access[_ -]?key|sk-[A-Za-z0-9_-]{8,})",
)
PRIVATE_SENSITIVE_TOPIC_RE = re.compile(
    r"(?:抑郁|焦虑症|诊断|药物|病史|收入|负债|银行卡|住址|家庭地址|政治立场|宗教|性取向|身份证)"
)
REPORTED_TOPIC_RE = re.compile(r"(?:朋友|同事|他|她|别人|原文|引用).{0,8}(?:说|写|表示)|[“‘\"]")
LOW_RISK_EXPRESSION_TYPES = {"prefer", "avoid"}

# This is an admission policy, not a list of things the model may infer. A
# signal may be operationally useful without becoming a preference, memory or
# authority change.
LEARNING_ADMISSION_POLICY = {
    "explicit_expression_feedback": {
        "admission": "candidate",
        "label": "明确表达偏好",
        "private": "私聊中的明确低风险表达偏好可限时试用。",
        "group": "仅已授权 Owner 的群内明确纠正可形成待确认候选。",
    },
    "delivery_outcome": {
        "admission": "diagnostic",
        "label": "送达与可靠性观察",
        "reason": "只用于诊断 Delivery/ACK 链路，不推断表达、人格、权限或能力。",
    },
    "execution_outcome": {
        "admission": "diagnostic",
        "label": "执行结果观察",
        "reason": "只用于诊断 Run/Task 可靠性，不能自动改变 Skill、Capability、Approval 或 Network。",
    },
    "goal_outcome_feedback": {
        "admission": "review",
        "label": "目标结果反馈",
        "reason": "仅作为 Goal/Outcome 复盘线索；当前没有自动应用器。",
    },
}

LEARNING_NEVER_AUTOMATIC = (
    "敏感或无来源事实、群聊记忆、知识库、关系状态、权限、审批、网络、模型凭据、Skill与代码"
)


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json(value: object) -> str:
    return json.dumps(value if isinstance(value, (dict, list)) else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _active_assistant(conn: sqlite3.Connection) -> tuple[str, str]:
    assistant = current_assistant(conn)
    if not assistant:
        raise ValueError("active_assistant_required")
    return str(assistant["id"]), str(assistant.get("owner_actor_id") or DEFAULT_OWNER_ACTOR_ID)


def _flag(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (name,),
    ).fetchone()
    return bool(row and int(row[0]))


def learning_feature_enabled(conn: sqlite3.Connection) -> bool:
    return _flag(conn, LEARNING_FEATURE_FLAG)


def low_risk_learning_enabled(conn: sqlite3.Connection) -> bool:
    return learning_feature_enabled(conn) and _flag(conn, LOW_RISK_LEARNING_FEATURE_FLAG)


def owner_group_expression_feedback_enabled(conn: sqlite3.Connection) -> bool:
    return learning_feature_enabled(conn) and _flag(conn, OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG)


def set_learning_flags(
    conn: sqlite3.Connection,
    *,
    enabled: bool | None = None,
    low_risk: bool | None = None,
    owner_group_expression_feedback: bool | None = None,
) -> dict:
    now = utc_now()
    values = {
        LEARNING_FEATURE_FLAG: enabled,
        LOW_RISK_LEARNING_FEATURE_FLAG: low_risk,
        OWNER_GROUP_EXPRESSION_FEEDBACK_FEATURE_FLAG: owner_group_expression_feedback,
    }
    for name, value in values.items():
        if value is not None:
            conn.execute(
                """
                INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
                """,
                (name, 1 if value else 0, now),
            )
    return {
        "learning_enabled": learning_feature_enabled(conn),
        "low_risk_enabled": low_risk_learning_enabled(conn),
        "owner_group_expression_feedback_enabled": owner_group_expression_feedback_enabled(conn),
    }


def _sensitivity(message: str, requested: str = "") -> str:
    if requested in {"private", "sensitive"}:
        return requested
    return "sensitive" if SENSITIVE_RE.search(message or "") else "normal"


def _idempotency(*parts: object) -> str:
    raw = "\0".join(str(item or "") for item in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_learning_signal(
    conn: sqlite3.Connection,
    *,
    actor_ref: str,
    channel_type: str,
    thread_id: str,
    group_id: str = "",
    source_message_id: str = "",
    signal_type: str,
    domain: str,
    payload: Mapping[str, object] | None = None,
    confidence: float = 0.5,
    consent_basis: str = "inferred",
    message_for_sensitivity: str = "",
    idempotency_key: str = "",
) -> dict:
    assistant_id, _ = _active_assistant(conn)
    key = _clip(idempotency_key or _idempotency(
        assistant_id, actor_ref, channel_type, thread_id, source_message_id, signal_type, domain,
    ), 128)
    sensitivity = _sensitivity(message_for_sensitivity, str((payload or {}).get("sensitivity") or ""))
    signal_id = "learning-signal-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    conn.execute(
        """
        INSERT OR IGNORE INTO learning_signals(
            id,assistant_id,actor_ref,channel_type,thread_id,group_id,source_message_id,
            signal_type,domain,payload_json,confidence,sensitivity,consent_basis,idempotency_key,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            signal_id, assistant_id, _clip(actor_ref, 160), _clip(channel_type, 40),
            _clip(thread_id, 160), _clip(group_id, 160), _clip(source_message_id, 160),
            _clip(signal_type, 80), _clip(domain, 80), _json(dict(payload or {})),
            max(0.0, min(float(confidence), 1.0)), sensitivity, _clip(consent_basis, 60),
            key, utc_now(),
        ),
    )
    return dict(conn.execute("SELECT * FROM learning_signals WHERE id=?", (signal_id,)).fetchone())


def _conflict(conn: sqlite3.Connection, assistant_id: str, subject_type: str, subject_id: str, domain: str, key: str, value: dict) -> str:
    rows = conn.execute(
        """
        SELECT id,value_json FROM learning_candidates
        WHERE assistant_id=? AND subject_type=? AND subject_id=? AND domain=?
          AND candidate_key<>? AND status IN ('trial','stable','needs_confirmation','conflicted')
        """,
        (assistant_id, subject_type, subject_id, domain, key),
    ).fetchall()
    for row in rows:
        try:
            old = json.loads(str(row["value_json"] or "{}"))
        except json.JSONDecodeError:
            old = {}
        if old.get("feedback_type") != value.get("feedback_type"):
            return str(row["id"])
    return ""


def capture_expression_candidate(
    conn: sqlite3.Connection,
    *,
    message: str,
    user_id: str,
    thread_id: str = "",
    source_message_id: str = "",
    group: Mapping[str, object] | None = None,
    allow_group_feedback: bool = False,
) -> dict | None:
    """Capture explicit expression feedback without storing the raw message."""

    text = " ".join(str(message or "").split()).strip()
    if not learning_feature_enabled(conn) or not text or _sensitivity(text) == "sensitive":
        return None
    from bridge_social_experience import detect_expression_feedback

    detected = detect_expression_feedback(text)
    if not detected or detected.get("feedback_type") not in LOW_RISK_EXPRESSION_TYPES:
        return None
    group_info = dict(group or {})
    group_id = _clip(group_info.get("group_id"), 160)
    sender_id = _clip(group_info.get("sender_id") or user_id, 160)
    # A normal group member must never create a private cross-channel trial by
    # commenting in a group. Group-wide change is opt-in, Owner-authorized and
    # always confirmation-gated.
    if group_id and not allow_group_feedback:
        return None
    if group_id and allow_group_feedback:
        subject_type, subject_id, scope_type, scope_id = "qq_group", group_id, "qq_group", "group:" + group_id
        risk, consent = "medium", "requires_owner_confirmation"
    else:
        subject_type, subject_id, scope_type, scope_id = "private_user", sender_id, "private_user", "user:" + sender_id
        risk, consent = "low", "explicit_user_feedback"
    value = {
        "feedback_type": str(detected["feedback_type"]),
        "preference_code": _clip(detected.get("preference_code"), 80),
        "style": _clip(detected.get("style"), 1000),
        "source_digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }
    signal = record_learning_signal(
        conn,
        actor_ref=sender_id,
        channel_type="qq_group" if group_id else "qq_private",
        thread_id=thread_id,
        group_id=group_id,
        source_message_id=source_message_id,
        signal_type="explicit_expression_feedback",
        domain="expression",
        payload={"feedback_type": value["feedback_type"]},
        confidence=float(detected.get("confidence") or 0.95),
        consent_basis=consent,
        message_for_sensitivity=text,
    )
    assistant_id, owner_actor_id = _active_assistant(conn)
    style_key = hashlib.sha256(str(value["style"]).encode("utf-8")).hexdigest()[:12]
    key = "expression:" + value["feedback_type"] + ":" + style_key
    conflict_with = _conflict(conn, assistant_id, subject_type, subject_id, "expression", key, value)
    status = "conflicted" if conflict_with else (
        "needs_confirmation" if risk != "low" else (
            "trial" if low_risk_learning_enabled(conn) else "candidate"
        )
    )
    now = utc_now()
    candidate_id = "learning-candidate-" + uuid.uuid4().hex
    existing = conn.execute(
        """
        SELECT * FROM learning_candidates
        WHERE assistant_id=? AND subject_type=? AND subject_id=? AND domain=? AND candidate_key=?
          AND status NOT IN ('rejected','expired','superseded')
        """,
        (assistant_id, subject_type, subject_id, "expression", key),
    ).fetchone()
    if existing and str(existing["source_signal_id"] or "") == str(signal["id"]):
        return dict(existing)
    if existing:
        candidate_id = str(existing["id"])
        conn.execute(
            """
            UPDATE learning_candidates
            SET value_json=?,status=?,risk_level=?,confidence=max(confidence,?),
                evidence_count=evidence_count+1,source_signal_id=?,conflict_with=?,updated_at=?
            WHERE id=?
            """,
            (_json(value), status, risk, float(detected.get("confidence") or 0.95),
             signal["id"], conflict_with, now, candidate_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO learning_candidates(
                id,assistant_id,owner_actor_id,subject_type,subject_id,scope_type,scope_id,
                domain,candidate_key,value_json,status,risk_level,confidence,evidence_count,
                source_signal_id,conflict_with,supersedes_id,trial_expires_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate_id, assistant_id, owner_actor_id, subject_type, subject_id,
                scope_type, scope_id, "expression", key, _json(value), status, risk,
                float(detected.get("confidence") or 0.95), 1, signal["id"], conflict_with, "",
                (datetime.now(timezone.utc) + timedelta(days=30)).isoformat() if status == "trial" else "",
                now, now,
            ),
        )
    candidate = dict(conn.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate_id,)).fetchone())
    if candidate["status"] == "trial" and not conflict_with:
        apply_learning_candidate(conn, candidate_id, reason="low_risk_expression_trial")
    return dict(conn.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate_id,)).fetchone())


def capture_owner_group_expression_candidate(
    conn: sqlite3.Connection,
    *,
    message: str,
    owner_authorized: bool,
    owner_actor_id: str,
    group_id: str,
    thread_id: str,
    source_message_id: str,
) -> dict | None:
    """Capture an Owner group correction before natural reply selection.

    A direct Owner correction must not be lost merely because this same group
    turn later becomes silent. The candidate is exact-group scoped, medium risk
    and never auto-applied.
    """

    if not owner_authorized or not owner_group_expression_feedback_enabled(conn):
        return None
    return capture_expression_candidate(
        conn,
        message=message,
        user_id=owner_actor_id,
        thread_id=thread_id,
        source_message_id=source_message_id,
        group={"group_id": group_id, "sender_id": owner_actor_id},
        allow_group_feedback=True,
    )


def _private_topic_phrase(content: str) -> tuple[str, str, bool]:
    """Admit only narrow source-backed self-statements or repeated praise."""
    text = str(content or "").strip()
    match = re.fullmatch(r"我(不喜欢|喜欢|更喜欢|偏好)([^，。！？!?；;]{2,40})", text)
    if match:
        return ("avoid" if match.group(1) == "不喜欢" else "prefer", match.group(2).strip(), True)
    match = re.fullmatch(r"([^，。！？!?；;]{2,40})(?:不错|挺好|很有意思|真有意思)", text)
    if match and not any(token in match.group(1) for token in ("他", "她", "朋友", "别人", "听说")):
        return "prefer", match.group(1).strip(), False
    return "", "", False


def capture_private_topic_signal(conn: sqlite3.Connection, memory_candidate_id: str) -> dict | None:
    """Two distinct human sources, not two deliveries, authorize implicit trial."""
    if not low_risk_learning_enabled(conn):
        return None
    row = conn.execute(
        """SELECT c.*,t.channel_type,t.external_thread_ref
           FROM memory_candidates AS c JOIN conversation_threads AS t ON t.id=c.source_thread_id
           WHERE c.id=? AND c.scope_type='thread' AND c.kind='preference'
             AND t.channel_type='qq_private'""", (memory_candidate_id,),
    ).fetchone()
    if not row or row["status"] not in {"accepted", "pending", "merged"} or not row["source_message_id"]:
        return None
    source = conn.execute(
        """SELECT id,content,actor_ref FROM conversation_messages
           WHERE id=? AND thread_id=? AND role='user'""",
        (row["source_message_id"], row["source_thread_id"]),
    ).fetchone()
    if not source or (source["actor_ref"] and str(source["actor_ref"]) != str(row["subject_actor_ref"])):
        return None
    content = str(row["content"] or "")
    source_text = str(source["content"] or "")
    if source_text.count(content) != 1 or _sensitivity(content) == "sensitive" or PRIVATE_SENSITIVE_TOPIC_RE.search(content):
        return None
    source_offset = source_text.index(content)
    if REPORTED_TOPIC_RE.search(source_text[max(0, source_offset - 40):source_offset]):
        return None
    mode, topic, explicit = _private_topic_phrase(content)
    if not topic or len(topic) > 40:
        return None
    canonical_id = str(row["duplicate_of"] or row["id"])
    assistant_id, owner_actor_id = _active_assistant(conn)
    user_id = str(row["external_thread_ref"])
    topic_digest = hashlib.sha256(f"{mode}\0{topic}".encode("utf-8")).hexdigest()
    signal = record_learning_signal(
        conn, actor_ref=user_id, channel_type="qq_private",
        thread_id=str(row["source_thread_id"]), source_message_id=str(source["id"]),
        signal_type="private_topic_content", domain="topic_direction",
        payload={"topic_digest": topic_digest, "source_candidate_id": canonical_id,
                 "explicit": explicit},
        confidence=0.95 if explicit else 0.72,
        consent_basis="explicit_self_preference" if explicit else "repeated_positive_content",
        message_for_sensitivity=content,
    )
    signals = conn.execute(
        """SELECT source_message_id,payload_json FROM learning_signals
           WHERE assistant_id=? AND actor_ref=? AND channel_type='qq_private'
             AND domain='topic_direction' AND signal_type='private_topic_content'""",
        (assistant_id, user_id),
    ).fetchall()
    independent = set()
    for item in signals:
        source_id = str(item["source_message_id"] or "")
        if not source_id or json.loads(str(item["payload_json"] or "{}")).get("topic_digest") != topic_digest:
            continue
        source_candidates = conn.execute(
            """SELECT status,content,conflict_with FROM memory_candidates
               WHERE assistant_id=? AND scope_type='thread' AND subject_actor_ref=?
                 AND source_message_id=?""",
            (assistant_id, str(row["subject_actor_ref"]), source_id),
        ).fetchall()
        if any(
            candidate["status"] in {"pending", "accepted", "merged"}
            and not candidate["conflict_with"]
            and _private_topic_phrase(str(candidate["content"] or ""))[:2] == (mode, topic)
            and (
                candidate["status"] != "accepted"
                or conn.execute(
                    """SELECT 1 FROM memory_records WHERE assistant_id=? AND status='active'
                       AND source_message_id=? AND content=? LIMIT 1""",
                    (assistant_id, source_id, candidate["content"]),
                ).fetchone()
            )
            for candidate in source_candidates
        ):
            independent.add(source_id)
    evidence_count = len(independent)
    key = "topic:" + topic_digest[:24]
    existing = conn.execute(
        """SELECT * FROM learning_candidates WHERE assistant_id=? AND subject_type='private_user'
           AND subject_id=? AND domain='topic_direction' AND candidate_key=?
           AND status NOT IN ('rejected','expired','superseded') ORDER BY updated_at DESC LIMIT 1""",
        (assistant_id, user_id, key),
    ).fetchone()
    if existing and existing["status"] in {"trial", "stable"}:
        return dict(existing)
    if row["conflict_with"]:
        status = "needs_confirmation"
    else:
        status = "trial" if explicit or evidence_count >= 2 else "candidate"
    value = {"mode": mode, "topic_digest": topic_digest, "memory_candidate_id": canonical_id}
    now = utc_now()
    candidate_id = str(existing["id"]) if existing else "learning-candidate-" + uuid.uuid4().hex
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat() if status == "trial" else ""
    if existing:
        conn.execute(
            """UPDATE learning_candidates SET value_json=?,status=?,evidence_count=?,
               source_signal_id=?,trial_expires_at=?,updated_at=? WHERE id=?""",
            (_json(value), status, evidence_count, signal["id"], expiry, now, candidate_id),
        )
    else:
        conn.execute(
            """INSERT INTO learning_candidates(
               id,assistant_id,owner_actor_id,subject_type,subject_id,scope_type,scope_id,
               domain,candidate_key,value_json,status,risk_level,confidence,evidence_count,
               source_signal_id,conflict_with,supersedes_id,trial_expires_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_id, assistant_id, owner_actor_id, "private_user", user_id,
             "private_user", "user:" + user_id, "topic_direction", key, _json(value),
             status, "low", 0.95 if explicit else 0.72, evidence_count,
             signal["id"], str(row["conflict_with"] or ""), "", expiry, now, now),
        )
    if status == "trial":
        apply_learning_candidate(conn, candidate_id, reason="source_backed_private_topic_trial")
    return dict(conn.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate_id,)).fetchone())


def active_private_topic_constraints(conn: sqlite3.Connection, *, user_id: str) -> dict[str, list[str]]:
    """Read only live, scoped trial applications; corrections disappear at once."""
    assistant_id, _ = _active_assistant(conn)
    now = utc_now()
    rows = conn.execute(
        """SELECT a.applied_value_json,c.trial_expires_at
           FROM learning_applications AS a JOIN learning_candidates AS c ON c.id=a.candidate_id
           WHERE a.assistant_id=? AND c.subject_type='private_user' AND c.subject_id=?
             AND c.domain='topic_direction' AND c.status IN ('trial','stable')
             AND a.target_type='topic_direction' AND a.status IN ('trial','accepted')""",
        (assistant_id, str(user_id)),
    ).fetchall()
    result: dict[str, list[str]] = {"prefer": [], "avoid": []}
    for item in rows:
        if item["trial_expires_at"] and str(item["trial_expires_at"]) < now:
            continue
        try:
            value = json.loads(str(item["applied_value_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        source_candidate = conn.execute(
            "SELECT status,content,source_message_id FROM memory_candidates WHERE id=?",
            (str(value.get("memory_candidate_id") or ""),),
        ).fetchone()
        if not source_candidate or source_candidate["status"] not in {"accepted", "pending"}:
            continue
        if source_candidate["status"] == "accepted":
            live_memory = conn.execute(
                """SELECT 1 FROM memory_records WHERE assistant_id=? AND status='active'
                   AND source_message_id=? AND content=? LIMIT 1""",
                (assistant_id, source_candidate["source_message_id"], source_candidate["content"]),
            ).fetchone()
            if not live_memory:
                continue
        mode, topic, _ = _private_topic_phrase(str(source_candidate["content"] or ""))
        if mode in result and mode == value.get("mode") and topic and topic not in result[mode]:
            result[mode].append(topic)
    return {key: values[:6] for key, values in result.items()}


def active_private_topic_preferences(conn: sqlite3.Connection, *, user_id: str) -> list[str]:
    return active_private_topic_constraints(conn, user_id=user_id)["prefer"]


def apply_learning_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    reason: str = "",
    confirmed_by_owner: bool = False,
) -> dict:
    row = conn.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("learning_candidate_not_found")
    if row["status"] not in {"trial", "stable"}:
        raise ValueError("learning_candidate_not_applicable")
    owner_confirmable_group_expression = (
        bool(confirmed_by_owner)
        and str(row["risk_level"]) == "medium"
        and str(row["domain"]) == "expression"
        and str(row["subject_type"]) == "qq_group"
    )
    if row["risk_level"] != "low" and not owner_confirmable_group_expression:
        raise ValueError("learning_candidate_requires_confirmation")
    existing_application = conn.execute(
        """
        SELECT * FROM learning_applications
        WHERE candidate_id=? AND status IN ('trial','accepted')
        ORDER BY applied_at DESC LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()
    if existing_application:
        return {"candidate": dict(row), "application": dict(existing_application)}
    value = json.loads(str(row["value_json"] or "{}"))
    target_id = "learning-" + hashlib.sha256(
        f"{row['subject_type']}\0{row['subject_id']}\0{row['candidate_key']}".encode("utf-8"),
    ).hexdigest()[:20]
    if row["domain"] == "topic_direction" and row["subject_type"] == "private_user":
        target_type, previous_value, applied_value = "topic_direction", {}, value
    elif row["domain"] == "expression":
        previous = conn.execute("SELECT * FROM expression_habits WHERE id=?", (target_id,)).fetchone()
        previous_value = dict(previous) if previous else {}
        from bridge_social_experience import upsert_expression_habit

        applied_value = upsert_expression_habit(
            conn,
            {
                "id": target_id,
                "situation": "由对话反馈形成的表达偏好",
                "cues": "",
                "style": value.get("style") or "",
                "scope": "daily" if row["subject_type"] == "private_user" else "group",
                "subject_type": row["subject_type"],
                "subject_id": row["subject_id"],
                "origin": "learning_trial",
                "confidence": row["confidence"],
                "priority": 16,
                "enabled": 1,
            },
        )
        target_type = "expression_habit"
    else:
        raise ValueError("learning_candidate_target_invalid")
    app_id = "learning-application-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO learning_applications(
            id,candidate_id,assistant_id,target_type,target_id,previous_value_json,
            applied_value_json,status,reason,applied_at,reverted_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            app_id, row["id"], row["assistant_id"], target_type, target_id,
            _json(previous_value), _json(applied_value),
            "accepted" if owner_confirmable_group_expression else "trial",
            _clip(reason, 120), utc_now(), "",
        ),
    )
    return {"candidate": dict(row), "application": dict(conn.execute("SELECT * FROM learning_applications WHERE id=?", (app_id,)).fetchone())}


def record_learning_feedback(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    feedback_type: str,
    actor_ref: str = "owner",
    note: str = "",
    idempotency_key: str = "",
) -> dict:
    feedback_type = _clip(feedback_type, 20).lower()
    if feedback_type not in {"accept", "reject", "undo", "correct"}:
        raise ValueError("invalid_learning_feedback")
    row = conn.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("learning_candidate_not_found")
    key = _clip(idempotency_key or _idempotency(candidate_id, feedback_type, note), 128)
    existing = conn.execute(
        "SELECT * FROM learning_feedback WHERE candidate_id=? AND idempotency_key=?",
        (candidate_id, key),
    ).fetchone()
    if existing:
        return {"candidate": dict(row), "feedback": dict(existing)}
    app = conn.execute(
        "SELECT * FROM learning_applications WHERE candidate_id=? ORDER BY applied_at DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if feedback_type in {"undo", "reject", "correct"} and app:
        target_id = str(app["target_id"])
        previous = json.loads(str(app["previous_value_json"] or "{}"))
        if app["target_type"] == "expression_habit":
            if previous:
                from bridge_social_experience import upsert_expression_habit
                upsert_expression_habit(conn, previous)
            else:
                conn.execute("UPDATE expression_habits SET enabled=0,updated_at=? WHERE id=?", (utc_now(), target_id))
        conn.execute(
            "UPDATE learning_applications SET status='reverted',reverted_at=? WHERE id=?",
            (utc_now(), app["id"]),
        )
    status = "stable" if feedback_type == "accept" else "rejected" if feedback_type in {"reject", "undo"} else "needs_confirmation"
    conn.execute(
        "UPDATE learning_candidates SET status=?,trial_expires_at=CASE WHEN ?='stable' THEN '' ELSE trial_expires_at END,updated_at=? WHERE id=?",
        (status, status, utc_now(), candidate_id),
    )
    if feedback_type == "accept" and app and app["status"] == "trial":
        conn.execute("UPDATE learning_applications SET status='accepted' WHERE id=?", (app["id"],))
    if feedback_type == "accept" and str(row["risk_level"]) == "medium":
        apply_learning_candidate(
            conn,
            candidate_id,
            reason="owner_confirmed_group_expression",
            confirmed_by_owner=True,
        )
        app = conn.execute(
            "SELECT * FROM learning_applications WHERE candidate_id=? ORDER BY applied_at DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
    feedback_id = "learning-feedback-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO learning_feedback(id,candidate_id,application_id,feedback_type,actor_ref,note,idempotency_key,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (feedback_id, candidate_id, str(app["id"]) if app else "", feedback_type, _clip(actor_ref, 160), _clip(note, 500), key, utc_now()),
    )
    return {
        "candidate": dict(conn.execute("SELECT * FROM learning_candidates WHERE id=?", (candidate_id,)).fetchone()),
        "feedback": dict(conn.execute("SELECT * FROM learning_feedback WHERE id=?", (feedback_id,)).fetchone()),
    }


def revoke_private_topic_applications_for_source(conn: sqlite3.Connection, source_message_id: str) -> int:
    """Invalidate trials when their exact memory source is corrected or deleted."""
    affected = set()
    for signal in conn.execute(
        """SELECT assistant_id,actor_ref,payload_json FROM learning_signals
           WHERE source_message_id=? AND signal_type='private_topic_content'
             AND domain='topic_direction'""",
        (source_message_id,),
    ).fetchall():
        try:
            digest = json.loads(str(signal["payload_json"] or "{}")).get("topic_digest")
        except json.JSONDecodeError:
            continue
        if digest:
            affected.add((str(signal["assistant_id"]), str(signal["actor_ref"]), str(digest)))
    rows = conn.execute(
        """SELECT a.id,a.applied_value_json,c.id AS candidate_id,
                  c.assistant_id,c.subject_id FROM learning_applications AS a
           JOIN learning_candidates AS c ON c.id=a.candidate_id
           WHERE a.target_type='topic_direction' AND a.status IN ('trial','accepted')
             AND c.domain='topic_direction'""",
    ).fetchall()
    count = 0
    for item in rows:
        value = json.loads(str(item["applied_value_json"] or "{}"))
        if (str(item["assistant_id"]), str(item["subject_id"]),
            str(value.get("topic_digest") or "")) in affected:
            conn.execute(
                "UPDATE learning_applications SET status='reverted',reverted_at=? WHERE id=?",
                (utc_now(), item["id"]),
            )
            conn.execute(
                """UPDATE learning_candidates SET status='needs_confirmation',updated_at=?
                   WHERE id=? AND status IN ('trial','stable')""",
                (utc_now(), item["candidate_id"]),
            )
            count += 1
    return count


def record_context_trace(
    conn: sqlite3.Connection,
    *,
    thread_id: str = "",
    message_id: str = "",
    domain: str,
    source_type: str,
    source_id: str = "",
    decision: str,
    detail: Mapping[str, object] | None = None,
) -> dict:
    # This prompt-side trace records an observation, not a learning admission.
    # Check its Assistant identity without scanning unrelated chat-history FKs;
    # the runtime connection enforces the trace row's Assistant FK on INSERT.
    assistant = current_assistant(conn, integrity_scope="identity")
    if not assistant:
        raise ValueError("active_assistant_required")
    assistant_id = str(assistant["id"])
    trace_id = "learning-trace-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO learning_context_trace(
            id,assistant_id,thread_id,message_id,domain,source_type,source_id,decision,detail_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trace_id, assistant_id, _clip(thread_id, 160), _clip(message_id, 160),
            _clip(domain, 80), _clip(source_type, 80), _clip(source_id, 160),
            _clip(decision, 80), _json(dict(detail or {})), utc_now(),
        ),
    )
    return dict(conn.execute("SELECT * FROM learning_context_trace WHERE id=?", (trace_id,)).fetchone())


def learning_summary(conn: sqlite3.Connection, *, limit: int = 50) -> dict:
    assistant_id, _ = _active_assistant(conn)
    bounded = max(1, min(int(limit or 50), 200))
    counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status,count(*) AS count FROM learning_candidates WHERE assistant_id=? GROUP BY status",
            (assistant_id,),
        )
    }
    signal_counts = {
        str(row["signal_type"]): int(row["count"])
        for row in conn.execute(
            "SELECT signal_type,count(*) AS count FROM learning_signals WHERE assistant_id=? GROUP BY signal_type",
            (assistant_id,),
        )
    }
    signal_total = sum(signal_counts.values())
    signal_counts_by_admission: dict[str, int] = {
        "candidate": 0,
        "diagnostic": 0,
        "review": 0,
    }
    for signal_type, count in signal_counts.items():
        admission = str(
            LEARNING_ADMISSION_POLICY.get(signal_type, {"admission": "diagnostic"}).get("admission"),
        )
        signal_counts_by_admission[admission] = signal_counts_by_admission.get(admission, 0) + count
    application_total = int(conn.execute(
        "SELECT count(*) FROM learning_applications WHERE assistant_id=?",
        (assistant_id,),
    ).fetchone()[0])
    feedback_total = int(conn.execute(
        """
        SELECT count(*) FROM learning_feedback f
        JOIN learning_candidates c ON c.id=f.candidate_id
        WHERE c.assistant_id=?
        """,
        (assistant_id,),
    ).fetchone()[0])
    candidates = [
        dict(row) for row in conn.execute(
            "SELECT * FROM learning_candidates WHERE assistant_id=? ORDER BY updated_at DESC LIMIT ?",
            (assistant_id, bounded),
        ).fetchall()
    ]
    for item in candidates:
        item["value"] = json.loads(str(item.pop("value_json") or "{}"))
    return {
        "feature_enabled": learning_feature_enabled(conn),
        "low_risk_enabled": low_risk_learning_enabled(conn),
        "owner_group_expression_feedback_enabled": owner_group_expression_feedback_enabled(conn),
        "counts": {
            **counts,
            "signals_total": signal_total,
            "applications_total": application_total,
            "feedback_total": feedback_total,
        },
        "signal_counts": signal_counts,
        "signal_counts_by_admission": signal_counts_by_admission,
        "candidate_policy": {
            "eligible_signal_type": "explicit_expression_feedback",
            "owner_group_feedback_enabled": owner_group_expression_feedback_enabled(conn),
            "never_automatic": LEARNING_NEVER_AUTOMATIC,
            "automatic_scope": "私聊中明确提出的低风险表达偏好",
            "group_scope": "群聊表达偏好须 Owner 确认，不能自动试用",
            "operational_signals": "送达、可靠性与系统观察仅用于诊断，不会自动改写表达或能力",
        },
        "policy_catalog": LEARNING_ADMISSION_POLICY,
        "never_automatic": LEARNING_NEVER_AUTOMATIC,
        "candidates": candidates,
    }


def list_learning_trace(conn: sqlite3.Connection, *, thread_id: str = "", limit: int = 50) -> list[dict]:
    assistant_id, _ = _active_assistant(conn)
    params: list[object] = [assistant_id]
    where = "assistant_id=?"
    if thread_id:
        where += " AND thread_id=?"
        params.append(_clip(thread_id, 160))
    params.append(max(1, min(int(limit or 50), 200)))
    rows = conn.execute(
        f"SELECT * FROM learning_context_trace WHERE {where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = json.loads(str(item.pop("detail_json") or "{}"))
        result.append(item)
    return result


__all__ = [
    "apply_learning_candidate",
    "capture_owner_group_expression_candidate",
    "capture_expression_candidate",
    "learning_feature_enabled",
    "learning_summary",
    "list_learning_trace",
    "low_risk_learning_enabled",
    "owner_group_expression_feedback_enabled",
    "record_context_trace",
    "record_learning_feedback",
    "record_learning_signal",
    "set_learning_flags",
]
