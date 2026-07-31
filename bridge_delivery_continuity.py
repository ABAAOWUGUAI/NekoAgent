#!/usr/bin/env python3
"""AC-3 cutover guard and stable QQ logical-response metadata."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_conversation_participation_engine import deterministic_participation_enabled
from bridge_delivery_continuity_schema import (
    DELIVERY_CONTINUITY_MIGRATION_CHECKSUM,
    UNIFIED_DELIVERY_FEATURE_FLAG,
)
from bridge_migrations import utc_now


DELIVERY_POLICY_VERSION = "ac3-single-owner-v1"


def unified_delivery_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (UNIFIED_DELIVERY_FEATURE_FLAG,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and int(row[0]))


def delivery_cutover_plan(conn: sqlite3.Connection) -> dict:
    payload = {
        "feature": UNIFIED_DELIVERY_FEATURE_FLAG,
        "feature_enabled": unified_delivery_enabled(conn),
        "deterministic_participation_enabled": deterministic_participation_enabled(conn),
        "migration_checksum": DELIVERY_CONTINUITY_MIGRATION_CHECKSUM,
        "policy_version": DELIVERY_POLICY_VERSION,
        "reversible": True,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {**payload, "plan_checksum": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def set_unified_delivery_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    plan = delivery_cutover_plan(conn)
    if expect_plan_checksum != plan["plan_checksum"]:
        raise ValueError("stale_unified_delivery_plan")
    if enabled and not plan["deterministic_participation_enabled"]:
        raise ValueError("deterministic_participation_required")
    conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (UNIFIED_DELIVERY_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return delivery_cutover_plan(conn)


def logical_response_id(
    *,
    channel: str,
    thread_ref: str,
    source_message_id: str,
    response_kind: str,
    trace_id: str = "",
) -> str:
    source = str(source_message_id or trace_id or "").strip()
    if not source:
        raise ValueError("logical_response_source_required")
    value = "|".join(
        (
            DELIVERY_POLICY_VERSION,
            str(channel or "").strip(),
            str(thread_ref or "").strip(),
            source,
            str(response_kind or "reply").strip(),
        ),
    )
    return "resp_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "DELIVERY_POLICY_VERSION",
    "delivery_cutover_plan",
    "logical_response_id",
    "set_unified_delivery_feature",
    "unified_delivery_enabled",
]
