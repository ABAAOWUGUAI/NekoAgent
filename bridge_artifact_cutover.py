#!/usr/bin/env python3
"""Gate 7 Artifact feature cutover plan and fail-closed toggle."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable
from urllib.parse import urlsplit

from bridge_artifact_schema import ARTIFACT_PREVIEW_FEATURE_FLAG, require_artifact_schema
from bridge_migrations import utc_now


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def artifact_preview_feature_enabled(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_feature_flags'",
    ).fetchone()
    if not table:
        return False
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (ARTIFACT_PREVIEW_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(str(url or ""))
    return parsed.scheme.lower(), str(parsed.hostname or "").lower(), parsed.port


def _probe(call: Callable[[], dict], failure_code: str) -> dict:
    try:
        result = call()
        if not isinstance(result, dict):
            return {"ok": False, "error": failure_code}
        return dict(result)
    except Exception:
        # Cutover plans are admin-visible and checksummed. Never persist or
        # expose arbitrary exception text that may contain paths or credentials.
        return {"ok": False, "error": failure_code}


def artifact_cutover_plan(
    assistant_conn: sqlite3.Connection,
    task_conn: sqlite3.Connection,
    *,
    storage_reconcile: Callable[[], dict],
    broker_probe: Callable[[], dict],
    preview_base_url: str,
    admin_origin: str,
    admin_cookie_secure: bool,
    tailscale_service_verified: bool,
) -> dict:
    schema = require_artifact_schema(task_conn)
    flags = {
        str(row[0]): bool(int(row[1]))
        for row in assistant_conn.execute("SELECT name,enabled FROM assistant_feature_flags")
    }
    storage = _probe(storage_reconcile, "artifact_storage_probe_failed")
    broker = _probe(broker_probe, "artifact_broker_probe_failed")
    preview_origin = _origin(preview_base_url)
    management_origin = _origin(admin_origin)
    origin_isolated = bool(
        preview_origin[0] == "https"
        and preview_origin[1]
        and management_origin[1]
        and preview_origin != management_origin
        and preview_origin[1] != management_origin[1]
    )
    prerequisites = {
        "identity_enabled": bool(flags.get("assistant_identity_v2")),
        "memory_scope_enabled": bool(flags.get("memory_scope_v2")),
        "daily_shell_enabled": bool(flags.get("daily_shell_v2")),
        "interaction_plan_enabled": bool(flags.get("interaction_plan_v2")),
        "formal_approval_enabled": bool(flags.get("formal_approval_v2")),
        "storage_reconciled": bool(storage.get("ok")),
        "broker_ready": bool(broker.get("ok")),
        "broker_linux_peercred": broker.get("security") == "linux_so_peercred",
        "preview_origin_isolated": origin_isolated,
        "admin_cookie_secure": bool(admin_cookie_secure),
        "tailscale_service_verified": bool(tailscale_service_verified),
    }
    payload = {
        "feature_enabled": bool(flags.get(ARTIFACT_PREVIEW_FEATURE_FLAG)),
        "schema": schema,
        "prerequisites": prerequisites,
        "storage": storage,
        "broker": {
            key: broker[key] for key in sorted(broker)
            if key in {"ok", "service", "security", "error"}
        },
        "preview_origin": {
            "scheme": preview_origin[0],
            "host": preview_origin[1],
            "isolated_from_management": origin_isolated,
        },
        "rollback": "disable_artifact_preview_v2_keep_immutable_versions_and_audit_events",
    }
    payload["ok"] = bool(schema["ok"] and all(prerequisites.values()))
    payload["plan_checksum"] = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return payload


def set_artifact_preview_feature(
    assistant_conn: sqlite3.Connection,
    task_conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
    storage_reconcile: Callable[[], dict],
    broker_probe: Callable[[], dict],
    preview_base_url: str,
    admin_origin: str,
    admin_cookie_secure: bool,
    tailscale_service_verified: bool,
) -> dict:
    arguments = {
        "storage_reconcile": storage_reconcile,
        "broker_probe": broker_probe,
        "preview_base_url": preview_base_url,
        "admin_origin": admin_origin,
        "admin_cookie_secure": admin_cookie_secure,
        "tailscale_service_verified": tailscale_service_verified,
    }
    plan = artifact_cutover_plan(assistant_conn, task_conn, **arguments)
    if str(expect_plan_checksum or "") != str(plan["plan_checksum"]):
        raise ValueError("stale_artifact_cutover_plan")
    if enabled and not plan["ok"]:
        raise ValueError("artifact_cutover_prerequisite_failed")
    assistant_conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (ARTIFACT_PREVIEW_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    if not enabled:
        # Commit the deny flag before cross-database revocation so a partial
        # rollback still fails closed at the broker boundary.
        assistant_conn.commit()
        from bridge_artifact_repository import ArtifactRepository

        ArtifactRepository(task_conn).revoke_all_preview_access(reason="feature_disabled")
    return artifact_cutover_plan(assistant_conn, task_conn, **arguments)


__all__ = [
    "artifact_cutover_plan",
    "artifact_preview_feature_enabled",
    "set_artifact_preview_feature",
]
