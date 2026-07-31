#!/usr/bin/env python3
"""Gate C2 object authorization rules, cutover, and project ownership."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from bridge_assistant_migrations import record_security_audit
from bridge_migrations import utc_now
from bridge_qq_access_schema import QQ_ACCESS_FEATURE_FLAG
from bridge_qq_runtime_service import get_runtime_summary
from bridge_qq_object_schema import QQ_OBJECT_FEATURE_FLAG, require_qq_object_schema


QQ_ID_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")
GLOBAL_TASK_ROLES = {"super_admin", "admin", "operator"}
GLOBAL_OBJECT_ROLES = {"super_admin", "admin"}


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        conn.execute("SAVEPOINT qq_object_write")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK TO qq_object_write")
            conn.execute("RELEASE qq_object_write")
            raise
        else:
            conn.execute("RELEASE qq_object_write")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def normalize_qq_id(value: object, error: str = "qq_actor_invalid") -> str:
    actor = str(value or "").strip()[:20]
    if not QQ_ID_PATTERN.fullmatch(actor):
        raise ValueError(error)
    return actor


def qq_object_feature_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (QQ_OBJECT_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def qq_actor_role(conn: sqlite3.Connection, actor: str, *, group: bool = False) -> str:
    actor = normalize_qq_id(actor)
    rows = conn.execute(
        """
        SELECT r.role FROM qq_role_assignments r
        JOIN qq_identities i ON i.id=r.identity_id
        WHERE i.qq_id=? AND i.status='active' AND r.enabled=1
        """,
        (actor,),
    ).fetchall()
    present = {str(row[0]) for row in rows}
    for role in ("super_admin", "admin", "operator", "user"):
        if role in present:
            return role
    return "user" if group else ""


def _identity_id(conn: sqlite3.Connection, actor: str) -> str:
    row = conn.execute(
        "SELECT id FROM qq_identities WHERE qq_id=? AND status='active'",
        (normalize_qq_id(actor),),
    ).fetchone()
    if not row:
        raise ValueError("qq_actor_identity_missing")
    return str(row[0])


def qq_object_cutover_plan(conn: sqlite3.Connection, *, channel_token_distinct: bool) -> dict:
    schema = require_qq_object_schema(conn)
    c1 = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (QQ_ACCESS_FEATURE_FLAG,),
    ).fetchone()
    admins = int(conn.execute(
        """
        SELECT count(*) FROM qq_role_assignments r
        JOIN qq_identities i ON i.id=r.identity_id
        WHERE r.role='super_admin' AND r.enabled=1 AND i.status='active'
        """,
    ).fetchone()[0])
    runtime = get_runtime_summary(conn)
    runtime_identity_ready = (
        not runtime.get("expected_bot_id") or runtime.get("state") == "applied"
    )
    prerequisites = {
        "schema_ok": bool(schema["ok"]),
        "qq_access_control_enabled": bool(c1 and int(c1[0])),
        "super_admin_present": admins > 0,
        "channel_token_distinct": bool(channel_token_distinct),
        "runtime_identity_ready": runtime_identity_ready,
    }
    feature_enabled = qq_object_feature_enabled(conn)
    checksum_payload = {
        "contract_checksum": schema["contract_checksum"],
        "feature_enabled": feature_enabled,
        "prerequisites": prerequisites,
    }
    return {
        "feature_enabled": feature_enabled,
        "prerequisites": prerequisites,
        "ready": all(prerequisites.values()),
        "plan_checksum": hashlib.sha256(
            json.dumps(checksum_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest(),
    }


def set_qq_object_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
    changed_by: str,
    channel_token_distinct: bool,
) -> dict:
    plan = qq_object_cutover_plan(conn, channel_token_distinct=channel_token_distinct)
    if not expect_plan_checksum or expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_qq_object_cutover_plan")
    if enabled and not plan["ready"]:
        raise ValueError("qq_object_cutover_prerequisite_missing")
    with _write_transaction(conn):
        conn.execute(
            "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
            (int(enabled), utc_now(), QQ_OBJECT_FEATURE_FLAG),
        )
        record_security_audit(
            conn,
            "qq_object_authorization_cutover_changed",
            "success",
            actor_type=str(changed_by or "admin")[:40],
            channel="web",
            detail={"enabled": bool(enabled)},
        )
    return qq_object_cutover_plan(conn, channel_token_distinct=channel_token_distinct)


def _project_row(row: sqlite3.Row | tuple | None) -> dict | None:
    if not row:
        return None
    keys = ("id", "name", "path", "description", "active", "created_at", "updated_at")
    return dict(zip(keys, tuple(row)))


def actor_projects(conn: sqlite3.Connection, actor: str, role: str) -> dict:
    identity_id = _identity_id(conn, actor)
    if role in GLOBAL_OBJECT_ROLES:
        rows = conn.execute(
            """
            SELECT id,name,path,description,active,created_at,updated_at
            FROM projects WHERE active=1 ORDER BY updated_at DESC,name ASC
            """,
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT p.id,p.name,p.path,p.description,p.active,p.created_at,p.updated_at
            FROM projects p JOIN qq_project_owners o ON o.project_id=p.id
            WHERE p.active=1 AND o.identity_id=? ORDER BY p.updated_at DESC,p.name ASC
            """,
            (identity_id,),
        ).fetchall()
    projects = [_project_row(row) for row in rows]
    current_row = conn.execute(
        """
        SELECT p.id,p.name,p.path,p.description,p.active,p.created_at,p.updated_at
        FROM qq_actor_project_bindings b JOIN projects p ON p.id=b.project_id
        WHERE b.identity_id=? AND p.active=1
        """,
        (identity_id,),
    ).fetchone()
    current = _project_row(current_row)
    if current is None and role in GLOBAL_OBJECT_ROLES:
        current = _project_row(conn.execute(
            """
            SELECT p.id,p.name,p.path,p.description,p.active,p.created_at,p.updated_at
            FROM settings s JOIN projects p ON p.id=s.value
            WHERE s.key='current_project_id' AND p.active=1
            """,
        ).fetchone())
    if current and role not in GLOBAL_OBJECT_ROLES:
        owned = any(item and item["id"] == current["id"] for item in projects)
        current = current if owned else None
    return {"ok": True, "current": current, "project": current, "projects": projects}


def actor_project_path(conn: sqlite3.Connection, actor: str, role: str) -> tuple[str, str]:
    result = actor_projects(conn, actor, role)
    current = result.get("current") or {}
    if not current:
        raise ValueError("qq_project_required")
    return str(current["id"]), str(current["path"])


def bind_actor_project(conn: sqlite3.Connection, actor: str, role: str, project_id: str) -> dict:
    identity_id = _identity_id(conn, actor)
    row = conn.execute(
        """
        SELECT p.id,p.name,p.path,p.description,p.active,p.created_at,p.updated_at,
               o.identity_id
        FROM projects p LEFT JOIN qq_project_owners o ON o.project_id=p.id
        WHERE p.active=1 AND (p.id=? OR p.name=? OR p.path=?)
        ORDER BY CASE WHEN p.id=? THEN 0 ELSE 1 END LIMIT 1
        """,
        (project_id, project_id, project_id, project_id),
    ).fetchone()
    if not row:
        raise ValueError("project_not_found")
    if role not in GLOBAL_OBJECT_ROLES and str(row[7] or "") != identity_id:
        raise PermissionError("qq_project_forbidden")
    with _write_transaction(conn):
        conn.execute(
            """
            INSERT INTO qq_actor_project_bindings(identity_id,project_id,updated_at)
            VALUES(?,?,?) ON CONFLICT(identity_id) DO UPDATE SET
                project_id=excluded.project_id,updated_at=excluded.updated_at
            """,
            (identity_id, str(row[0]), utc_now()),
        )
    return _project_row(row[:7]) or {}


def project_id_exists(conn: sqlite3.Connection, project_id: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone())


def claim_actor_project(conn: sqlite3.Connection, actor: str, project_id: str) -> None:
    identity_id = _identity_id(conn, actor)
    with _write_transaction(conn):
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO qq_project_owners(project_id,identity_id,created_by,created_at)
            VALUES(?,?,?,?)
            """,
            (project_id, identity_id, f"qq:{actor}", utc_now()),
        )
        if cursor.rowcount != 1:
            raise ValueError("project_already_owned")
        conn.execute(
            """
            INSERT INTO qq_actor_project_bindings(identity_id,project_id,updated_at)
            VALUES(?,?,?) ON CONFLICT(identity_id) DO UPDATE SET
                project_id=excluded.project_id,updated_at=excluded.updated_at
            """,
            (identity_id, project_id, utc_now()),
        )


def task_allowed(task: dict | None, actor: str, role: str) -> bool:
    if not task:
        return False
    return role in GLOBAL_TASK_ROLES or str(task.get("user_id") or "") == actor


def memory_allowed(conn: sqlite3.Connection, memory_id: str, actor: str, role: str) -> bool:
    if role in GLOBAL_OBJECT_ROLES:
        return True
    row = conn.execute(
        """
        SELECT 1 FROM memory_records
        WHERE (id=? OR legacy_memory_id=?) AND subject_actor_ref=? AND status<>'deleted'
        UNION ALL
        SELECT 1 FROM memories WHERE id=? AND user_id=? AND deleted=0
        LIMIT 1
        """,
        (memory_id, memory_id, actor, memory_id, actor),
    ).fetchone()
    return bool(row)


__all__ = [
    "GLOBAL_OBJECT_ROLES",
    "GLOBAL_TASK_ROLES",
    "actor_project_path",
    "actor_projects",
    "bind_actor_project",
    "claim_actor_project",
    "memory_allowed",
    "normalize_qq_id",
    "project_id_exists",
    "qq_actor_role",
    "qq_object_cutover_plan",
    "qq_object_feature_enabled",
    "set_qq_object_feature",
    "task_allowed",
]
