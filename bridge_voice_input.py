#!/usr/bin/env python3
"""VM-1C controlled fetch lifecycle using the single Voice Receipt fact."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import utc_now
from bridge_voice_media_fetch import VoiceMediaFetchError, fetch_qq_voice_to_temp
from bridge_voice_message_schema import VOICE_INPUT_FETCH_FEATURE_FLAG, require_voice_message_schema
from bridge_voice_message_schema import VOICE_INPUT_FEATURE_FLAG, require_voice_input_schema
from bridge_voice_message_source import MAX_QQ_VOICE_BYTES, validate_qq_private_record_source
from bridge_voice_input_runtime import VoiceTranscriptionError, transcribe_voice_file, transcription_policy


class VoiceInputError(ValueError):
    pass


def voice_input_fetch_enabled(conn: sqlite3.Connection) -> bool:
    require_voice_message_schema(conn)
    row = conn.execute("SELECT enabled FROM assistant_feature_flags WHERE name=?", (VOICE_INPUT_FETCH_FEATURE_FLAG,)).fetchone()
    return bool(row and row[0])


def voice_input_enabled(conn: sqlite3.Connection) -> bool:
    require_voice_input_schema(conn)
    row = conn.execute("SELECT enabled FROM assistant_feature_flags WHERE name=?", (VOICE_INPUT_FEATURE_FLAG,)).fetchone()
    return bool(row and row[0])


def set_voice_input_fetch_enabled(conn: sqlite3.Connection, enabled: bool) -> dict:
    require_voice_message_schema(conn)
    now = utc_now()
    conn.execute("UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?", (1 if enabled else 0, now, VOICE_INPUT_FETCH_FEATURE_FLAG))
    conn.execute("UPDATE qq_channel_settings SET config_version=config_version+1,updated_at=? WHERE channel_id='qq-main'", (now,))
    version = conn.execute("SELECT config_version FROM qq_channel_settings WHERE channel_id='qq-main'").fetchone()[0]
    return {"enabled": bool(enabled), "config_version": int(version), "updated_at": now}


def set_voice_input_enabled(conn: sqlite3.Connection, enabled: bool) -> dict:
    require_voice_input_schema(conn)
    now = utc_now()
    conn.execute("UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?", (1 if enabled else 0, now, VOICE_INPUT_FEATURE_FLAG))
    conn.execute("UPDATE qq_channel_settings SET config_version=config_version+1,updated_at=? WHERE channel_id='qq-main'", (now,))
    version = conn.execute("SELECT config_version FROM qq_channel_settings WHERE channel_id='qq-main'").fetchone()[0]
    return {"enabled": bool(enabled), "config_version": int(version), "updated_at": now}


def complete_voice_dispatch(
    conn: sqlite3.Connection,
    receipt_id: str,
    *,
    conversation_message_id: str = "",
    delivery_id: str = "",
    error_kind: str = "",
) -> None:
    """Set the final Bridge dispatch state without storing transcript text."""

    require_voice_input_schema(conn)
    now = utc_now()
    if error_kind:
        conn.execute(
            "UPDATE voice_message_receipts SET dispatch_status='failed',error_kind=?,"
            "updated_at=? WHERE id=? AND dispatch_status='running'",
            (str(error_kind)[:80], now, str(receipt_id)),
        )
    else:
        if not conversation_message_id or not delivery_id:
            raise VoiceInputError("voice_dispatch_evidence_missing")
        cursor = conn.execute(
            "UPDATE voice_message_receipts SET thread_id=(SELECT thread_id FROM "
            "conversation_messages WHERE id=?),conversation_message_id=?,delivery_id=?,"
            "dispatch_status='completed',error_kind='',updated_at=? "
            "WHERE id=? AND dispatch_status='running'",
            (
                conversation_message_id, conversation_message_id, delivery_id,
                now, str(receipt_id),
            ),
        )
        if cursor.rowcount != 1:
            raise VoiceInputError("voice_dispatch_receipt_state_invalid")
    conn.commit()


def _policy(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT voice_record_host_suffixes_json,voice_record_max_bytes,"
        "voice_record_connect_timeout_seconds,voice_record_read_timeout_seconds,"
        "voice_record_max_redirects FROM qq_channel_settings WHERE channel_id='qq-main'"
    ).fetchone()
    if not row:
        raise VoiceInputError("qq_voice_policy_missing")
    try:
        values = json.loads(str(row[0]))
        max_bytes = int(row[1])
        connect_timeout = int(row[2])
        read_timeout = int(row[3])
        max_redirects = int(row[4])
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VoiceInputError("qq_voice_host_policy_invalid") from exc
    if not isinstance(values, list) or not values:
        raise VoiceInputError("qq_voice_host_policy_invalid")
    if not 1 <= max_bytes <= MAX_QQ_VOICE_BYTES:
        raise VoiceInputError("qq_voice_max_bytes_policy_invalid")
    if not 1 <= connect_timeout <= 30:
        raise VoiceInputError("qq_voice_connect_timeout_policy_invalid")
    if not 1 <= read_timeout <= 60:
        raise VoiceInputError("qq_voice_read_timeout_policy_invalid")
    if not 0 <= max_redirects <= 5:
        raise VoiceInputError("qq_voice_redirect_policy_invalid")
    return {
        "allowed_host_suffixes": tuple(str(value) for value in values),
        "max_bytes": max_bytes,
        "connect_timeout_seconds": connect_timeout,
        "read_timeout_seconds": read_timeout,
        "max_redirects": max_redirects,
    }


def fetch_owner_voice(conn: sqlite3.Connection, payload: Mapping[str, object], **fetch_kwargs) -> dict:
    if not voice_input_fetch_enabled(conn):
        raise VoiceInputError("voice_input_fetch_disabled")
    assistant = conn.execute("SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1").fetchone()
    if not assistant:
        raise VoiceInputError("active_assistant_missing")
    policy = _policy(conn)
    try:
        validated = validate_qq_private_record_source(
            payload,
            allowed_host_suffixes=policy["allowed_host_suffixes"],
        )
    except ValueError as exc:
        kind = str(exc) if str(exc).startswith("qq_voice_") else "qq_voice_source_invalid"
        raise VoiceInputError(kind) from exc
    declared_size = validated.get("declared_size_bytes")
    if declared_size is not None and int(declared_size) > int(policy["max_bytes"]):
        raise VoiceInputError("qq_voice_media_too_large")
    message_id = str(validated["external_message_id"])
    index = int(validated["attachment_index"])
    receipt_id = "voice-receipt-" + hashlib.sha256(
        f"qq\n{message_id}\n{index}".encode()
    ).hexdigest()[:32]
    now = utc_now()
    existing = conn.execute(
        "SELECT fetch_status FROM voice_message_receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    if existing and str(existing[0]) == "fetched":
        return {"id": receipt_id, "fetch_status": "fetched", "deduplicated": True}
    conn.execute(
        "INSERT OR IGNORE INTO voice_message_receipts("
        "id,assistant_id,channel_type,external_message_id,attachment_index,media_kind,"
        "fetch_status,created_at,updated_at) VALUES(?,?,?,?,?,'voice','pending',?,?)",
        (receipt_id, assistant[0], "qq", message_id, index, now, now),
    )
    conn.commit()
    try:
        with fetch_qq_voice_to_temp(validated, **policy, **fetch_kwargs) as media:
            finished = utc_now()
            conn.execute(
                "UPDATE voice_message_receipts SET detected_media_type=?,size_bytes=?,"
                "sha256=?,fetch_status='fetched',source_deleted_at=?,error_kind='',updated_at=? "
                "WHERE id=?",
                (
                    media.detected_media_type, media.size_bytes, media.sha256,
                    finished, finished, receipt_id,
                ),
            )
            conn.commit()
            return {
                "id": receipt_id,
                "fetch_status": "fetched",
                "detected_media_type": media.detected_media_type,
                "size_bytes": media.size_bytes,
                "sha256": media.sha256,
                "source_deleted": True,
                "deduplicated": False,
            }
    except (VoiceMediaFetchError, ValueError) as exc:
        kind = str(exc) if str(exc).startswith("qq_voice_") else "qq_voice_fetch_failed"
        failed = utc_now()
        conn.execute(
            "UPDATE voice_message_receipts SET fetch_status='failed',source_deleted_at=?,"
            "error_kind=?,updated_at=? WHERE id=?",
            (failed, kind[:80], failed, receipt_id),
        )
        conn.commit()
        raise VoiceInputError(kind) from exc


def process_owner_voice(conn: sqlite3.Connection, payload: Mapping[str, object], **fetch_kwargs) -> dict:
    if not voice_input_enabled(conn):
        raise VoiceInputError("voice_input_disabled")
    assistant = conn.execute("SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1").fetchone()
    if not assistant:
        raise VoiceInputError("active_assistant_missing")
    policy = _policy(conn)
    try:
        validated = validate_qq_private_record_source(payload, allowed_host_suffixes=policy["allowed_host_suffixes"])
    except ValueError as exc:
        raise VoiceInputError(str(exc) if str(exc).startswith("qq_voice_") else "qq_voice_source_invalid") from exc
    message_id, index = str(validated["external_message_id"]), int(validated["attachment_index"])
    receipt_id = "voice-receipt-" + hashlib.sha256(f"qq\n{message_id}\n{index}".encode()).hexdigest()[:32]
    existing = conn.execute(
        "SELECT transcription_status,dispatch_status,conversation_message_id,delivery_id "
        "FROM voice_message_receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    if existing and str(existing[1]) == "completed":
        return {
            "id": receipt_id,
            "deduplicated": True,
            "dispatch_required": False,
            "delivery_queued": bool(str(existing[3] or "")),
        }
    if existing and str(existing[1]) == "running":
        return {
            "id": receipt_id,
            "deduplicated": True,
            "dispatch_required": False,
            "processing": True,
            "delivery_queued": False,
        }
    now = utc_now()
    conn.execute("INSERT OR IGNORE INTO voice_message_receipts(id,assistant_id,channel_type,external_message_id,attachment_index,media_kind,fetch_status,created_at,updated_at) VALUES(?,?,?,?,?,'voice','pending',?,?)", (receipt_id, assistant[0], "qq", message_id, index, now, now))
    conn.execute(
        "UPDATE voice_message_receipts SET transcription_status='running',"
        "dispatch_status='not_started',delivery_id='',error_kind='',updated_at=? WHERE id=?",
        (now, receipt_id),
    )
    conn.commit()
    try:
        with fetch_qq_voice_to_temp(validated, **policy, **fetch_kwargs) as media:
            fetched = utc_now()
            conn.execute(
                "UPDATE voice_message_receipts SET detected_media_type=?,size_bytes=?,sha256=?,"
                "fetch_status='fetched',source_deleted_at=?,error_kind='',updated_at=? WHERE id=?",
                (
                    media.detected_media_type, media.size_bytes, media.sha256,
                    fetched, fetched, receipt_id,
                ),
            )
            conn.commit()
            transcript = transcribe_voice_file(media.path, transcription_policy(conn), temp_root=fetch_kwargs.get("temp_root"))
            finished = utc_now()
            conn.execute(
                "UPDATE voice_message_receipts SET duration_ms=?,transcription_status='completed',"
                "transcriber_role=?,language=?,dispatch_status='running',source_deleted_at=?,"
                "error_kind='',updated_at=? WHERE id=?",
                (
                    transcript["duration_ms"], transcript["role"], transcript["language"],
                    finished, finished, receipt_id,
                ),
            )
            conn.commit()
            return {"id": receipt_id, "transcript": transcript["text"], "duration_ms": transcript["duration_ms"], "deduplicated": False, "dispatch_required": True}
    except (VoiceMediaFetchError, VoiceTranscriptionError, ValueError) as exc:
        kind = str(exc) if str(exc).startswith(("qq_voice_", "voice_")) else "voice_input_failed"
        failed = utc_now()
        conn.execute("UPDATE voice_message_receipts SET fetch_status=CASE WHEN fetch_status='pending' THEN 'failed' ELSE fetch_status END,transcription_status='failed',dispatch_status='failed',source_deleted_at=?,error_kind=?,updated_at=? WHERE id=?", (failed, kind[:80], failed, receipt_id))
        conn.commit()
        raise VoiceInputError(kind) from exc


__all__ = ["VoiceInputError", "complete_voice_dispatch", "fetch_owner_voice", "process_owner_voice", "set_voice_input_enabled", "set_voice_input_fetch_enabled", "voice_input_enabled", "voice_input_fetch_enabled"]
