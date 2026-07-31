#!/usr/bin/env python3
"""Stable QQ inbound receipt lifecycle for Gate C3."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from bridge_reliability_service import reliability_enabled


MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,180}$")


class InboundConflictError(ValueError):
    pass


class InboundProcessingError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def begin_receipt(
    connect, platform_message_id: str, actor_id: str, conversation_ref: str, payload: dict,
) -> dict | None:
    message_id = str(platform_message_id or "").strip()
    if not message_id:
        return None
    if not MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError("qq_message_id_invalid")
    stable_payload = {key: value for key, value in payload.items() if key != "trace_id"}
    digest = hashlib.sha256(
        json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    current = _now()
    lease_until = (current + timedelta(minutes=5)).isoformat()
    with connect() as conn:
        if not reliability_enabled(conn):
            return None
        row = conn.execute(
            "SELECT * FROM qq_inbound_receipts WHERE platform_message_id=?", (message_id,),
        ).fetchone()
        if row:
            saved = dict(row)
            if str(saved["payload_hash"]) != digest:
                raise InboundConflictError("qq_message_id_payload_conflict")
            if saved["status"] == "completed":
                return {"id": message_id, "replay": json.loads(saved["response_json"])}
            lease = datetime.fromisoformat(str(saved.get("lease_until") or current.isoformat()))
            if saved["status"] == "processing" and lease > current:
                raise InboundProcessingError("qq_message_processing")
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
    payload: dict, operation,
) -> dict:
    receipt = begin_receipt(connect, platform_message_id, actor_id, conversation_ref, payload)
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
    "execute_once", "fail_receipt",
]
