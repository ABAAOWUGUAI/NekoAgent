#!/usr/bin/env python3
"""Body-free quality evidence for QQ participation and delivery decisions."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
import uuid


_OUTCOMES = {"replied", "silent", "blocked", "error"}
_MAX_LIMIT = 80


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _cursor_encode(timestamp: object, identifier: object) -> str:
    raw = json.dumps([str(timestamp or ""), str(identifier or "")], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_decode(cursor: object) -> tuple[str, str] | None:
    value = str(cursor or "").strip()
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        timestamp, identifier = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8"),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("qq_quality_receipt_cursor_invalid") from exc
    if not isinstance(timestamp, str) or not timestamp or not isinstance(identifier, str) or not identifier:
        raise ValueError("qq_quality_receipt_cursor_invalid")
    return timestamp, identifier


def _limit(value: object, default: int = 20) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError("qq_quality_receipt_limit_invalid") from exc
    if not 1 <= parsed <= _MAX_LIMIT:
        raise ValueError("qq_quality_receipt_limit_invalid")
    return parsed


@dataclass(frozen=True)
class QualityReceiptInput:
    decision_id: str
    inbound_ref: str
    conversation_ref: str
    outcome: str
    reason: str
    response_class: str
    grounding_status: str
    observation_status: str
    truth_verdict: str
    persona_verdict: str
    evidence_refs: list[str] = field(default_factory=list)
    rewrite_or_block_code: str = ""
    continuity_ref: str = ""


def _stored_receipt(row: sqlite3.Row | None) -> dict:
    if row is None:
        raise ValueError("qq_quality_receipt_not_found")
    item = dict(row)
    # The receipt model intentionally does not emit source payloads or reply
    # text; evidence references remain internal correlation identifiers.
    return {
        "id": str(item["id"]),
        "decision_id": str(item["decision_id"]),
        "inbound_ref": str(item["inbound_ref"]),
        "conversation_ref": str(item["conversation_ref"]),
        "outcome": str(item["outcome"]),
        "reason_code": str(item["reason_code"]),
        "response_class": str(item["response_class"]),
        "grounding_status": str(item["grounding_status"]),
        "observation_status": str(item["observation_status"]),
        "truth_verdict": str(item["truth_verdict"]),
        "persona_verdict": str(item["persona_verdict"]),
        "rewrite_or_block_code": str(item["rewrite_or_block_code"]),
        "delivery_id": str(item["delivery_id"]),
        "delivery_status": str(item["delivery_status"]),
        "client_projection_status": str(item["client_projection_status"]),
        "continuity_ref": str(item["continuity_ref"]),
        "created_at": str(item["created_at"]),
        "updated_at": str(item["updated_at"]),
    }


def _normalize_input(value: QualityReceiptInput) -> dict:
    outcome = _clip(value.outcome, 24)
    if outcome not in _OUTCOMES:
        raise ValueError("qq_quality_receipt_outcome_invalid")
    fields = {
        "decision_id": _clip(value.decision_id, 180),
        "inbound_ref": _clip(value.inbound_ref, 240),
        "conversation_ref": _clip(value.conversation_ref, 180),
        "outcome": outcome,
        "reason_code": _clip(value.reason, 120),
        "response_class": _clip(value.response_class, 80),
        "grounding_status": _clip(value.grounding_status, 80),
        "observation_status": _clip(value.observation_status, 80),
        "truth_verdict": _clip(value.truth_verdict, 80),
        "persona_verdict": _clip(value.persona_verdict, 80),
        "rewrite_or_block_code": _clip(value.rewrite_or_block_code, 120),
        "continuity_ref": _clip(value.continuity_ref, 180),
    }
    required = ("inbound_ref", "conversation_ref", "outcome", "reason_code")
    if any(not fields[name] for name in required):
        raise ValueError("qq_quality_receipt_required")
    evidence_refs = [
        _clip(item, 180)
        for item in value.evidence_refs
        if _clip(item, 180)
    ][:12]
    fields["evidence_refs_json"] = json.dumps(evidence_refs, ensure_ascii=False, separators=(",", ":"))
    return fields


def record_quality_receipt(conn: sqlite3.Connection, receipt: QualityReceiptInput) -> dict:
    fields = _normalize_input(receipt)
    now = _utc_now()
    receipt_id = f"qqqr-{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO qq_quality_receipts(
            id,decision_id,inbound_ref,conversation_ref,outcome,reason_code,response_class,
            grounding_status,evidence_refs_json,observation_status,truth_verdict,persona_verdict,
            rewrite_or_block_code,delivery_id,delivery_status,client_projection_status,continuity_ref,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'','not_applicable','unverified',?,?,?)
        """,
        (
            receipt_id, fields["decision_id"], fields["inbound_ref"], fields["conversation_ref"],
            fields["outcome"], fields["reason_code"], fields["response_class"],
            fields["grounding_status"], fields["evidence_refs_json"], fields["observation_status"],
            fields["truth_verdict"], fields["persona_verdict"], fields["rewrite_or_block_code"],
            fields["continuity_ref"], now, now,
        ),
    )
    stored = _stored_receipt(conn.execute("SELECT * FROM qq_quality_receipts WHERE id=?", (receipt_id,)).fetchone())
    # BE-2 is optional and default-off.  A receipt remains the source fact even
    # when its conservative, body-free observation cannot be formed.
    try:
        from bridge_behavior_observation import observe_quality_receipt

        observe_quality_receipt(conn, stored)
    except (sqlite3.Error, ValueError):
        pass
    return stored


def record_from_truth_gate_result(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    inbound_ref: str,
    conversation_ref: str,
    media_ready: bool,
    final_text: str,
) -> dict:
    from bridge_group_truth_gate import (
        ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE,
        group_final_truth_issues,
    )

    issues = group_final_truth_issues(
        str(final_text or ""),
        {"media": {"visual_context": "ready" if media_ready else "none", "observation": "ready" if media_ready else "none"}},
    )
    experiential_claim = ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE in issues
    return record_quality_receipt(
        conn,
        QualityReceiptInput(
            decision_id=decision_id,
            inbound_ref=inbound_ref,
            conversation_ref=conversation_ref,
            outcome="blocked" if experiential_claim else "replied",
            reason="media_claim_without_evidence" if experiential_claim else "truth_gate_passed",
            response_class="social" if final_text else "none",
            grounding_status="ready" if media_ready else "not_ready",
            observation_status="ready" if media_ready else "none",
            truth_verdict="blocked" if experiential_claim else "passed",
            persona_verdict="not_applicable",
            rewrite_or_block_code="media_claim_without_evidence" if experiential_claim else "",
        ),
    )


def record_group_dispatch_quality_receipt(
    conn: sqlite3.Connection,
    *,
    result: dict,
    decision: dict,
    group_id: str,
    inbound_ref: str,
    decision_id: str,
    event_ref: str = "",
    conversation_frame: dict | None = None,
) -> dict | None:
    """Persist one grouped decision result when the staged feature is enabled.

    The caller supplies only decision/gate metadata.  Generated text, prompt,
    raw model errors and transport payloads never enter the receipt table.
    """

    from bridge_qq_quality_receipt_schema import quality_receipt_enabled

    if not quality_receipt_enabled(conn):
        return None
    result = result if isinstance(result, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    frame = conversation_frame if isinstance(conversation_frame, dict) else {}
    issues = [str(item).strip() for item in result.get("group_truth_issues") or [] if str(item).strip()]
    rewrite_codes = [str(item).strip() for item in result.get("group_truth_rewrite_codes") or [] if str(item).strip()]
    blocked_reason = (
        (issues[0] if issues else "")
        or _clip(result.get("group_safety_reason"), 120)
        or ("model_turn_failed" if not bool(result.get("ok")) else "")
    )
    reply = _clip(result.get("reply") or result.get("output"), 1)
    style_issues = [
        str(item).strip() for item in result.get("group_style_final_issues") or []
        if str(item).strip()
    ]
    advisory_codes = [
        "advisory:repeated_reply_shape"
        for item in result.get("group_style_advisory_issues") or []
        if item == "repeated_reply_shape"
    ][:1]
    if result.get('group_quality_initial_issues'):
        advisory_codes += ['contextual:' + str(code) for code in result['group_quality_initial_issues']]
        advisory_codes += ['repair:' + str(result.get('group_quality_repair_status') or 'not_attempted')]
    style_only_blocked = bool(
        not blocked_reason and result.get("group_truth_blocked") and style_issues
    )
    if result.get("group_generation_invalid"):
        outcome = "error"
        reason = _clip(result.get("error_kind"), 120) or "group_direct_single_plan_reply_invalid"
    elif blocked_reason or bool(result.get("group_truth_blocked")) or bool(result.get("group_safety_blocked")):
        outcome = "blocked"
        reason = blocked_reason or ("group_style_blocked" if style_only_blocked else "group_truth_blocked")
    elif bool(result.get("ok")) and reply:
        outcome = "replied"
        reason = _clip(decision.get("reason"), 120) or "direct_reply"
    else:
        outcome = "silent"
        reason = _clip(decision.get("reason"), 120) or "no_concrete_anchor"
    envelope = frame.get("grounding_envelope") if isinstance(frame.get("grounding_envelope"), dict) else {}
    media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
    visual_context = _clip(media.get("visual_context"), 80)
    observation = _clip(media.get("observation"), 80)
    if visual_context == "ready":
        grounding_status = "ready"
    elif visual_context and visual_context != "none":
        grounding_status = "not_ready"
    else:
        grounding_status = "not_required"
    persona_verdict = "not_applicable" if outcome == "error" else "blocked" if style_only_blocked or "persona_signature_overuse" in issues else (
        "rewritten_passed" if rewrite_codes else (
        "not_applicable" if outcome == "silent" else "passed"
        )
    )
    receipt = record_quality_receipt(
        conn,
        QualityReceiptInput(
            decision_id=_clip(decision_id, 180),
            inbound_ref=_clip(inbound_ref, 240),
            conversation_ref=f"group:{_clip(group_id, 80)}",
            # outcome is the observed reply fact, not generation success.
            # The existing schema's silent + generated/pending stage means
            # no reply has been acknowledged yet; only ACK promotes replied.
            outcome="silent" if outcome == "replied" else outcome,
            reason=reason,
            response_class=_clip(result.get("dispatch"), 80) or ("social" if outcome == "replied" else "none"),
            grounding_status=grounding_status,
            observation_status=observation or "none",
            truth_verdict="not_applicable" if outcome == "error" else "passed" if style_only_blocked else "blocked" if outcome == "blocked" else (
                "passed" if outcome == "replied" else "not_applicable"
            ),
            persona_verdict=persona_verdict,
            evidence_refs=[value for value in (_clip(event_ref, 180), _clip(decision_id, 180)) if value],
            rewrite_or_block_code=(",".join(style_issues) if style_only_blocked else
                                  reason if outcome == "blocked" else
                                  ",".join(rewrite_codes + advisory_codes)),
        ),
    )
    if outcome == "replied":
        conn.execute("UPDATE qq_quality_receipts SET delivery_status='generated' WHERE id=?", (receipt["id"],))
        receipt["delivery_status"] = "generated"
    return receipt


def finalize_quality_receipt_delivery(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    delivery: dict,
) -> dict:
    identifier = _clip(receipt_id, 120)
    if not identifier:
        raise ValueError("qq_quality_receipt_not_found")
    existing = _stored_receipt(conn.execute("SELECT * FROM qq_quality_receipts WHERE id=?", (identifier,)).fetchone())
    delivery_id = _clip(delivery.get("id"), 120)
    if not delivery_id or (existing["delivery_id"] and existing["delivery_id"] != delivery_id):
        raise ValueError("qq_quality_receipt_delivery_mismatch")
    certainty = _clip(delivery.get("delivery_certainty") or delivery.get("certainty"), 40)
    acknowledged = certainty == "confirmed" or bool(_clip(delivery.get("acked_at"), 80))
    if not acknowledged and existing["delivery_status"] in {"application_ack", "client_projected"}:
        return existing  # A late queue projection cannot regress an ACK.
    proof_ref = _clip(delivery.get("client_projection_proof_ref"), 180)
    projected = _clip(delivery.get("client_projection_status"), 40) == "client_projected" and bool(proof_ref)
    delivery_status = "client_projected" if projected else ("application_ack" if acknowledged else "delivery_pending")
    client_projection_status = "verified" if projected else "unverified"
    conn.execute(
        """
        UPDATE qq_quality_receipts
        SET delivery_id=?,delivery_status=?,client_projection_status=?,updated_at=?,
            outcome=CASE WHEN ? AND outcome='silent' THEN 'replied' ELSE outcome END
        WHERE id=?
        """,
        (
            _clip(delivery.get("id"), 120), delivery_status, client_projection_status,
            _utc_now(), bool(acknowledged), identifier,
        ),
    )
    return _stored_receipt(conn.execute("SELECT * FROM qq_quality_receipts WHERE id=?", (identifier,)).fetchone())


def settle_group_dispatch_delivery(conn: sqlite3.Connection, result: dict) -> None:
    """Project the one enqueue outcome; never sends or retries a response."""
    receipt_id = _clip(result.get("_quality_receipt_id"), 120)
    delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
    if result.get("delivery_queued") and delivery.get("id"):
        if receipt_id:
            finalize_quality_receipt_delivery(conn, receipt_id=receipt_id, delivery=delivery)
        return
    reason = _clip(result.get("group_ambient_metadata_reason") or result.get("_delivery_enqueue_error")
                   or result.get("group_window_reason") or "delivery_not_queued", 120)
    if receipt_id:
        conn.execute("""UPDATE qq_quality_receipts SET outcome='error', delivery_status='not_queued',
                     reason_code=?,updated_at=? WHERE id=? AND delivery_id=''
                     AND delivery_status='generated'""", (reason, _utc_now(), receipt_id))
    meme = result.get("meme") if isinstance(result.get("meme"), dict) else {}
    if meme.get("selection_id"):
        from bridge_meme_social import mark_meme_delivery
        selected = conn.execute('SELECT status FROM meme_send_history WHERE id=?', (str(meme['selection_id']),)).fetchone()
        if selected and selected[0] == 'selected':
            mark_meme_delivery(conn, str(meme["selection_id"]), status="failed", error=reason)


def project_group_dispatch_delivery(db_connect, result: dict) -> None:
    with db_connect() as conn:
        settle_group_dispatch_delivery(conn, result)


def list_quality_receipts(
    conn: sqlite3.Connection,
    *,
    conversation_ref: str,
    limit: int = 20,
    cursor: str = "",
) -> dict:
    reference = _clip(conversation_ref, 180)
    if not reference:
        raise ValueError("qq_quality_receipt_conversation_required")
    safe_limit = _limit(limit)
    after = _cursor_decode(cursor)
    clauses = ["conversation_ref=?"]
    params: list[object] = [reference]
    if after:
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        params.extend((after[0], after[0], after[1]))
    params.append(safe_limit + 1)
    rows = conn.execute(
        f"SELECT * FROM qq_quality_receipts WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC,id DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    selected = rows[:safe_limit]
    next_cursor = ""
    if len(rows) > safe_limit and selected:
        last = selected[-1]
        next_cursor = _cursor_encode(last["created_at"], last["id"])
    return {
        "items": [_stored_receipt(row) for row in selected],
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


def list_recent_quality_receipts(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """Return bounded body-free material-event candidates for the Owner Brief."""

    safe_limit = _limit(limit)
    rows = conn.execute(
        "SELECT * FROM qq_quality_receipts ORDER BY created_at DESC,id DESC LIMIT ?",
        (safe_limit,),
    ).fetchall()
    return [_stored_receipt(row) for row in rows]


def quality_receipt_for_decision(conn: sqlite3.Connection, *, decision_id: str) -> dict | None:
    """Return the newest body-free receipt for one immutable decision ID."""

    identifier = _clip(decision_id, 180)
    if not identifier:
        return None
    row = conn.execute(
        "SELECT * FROM qq_quality_receipts WHERE decision_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (identifier,),
    ).fetchone()
    return _stored_receipt(row) if row is not None else None


__all__ = [
    "QualityReceiptInput",
    "finalize_quality_receipt_delivery",
    "list_quality_receipts",
    "list_recent_quality_receipts",
    "record_from_truth_gate_result",
    "record_group_dispatch_quality_receipt",
    "record_quality_receipt",
    "quality_receipt_for_decision",
]
