#!/usr/bin/env python3
"""Fact-first internal assessment for one group response path.

This produces an internal decision record rather than a reply template.  It is
intentionally unable to generate text, enqueue Delivery, change permissions,
or make a research/knowledge decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from bridge_response_assessment_contract import make_response_assessment
from bridge_response_assessment_schema import (
    RESPONSE_ASSESSMENT_TABLE,
    require_response_assessment_assistant_isolation_schema,
    require_response_assessment_schema,
    response_assessment_enabled,
    set_response_assessment_feature,
)


_POLICY_REF = "policy:response-assessment-v1"
_SOURCE_PREFIXES = frozenset({"decision", "quality-receipt", "outcome"})
_SOCIAL_ACTION_TO_SPEECH_ACT = {
    "ack": "acknowledge",
    "ack_add": "answer",
    "follow_up": "clarify",
    "reply": "answer",
    "bridge_topic": "explain",
    "topic_start": "explain",
    "repair": "explain",
}


def _opaque_ref(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return f"{prefix}:sha256-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _research_state(value: object) -> tuple[str, str, str]:
    research = value if isinstance(value, Mapping) else {}
    if research.get("fact_unverified"):
        return "clarify", "unknown", "research_blocked"
    source_urls = research.get("source_urls") or research.get("sources") or []
    if research.get("available") and isinstance(source_urls, list) and source_urls:
        return "research_answer", "grounded", "not_needed"
    return "", "not_needed", "not_needed"


def build_group_response_assessment(
    *,
    group_id: object,
    sender_id: object,
    source_message_id: object,
    topic_revision: object,
    decision: Mapping[str, object],
    result: Mapping[str, object],
    conversation_frame: Mapping[str, object] | None,
    research_context: Mapping[str, object] | None,
    policy_version: str = _POLICY_REF,
) -> dict:
    """Derive a body-free assessment after route/truth gates, before Delivery.

    ``result`` is accepted only to keep this adapter aligned with the existing
    final gate call shape; reply body and model output are deliberately not
    inspected or copied here.
    """

    del result
    try:
        revision = int(topic_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("group_response_assessment_topic_invalid") from exc
    if revision < 0:
        raise ValueError("group_response_assessment_topic_invalid")
    frame = conversation_frame if isinstance(conversation_frame, Mapping) else {}
    action = str((decision or {}).get("social_action") or "").strip()
    obligation = (
        "required"
        if str(frame.get("attention") or "") in {"explicit_mention", "reply_to_assistant"}
        else "ambient_optional"
    )
    speech_act, grounding, research = _research_state(research_context)
    if not speech_act:
        speech_act = _SOCIAL_ACTION_TO_SPEECH_ACT.get(action, "answer")
    stance_kind = "uncertain" if grounding == "unknown" else "none"
    work_lifecycle = str((decision or {}).get("work_lifecycle") or "").strip()
    task_continuation = "preserve_due_work" if work_lifecycle in {"continue", "active"} else "not_applicable"
    source = source_message_id or f"{group_id}:{revision}"
    return make_response_assessment(
        {
            "schema_version": 1,
            "scope_type": "group",
            "scope_ref": _opaque_ref("scope", group_id),
            "topic_ref": _opaque_ref("topic", source),
            "topic_revision": revision,
            "target_ref": _opaque_ref("member", sender_id),
            "reply_obligation": obligation,
            "speech_act": speech_act,
            "grounding_status": grounding,
            "stance": {
                "kind": stance_kind,
                "confidence": 1.0,
                "basis_refs": [],
                "missing_fact_refs": [],
            },
            "research_disposition": research,
            "task_continuation": task_continuation,
            "policy_version": policy_version,
        },
    )


def _source(source_kind: object, source_ref: object) -> tuple[str, str]:
    kind = str(source_kind or "").strip()
    ref = str(source_ref or "").strip()
    if kind not in _SOURCE_PREFIXES or not ref.startswith(kind + ":"):
        raise ValueError("response_assessment_source_invalid")
    return kind, ref


def _identifier(value: Mapping[str, object], assistant_id: str, source_kind: str, source_ref: str) -> str:
    token = "|".join((assistant_id, source_kind, source_ref, str(value["policy_version"])))
    return "response-assessment:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def _stored(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["stance"] = json.loads(str(item.pop("stance_json")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("response_assessment_stored_invalid") from exc
    return item


def record_response_assessment(
    conn: sqlite3.Connection,
    value: Mapping[str, object],
    *,
    source_kind: object,
    source_ref: object,
    assistant_id: object,
) -> dict | None:
    """Persist an assessment only when its default-off evidence plane is enabled."""

    if not response_assessment_enabled(conn):
        return None
    require_response_assessment_assistant_isolation_schema(conn)
    owner = str(assistant_id or "").strip()
    if not owner:
        raise ValueError("response_assessment_assistant_id_required")
    normalized = make_response_assessment(value)
    kind, source = _source(source_kind, source_ref)
    identifier = _identifier(normalized, owner, kind, source)
    conn.execute(
        f"""
        INSERT INTO {RESPONSE_ASSESSMENT_TABLE}(
            id,assistant_id,scope_type,scope_ref,topic_ref,topic_revision,target_ref,reply_obligation,speech_act,
            grounding_status,stance_json,research_disposition,task_continuation,policy_version,
            source_kind,source_ref,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(assistant_id,source_kind,source_ref) DO NOTHING
        """,
        (
            identifier, owner, normalized["scope_type"], normalized["scope_ref"], normalized["topic_ref"],
            normalized["topic_revision"], normalized["target_ref"], normalized["reply_obligation"],
            normalized["speech_act"], normalized["grounding_status"],
            json.dumps(normalized["stance"], ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            normalized["research_disposition"], normalized["task_continuation"], normalized["policy_version"],
            kind, source, _utc_now(),
        ),
    )
    row = conn.execute(
        f"SELECT * FROM {RESPONSE_ASSESSMENT_TABLE} WHERE assistant_id=? AND source_kind=? AND source_ref=?",
        (owner, kind, source),
    ).fetchone()
    return _stored(row)


def list_response_assessments(conn: sqlite3.Connection, *, assistant_id: object, limit: int = 100) -> list[dict]:
    require_response_assessment_assistant_isolation_schema(conn)
    owner = str(assistant_id or "").strip()
    if not owner:
        raise ValueError("response_assessment_assistant_id_required")
    safe_limit = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"SELECT * FROM {RESPONSE_ASSESSMENT_TABLE} WHERE assistant_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
        (owner, safe_limit),
    ).fetchall()
    return [_stored(row) for row in rows if row is not None]


__all__ = [
    "build_group_response_assessment",
    "list_response_assessments",
    "record_response_assessment",
    "set_response_assessment_feature",
]
