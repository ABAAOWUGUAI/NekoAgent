#!/usr/bin/env python3
"""Assistant Core v32: one provenance Receipt for QQ voice input."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError, utc_now


VOICE_INPUT_FETCH_FEATURE_FLAG = "voice_input_fetch_v1"
VOICE_INPUT_FEATURE_FLAG = "voice_input_v1"
DEFAULT_QQ_VOICE_HOST_SUFFIXES = ("multimedia.nt.qq.com.cn",)
RECEIPT_COLUMNS = (
    "id", "assistant_id", "thread_id", "conversation_message_id",
    "channel_type", "external_message_id", "attachment_index", "media_kind",
    "declared_media_type", "detected_media_type", "size_bytes", "duration_ms",
    "sha256", "fetch_status", "transcription_status", "transcriber_role",
    "language", "confidence", "retention_class", "source_deleted_at",
    "error_kind", "created_at", "updated_at",
)


def _checksum() -> str:
    return hashlib.sha256(json.dumps({
        "flag": VOICE_INPUT_FETCH_FEATURE_FLAG,
        "suffixes": DEFAULT_QQ_VOICE_HOST_SUFFIXES,
        "columns": RECEIPT_COLUMNS,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


VOICE_MESSAGE_MIGRATION_CHECKSUM = _checksum()


def _input_checksum() -> str:
    return hashlib.sha256(json.dumps({
        "flag": VOICE_INPUT_FEATURE_FLAG,
        "adapter": "whisper_cpp",
        "max_duration_seconds": 60,
        "role": "speech_transcription",
        "receipt_dispatch": ("dispatch_status", "delivery_id"),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


VOICE_INPUT_MIGRATION_CHECKSUM = _input_checksum()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_voice_message_v1(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"assistant_instances", "assistant_feature_flags", "qq_channel_settings"}.issubset(tables):
        raise MigrationDriftError("voice_message_prerequisite_missing")
    columns = _columns(conn, "qq_channel_settings")
    additions = (
        ("voice_record_host_suffixes_json", "TEXT NOT NULL DEFAULT '[\"multimedia.nt.qq.com.cn\"]'"),
        ("voice_record_max_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ("voice_record_connect_timeout_seconds", "INTEGER NOT NULL DEFAULT 5"),
        ("voice_record_read_timeout_seconds", "INTEGER NOT NULL DEFAULT 15"),
        ("voice_record_max_redirects", "INTEGER NOT NULL DEFAULT 2"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE qq_channel_settings ADD COLUMN {name} {definition}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS voice_message_receipts (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            thread_id TEXT,
            conversation_message_id TEXT,
            channel_type TEXT NOT NULL CHECK(channel_type='qq'),
            external_message_id TEXT NOT NULL,
            attachment_index INTEGER NOT NULL CHECK(attachment_index>=0),
            media_kind TEXT NOT NULL CHECK(media_kind='voice'),
            declared_media_type TEXT NOT NULL DEFAULT '',
            detected_media_type TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes>0),
            duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
            sha256 TEXT NOT NULL DEFAULT '' CHECK(sha256='' OR length(sha256)=64),
            fetch_status TEXT NOT NULL CHECK(fetch_status IN ('pending','fetched','failed')),
            transcription_status TEXT NOT NULL DEFAULT 'not_started'
                CHECK(transcription_status IN ('not_started','running','completed','failed')),
            transcriber_role TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            confidence REAL,
            retention_class TEXT NOT NULL DEFAULT 'transient' CHECK(retention_class='transient'),
            source_deleted_at TEXT NOT NULL DEFAULT '',
            error_kind TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(channel_type,external_message_id,attachment_index)
        );
        CREATE INDEX IF NOT EXISTS idx_voice_receipts_status
        ON voice_message_receipts(fetch_status,transcription_status,updated_at DESC);
        """,
    )
    conn.execute(
        "INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,0,?)",
        (VOICE_INPUT_FETCH_FEATURE_FLAG, utc_now()),
    )


def apply_voice_input_v1(conn: sqlite3.Connection) -> None:
    require_voice_message_schema(conn)
    columns = _columns(conn, "qq_channel_settings")
    additions = (
        ("voice_transcription_adapter", "TEXT NOT NULL DEFAULT 'whisper_cpp'"),
        ("voice_transcription_executable", "TEXT NOT NULL DEFAULT ''"),
        ("voice_transcription_model_path", "TEXT NOT NULL DEFAULT ''"),
        ("voice_transcription_model_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("voice_transcription_ffmpeg", "TEXT NOT NULL DEFAULT '/usr/bin/ffmpeg'"),
        ("voice_transcription_threads", "INTEGER NOT NULL DEFAULT 2"),
        ("voice_transcription_timeout_seconds", "INTEGER NOT NULL DEFAULT 90"),
        ("voice_transcription_max_duration_seconds", "INTEGER NOT NULL DEFAULT 60"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE qq_channel_settings ADD COLUMN {name} {definition}")
    receipt_columns = _columns(conn, "voice_message_receipts")
    receipt_additions = (
        (
            "dispatch_status",
            "TEXT NOT NULL DEFAULT 'not_started' "
            "CHECK(dispatch_status IN ('not_started','running','completed','failed'))",
        ),
        ("delivery_id", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, definition in receipt_additions:
        if name not in receipt_columns:
            conn.execute(f"ALTER TABLE voice_message_receipts ADD COLUMN {name} {definition}")
    conn.execute(
        "INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,0,?)",
        (VOICE_INPUT_FEATURE_FLAG, utc_now()),
    )


def inspect_voice_input_schema(conn: sqlite3.Connection) -> dict:
    required = {
        "voice_transcription_adapter", "voice_transcription_executable",
        "voice_transcription_model_path", "voice_transcription_model_sha256",
        "voice_transcription_ffmpeg", "voice_transcription_threads",
        "voice_transcription_timeout_seconds", "voice_transcription_max_duration_seconds",
    }
    settings = _columns(conn, "qq_channel_settings")
    receipt_columns = _columns(conn, "voice_message_receipts")
    flag = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (VOICE_INPUT_FEATURE_FLAG,),
    ).fetchone()
    return {
        "ok": (
            required.issubset(settings)
            and {"dispatch_status", "delivery_id"}.issubset(receipt_columns)
            and flag is not None
        ),
        "contract_checksum": VOICE_INPUT_MIGRATION_CHECKSUM,
        "missing_settings_columns": sorted(required - settings),
        "missing_receipt_columns": sorted(
            {"dispatch_status", "delivery_id"} - receipt_columns
        ),
        "feature_enabled": bool(flag and flag[0]),
    }


def require_voice_input_schema(conn: sqlite3.Connection) -> dict:
    result = inspect_voice_input_schema(conn)
    if not result["ok"]:
        raise MigrationDriftError("voice_input_schema_drift:" + json.dumps(result, sort_keys=True, separators=(",", ":")))
    return result


def inspect_voice_message_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(set(RECEIPT_COLUMNS) - _columns(conn, "voice_message_receipts")) if "voice_message_receipts" in tables else list(RECEIPT_COLUMNS)
    settings = _columns(conn, "qq_channel_settings") if "qq_channel_settings" in tables else set()
    required_settings = {"voice_record_host_suffixes_json", "voice_record_max_bytes", "voice_record_connect_timeout_seconds", "voice_record_read_timeout_seconds", "voice_record_max_redirects"}
    flag = conn.execute("SELECT enabled FROM assistant_feature_flags WHERE name=?", (VOICE_INPUT_FETCH_FEATURE_FLAG,)).fetchone() if "assistant_feature_flags" in tables else None
    return {"ok": "voice_message_receipts" in tables and not missing and required_settings.issubset(settings) and flag is not None, "contract_checksum": VOICE_MESSAGE_MIGRATION_CHECKSUM, "missing_columns": missing, "missing_settings_columns": sorted(required_settings-settings), "feature_enabled": bool(flag and flag[0])}


def require_voice_message_schema(conn: sqlite3.Connection) -> dict:
    result = inspect_voice_message_schema(conn)
    if not result["ok"]:
        raise MigrationDriftError("voice_message_schema_drift:" + json.dumps(result, sort_keys=True, separators=(",", ":")))
    return result


__all__ = ["DEFAULT_QQ_VOICE_HOST_SUFFIXES", "VOICE_INPUT_FEATURE_FLAG", "VOICE_INPUT_FETCH_FEATURE_FLAG", "VOICE_INPUT_MIGRATION_CHECKSUM", "VOICE_MESSAGE_MIGRATION_CHECKSUM", "apply_voice_input_v1", "apply_voice_message_v1", "inspect_voice_input_schema", "inspect_voice_message_schema", "require_voice_input_schema", "require_voice_message_schema"]
