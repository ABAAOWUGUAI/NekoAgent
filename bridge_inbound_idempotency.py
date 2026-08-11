#!/usr/bin/env python3
"""Stable QQ inbound receipt lifecycle for Gate C3."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_reliability_schema import require_reliability_schema
from bridge_reliability_service import reliability_enabled


MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,180}$")


class InboundConflictError(ValueError):
    pass


class InboundProcessingError(ValueError):
    pass


class InboundOutcomeUnknownError(ValueError):
    """A protected request may have reached its operation but has no receipt result."""


class InboundIdempotencyUnavailableError(RuntimeError):
    """Fail closed when a caller requires durable receipt protection."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def web_dispatch_receipt_context(
    actor_identity: str,
    session_identity: str,
    client_request_id: str,
) -> dict[str, str]:
    """Derive a receipt namespace from server-authenticated Web identity.

    The browser-generated ID remains part of the key, but an untrusted
    ``X-QQ-Actor-ID`` never decides which Web receipt is replayed.  Raw session
    material is hashed before persistence.
    """
    request_id = str(client_request_id or "").strip()
    if not request_id:
        raise ValueError("web_dispatch_request_id_required")
    if not MESSAGE_ID_RE.fullmatch(request_id):
        raise ValueError("web_dispatch_request_id_invalid")
    actor = str(actor_identity or "").strip()
    session = str(session_identity or "").strip()
    if not actor or not session:
        raise ValueError("web_dispatch_identity_unavailable")
    scope_hash = hashlib.sha256(f"{actor}\0{session}".encode("utf-8")).hexdigest()
    receipt_hash = hashlib.sha256(f"{scope_hash}\0{request_id}".encode("utf-8")).hexdigest()
    return {
        "platform_message_id": f"web:{receipt_hash}",
        "actor_id": f"web:{actor}:{scope_hash[:24]}",
        "conversation_ref": "web-console",
    }


def begin_receipt(
    connect, platform_message_id: str, actor_id: str, conversation_ref: str, payload: dict,
    *, require_receipt: bool = False,
) -> dict | None:
    message_id = str(platform_message_id or "").strip()
    if not message_id:
        if require_receipt:
            raise ValueError("web_dispatch_request_id_required")
        return None
    if not MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError("web_dispatch_request_id_invalid" if require_receipt else "qq_message_id_invalid")
    stable_payload = {key: value for key, value in payload.items() if key != "trace_id"}
    digest = hashlib.sha256(
        json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    current = _now()
    lease_until = (current + timedelta(minutes=5)).isoformat()
    outcome_unknown = False
    with connect() as conn:
        if not require_receipt and not reliability_enabled(conn):
            return None
        if require_receipt:
            try:
                require_reliability_schema(conn)
            except Exception as exc:
                raise InboundIdempotencyUnavailableError("web_dispatch_idempotency_unavailable") from exc
        if not conn.in_transaction:
            # Select + insert/update must be one SQLite writer acquisition.  This
            # keeps two concurrent same-ID Web retries from both becoming owners.
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM qq_inbound_receipts WHERE platform_message_id=?", (message_id,),
        ).fetchone()
        if row:
            saved = dict(row)
            if str(saved["payload_hash"]) != digest:
                raise InboundConflictError(
                    "web_dispatch_request_id_payload_conflict" if require_receipt else "qq_message_id_payload_conflict",
                )
            if saved["status"] == "completed":
                return {"id": message_id, "replay": json.loads(saved["response_json"])}
            lease = datetime.fromisoformat(str(saved.get("lease_until") or current.isoformat()))
            if saved["status"] == "processing" and lease > current:
                raise InboundProcessingError("web_dispatch_processing" if require_receipt else "qq_message_processing")
            if require_receipt:
                # Once the operation's outcome is no longer provable, a Web
                # retry must never reacquire the receipt and execute again.
                # The terminal state is inspectable/recoverable, not a lease
                # deadlock, and the caller must inspect existing Work first.
                if saved["status"] == "processing":
                    conn.execute(
                        "UPDATE qq_inbound_receipts SET status='failed',lease_until='',updated_at=? WHERE platform_message_id=?",
                        (current.isoformat(), message_id),
                    )
                outcome_unknown = True
            elif not outcome_unknown:
                conn.execute(
                    """UPDATE qq_inbound_receipts SET status='processing',lease_until=?,
                              trace_id=?,updated_at=? WHERE platform_message_id=?""",
                    (lease_until, str(payload.get("trace_id") or "")[:100], current.isoformat(), message_id),
                )
        else:
            conn.execute(
                """INSERT INTO qq_inbound_receipts(
                       platform_message_id,actor_id,conversation_ref,payload_hash,trace_id,
                       status,response_json,lease_until,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'processing','',?,?,?)""",
                (
                    message_id, str(actor_id)[:80], str(conversation_ref)[:200], digest,
                    str(payload.get("trace_id") or "")[:100], lease_until,
                    current.isoformat(), current.isoformat(),
                ),
            )
    if outcome_unknown:
        raise InboundOutcomeUnknownError("web_dispatch_outcome_unknown")
    return {"id": message_id, "replay": None}


def complete_receipt(connect, receipt: dict | None, response: dict) -> None:
    if not receipt:
        return
    now = _now().isoformat()
    with connect() as conn:
        conn.execute(
            """UPDATE qq_inbound_receipts SET status='completed',response_json=?,
                      lease_until='',updated_at=? WHERE platform_message_id=?""",
            (
                json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                now, receipt["id"],
            ),
        )


def fail_receipt(connect, receipt: dict | None) -> None:
    if not receipt:
        return
    with connect() as conn:
        conn.execute(
            """UPDATE qq_inbound_receipts SET status='failed',lease_until='',updated_at=?
               WHERE platform_message_id=?""",
            (_now().isoformat(), receipt["id"]),
        )


def execute_once(
    connect, platform_message_id: str, actor_id: str, conversation_ref: str,
    payload: dict, operation, *, require_receipt: bool = False,
) -> dict:
    receipt = begin_receipt(
        connect, platform_message_id, actor_id, conversation_ref, payload,
        require_receipt=require_receipt,
    )
    if receipt and receipt.get("replay") is not None:
        return dict(receipt["replay"])
    try:
        response = operation()
    except Exception:
        fail_receipt(connect, receipt)
        raise
    complete_receipt(connect, receipt, response)
    return response


__all__ = [
    "InboundConflictError", "InboundProcessingError", "begin_receipt", "complete_receipt",
    "InboundOutcomeUnknownError", "InboundIdempotencyUnavailableError", "execute_once", "fail_receipt",
    "web_dispatch_receipt_context",
]
