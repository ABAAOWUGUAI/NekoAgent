#!/usr/bin/env python3
"""Unified auditable contract for reply, group join, and social start."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from bridge_social_virtual_schema import SOCIAL_OPPORTUNITY_FEATURE_FLAG


KINDS = {"reply", "join", "start"}
APPROACHES = {"continue", "share", "ask", "check_in", "celebrate", "remind", "light_join", "inform"}
MEME_INTENTS = {"none", "optional", "strong"}
FEEDBACK_SIGNALS = {"replied", "ignored", "corrected", "muted", "delivery_failed", "ambiguous"}
SOURCE_TYPES = {
    "inbound_message", "follow_up", "reminder", "project", "goal", "conversation",
    "memory", "knowledge", "relationship", "virtual_life",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: object, fallback):
    try:
        result = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return result


def social_opportunity_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (SOCIAL_OPPORTUNITY_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0] or 0))


def set_social_opportunity_feature(conn: sqlite3.Connection, enabled: bool) -> dict:
    now = utc_now()
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
        (1 if enabled else 0, now, SOCIAL_OPPORTUNITY_FEATURE_FLAG),
    )
    if not conn.execute("SELECT changes()").fetchone()[0]:
        raise ValueError("social_opportunity_schema_unavailable")
    return {"feature": SOCIAL_OPPORTUNITY_FEATURE_FLAG, "enabled": bool(enabled), "updated_at": now}


def create_opportunity(
    conn: sqlite3.Connection,
    *,
    assistant_id: str,
    kind: str,
    subject_type: str,
    subject_id: str,
    thread_id: str = "",
    trigger_type: str,
    trigger_ref: str = "",
    policy_snapshot: dict | None = None,
    relationship_version: int = 0,
    expires_at: str = "",
    opportunity_id: str = "",
) -> dict:
    kind = _clip(kind, 16)
    if kind not in KINDS:
        raise ValueError("social_opportunity_kind_invalid")
    assistant_id = _clip(assistant_id, 80)
    subject_type = _clip(subject_type, 24)
    subject_id = _clip(subject_id, 160)
    trigger_type = _clip(trigger_type, 60)
    if not assistant_id or not subject_id or not trigger_type:
        raise ValueError("social_opportunity_identity_required")
    item_id = _clip(opportunity_id, 80) or uuid.uuid4().hex
    existing = conn.execute("SELECT * FROM social_opportunities WHERE id=?", (item_id,)).fetchone()
    if existing:
        return present_opportunity(conn, dict(existing))
    now = utc_now()
    conn.execute(
        """INSERT INTO social_opportunities(
            id,assistant_id,kind,subject_type,subject_id,thread_id,trigger_type,
            trigger_ref,default_action,status,policy_snapshot_json,
            relationship_version,created_at,expires_at,decided_at
        ) VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,?,?, '')""",
        (
            item_id, assistant_id, kind, subject_type, subject_id, _clip(thread_id, 240),
            trigger_type, _clip(trigger_ref, 240), "reply" if kind == "reply" else "silent",
            _json(policy_snapshot or {}), max(0, int(relationship_version or 0)), now,
            _clip(expires_at, 80),
        ),
    )
    return present_opportunity(conn, dict(conn.execute("SELECT * FROM social_opportunities WHERE id=?", (item_id,)).fetchone()))


def add_topic_candidate(conn: sqlite3.Connection, opportunity_id: str, payload: dict) -> dict:
    opportunity = conn.execute("SELECT * FROM social_opportunities WHERE id=?", (_clip(opportunity_id, 80),)).fetchone()
    if not opportunity:
        raise ValueError("social_opportunity_not_found")
    source_type = _clip(payload.get("source_type"), 32)
    if source_type not in SOURCE_TYPES:
        raise ValueError("social_topic_source_invalid")
    scope_type = _clip(payload.get("scope_type"), 32)
    scope_id = _clip(payload.get("scope_id"), 160)
    summary = _clip(payload.get("summary"), 800)
    why = _clip(payload.get("why_relevant"), 800)
    source_id = _clip(payload.get("source_id"), 240)
    if not source_id or not scope_type or not summary or not why:
        raise ValueError("social_topic_candidate_incomplete")
    if opportunity["subject_type"] == "qq_group":
        if scope_type != "qq_group" or scope_id != opportunity["subject_id"]:
            raise ValueError("social_topic_scope_mismatch")
    elif scope_type == "qq_group":
        raise ValueError("social_topic_scope_mismatch")
    risk = _clip(payload.get("risk") or "low", 16)
    if risk not in {"low", "medium", "high", "blocked"}:
        raise ValueError("social_topic_risk_invalid")
    evidence = _json(
        {"source_type": source_type, "source_id": source_id, "scope_type": scope_type,
         "scope_id": scope_id, "summary": summary, "why_relevant": why},
    )
    fingerprint = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    item_id = _clip(payload.get("candidate_id"), 80) or uuid.uuid4().hex
    eligible = bool(payload.get("eligible", True)) and risk != "blocked" and source_type != "virtual_life"
    conn.execute(
        """INSERT OR IGNORE INTO social_topic_candidates(
            id,opportunity_id,source_type,source_id,scope_type,scope_id,summary,
            freshness,why_relevant,risk,evidence_sha256,eligible,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id, opportunity["id"], source_type, source_id, scope_type, scope_id,
            summary, _clip(payload.get("freshness"), 80), why, risk, fingerprint,
            1 if eligible else 0, utc_now(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM social_topic_candidates WHERE opportunity_id=? AND evidence_sha256=?",
        (opportunity["id"], fingerprint),
    ).fetchone()
    return dict(row)


def normalize_social_decision(conn: sqlite3.Connection, opportunity_id: str, payload: dict) -> dict:
    opportunity = conn.execute("SELECT * FROM social_opportunities WHERE id=?", (_clip(opportunity_id, 80),)).fetchone()
    if not opportunity:
        raise ValueError("social_opportunity_not_found")
    action = _clip(payload.get("action"), 16)
    if action not in {"reply", "silent"}:
        action = opportunity["default_action"]
    approach = _clip(payload.get("approach"), 24)
    if action == "reply" and approach not in APPROACHES:
        raise ValueError("social_decision_approach_invalid")
    if action == "silent" and approach and approach not in APPROACHES:
        raise ValueError("social_decision_approach_invalid")
    candidate_id = _clip(payload.get("topic_candidate_id"), 80)
    candidate = None
    if candidate_id:
        candidate = conn.execute(
            "SELECT * FROM social_topic_candidates WHERE id=? AND opportunity_id=?",
            (candidate_id, opportunity["id"]),
        ).fetchone()
    if candidate_id and (not candidate or not int(candidate["eligible"] or 0)):
        raise ValueError("social_decision_topic_candidate_invalid")
    if action == "reply" and not candidate:
        raise ValueError("social_decision_topic_candidate_invalid")
    why_now = _clip(payload.get("why_now"), 800)
    if action == "reply" and not why_now:
        raise ValueError("social_decision_why_now_required")
    try:
        confidence = max(0.0, min(float(payload.get("confidence", 1.0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    meme_intent = _clip(payload.get("meme_intent") or "none", 16)
    if meme_intent not in MEME_INTENTS:
        meme_intent = "none"
    evidence = dict(candidate) if candidate else {}
    evidence.pop("summary", None)
    return {
        "opportunity_id": opportunity["id"],
        "kind": opportunity["kind"],
        "action": action,
        "reason_code": _clip(payload.get("reason_code") or payload.get("reason") or "policy_decision", 120),
        "why_now": why_now,
        "topic_candidate_id": candidate_id,
        "approach": approach,
        "confidence": confidence,
        "meme_intent": meme_intent,
        "scope_type": opportunity["subject_type"],
        "scope_id": opportunity["subject_id"],
        "evidence_snapshot": evidence,
    }


def decide_opportunity(conn: sqlite3.Connection, opportunity_id: str, payload: dict) -> dict:
    result = normalize_social_decision(conn, opportunity_id, payload)
    now = utc_now()
    conn.execute(
        "UPDATE social_opportunities SET status='decided',decided_at=? WHERE id=? AND status='open'",
        (now, result["opportunity_id"]),
    )
    return result


def enrich_participation_payload(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    assistant_id: str,
    thread_id: str,
    source_message_id: str,
    conversation_frame: dict | None = None,
) -> dict:
    if not social_opportunity_enabled(conn):
        return payload
    event = conn.execute("SELECT * FROM conversation_events WHERE id=?", (payload.get("event_id"),)).fetchone()
    if not event:
        return payload
    is_group = str(event["conversation_scope"]) == "qq_group"
    action = str(payload.get("action") or "")
    kind = "join" if (
        action == "contextual_participation" or str(payload.get("candidate_kind") or "") == "ambient_group"
    ) else "reply"
    subject_type = "qq_group" if is_group else "private_user"
    subject_id = str(event["external_thread_ref"] or "").split(":")[-1] or str(event["actor_ref"])
    opportunity = create_opportunity(
        conn, assistant_id=assistant_id, kind=kind, subject_type=subject_type,
        subject_id=subject_id, thread_id=thread_id, trigger_type="inbound_message",
        trigger_ref=source_message_id or str(event["external_message_id"]),
        policy_snapshot={"policy_version": payload.get("policy_version")},
        opportunity_id=f"decision-{payload.get('decision_id')}",
    )
    frame = conversation_frame or {}
    candidate = add_topic_candidate(
        conn, opportunity["id"],
        {"source_type": "conversation" if is_group and frame.get("topic_summary") else "inbound_message",
         "source_id": source_message_id or str(event["id"]),
         "scope_type": subject_type, "scope_id": subject_id,
         "summary": str(frame.get("topic_summary") or "当前入站消息")[:800],
         "freshness": str(event["created_at"]),
         "why_relevant": (
             "助手正在参与且同一成员自然续接"
             if frame.get("active_continuation")
             else "用户当前正在与助手交互"
         ),
         "risk": "low"},
    )
    sending = action in {"direct_reply", "continuation_reply", "contextual_participation"}
    if frame.get("active_continuation"):
        why_now = "助手刚参与当前对话，同一成员正在自然续接"
        approach = "continue"
    elif kind == "reply":
        why_now = "当前消息直接吸引助手注意或回复助手"
        approach = "continue"
    else:
        why_now = "评估当前群话题是否具有即时参与价值"
        approach = "light_join"
    social = decide_opportunity(
        conn, opportunity["id"],
        {
            "action": "reply" if sending else "silent",
            "reason_code": payload.get("reason_code") or payload.get("reason") or "policy_decision",
         "why_now": why_now,
         "topic_candidate_id": candidate["id"], "approach": approach,
         "confidence": payload.get("confidence", 1.0), "meme_intent": "none"},
    )
    return {**payload, "social_opportunity": social}


def record_feedback(conn: sqlite3.Connection, payload: dict) -> dict:
    signal = _clip(payload.get("signal"), 32)
    if signal not in FEEDBACK_SIGNALS:
        raise ValueError("social_feedback_signal_invalid")
    item = {
        "id": _clip(payload.get("id"), 80) or uuid.uuid4().hex,
        "assistant_id": _clip(payload.get("assistant_id"), 80),
        "opportunity_id": _clip(payload.get("opportunity_id"), 80),
        "decision_ref": _clip(payload.get("decision_ref"), 120),
        "subject_type": _clip(payload.get("subject_type"), 32),
        "subject_id": _clip(payload.get("subject_id"), 160),
        "topic_candidate_id": _clip(payload.get("topic_candidate_id"), 80),
        "approach": _clip(payload.get("approach"), 24),
        "signal": signal,
        "source": _clip(payload.get("source"), 40),
        "detail_json": _json(payload.get("detail") or {}),
        "created_at": utc_now(),
    }
    if not item["assistant_id"] or not item["subject_id"] or not item["source"]:
        raise ValueError("social_feedback_identity_required")
    conn.execute(
        """INSERT INTO social_feedback_events(
            id,assistant_id,opportunity_id,decision_ref,subject_type,subject_id,
            topic_candidate_id,approach,signal,source,detail_json,created_at
        ) VALUES(:id,:assistant_id,:opportunity_id,:decision_ref,:subject_type,:subject_id,
                 :topic_candidate_id,:approach,:signal,:source,:detail_json,:created_at)""",
        item,
    )
    result = dict(item)
    result["detail"] = _load(result.pop("detail_json"), {})
    return result


def present_opportunity(conn: sqlite3.Connection, row: dict) -> dict:
    item = dict(row)
    item["policy_snapshot"] = _load(item.pop("policy_snapshot_json", "{}"), {})
    candidates = conn.execute(
        "SELECT * FROM social_topic_candidates WHERE opportunity_id=? ORDER BY created_at,id",
        (item["id"],),
    ).fetchall()
    item["candidates"] = [dict(candidate) for candidate in candidates]
    proactive = conn.execute(
        """SELECT id,action,reason,delivery_id,delivered_at,error,topic_candidate_id,
                  why_now,approach,meme_intent,evidence_snapshot_json,feedback_state
           FROM proactive_events WHERE opportunity_id=? ORDER BY decision_at DESC LIMIT 1""",
        (item["id"],),
    ).fetchone()
    if proactive:
        decision = dict(proactive)
        decision["evidence_snapshot"] = _load(decision.pop("evidence_snapshot_json"), {})
        # This is an audit projection, not another decision source.  The event
        # row is already the final authority for proactive delivery attempts.
        decision["phase"] = "final"
        decision["decision_source"] = "proactive_event"
        item["decision"] = decision
    else:
        engagement = conn.execute(
            """SELECT id,action,reason_code,decision_json FROM engagement_decisions
               WHERE decision_json LIKE ? ORDER BY created_at DESC LIMIT 1""",
            (f'%"opportunity_id":"{item["id"]}"%',),
        ).fetchone()
        if engagement:
            payload = _load(engagement["decision_json"], {})
            social = payload.get("social_opportunity") if isinstance(payload, dict) else {}
            # A natural group turn is initially recorded as a coalesced
            # observation and later finalized by the participation worker. The
            # engagement row is updated with that final action/reason, while
            # the embedded social object intentionally preserves its original
            # context.  Prefer the final row here so an Owner never sees a
            # completed decision mislabelled as the earlier deferred state.
            final_action = _clip(engagement["action"], 32)
            final_reason = _clip(engagement["reason_code"], 120)
            item["decision"] = {
                **(social if isinstance(social, dict) else {}),
                "id": engagement["id"],
                "action": final_action or _clip(social.get("action"), 32),
                "reason_code": final_reason or _clip(social.get("reason_code") or social.get("reason"), 120),
                "reason": final_reason or _clip(social.get("reason_code") or social.get("reason"), 120),
                "phase": "final",
                "decision_source": "engagement_decision",
            }
        else:
            item["decision"] = {
                "phase": "awaiting_final_decision",
                "decision_source": "social_opportunity",
            }
    return item


def list_opportunities(
    conn: sqlite3.Connection,
    *,
    assistant_id: str = "",
    limit: int = 50,
    status: str | None = None,
) -> list[dict]:
    clauses, params = [], []
    if assistant_id:
        clauses.append("assistant_id=?")
        params.append(_clip(assistant_id, 80))
    normalized_status = str(status or "").strip().lower()
    if normalized_status:
        if normalized_status not in {"open", "decided"}:
            raise ValueError("social_opportunity_status_invalid")
        clauses.append("status=?")
        params.append(normalized_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 50), 200)))
    rows = conn.execute(
        f"SELECT * FROM social_opportunities {where} ORDER BY created_at DESC LIMIT ?", params,
    ).fetchall()
    return [present_opportunity(conn, dict(row)) for row in rows]


def list_feedback(conn: sqlite3.Connection, *, assistant_id: str = "", limit: int = 50) -> list[dict]:
    where, params = "", []
    if assistant_id:
        where, params = "WHERE assistant_id=?", [_clip(assistant_id, 80)]
    params.append(max(1, min(int(limit or 50), 200)))
    rows = conn.execute(
        f"SELECT * FROM social_feedback_events {where} ORDER BY created_at DESC LIMIT ?", params,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = _load(item.pop("detail_json"), {})
        result.append(item)
    return result


def record_delivery_feedback(conn: sqlite3.Connection, delivery: dict | None, signal: str) -> dict | None:
    if not delivery or signal not in {"delivery_failed", "ambiguous"}:
        return None
    delivery_id = _clip(delivery.get("id"), 80)
    payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
    if payload.get("kind") == "proactive_chat":
        event = conn.execute("SELECT * FROM proactive_events WHERE delivery_id=?", (delivery_id,)).fetchone()
        if not event or not event["opportunity_id"]:
            return None
        event = dict(event)
        if "feedback_state" in event:
            conn.execute("UPDATE proactive_events SET feedback_state=? WHERE id=?", (signal, event["id"]))
        return record_feedback(conn, {
            "assistant_id": event.get("assistant_id"), "opportunity_id": event.get("opportunity_id"),
            "decision_ref": event["id"], "subject_type": "private_user", "subject_id": event["user_id"],
            "topic_candidate_id": event.get("topic_candidate_id"), "approach": event.get("approach"),
            "signal": signal, "source": "delivery_outbox", "detail": {"delivery_id": delivery_id},
        })
    decision_id = _clip(delivery.get("engagement_decision_id"), 80)
    if not decision_id:
        return None
    row = conn.execute(
        "SELECT assistant_id,decision_json FROM engagement_decisions WHERE id=?", (decision_id,),
    ).fetchone()
    if not row:
        return None
    decision_payload = _load(row["decision_json"], {})
    social = decision_payload.get("social_opportunity") if isinstance(decision_payload, dict) else None
    if not isinstance(social, dict) or not social.get("opportunity_id"):
        return None
    return record_feedback(conn, {
        "assistant_id": row["assistant_id"], "opportunity_id": social.get("opportunity_id"),
        "decision_ref": decision_id, "subject_type": social.get("scope_type"),
        "subject_id": social.get("scope_id"), "topic_candidate_id": social.get("topic_candidate_id"),
        "approach": social.get("approach"), "signal": signal, "source": "delivery_outbox",
        "detail": {"delivery_id": delivery_id},
    })


__all__ = [
    "add_topic_candidate", "create_opportunity", "decide_opportunity",
    "enrich_participation_payload", "list_feedback", "list_opportunities",
    "normalize_social_decision", "record_delivery_feedback", "record_feedback", "set_social_opportunity_feature",
    "social_opportunity_enabled",
]
