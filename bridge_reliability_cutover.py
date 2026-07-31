#!/usr/bin/env python3
"""Gate C3 cutover plan, orphan repair, and audited feature switch."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_assistant_migrations import record_security_audit
from bridge_migrations import utc_now
from bridge_qq_access_schema import QQ_ACCESS_FEATURE_FLAG
from bridge_qq_object_schema import QQ_OBJECT_FEATURE_FLAG
from bridge_reliability_schema import RELIABILITY_FEATURE_FLAG, require_reliability_schema
from bridge_reliability_service import stage_proactive_delivery
from bridge_qq_runtime_service import get_runtime_summary


def _enabled(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT enabled FROM assistant_feature_flags WHERE name=?", (name,)).fetchone()
    return bool(row and int(row[0]))


def _repairable_proactive(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT e.id,e.user_id,e.message,COALESCE(s.session,'') AS send_session
           FROM proactive_events e LEFT JOIN qq_sessions s ON s.user_id=e.user_id
           LEFT JOIN assistant_action_outbox a
             ON a.aggregate_type='proactive_event' AND a.aggregate_id=e.id
           WHERE e.action='send' AND e.delivered_at='' AND e.error=''
             AND e.delivery_id='' AND a.id IS NULL""",
    ).fetchall()
    return [dict(row) for row in rows]


def reliability_cutover_plan(
    conn: sqlite3.Connection, *, channel_token_distinct: bool, qq_ready: bool,
) -> dict:
    schema = require_reliability_schema(conn)
    orphan_count = len(_repairable_proactive(conn))
    runtime = get_runtime_summary(conn)
    runtime_identity_ready = (
        not runtime.get("expected_bot_id") or runtime.get("state") == "applied"
    )
    prerequisites = {
        "schema_ok": bool(schema["ok"]),
        "qq_access_control_enabled": _enabled(conn, QQ_ACCESS_FEATURE_FLAG),
        "qq_object_authorization_enabled": _enabled(conn, QQ_OBJECT_FEATURE_FLAG),
        "channel_token_distinct": bool(channel_token_distinct),
        "qq_text_channel_ready": bool(qq_ready),
        "runtime_identity_ready": runtime_identity_ready,
    }
    feature_enabled = _enabled(conn, RELIABILITY_FEATURE_FLAG)
    payload = {
        "contract_checksum": schema["contract_checksum"],
        "feature_enabled": feature_enabled,
        "prerequisites": prerequisites,
        "repairable_proactive_orphans": orphan_count,
    }
    return {
        **payload,
        "ready": all(prerequisites.values()),
        "plan_checksum": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest(),
    }


def set_reliability_feature(
    conn: sqlite3.Connection, enabled: bool, *, expect_plan_checksum: str,
    channel_token_distinct: bool, qq_ready: bool, changed_by: str = "admin",
) -> dict:
    plan = reliability_cutover_plan(
        conn, channel_token_distinct=channel_token_distinct, qq_ready=qq_ready,
    )
    if not expect_plan_checksum or expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_reliability_cutover_plan")
    if enabled and not plan["ready"]:
        raise ValueError("reliability_cutover_prerequisite_missing")
    repaired = 0
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
        (int(enabled), utc_now(), RELIABILITY_FEATURE_FLAG),
    )
    if enabled:
        for item in _repairable_proactive(conn):
            if stage_proactive_delivery(conn, item, str(item["id"]), str(item["message"])):
                repaired += 1
    record_security_audit(
        conn, "task_message_reliability_cutover_changed", "success",
        actor_type=str(changed_by)[:40], channel="web",
        detail={"enabled": bool(enabled), "proactive_orphans_repaired": repaired},
    )
    return reliability_cutover_plan(
        conn, channel_token_distinct=channel_token_distinct, qq_ready=qq_ready,
    )


__all__ = ["reliability_cutover_plan", "set_reliability_feature"]
