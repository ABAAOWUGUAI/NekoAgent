#!/usr/bin/env python3
"""v45 persistence for inherited ambient-participation time windows."""

from __future__ import annotations

import json
import sqlite3
import hashlib

from bridge_group_participation_windows import (
    DEFAULT_AMBIENT_PARTICIPATION_POLICY,
    normalize_ambient_windows,
)
from bridge_migrations import MigrationDriftError, utc_now


GROUP_PARTICIPATION_DEFAULTS_TABLE = "group_participation_defaults"
GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE = "group_participation_window_overrides"
_DEFAULT_COLUMNS = {
    "id", "timezone", "allowed_windows_json", "effective_at", "version", "updated_by", "updated_at",
}
_OVERRIDE_COLUMNS = {
    "group_id", "timezone", "allowed_windows_json", "effective_at", "version", "updated_by", "updated_at",
}
_CONTRACT = {
    "defaults_table": GROUP_PARTICIPATION_DEFAULTS_TABLE,
    "defaults_columns": sorted(_DEFAULT_COLUMNS),
    "overrides_table": GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE,
    "overrides_columns": sorted(_OVERRIDE_COLUMNS),
    "default_policy": DEFAULT_AMBIENT_PARTICIPATION_POLICY,
}
GROUP_PARTICIPATION_WINDOWS_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def _encoded(policy: object) -> tuple[str, str, str]:
    normalized = normalize_ambient_windows(policy)
    return (
        str(normalized["timezone"]),
        json.dumps(normalized["allowed_windows"], ensure_ascii=False, separators=(",", ":")),
        str(normalized["effective_at"]),
    )


def apply_group_participation_windows_v1(conn: sqlite3.Connection) -> None:
    """Create one default and optional per-group overrides without copied policy rows."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {GROUP_PARTICIPATION_DEFAULTS_TABLE} (
            id INTEGER PRIMARY KEY CHECK(id=1),
            timezone TEXT NOT NULL CHECK(timezone='Asia/Shanghai'),
            allowed_windows_json TEXT NOT NULL CHECK(json_valid(allowed_windows_json))
                CHECK(json_array_length(allowed_windows_json)=2),
            effective_at TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>=1),
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE} (
            group_id TEXT PRIMARY KEY,
            timezone TEXT NOT NULL CHECK(timezone='Asia/Shanghai'),
            allowed_windows_json TEXT NOT NULL CHECK(json_valid(allowed_windows_json))
                CHECK(json_array_length(allowed_windows_json)=2),
            effective_at TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>=1),
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """,
    )
    timezone_name, windows_json, effective_at = _encoded(DEFAULT_AMBIENT_PARTICIPATION_POLICY)
    conn.execute(
        f"""INSERT OR IGNORE INTO {GROUP_PARTICIPATION_DEFAULTS_TABLE}(
                id,timezone,allowed_windows_json,effective_at,version,updated_by,updated_at
            ) VALUES(1,?,?,?,?,?,?)""",
        (timezone_name, windows_json, effective_at, 1, "migration-v45", utc_now()),
    )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def require_group_participation_windows_schema(conn: sqlite3.Connection) -> dict:
    tables = _tables(conn)
    missing_tables = sorted({GROUP_PARTICIPATION_DEFAULTS_TABLE, GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE} - tables)
    default_columns = _columns(conn, GROUP_PARTICIPATION_DEFAULTS_TABLE) if not missing_tables else set()
    override_columns = _columns(conn, GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE) if not missing_tables else set()
    missing_columns = sorted((_DEFAULT_COLUMNS - default_columns) | (_OVERRIDE_COLUMNS - override_columns))
    default_exists = bool(
        not missing_tables
        and conn.execute(f"SELECT 1 FROM {GROUP_PARTICIPATION_DEFAULTS_TABLE} WHERE id=1").fetchone()
    )
    if missing_tables or missing_columns or not default_exists:
        raise MigrationDriftError(
            "group_participation_windows_schema_drift:"
            + ",".join(missing_tables)
            + "|"
            + ",".join(missing_columns)
            + ("|default_row" if not default_exists else ""),
        )
    return {"ok": True}


def _row_policy(row: sqlite3.Row | tuple | None) -> dict:
    if row is None:
        raise MigrationDriftError("group_participation_windows_policy_missing")
    data = dict(row) if isinstance(row, sqlite3.Row) else {
        "timezone": row[0], "allowed_windows_json": row[1], "effective_at": row[2], "version": row[3],
    }
    try:
        windows = json.loads(str(data["allowed_windows_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationDriftError("group_participation_windows_json_invalid") from exc
    policy = normalize_ambient_windows({
        "timezone": data["timezone"], "allowed_windows": windows, "effective_at": data["effective_at"],
    })
    policy["version"] = int(data["version"])
    return policy


def effective_ambient_window_policy(conn: sqlite3.Connection, group_id: object) -> dict:
    require_group_participation_windows_schema(conn)
    group = str(group_id or "").strip()
    if group:
        row = conn.execute(
            f"SELECT timezone,allowed_windows_json,effective_at,version FROM {GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE} WHERE group_id=?",
            (group,),
        ).fetchone()
        if row:
            return {**_row_policy(row), "origin": "override", "group_id": group}
    row = conn.execute(
        f"SELECT timezone,allowed_windows_json,effective_at,version FROM {GROUP_PARTICIPATION_DEFAULTS_TABLE} WHERE id=1",
    ).fetchone()
    return {**_row_policy(row), "origin": "default", "group_id": group}


def update_group_participation_windows(
    conn: sqlite3.Connection,
    *,
    scope: object,
    expected_version: object,
    policy: object,
    changed_by: object,
    group_id: object = "",
) -> dict:
    """Atomically replace one default or one explicit override after version validation."""

    require_group_participation_windows_schema(conn)
    normalized = normalize_ambient_windows(policy)
    try:
        expected = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("group_participation_windows_version_invalid") from exc
    scope_name = str(scope or "").strip()
    group = str(group_id or "").strip()
    actor = str(changed_by or "").strip()[:80] or "owner"
    timezone_name, windows_json, effective_at = _encoded(normalized)
    now = utc_now()
    if scope_name == "default":
        row = conn.execute(
            f"SELECT version FROM {GROUP_PARTICIPATION_DEFAULTS_TABLE} WHERE id=1",
        ).fetchone()
        if row is None or int(row[0]) != expected:
            raise ValueError("stale_group_participation_windows_version")
        next_version = expected + 1
        conn.execute(
            f"""UPDATE {GROUP_PARTICIPATION_DEFAULTS_TABLE}
                SET timezone=?,allowed_windows_json=?,effective_at=?,version=?,updated_by=?,updated_at=? WHERE id=1""",
            (timezone_name, windows_json, effective_at, next_version, actor, now),
        )
        return effective_ambient_window_policy(conn, "")
    if scope_name != "group" or not group:
        raise ValueError("group_participation_windows_scope_invalid")
    row = conn.execute(
        f"SELECT version FROM {GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE} WHERE group_id=?",
        (group,),
    ).fetchone()
    current_version = int(row[0]) if row else 0
    if current_version != expected:
        raise ValueError("stale_group_participation_windows_version")
    next_version = current_version + 1
    conn.execute(
        f"""INSERT INTO {GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE}(
                group_id,timezone,allowed_windows_json,effective_at,version,updated_by,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(group_id) DO UPDATE SET
                timezone=excluded.timezone,allowed_windows_json=excluded.allowed_windows_json,
                effective_at=excluded.effective_at,version=excluded.version,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
        (group, timezone_name, windows_json, effective_at, next_version, actor, now),
    )
    return effective_ambient_window_policy(conn, group)


def clear_group_participation_window_override(
    conn: sqlite3.Connection,
    *,
    group_id: object,
    expected_version: object,
) -> dict:
    """Remove one explicit override so the group dynamically inherits the default."""

    require_group_participation_windows_schema(conn)
    group = str(group_id or "").strip()
    if not group:
        raise ValueError("group_participation_windows_scope_invalid")
    try:
        expected = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("group_participation_windows_version_invalid") from exc
    row = conn.execute(
        f"SELECT version FROM {GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE} WHERE group_id=?",
        (group,),
    ).fetchone()
    if row is None or int(row[0]) != expected:
        raise ValueError("stale_group_participation_windows_version")
    conn.execute(
        f"DELETE FROM {GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE} WHERE group_id=?",
        (group,),
    )
    return {**effective_ambient_window_policy(conn, group), "cleared": True}


__all__ = [
    "GROUP_PARTICIPATION_DEFAULTS_TABLE",
    "GROUP_PARTICIPATION_WINDOW_OVERRIDES_TABLE",
    "GROUP_PARTICIPATION_WINDOWS_MIGRATION_CHECKSUM",
    "apply_group_participation_windows_v1",
    "clear_group_participation_window_override",
    "effective_ambient_window_policy",
    "require_group_participation_windows_schema",
    "update_group_participation_windows",
]
