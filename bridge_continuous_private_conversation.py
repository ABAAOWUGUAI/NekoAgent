#!/usr/bin/env python3
"""Durable private-message ingress and exact-source response-cycle coordination."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from bridge_continuous_private_conversation_schema import (
    CONTINUOUS_PRIVATE_CONVERSATION_FEATURE_FLAG,
    require_continuous_private_conversation_schema,
)
from bridge_assistant_affect_observer import observe_visible_private_affect
from bridge_conversation_memory import record_conversation, resolve_thread
from bridge_conversation_participation import record_conversation_event
from bridge_inbound_idempotency import InboundConflictError, InboundRequestValidationError
from bridge_migrations import utc_now
from bridge_qq_participation_shadow import qq_participation_event
from bridge_qq_delivery import (
    MAX_PREPARED_QQ_DELIVERY_BYTES,
    prepared_qq_delivery_matches,
    qq_route_binding_sha256,
)
from bridge_reliability_schema import require_reliability_schema
from bridge_response_cycle_successor_schema import require_response_cycle_successor_schema


MAX_SOURCE_MESSAGES = 64
MAX_SITUATION_REVISIONS = 8
LEASE_SECONDS = 300
_ACTIVE_STATES = {"pending", "processing", "commit_ready", "retryable", "manual_hold"}
_DELIVERY_STATES = {
    "not_applicable", "pending_outbox", "outbox_queued", "channel_acked", "ambiguous", "failed",
}


class ContinuousPrivateConversationDisabledError(RuntimeError):
    pass


class ResponseCycleBusyError(RuntimeError):
    pass


class ResponseCycleLeaseError(RuntimeError):
    pass


class ResponseCycleRecoveryError(RuntimeError):
    pass


class ResponseCycleEffectBlockedError(RuntimeError):
    pass


class ResponseCycleSupersededError(RuntimeError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _rows(cursor) -> list[dict]:
    names = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def continuous_private_conversation_enabled(conn: sqlite3.Connection) -> bool:
    require_continuous_private_conversation_schema(conn)
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (CONTINUOUS_PRIVATE_CONVERSATION_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def set_continuous_private_conversation_feature(
    conn: sqlite3.Connection,
    enabled: bool,
) -> dict:
    schema = require_continuous_private_conversation_schema(conn)
    if not enabled:
        active = conn.execute(
            "SELECT count(*) FROM conversation_response_cycles "
            "WHERE state IN ('pending','processing','commit_ready','retryable','manual_hold') "
            "AND blocks_thread=1",
        ).fetchone()
        if active and int(active[0]):
            raise ResponseCycleBusyError("continuous_private_conversation_active_cycles")
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (CONTINUOUS_PRIVATE_CONVERSATION_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return {**schema, "feature_enabled": bool(enabled)}


def _require_enabled(conn: sqlite3.Connection) -> None:
    if not continuous_private_conversation_enabled(conn):
        raise ContinuousPrivateConversationDisabledError("continuous_private_conversation_disabled")


def _safe_components(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 64:
        raise InboundRequestValidationError("private_ingress_components_invalid")
    result: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InboundRequestValidationError("private_ingress_components_invalid")
        kind = str(item.get("type") or "unknown").strip().lower()[:40]
        component = {"type": kind or "unknown", "position": index}
        for key in ("media_kind", "source_component"):
            token = str(item.get(key) or "").strip().lower()[:40]
            if token:
                component[key] = token
        if "visual_index" in item:
            try:
                component["visual_index"] = max(0, int(item["visual_index"]))
            except (TypeError, ValueError):
                raise InboundRequestValidationError("private_ingress_components_invalid")
        if "text_start" in item or "text_end" in item:
            if kind not in {"plain", "text"}:
                raise InboundRequestValidationError("private_ingress_plain_span_mismatch")
            start, end = item.get("text_start"), item.get("text_end")
            if type(start) is not int or type(end) is not int or start < 0 or end <= start:
                raise InboundRequestValidationError("private_ingress_plain_span_mismatch")
            component["text_start"] = start
            component["text_end"] = end
        result.append(component)
    return result


def _validate_plain_spans(components: list[dict], body: str) -> None:
    plain = [item for item in components if item["type"] in {"plain", "text"}]
    if not any("text_start" in item for item in plain):
        return  # Deployed v1/v2 receipts did not carry ordered offsets.
    cursor = 0
    for item in plain:
        if item.get("text_start") != cursor or "text_end" not in item:
            raise InboundRequestValidationError("private_ingress_plain_span_mismatch")
        cursor = item["text_end"]
    if cursor != len(body):
        raise InboundRequestValidationError("private_ingress_plain_span_mismatch")


def _safe_attachments(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 16:
        raise InboundRequestValidationError("private_ingress_attachments_invalid")
    result: list[dict] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise InboundRequestValidationError("private_ingress_attachments_invalid")
        kind = str(item.get("type") or "unknown").strip().lower()[:40]
        attachment = {"type": kind or "unknown"}
        source_component = str(item.get("source_component") or "").strip().lower()[:40]
        if source_component:
            attachment["source_component"] = source_component
        result.append(attachment)
    return result


def _stable_ingress(payload: Mapping[str, object]) -> dict:
    external_message_id = str(payload.get("external_message_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    if not external_message_id or len(external_message_id) > 300:
        raise InboundRequestValidationError("private_ingress_message_id_invalid")
    if not user_id or len(user_id) > 80:
        raise InboundRequestValidationError("private_ingress_actor_invalid")
    if str(payload.get("channel") or "").strip() != "qq":
        raise InboundRequestValidationError("private_ingress_channel_invalid")
    if str(payload.get("conversation_type") or "").strip() != "private":
        raise InboundRequestValidationError("private_ingress_scope_invalid")
    raw_text = str(payload.get("raw_text") or "").strip()[:6000]
    text = str(payload.get("text") or "").strip()[:6000]
    components = _safe_components(payload.get("message_components") or [])
    _validate_plain_spans(components, raw_text or text)
    attachments = _safe_attachments(payload.get("attachments") or [])
    coordinator_version = payload.get("response_coordinator_version", 1)
    if type(coordinator_version) is not int or coordinator_version not in {1, 2}:
        raise InboundRequestValidationError("private_ingress_coordinator_version_invalid")
    if not raw_text and not text and not attachments:
        raise InboundRequestValidationError("private_ingress_content_required")
    stable = {
        "channel": "qq",
        "conversation_type": "private",
        "user_id": user_id,
        "external_message_id": external_message_id,
        "raw_text": raw_text,
        "text": text,
        "self_id": str(payload.get("self_id") or "").strip()[:80],
        "reply_to_external_message_id": str(
            payload.get("reply_to_external_message_id") or ""
        ).strip()[:300],
        "reply_to_assistant": bool(payload.get("reply_to_assistant")),
        "message_components": components,
        "attachments": attachments,
    }
    # Preserve the exact v1 receipt digest deployed before the Bridge-owned
    # coordinator existed.  Only the explicit v2 handoff changes identity.
    if coordinator_version == 2:
        stable["response_coordinator_version"] = 2
    return stable


def accept_private_ingress(conn: sqlite3.Connection, payload: Mapping[str, object]) -> dict:
    """Atomically persist one raw Owner-private event and its accepted receipt."""

    _require_enabled(conn)
    require_reliability_schema(conn)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    stable = _stable_ingress(payload)
    if stable["attachments"]:
        print("private_ingress_component_shape " + json.dumps({
            "external_message_id": stable["external_message_id"],
            "types": [item["type"] for item in stable["message_components"]],
            "plain_lengths": [
                item["text_end"] - item["text_start"]
                for item in stable["message_components"]
                if "text_start" in item
            ],
            "body_length": len(stable["raw_text"]),
        }, ensure_ascii=True, separators=(",", ":")), flush=True)
    coordinator_version = int(stable.get("response_coordinator_version") or 1)
    digest = _hash(stable)
    receipt_id = "private-ingress:" + hashlib.sha256(
        f"{stable['self_id']}\0{stable['external_message_id']}".encode("utf-8"),
    ).hexdigest()
    existing = conn.execute(
        "SELECT payload_hash,status,response_json FROM qq_inbound_receipts WHERE platform_message_id=?",
        (receipt_id,),
    ).fetchone()
    if existing:
        legacy_stable = dict(stable)
        legacy_stable["message_components"] = [
            {key: value for key, value in item.items() if key not in {"text_start", "text_end"}}
            for item in stable["message_components"]
        ]
        if str(existing[0]) not in {digest, _hash(legacy_stable)}:
            raise InboundConflictError("private_ingress_payload_conflict")
        if str(existing[1]) != "completed":
            raise RuntimeError("private_ingress_receipt_incomplete")
        try:
            replay = json.loads(str(existing[2] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("private_ingress_receipt_invalid") from exc
        if not isinstance(replay, dict) or not replay.get("accepted"):
            raise RuntimeError("private_ingress_receipt_invalid")
        replay = dict(replay)
        replay["idempotent_replay"] = True
        return replay

    thread = resolve_thread(conn, stable["user_id"], source="qq")
    transport = {
        **stable,
        "_external_message_id": stable["external_message_id"],
        "_qq_actor_id": stable["user_id"],
        "_qq_actor_role": "owner",
        "_qq_self_id": stable["self_id"],
        "_event_timestamp": utc_now(),
    }
    event = qq_participation_event(
        conn,
        transport,
        scope="qq_private",
        actor_id=stable["user_id"],
        thread_ref=stable["user_id"],
        plain_text=stable["raw_text"] or stable["text"],
        assistant_id=str(thread["assistant_id"]),
    )
    event_result = record_conversation_event(conn, event)
    if event_result.get("created"):
        cursor = conn.execute(
            """
            UPDATE conversation_threads
            SET latest_inbound_sequence=latest_inbound_sequence+1,updated_at=?
            WHERE id=?
            """,
            (utc_now(), thread["id"]),
        )
        if int(cursor.rowcount or 0) != 1:
            raise RuntimeError("private_ingress_thread_update_failed")
        sequence = int(conn.execute(
            "SELECT latest_inbound_sequence FROM conversation_threads WHERE id=?",
            (thread["id"],),
        ).fetchone()[0])
        content = stable["raw_text"] or stable["text"] or "（发送了一项媒体内容）"
        message_id = record_conversation(
            conn,
            stable["user_id"],
            "user",
            content,
            source="qq",
            external_message_id=stable["external_message_id"],
            reply_to_external_message_id=stable["reply_to_external_message_id"],
            directed_to_assistant=True,
            message_kind=str(getattr(event.message_kind, "value", event.message_kind) or "text"),
            metadata={
                "conversation_event_id": event.event_id,
                "transport_protocol": "continuous_private_conversation_v1",
                "response_coordinator_version": coordinator_version,
                "session": str(payload.get("session") or f"qq:private:{stable['user_id']}")[:200],
                "logical_turn_has_text": bool(stable["raw_text"]),
                "attachments": stable["attachments"],
                "message_components": stable["message_components"],
            },
        )
        if not message_id:
            raise RuntimeError("private_ingress_message_write_failed")
        # User return is an accepted ingress fact, not a successful-generation
        # side effect. Keep it in this same transaction even when a prior
        # response cycle is held. Receipt replay returns before this branch.
        from bridge_automation import ensure_automation_tables
        from bridge_proactive_feedback import note_activity

        ensure_automation_tables(conn)
        received_at = conn.execute(
            "SELECT created_at FROM conversation_messages WHERE id=?", (message_id,),
        ).fetchone()[0]
        feedback_policy = conn.execute(
            "SELECT * FROM proactive_policies WHERE user_id=? AND assistant_id=? AND policy_kind='social'",
            (stable["user_id"], str(thread["assistant_id"])),
        ).fetchone()
        if feedback_policy:
            note_activity(conn, stable["user_id"], now=datetime.fromisoformat(received_at),
                          expected_policy=dict(feedback_policy))
        cursor = conn.execute(
            "UPDATE conversation_messages SET inbound_sequence=? WHERE id=?",
            (sequence, message_id),
        )
    else:
        rows = _rows(conn.execute(
            """
            SELECT id,inbound_sequence FROM conversation_messages
            WHERE thread_id=? AND role='user' AND external_message_id=?
              AND inbound_sequence IS NOT NULL
            ORDER BY inbound_sequence LIMIT 2
            """,
            (thread["id"], stable["external_message_id"]),
        ))
        if len(rows) != 1:
            raise RuntimeError("private_ingress_event_message_binding_invalid")
        message_id = str(rows[0]["id"])
        sequence = int(rows[0]["inbound_sequence"])
    # Affect observes the same durable ingress fact before generation.  It is
    # body-free and best-effort: a Shadow/schema failure cannot reject an
    # otherwise valid private message or create a second response path.
    try:
        observe_visible_private_affect(
            conn,
            thread_id=str(thread["id"]),
            user_id=stable["user_id"],
            source_message_id=message_id,
            message=stable["raw_text"] or stable["text"],
            assistant_id=str(thread["assistant_id"]),
            topic_revision=sequence,
            now=utc_now(),
        )
    except (sqlite3.Error, ValueError):
        pass
    response = {
        "ok": True,
        "accepted": True,
        "protocol_version": 1,
        "cycle_coordinator": True,
        "response_coordinator_version": coordinator_version,
        "receipt_id": receipt_id,
        "event_id": event.event_id,
        "thread_id": thread["id"],
        "source_message_id": message_id,
        "inbound_sequence": sequence,
        "idempotent_replay": False,
    }
    now = utc_now()
    conn.execute(
        """
        INSERT INTO qq_inbound_receipts(
            platform_message_id,actor_id,conversation_ref,payload_hash,trace_id,
            status,response_json,lease_until,created_at,updated_at
        ) VALUES(?,?,?,?,?,'completed',?,'',?,?)
        """,
        (
            receipt_id,
            stable["user_id"],
            stable["user_id"],
            digest,
            str(payload.get("trace_id") or "")[:100],
            _canonical(response),
            now,
            now,
        ),
    )
    return response


def _binding_fingerprint(conn: sqlite3.Connection, thread: Mapping[str, object]) -> str:
    setting_rows = conn.execute(
        "SELECT key,updated_at FROM settings ORDER BY key",
    ).fetchall()
    return _hash({
        "assistant_id": thread["assistant_id"],
        "settings_revisions": [(str(row[0]), str(row[1] or "")) for row in setting_rows],
    })


def _source_set_hash(messages: Sequence[Mapping[str, object]]) -> str:
    return _hash([
        {
            "message_id": str(item["id"]),
            "inbound_sequence": int(item["inbound_sequence"]),
        }
        for item in messages
    ])


def _response_cycle_identity(cycle_id: str) -> tuple[str, str]:
    logical_id = "qq-cycle-response-" + hashlib.sha256(
        str(cycle_id).encode("utf-8"),
    ).hexdigest()
    return logical_id, f"qq:response:{logical_id}"


def _lease_expiry(now: str) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=LEASE_SECONDS)).isoformat()


def _response_cycle_result(conn: sqlite3.Connection, cycle_id: str) -> dict:
    cycle = conn.execute(
        """
        SELECT id,thread_id,assistant_id,state,source_count,source_set_hash,
               lease_owner,lease_token,lease_expires_at,attempt_count,max_attempts,
               logical_response_id,outbox_dedupe_key,
               predecessor_cycle_id,successor_cycle_id,replan_depth
        FROM conversation_response_cycles WHERE id=?
        """,
        (str(cycle_id),),
    ).fetchone()
    if not cycle:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    messages = _rows(conn.execute(
        """
        SELECT m.id,m.external_message_id,m.content
        FROM conversation_response_cycle_sources s
        JOIN conversation_messages m ON m.id=s.source_message_id
        WHERE s.cycle_id=? ORDER BY s.source_order
        """,
        (str(cycle_id),),
    ))
    return {
        "id": str(cycle[0]),
        "thread_id": str(cycle[1]),
        "assistant_id": str(cycle[2]),
        "state": str(cycle[3]),
        "source_count": int(cycle[4]),
        "source_set_hash": str(cycle[5]),
        "source_message_ids": [str(item["id"]) for item in messages],
        "source_external_message_ids": [str(item["external_message_id"]) for item in messages],
        "source_contents": [str(item["content"] or "") for item in messages],
        "lease_owner": str(cycle[6]),
        "lease_token": str(cycle[7]),
        "lease_expires_at": str(cycle[8]),
        "attempt_count": int(cycle[9]),
        "max_attempts": int(cycle[10]),
        "logical_response_id": str(cycle[11]),
        "outbox_dedupe_key": str(cycle[12]),
        "predecessor_cycle_id": str(cycle[13] or ""),
        "successor_cycle_id": str(cycle[14] or ""),
        "replan_depth": int(cycle[15] or 0),
    }


def acquire_response_cycle(
    conn: sqlite3.Connection,
    *,
    legacy_user_id: str,
    source_external_message_ids: Sequence[str],
    lease_owner: str,
) -> dict:
    """Freeze an exact source set immediately before paid/effectful processing."""

    _require_enabled(conn)
    require_response_cycle_successor_schema(conn)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    source_ids = [str(item or "").strip() for item in source_external_message_ids]
    if (
        not source_ids
        or len(source_ids) > MAX_SOURCE_MESSAGES
        or any(not item for item in source_ids)
        or len(set(source_ids)) != len(source_ids)
    ):
        raise InboundRequestValidationError("response_cycle_sources_invalid")
    worker = str(lease_owner or "").strip()[:120]
    if not worker:
        raise InboundRequestValidationError("response_cycle_lease_owner_required")
    thread = resolve_thread(conn, legacy_user_id, source="qq")
    active = conn.execute(
        """
        SELECT id,state FROM conversation_response_cycles
        WHERE thread_id=? AND state IN ('pending','processing','commit_ready','retryable','manual_hold')
          AND blocks_thread=1
        LIMIT 1
        """,
        (thread["id"],),
    ).fetchone()
    if active:
        raise ResponseCycleBusyError(f"response_cycle_busy:{active[1]}")
    requested_messages: list[dict] = []
    for external_id in source_ids:
        rows = _rows(conn.execute(
            """
            SELECT id,thread_id,external_message_id,inbound_sequence,content
            FROM conversation_messages
            WHERE thread_id=? AND role='user' AND external_message_id=?
              AND inbound_sequence IS NOT NULL
            ORDER BY inbound_sequence LIMIT 2
            """,
            (thread["id"], external_id),
        ))
        if len(rows) != 1:
            raise InboundRequestValidationError("response_cycle_source_not_found")
        if conn.execute(
            "SELECT 1 FROM conversation_response_cycle_sources WHERE source_message_id=?",
            (rows[0]["id"],),
        ).fetchone():
            raise InboundConflictError("response_cycle_source_already_assigned")
        requested_messages.append(rows[0])
    through_sequence = max(int(item["inbound_sequence"]) for item in requested_messages)
    messages = _rows(conn.execute(
        """
        SELECT m.id,m.thread_id,m.external_message_id,m.inbound_sequence,m.content
        FROM conversation_messages m
        WHERE m.thread_id=? AND m.role='user' AND m.inbound_sequence IS NOT NULL
          AND m.inbound_sequence<=?
          AND NOT EXISTS(
              SELECT 1 FROM conversation_response_cycle_sources s
              WHERE s.source_message_id=m.id
          )
        ORDER BY m.inbound_sequence,m.id
        LIMIT ?
        """,
        (thread["id"], through_sequence, MAX_SOURCE_MESSAGES + 1),
    ))
    if len(messages) > MAX_SOURCE_MESSAGES:
        raise ResponseCycleBusyError("response_cycle_source_backlog_exceeded")
    selected_ids = {str(item["id"]) for item in messages}
    if any(str(item["id"]) not in selected_ids for item in requested_messages):
        raise InboundConflictError("response_cycle_source_already_assigned")
    sequences = [int(item["inbound_sequence"]) for item in messages]
    if sequences != sorted(sequences):
        raise InboundRequestValidationError("response_cycle_source_order_invalid")
    now = utc_now()
    lease_token = uuid.uuid4().hex
    lease_expires = _lease_expiry(now)
    cycle_id = "response-cycle-" + uuid.uuid4().hex
    source_hash = _source_set_hash(messages)
    logical_response_id, outbox_dedupe_key = _response_cycle_identity(cycle_id)
    predecessor = conn.execute(
        """
        SELECT id,predecessor_cycle_id,replan_depth FROM conversation_response_cycles
        WHERE thread_id=? AND state='manual_hold' AND blocks_thread=0
          AND recovery_disposition='superseded_precommit'
          AND successor_cycle_id=''
          AND through_inbound_sequence<?
        ORDER BY recovery_decided_at DESC,updated_at DESC,id DESC
        LIMIT 1
        """,
        (thread["id"], min(sequences)),
    ).fetchone()
    predecessor_id = str(predecessor[0]) if predecessor else ""
    if predecessor_id:
        predecessor_chain = validated_response_cycle_predecessors(
            conn,
            predecessor_id,
        )
        if len(predecessor_chain) >= MAX_SITUATION_REVISIONS:
            raise ResponseCycleRecoveryError("response_cycle_revision_bound_invalid")
    replan_depth = 1 if predecessor_id else 0
    conn.execute(
        """
        INSERT INTO conversation_response_cycles(
            id,thread_id,assistant_id,owner_actor_id,channel_type,external_thread_ref,
            assistant_binding_fingerprint,from_inbound_sequence,through_inbound_sequence,
            source_count,source_set_hash,state,source_frozen_at,generation_started_at,
            effects_admitted_at,lease_owner,lease_token,lease_expires_at,attempt_count,
            max_attempts,last_error,logical_response_id,outbox_dedupe_key,delivery_id,
            created_at,updated_at,predecessor_cycle_id,successor_cycle_id,replan_depth
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'processing',?,'','',?,?,?,1,2,'',?,?,'',?,?,?,'',?)
        """,
        (
            cycle_id,
            thread["id"],
            thread["assistant_id"],
            thread["owner_actor_id"],
            thread["channel_type"],
            thread["external_thread_ref"],
            _binding_fingerprint(conn, thread),
            min(sequences),
            max(sequences),
            len(messages),
            source_hash,
            now,
            worker,
            lease_token,
            lease_expires,
            logical_response_id,
            outbox_dedupe_key,
            now,
            now,
            predecessor_id,
            replan_depth,
        ),
    )
    if predecessor_id:
        linked = conn.execute(
            """
            UPDATE conversation_response_cycles SET successor_cycle_id=?,updated_at=?
            WHERE id=? AND state='manual_hold' AND blocks_thread=0
              AND recovery_disposition='superseded_precommit'
              AND successor_cycle_id='' AND predecessor_cycle_id=?
              AND replan_depth=?
            """,
            (
                cycle_id,
                now,
                predecessor_id,
                str(predecessor[1] or ""),
                int(predecessor[2] or 0),
            ),
        )
        if int(linked.rowcount or 0) != 1:
            raise ResponseCycleRecoveryError("response_cycle_successor_link_conflict")
    for order, message in enumerate(messages):
        conn.execute(
            """
            INSERT INTO conversation_response_cycle_sources(
                cycle_id,source_message_id,inbound_sequence,source_order,created_at
            ) VALUES(?,?,?,?,?)
            """,
            (cycle_id, message["id"], int(message["inbound_sequence"]), order, now),
        )
    return {
        "id": cycle_id,
        "thread_id": thread["id"],
        "assistant_id": thread["assistant_id"],
        "state": "processing",
        "source_count": len(messages),
        "source_set_hash": source_hash,
        "source_message_ids": [str(item["id"]) for item in messages],
        "source_external_message_ids": [str(item["external_message_id"]) for item in messages],
        "source_contents": [str(item["content"] or "") for item in messages],
        "lease_owner": worker,
        "lease_token": lease_token,
        "lease_expires_at": lease_expires,
        "attempt_count": 1,
        "max_attempts": 2,
        "logical_response_id": logical_response_id,
        "outbox_dedupe_key": outbox_dedupe_key,
        "predecessor_cycle_id": predecessor_id,
        "successor_cycle_id": "",
        "replan_depth": replan_depth,
    }


def reclaim_response_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    source_set_hash: str,
    lease_owner: str,
) -> dict:
    """Atomically rotate an expired pre-generation lease on the same cycle."""

    _require_enabled(conn)
    worker = str(lease_owner or "").strip()[:120]
    source_hash = str(source_set_hash or "").strip()
    if not worker or not source_hash:
        raise ValueError("response_cycle_reclaim_identity_required")
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    now = utc_now()
    lease_token = uuid.uuid4().hex
    lease_expires = _lease_expiry(now)
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles
        SET state='processing',lease_owner=?,lease_token=?,lease_expires_at=?,
            attempt_count=attempt_count+1,last_error='',updated_at=?
        WHERE id=? AND source_set_hash=? AND blocks_thread=1
          AND generation_started_at='' AND attempt_count<max_attempts
          AND (
              (state='processing' AND lease_expires_at<>'' AND lease_expires_at<=?)
              OR (state='retryable' AND lease_owner='' AND lease_token='' AND lease_expires_at='')
          )
        """,
        (
            worker,
            lease_token,
            lease_expires,
            now,
            str(cycle_id),
            source_hash,
            now,
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    return _response_cycle_result(conn, str(cycle_id))


def mark_response_cycle_generation_started(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    lease_token: str,
) -> None:
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles
        SET generation_started_at=?,updated_at=?
        WHERE id=? AND state='processing' AND lease_token=? AND lease_owner<>''
          AND lease_expires_at>? AND generation_started_at=''
        """,
        (now, now, str(cycle_id), str(lease_token), now),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")


def mark_response_cycle_failure(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    lease_token: str,
    error: str,
) -> dict:
    now = utc_now()
    row = conn.execute(
        """
        SELECT state,generation_started_at,prepared_delivery_json,prepared_delivery_sha256
        FROM conversation_response_cycles
        WHERE id=? AND lease_token=? AND lease_owner<>'' AND lease_expires_at>?
        """,
        (str(cycle_id), str(lease_token), now),
    ).fetchone()
    if not row:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    if str(row[0]) == "commit_ready" and str(row[2] or "") and str(row[3] or ""):
        state = "commit_ready"
        blocks_effects = 0
    elif str(row[1] or ""):
        state = "manual_hold"
        blocks_effects = 1
    else:
        state = "retryable"
        blocks_effects = 0
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles SET state=?,blocks_effects=?,last_error=?,
            lease_owner='',lease_token='',lease_expires_at='',updated_at=?
        WHERE id=? AND lease_token=? AND lease_owner<>'' AND lease_expires_at>?
        """,
        (
            state, blocks_effects, str(error or "")[:500], now,
            str(cycle_id), str(lease_token), now,
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    return {"id": str(cycle_id), "state": state}


def claim_prepared_response_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    source_set_hash: str,
    lease_owner: str,
    binding_only: bool = False,
) -> dict:
    """Rotate recovery ownership for one immutable prepared intent."""

    worker = str(lease_owner or "").strip()[:120]
    source_hash = str(source_set_hash or "").strip()
    if not worker or not source_hash:
        raise ValueError("response_cycle_recovery_identity_required")
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    now = utc_now()
    token = uuid.uuid4().hex
    expiry = _lease_expiry(now)
    attempt_sql = "attempt_count" if binding_only else "attempt_count+1"
    attempt_guard = "" if binding_only else "AND attempt_count<max_attempts"
    cursor = conn.execute(
        f"""
        UPDATE conversation_response_cycles
        SET lease_owner=?,lease_token=?,lease_expires_at=?,
            attempt_count={attempt_sql},last_error='',updated_at=?
        WHERE id=? AND state='commit_ready' AND source_set_hash=?
          AND prepared_delivery_json<>'' AND prepared_delivery_sha256<>''
          {attempt_guard}
          AND (
              (lease_owner='' AND lease_token='' AND lease_expires_at='')
              OR (lease_owner<>'' AND lease_token<>'' AND lease_expires_at<=?)
          )
        """,
        (worker, token, expiry, now, str(cycle_id), source_hash, now),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    return _response_cycle_result(conn, str(cycle_id))


def hold_response_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    source_set_hash: str,
    expected_states: Sequence[str],
    reason: str,
    blocks_effects: bool,
    require_recoverable_lease: bool = False,
) -> dict:
    """Fail closed without deleting sources or inventing a delivery result."""

    states = tuple(str(item or "").strip() for item in expected_states)
    if not states or any(item not in _ACTIVE_STATES for item in states):
        raise ValueError("response_cycle_hold_state_invalid")
    placeholders = ",".join("?" for _ in states)
    now = utc_now()
    lease_guard = ""
    lease_params: tuple[str, ...] = ()
    if require_recoverable_lease:
        lease_guard = (
            " AND ((lease_owner='' AND lease_token='' AND lease_expires_at='')"
            " OR lease_expires_at<=?)"
        )
        lease_params = (now,)
    cursor = conn.execute(
        f"""
        UPDATE conversation_response_cycles
        SET state='manual_hold',blocks_thread=1,blocks_effects=?,last_error=?,
            lease_owner='',lease_token='',lease_expires_at='',updated_at=?
        WHERE id=? AND source_set_hash=? AND state IN ({placeholders})
          {lease_guard}
        """,
        (
            1 if blocks_effects else 0, str(reason or "")[:500], now,
            str(cycle_id), str(source_set_hash), *states, *lease_params,
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleRecoveryError("response_cycle_hold_conflict")
    return {
        "id": str(cycle_id), "state": "manual_hold",
        "blocks_thread": True, "blocks_effects": bool(blocks_effects),
    }


def isolate_response_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    expected_state: str,
    source_set_hash: str,
    actor: str,
    reason: str,
) -> dict:
    """Release only the conversation block while preserving uncertain effects."""

    actor = str(actor or "").strip()[:120]
    reason = str(reason or "").strip()[:500]
    if str(expected_state or "") != "manual_hold" or not actor or not reason:
        raise ValueError("response_cycle_isolation_contract_invalid")
    row = conn.execute(
        """
        SELECT generation_started_at,effects_admitted_at
        FROM conversation_response_cycles
        WHERE id=? AND state='manual_hold' AND source_set_hash=?
        """,
        (str(cycle_id), str(source_set_hash)),
    ).fetchone()
    if row is None:
        raise ResponseCycleRecoveryError("response_cycle_isolation_conflict")
    effects_unknown = bool(str(row[0] or "") or str(row[1] or ""))
    now = utc_now()
    disposition = "isolated_effects_unknown" if effects_unknown else "isolated_no_effects"
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles
        SET blocks_thread=0,blocks_effects=?,recovery_disposition=?,
            recovery_decided_at=?,recovery_decided_by=?,last_error=?,updated_at=?
        WHERE id=? AND state='manual_hold' AND source_set_hash=? AND blocks_thread=1
        """,
        (
            1 if effects_unknown else 0, disposition, now, actor, reason, now,
            str(cycle_id), str(source_set_hash),
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleRecoveryError("response_cycle_isolation_conflict")
    return {
        "id": str(cycle_id), "state": "manual_hold",
        "blocks_thread": False, "blocks_effects": effects_unknown,
        "recovery_disposition": disposition,
    }


def resolve_response_cycle_effects(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    source_set_hash: str,
    actor: str,
    reason: str,
    disposition: str,
    authoritative_ref: str,
) -> dict:
    """Clear an effect hold only after an explicit authoritative disposition."""

    actor = str(actor or "").strip()[:120]
    reason = str(reason or "").strip()[:500]
    disposition = str(disposition or "").strip().lower()
    reference = str(authoritative_ref or "").strip()[:300]
    if (
        not actor or not reason
        or disposition not in {"no_effect", "completed", "cancelled", "failed"}
        or not reference.startswith(("task:", "run:", "approval:", "delivery:", "no-effect:"))
    ):
        raise ValueError("response_cycle_effect_resolution_invalid")
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles
        SET blocks_effects=0,recovery_disposition=?,recovery_decided_at=?,
            recovery_decided_by=?,last_error=?,updated_at=?
        WHERE id=? AND state='manual_hold' AND source_set_hash=?
          AND blocks_thread=0 AND blocks_effects=1
        """,
        (
            f"effects_resolved:{disposition}:{reference}", now, actor, reason, now,
            str(cycle_id), str(source_set_hash),
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleRecoveryError("response_cycle_effect_resolution_conflict")
    return {
        "id": str(cycle_id), "blocks_effects": False,
        "disposition": disposition, "authoritative_ref": reference,
    }


def assert_response_cycle_effects_allowed(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    cycle_id: str,
    source_set_hash: str,
    lease_token: str,
) -> dict:
    """Fence every new private effect against unresolved older cycle facts."""

    now = utc_now()
    current = conn.execute(
        """
        SELECT effects_admitted_at FROM conversation_response_cycles
        WHERE id=? AND thread_id=? AND state='processing' AND source_set_hash=?
          AND lease_token=? AND lease_owner<>'' AND lease_expires_at>?
        """,
        (
            str(cycle_id), str(thread_id), str(source_set_hash),
            str(lease_token), now,
        ),
    ).fetchone()
    if current is None:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    blocker = conn.execute(
        """
        SELECT id FROM conversation_response_cycles
        WHERE thread_id=? AND id<>? AND state='manual_hold' AND blocks_effects=1
        ORDER BY created_at,id LIMIT 1
        """,
        (str(thread_id), str(cycle_id)),
    ).fetchone()
    if blocker is not None:
        raise ResponseCycleEffectBlockedError("response_cycle_effects_blocked")
    admitted_at = str(current[0] or "")
    if not admitted_at:
        cursor = conn.execute(
            """
            UPDATE conversation_response_cycles SET effects_admitted_at=?,updated_at=?
            WHERE id=? AND thread_id=? AND state='processing' AND source_set_hash=?
              AND lease_token=? AND lease_owner<>'' AND lease_expires_at>?
              AND effects_admitted_at=''
            """,
            (
                now, now, str(cycle_id), str(thread_id), str(source_set_hash),
                str(lease_token), now,
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ResponseCycleLeaseError("response_cycle_lease_lost")
        admitted_at = now
    return {
        "cycle_id": str(cycle_id), "effects_admitted": True,
        "effects_admitted_at": admitted_at,
    }


_PREPARED_DELIVERY_KEYS = frozenset({
    "version", "enqueue", "scope", "cycle_id", "source_set_hash",
    "logical_response_id", "outbox_dedupe_key", "channel", "thread_ref",
    "source_message_id", "engagement_decision_id", "delivery_class",
    "max_attempts", "supersede_pending_social", "response_sequence", "payload",
    "media_categories", "voice_error",
    "thread_id", "assistant_id", "channel_type", "external_thread_ref",
    "assistant_binding_fingerprint", "route_binding_sha256",
})
_PREPARED_FORBIDDEN_KEYS = frozenset({
    "session", "send_session", "destination", "cookie", "cookies", "token",
    "authorization", "credential", "credentials", "data_base64", "raw_data",
    "raw_payload", "visual_media",
})


def _contains_prepared_forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key or "").strip().lower() in _PREPARED_FORBIDDEN_KEYS
            or _contains_prepared_forbidden(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_prepared_forbidden(item) for item in value)
    return False


def _cycle_route(row: Mapping[str, object]) -> tuple[str, str]:
    if str(row.get("channel_type") or "") != "qq_private":
        raise ResponseCycleRecoveryError("response_cycle_route_unsupported")
    external_ref = str(row.get("external_thread_ref") or "").strip()
    if not external_ref:
        raise ResponseCycleRecoveryError("response_cycle_route_invalid")
    destination = f"qq:private:{external_ref}"
    return destination, destination


def _cycle_row(conn: sqlite3.Connection, cycle_id: str) -> dict:
    rows = _rows(conn.execute(
        """
        SELECT id,thread_id,assistant_id,owner_actor_id,channel_type,
               external_thread_ref,assistant_binding_fingerprint,state,
               source_set_hash,generation_started_at,effects_admitted_at,
               lease_owner,lease_token,lease_expires_at,attempt_count,max_attempts,
               logical_response_id,outbox_dedupe_key,delivery_id,
               prepared_delivery_json,prepared_delivery_sha256,
               blocks_thread,blocks_effects,recovery_disposition,
               from_inbound_sequence,through_inbound_sequence,
               predecessor_cycle_id,successor_cycle_id,replan_depth
        FROM conversation_response_cycles WHERE id=?
        """,
        (str(cycle_id),),
    ))
    if len(rows) != 1:
        raise ResponseCycleRecoveryError("response_cycle_not_found")
    return rows[0]


def validated_response_cycle_predecessors(
    conn: sqlite3.Connection,
    cycle_id: str,
) -> list[dict]:
    """Return root-to-parent ancestry after validating every durable hop."""

    child = _cycle_row(conn, cycle_id)
    child_id = str(child.get("id") or "")
    seen = {child_id}
    reverse_chain: list[dict] = []
    while str(child.get("predecessor_cycle_id") or ""):
        predecessor_id = str(child["predecessor_cycle_id"])
        if predecessor_id in seen:
            raise ResponseCycleRecoveryError("response_cycle_predecessor_chain_cycle")
        if len(reverse_chain) >= MAX_SITUATION_REVISIONS:
            raise ResponseCycleRecoveryError("response_cycle_predecessor_chain_bound")
        parent = _cycle_row(conn, predecessor_id)
        if str(parent.get("successor_cycle_id") or "") != str(child.get("id") or ""):
            raise ResponseCycleRecoveryError(
                "response_cycle_predecessor_successor_backlink_invalid",
            )
        if (
            str(parent.get("thread_id") or "") != str(child.get("thread_id") or "")
            or str(parent.get("assistant_id") or "") != str(child.get("assistant_id") or "")
        ):
            raise ResponseCycleRecoveryError("response_cycle_predecessor_identity_drift")
        if not all((
            str(parent.get("state") or "") == "manual_hold",
            int(parent.get("blocks_thread") or 0) == 0,
            int(parent.get("blocks_effects") or 0) == 0,
            str(parent.get("recovery_disposition") or "") == "superseded_precommit",
            not str(parent.get("effects_admitted_at") or ""),
            not str(parent.get("prepared_delivery_json") or ""),
            not str(parent.get("prepared_delivery_sha256") or ""),
        )):
            raise ResponseCycleRecoveryError("response_cycle_predecessor_state_invalid")
        if int(parent.get("through_inbound_sequence") or 0) >= int(
            child.get("from_inbound_sequence") or 0,
        ):
            raise ResponseCycleRecoveryError("response_cycle_predecessor_sequence_invalid")
        if int(child.get("replan_depth") or 0) != 1:
            raise ResponseCycleRecoveryError("response_cycle_predecessor_depth_invalid")
        reverse_chain.append(parent)
        seen.add(predecessor_id)
        child = parent

    expected_root_depth = 0
    if int(child.get("replan_depth") or 0) != expected_root_depth:
        raise ResponseCycleRecoveryError("response_cycle_predecessor_root_depth_invalid")
    return list(reversed(reverse_chain))


def _assert_current_cycle_lease(
    row: Mapping[str, object],
    *,
    lease_token: str,
    source_set_hash: str,
    allowed_states: Sequence[str],
) -> str:
    """Reject every stale worker before any idempotent or failure transition."""

    now = utc_now()
    if (
        str(row.get("state") or "") not in set(allowed_states)
        or str(row.get("source_set_hash") or "") != str(source_set_hash or "")
        or not str(row.get("lease_owner") or "")
        or str(row.get("lease_token") or "") != str(lease_token or "")
        or str(row.get("lease_expires_at") or "") <= now
    ):
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    return now


def _validated_prepared_delivery(
    conn: sqlite3.Connection,
    row: Mapping[str, object],
    delivery: Mapping[str, object],
    *,
    expected_destination: str,
) -> dict:
    item = dict(delivery)
    if set(item) - _PREPARED_DELIVERY_KEYS:
        raise ResponseCycleRecoveryError("response_cycle_prepared_fields_invalid")
    if _contains_prepared_forbidden(item):
        raise ResponseCycleRecoveryError("response_cycle_prepared_sensitive_data")
    if item.get("version") != 1 or item.get("enqueue") is not True:
        raise ResponseCycleRecoveryError("response_cycle_prepared_version_invalid")
    destination, thread_ref = _cycle_route(row)
    if str(expected_destination or "").strip() != destination:
        raise ResponseCycleRecoveryError("response_cycle_route_drift")
    source_external_ids = {
        str(item[0])
        for item in conn.execute(
            """
            SELECT m.external_message_id
            FROM conversation_response_cycle_sources s
            JOIN conversation_messages m ON m.id=s.source_message_id
            WHERE s.cycle_id=?
            """,
            (str(row["id"]),),
        ).fetchall()
    }
    checks = (
        str(item.get("scope") or "") == "private",
        str(item.get("channel") or "") == "qq",
        str(item.get("cycle_id") or "") == str(row["id"]),
        str(item.get("source_set_hash") or "") == str(row["source_set_hash"]),
        str(item.get("logical_response_id") or "") == str(row["logical_response_id"]),
        str(item.get("outbox_dedupe_key") or "") == str(row["outbox_dedupe_key"]),
        str(item.get("thread_ref") or "") == thread_ref,
        str(item.get("source_message_id") or "") in source_external_ids,
        isinstance(item.get("payload"), Mapping),
    )
    if not all(checks):
        raise ResponseCycleRecoveryError("response_cycle_prepared_identity_drift")
    thread = conn.execute(
        "SELECT * FROM conversation_threads WHERE id=?",
        (str(row["thread_id"]),),
    ).fetchone()
    if thread is None:
        raise ResponseCycleRecoveryError("response_cycle_thread_missing")
    thread_map = dict(thread)
    if _binding_fingerprint(conn, thread_map) != str(row["assistant_binding_fingerprint"]):
        raise ResponseCycleRecoveryError("response_cycle_assistant_binding_drift")
    item.update({
        "thread_id": str(row["thread_id"]),
        "assistant_id": str(row["assistant_id"]),
        "channel_type": str(row["channel_type"]),
        "external_thread_ref": str(row["external_thread_ref"]),
        "assistant_binding_fingerprint": str(row["assistant_binding_fingerprint"]),
    })
    item["route_binding_sha256"] = qq_route_binding_sha256(item, destination)
    encoded = _canonical(item)
    if len(encoded.encode("utf-8")) > MAX_PREPARED_QQ_DELIVERY_BYTES:
        raise ResponseCycleRecoveryError("response_cycle_prepared_too_large")
    return item


def prepare_response_cycle_delivery(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    lease_token: str,
    source_set_hash: str,
    delivery: Mapping[str, object],
    expected_destination: str,
    interaction_plan_id: str = "",
    context_message_ids: Sequence[str] = (),
    assistant_metadata: Mapping[str, object] | None = None,
) -> dict:
    """Durably freeze one exact enqueue intent before touching the Outbox DB."""

    row = _cycle_row(conn, cycle_id)
    now = _assert_current_cycle_lease(
        row,
        lease_token=lease_token,
        source_set_hash=source_set_hash,
        allowed_states=("processing", "commit_ready"),
    )
    item = _validated_prepared_delivery(
        conn, row, delivery, expected_destination=expected_destination,
    )
    content = str((item.get("payload") or {}).get("content") or "").strip()
    if not content:
        raise ResponseCycleRecoveryError("response_cycle_prepared_content_required")
    if assistant_metadata is not None and not isinstance(assistant_metadata, Mapping):
        raise ValueError("response_cycle_assistant_metadata_invalid")
    encoded = _canonical(item)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if row["prepared_delivery_json"] or row["prepared_delivery_sha256"]:
        if (
            str(row["prepared_delivery_json"] or "") == encoded
            and str(row["prepared_delivery_sha256"] or "") == digest
        ):
            return item
        cursor = conn.execute(
            """
            UPDATE conversation_response_cycles
            SET state='manual_hold',blocks_effects=1,last_error=?,
                lease_owner='',lease_token='',lease_expires_at='',updated_at=?
            WHERE id=? AND state IN ('processing','commit_ready')
              AND source_set_hash=? AND lease_token=? AND lease_owner<>''
              AND lease_expires_at>? AND prepared_delivery_json=?
              AND prepared_delivery_sha256=?
            """,
            (
                "response_cycle_prepared_drift", now, str(cycle_id),
                str(source_set_hash), str(lease_token), now,
                str(row["prepared_delivery_json"] or ""),
                str(row["prepared_delivery_sha256"] or ""),
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ResponseCycleLeaseError("response_cycle_lease_lost")
        raise ResponseCycleRecoveryError("response_cycle_prepared_drift")
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles
        SET state='commit_ready',prepared_delivery_json=?,
            prepared_delivery_sha256=?,updated_at=?
        WHERE id=? AND state='processing' AND source_set_hash=?
          AND lease_token=? AND lease_owner<>'' AND lease_expires_at>?
          AND logical_response_id=? AND outbox_dedupe_key=?
          AND prepared_delivery_json='' AND prepared_delivery_sha256=''
        """,
        (
            encoded, digest, now, str(cycle_id), str(source_set_hash),
            str(lease_token), now, str(item["logical_response_id"]),
            str(item["outbox_dedupe_key"]),
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    from bridge_inbound_context import inbound_exchange_context
    with inbound_exchange_context({"_response_cycle_id": str(cycle_id)}):
        record_conversation(
            conn, str(row["owner_actor_id"]), "assistant", content,
            source="qq", thread_id=str(row["thread_id"]),
            metadata=dict(assistant_metadata or {}),
        )
    conn.execute(
        """
        UPDATE conversation_messages SET delivery_projection_state='pending_outbox'
        WHERE response_cycle_id=? AND thread_id=? AND role='assistant'
        """,
        (str(cycle_id), str(row["thread_id"])),
    )
    plan_id = str(interaction_plan_id or "").strip()
    if plan_id:
        cycle_sources = [
            str(item[0])
            for item in conn.execute(
                """
                SELECT source_message_id FROM conversation_response_cycle_sources
                WHERE cycle_id=? ORDER BY source_order
                """,
                (str(cycle_id),),
            ).fetchall()
        ]
        ordered_sources = []
        for message_id in [*context_message_ids, *cycle_sources]:
            token = str(message_id or "").strip()
            if token and token not in ordered_sources:
                ordered_sources.append(token)
        bind_interaction_plan_sources(
            conn,
            plan_id=plan_id,
            cycle_id=str(cycle_id),
            source_message_ids=ordered_sources,
            trigger_message_id=cycle_sources[-1],
            status="dispatched",
        )
    return item


def _chat_replan_eligible(
    conn: sqlite3.Connection,
    row: Mapping[str, object],
    delivery: Mapping[str, object],
    *,
    interaction_plan_id: str,
) -> bool:
    payload = delivery.get("payload")
    if not isinstance(payload, Mapping):
        return False
    predecessors = validated_response_cycle_predecessors(
        conn,
        str(row.get("id") or ""),
    )
    if len(predecessors) >= MAX_SITUATION_REVISIONS:
        return False
    if not all((
        str(row.get("state") or "") == "processing",
        bool(str(row.get("generation_started_at") or "")),
        not str(row.get("effects_admitted_at") or ""),
        not str(row.get("prepared_delivery_json") or ""),
        not str(row.get("prepared_delivery_sha256") or ""),
        not str(row.get("successor_cycle_id") or ""),
        str(delivery.get("delivery_class") or "") in {"social", "operational"},
        str(payload.get("response_kind") or "") == "chat",
        not str(payload.get("task_id") or ""),
        not str(payload.get("automation_job_id") or ""),
        not str(payload.get("automation_action_plan_id") or ""),
    )):
        return False
    plan_id = str(interaction_plan_id or "").strip()
    if not plan_id:
        return False
    plan = conn.execute(
        """
        SELECT thread_id,status,plan_json FROM interaction_plans WHERE id=?
        """,
        (plan_id,),
    ).fetchone()
    try:
        plan_payload = json.loads(str(plan[2] or "{}")) if plan is not None else {}
    except json.JSONDecodeError:
        plan_payload = {}
    plan_actions = plan_payload.get("actions") if isinstance(plan_payload, dict) else None
    actions_are_chat_only = bool(
        isinstance(plan_actions, list)
        and plan_actions
        and all(
            isinstance(action, Mapping)
            and str(action.get("type") or "") == "respond"
            and action.get("requires_tools") is False
            and str(action.get("risk_level") or "") in {"none", "low"}
            for action in plan_actions
        )
    )
    if (
        plan is None
        or str(plan[0]) != str(row.get("thread_id") or "")
        or str(plan[1]) != "planned"
        or not actions_are_chat_only
    ):
        return False
    assistant = conn.execute(
        """
        SELECT 1 FROM conversation_messages
        WHERE response_cycle_id=? AND thread_id=? AND role='assistant' LIMIT 1
        """,
        (str(row["id"]), str(row["thread_id"])),
    ).fetchone()
    return assistant is None


def prepare_or_supersede_response_cycle_delivery(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    lease_token: str,
    source_set_hash: str,
    delivery: Mapping[str, object],
    expected_destination: str,
    interaction_plan_id: str = "",
    context_message_ids: Sequence[str] = (),
    assistant_metadata: Mapping[str, object] | None = None,
) -> dict:
    """Atomically prepare one reply or fence one stale, pre-effect chat draft."""

    require_response_cycle_successor_schema(conn)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = _cycle_row(conn, cycle_id)
    now = _assert_current_cycle_lease(
        row,
        lease_token=lease_token,
        source_set_hash=source_set_hash,
        allowed_states=("processing", "commit_ready"),
    )
    item = _validated_prepared_delivery(
        conn, row, delivery, expected_destination=expected_destination,
    )
    newer = conn.execute(
        """
        SELECT MIN(inbound_sequence) FROM conversation_messages m
        WHERE m.thread_id=? AND m.role='user' AND m.inbound_sequence>?
          AND NOT EXISTS(
              SELECT 1 FROM conversation_response_cycle_sources s
              WHERE s.source_message_id=m.id
          )
        """,
        (str(row["thread_id"]), int(row["through_inbound_sequence"])),
    ).fetchone()
    newer_sequence = int(newer[0]) if newer and newer[0] is not None else 0
    if newer_sequence and _chat_replan_eligible(
        conn, row, item, interaction_plan_id=interaction_plan_id,
    ):
        cycle_sources = [
            str(value[0])
            for value in conn.execute(
                """
                SELECT source_message_id FROM conversation_response_cycle_sources
                WHERE cycle_id=? ORDER BY source_order
                """,
                (str(cycle_id),),
            ).fetchall()
        ]
        ordered_sources = []
        for message_id in [*context_message_ids, *cycle_sources]:
            token = str(message_id or "").strip()
            if token and token not in ordered_sources:
                ordered_sources.append(token)
        bind_interaction_plan_sources(
            conn,
            plan_id=str(interaction_plan_id),
            cycle_id=str(cycle_id),
            source_message_ids=ordered_sources,
            trigger_message_id=cycle_sources[-1],
            status="cancelled",
        )
        cursor = conn.execute(
            """
            UPDATE conversation_response_cycles
            SET state='manual_hold',blocks_thread=0,blocks_effects=0,
                recovery_disposition='superseded_precommit',
                recovery_decided_at=?,recovery_decided_by='response_coordinator',
                last_error='newer_private_input',lease_owner='',lease_token='',
                lease_expires_at='',updated_at=?
            WHERE id=? AND state='processing' AND source_set_hash=?
              AND lease_token=? AND lease_owner<>'' AND lease_expires_at>?
              AND generation_started_at<>'' AND effects_admitted_at=''
              AND prepared_delivery_json='' AND prepared_delivery_sha256=''
              AND predecessor_cycle_id=? AND replan_depth=?
              AND successor_cycle_id=''
            """,
            (
                now,
                now,
                str(cycle_id),
                str(source_set_hash),
                str(lease_token),
                now,
                str(row.get("predecessor_cycle_id") or ""),
                int(row.get("replan_depth") or 0),
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            raise ResponseCycleLeaseError("response_cycle_lease_lost")
        return {
            "status": "superseded",
            "cycle_id": str(cycle_id),
            "newer_inbound_sequence": newer_sequence,
        }
    prepared = prepare_response_cycle_delivery(
        conn,
        cycle_id=cycle_id,
        lease_token=lease_token,
        source_set_hash=source_set_hash,
        delivery=item,
        expected_destination=expected_destination,
        interaction_plan_id=interaction_plan_id,
        context_message_ids=context_message_ids,
        assistant_metadata=assistant_metadata,
    )
    return {"status": "prepared", "delivery": prepared}


def load_prepared_response_cycle(conn: sqlite3.Connection, cycle_id: str) -> dict:
    """Read and verify one prepared intent plus its deterministic private route."""

    row = _cycle_row(conn, cycle_id)
    encoded = str(row["prepared_delivery_json"] or "")
    digest = str(row["prepared_delivery_sha256"] or "")
    if not encoded or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
        raise ResponseCycleRecoveryError("response_cycle_prepared_hash_invalid")
    try:
        raw = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResponseCycleRecoveryError("response_cycle_prepared_json_invalid") from exc
    destination, _ = _cycle_route(row)
    item = _validated_prepared_delivery(
        conn, row, raw, expected_destination=destination,
    )
    if _canonical(item) != encoded:
        raise ResponseCycleRecoveryError("response_cycle_prepared_not_canonical")
    return {"cycle": row, "delivery": item, "destination": destination}


def bind_prepared_response_delivery(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    lease_token: str,
    source_set_hash: str,
    delivery: Mapping[str, object],
) -> dict:
    """Bind only an Outbox fact that exactly matches the immutable envelope."""

    loaded = load_prepared_response_cycle(conn, cycle_id)
    row = loaded["cycle"]
    now = _assert_current_cycle_lease(
        row,
        lease_token=lease_token,
        source_set_hash=source_set_hash,
        allowed_states=("commit_ready",),
    )
    delivery_id = str(delivery.get("id") or "").strip()[:80]
    if (
        not delivery_id
        or not prepared_qq_delivery_matches(
            loaded["delivery"], delivery, loaded["destination"],
        )
    ):
        raise ResponseCycleRecoveryError("response_cycle_outbox_fact_drift")
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles
        SET state='outbox_queued',delivery_id=?,lease_owner='',lease_token='',
            lease_expires_at='',updated_at=?
        WHERE id=? AND state='commit_ready' AND source_set_hash=?
          AND lease_token=? AND lease_owner<>'' AND lease_expires_at>?
          AND logical_response_id=? AND outbox_dedupe_key=?
        """,
        (
            delivery_id, now, str(cycle_id), str(source_set_hash),
            str(lease_token), now, str(row["logical_response_id"]),
            str(row["outbox_dedupe_key"]),
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    conn.execute(
        """
        UPDATE conversation_messages
        SET logical_response_id=?,delivery_id=?,delivery_projection_state='outbox_queued'
        WHERE response_cycle_id=? AND thread_id=? AND role='assistant'
        """,
        (
            str(row["logical_response_id"]), delivery_id,
            str(cycle_id), str(row["thread_id"]),
        ),
    )
    return {
        "id": str(cycle_id), "state": "outbox_queued",
        "logical_response_id": str(row["logical_response_id"]),
        "delivery_id": delivery_id,
    }


def complete_response_cycle_outbox(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    lease_token: str,
    logical_response_id: str,
    delivery_id: str,
) -> dict:
    logical_id = str(logical_response_id or "").strip()[:120]
    delivery = str(delivery_id or "").strip()[:80]
    if not logical_id or not delivery:
        raise ValueError("response_cycle_delivery_binding_required")
    now = utc_now()
    identity = conn.execute(
        """
        SELECT logical_response_id,outbox_dedupe_key,thread_id
        FROM conversation_response_cycles
        WHERE id=? AND state IN ('processing','commit_ready') AND lease_token=?
          AND lease_owner<>'' AND lease_expires_at>?
        """,
        (str(cycle_id), str(lease_token), now),
    ).fetchone()
    if not identity:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    if (
        logical_id != str(identity[0] or "")
        or str(identity[1] or "") != f"qq:response:{logical_id}"
    ):
        raise ValueError("response_cycle_identity_drift")
    cursor = conn.execute(
        """
        UPDATE conversation_response_cycles
        SET state='outbox_queued',delivery_id=?,
            lease_owner='',lease_token='',lease_expires_at='',updated_at=?
        WHERE id=? AND state IN ('processing','commit_ready') AND lease_token=?
          AND lease_owner<>'' AND lease_expires_at>?
        """,
        (delivery, now, str(cycle_id), str(lease_token), now),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ResponseCycleLeaseError("response_cycle_lease_lost")
    conn.execute(
        """
        UPDATE conversation_messages
        SET logical_response_id=?,delivery_id=?,delivery_projection_state='outbox_queued'
        WHERE response_cycle_id=? AND thread_id=? AND role='assistant'
        """,
        (logical_id, delivery, str(cycle_id), str(identity[2])),
    )
    return {
        "id": str(cycle_id),
        "state": "outbox_queued",
        "logical_response_id": logical_id,
        "delivery_id": delivery,
    }


def bind_interaction_plan_sources(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    cycle_id: str,
    source_message_ids: Sequence[str],
    trigger_message_id: str,
    status: str = "dispatched",
) -> list[dict]:
    sources = [str(item or "").strip() for item in source_message_ids]
    trigger = str(trigger_message_id or "").strip()
    if not sources or trigger not in sources or len(set(sources)) != len(sources):
        raise ValueError("interaction_plan_sources_invalid")
    if status not in {"dispatched", "cancelled"}:
        raise ValueError("interaction_plan_source_status_invalid")
    plan = conn.execute(
        "SELECT thread_id,request_message_id,response_cycle_id FROM interaction_plans WHERE id=?",
        (str(plan_id),),
    ).fetchone()
    cycle = conn.execute(
        "SELECT thread_id FROM conversation_response_cycles WHERE id=?",
        (str(cycle_id),),
    ).fetchone()
    if not plan or not cycle:
        raise ValueError("interaction_plan_cycle_not_found")
    if str(plan[0]) != str(cycle[0]):
        raise ValueError("interaction_plan_cycle_thread_mismatch")
    now = utc_now()
    result: list[dict] = []
    for order, message_id in enumerate(sources):
        message = conn.execute(
            "SELECT thread_id FROM conversation_messages WHERE id=? AND role='user'",
            (message_id,),
        ).fetchone()
        if not message or str(message[0]) != str(plan[0]):
            raise ValueError("interaction_plan_source_thread_mismatch")
        role = "trigger" if message_id == trigger else "context"
        conn.execute(
            """
            INSERT INTO interaction_plan_source_messages(
                plan_id,source_message_id,source_order,source_role,created_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(plan_id,source_message_id) DO UPDATE SET
                source_order=excluded.source_order,source_role=excluded.source_role
            """,
            (str(plan_id), message_id, order, role, now),
        )
        result.append({
            "plan_id": str(plan_id),
            "source_message_id": message_id,
            "source_order": order,
            "source_role": role,
        })
    conn.execute(
        """
        UPDATE interaction_plans
        SET response_cycle_id=?,request_message_id=?,status=?,updated_at=?
        WHERE id=?
        """,
        (str(cycle_id), trigger, status, now, str(plan_id)),
    )
    return result


def project_delivery_state(
    conn: sqlite3.Connection,
    delivery_id: str,
    state: str,
) -> int:
    state = str(state or "").strip()
    if state not in _DELIVERY_STATES:
        raise ValueError("conversation_delivery_projection_state_invalid")
    cursor = conn.execute(
        "UPDATE conversation_messages SET delivery_projection_state=? WHERE delivery_id=? AND role='assistant'",
        (state, str(delivery_id or "").strip()),
    )
    return int(cursor.rowcount or 0)


__all__ = [
    "ContinuousPrivateConversationDisabledError",
    "ResponseCycleBusyError",
    "ResponseCycleLeaseError",
    "ResponseCycleRecoveryError",
    "ResponseCycleEffectBlockedError",
    "ResponseCycleSupersededError",
    "accept_private_ingress",
    "acquire_response_cycle",
    "assert_response_cycle_effects_allowed",
    "bind_interaction_plan_sources",
    "bind_prepared_response_delivery",
    "claim_prepared_response_cycle",
    "complete_response_cycle_outbox",
    "continuous_private_conversation_enabled",
    "mark_response_cycle_failure",
    "mark_response_cycle_generation_started",
    "load_prepared_response_cycle",
    "hold_response_cycle",
    "isolate_response_cycle",
    "prepare_response_cycle_delivery",
    "prepare_or_supersede_response_cycle_delivery",
    "project_delivery_state",
    "reclaim_response_cycle",
    "resolve_response_cycle_effects",
    "set_continuous_private_conversation_feature",
    "validated_response_cycle_predecessors",
]
