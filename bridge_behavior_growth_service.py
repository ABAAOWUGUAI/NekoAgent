#!/usr/bin/env python3
"""Read-only Owner projection for controlled Behavior Evolution growth."""

from __future__ import annotations

import sqlite3

from bridge_assistant_identity import current_assistant
from bridge_assistant_affect_shadow_schema import ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG, ASSISTANT_AFFECT_SHADOW_TABLE
from bridge_behavior_benchmark_registry import behavior_benchmark_growth_summary
from bridge_behavior_observation_schema import (
    BEHAVIOR_OBSERVATION_CLUSTER_TABLE,
    BEHAVIOR_OBSERVATION_FEATURE_FLAG,
    BEHAVIOR_OBSERVATION_TABLE,
)
from bridge_response_assessment_schema import (
    RESPONSE_ASSESSMENT_FEATURE_FLAG,
    RESPONSE_ASSESSMENT_TABLE,
)
from bridge_behavior_combined_shadow_runtime import combined_shadow_runtime_projection


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _feature_enabled(conn: sqlite3.Connection, tables: set[str], name: str) -> bool:
    if "assistant_feature_flags" not in tables:
        return False
    row = conn.execute("SELECT enabled FROM assistant_feature_flags WHERE name=?", (name,)).fetchone()
    return bool(row and int(row[0]))


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    try:
        return str((current_assistant(conn) or {}).get("id") or "").strip()
    except (sqlite3.Error, ValueError):
        return ""


def _count(conn: sqlite3.Connection, tables: set[str], name: str, *, assistant_id: str = "") -> int:
    if name not in tables:
        return 0
    if not assistant_id:
        return 0
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}
    if "assistant_id" not in columns:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {name} WHERE assistant_id=?", (assistant_id,)).fetchone()[0])


def behavior_growth_summary(conn: sqlite3.Connection) -> dict:
    """Return counts and immutable gate state, never observations or raw evidence."""

    tables = _tables(conn)
    assistant_id = _active_assistant_id(conn)
    observation_installed = {BEHAVIOR_OBSERVATION_TABLE, BEHAVIOR_OBSERVATION_CLUSTER_TABLE} <= tables
    affect_installed = ASSISTANT_AFFECT_SHADOW_TABLE in tables
    response_assessment_installed = RESPONSE_ASSESSMENT_TABLE in tables
    observation_enabled = observation_installed and _feature_enabled(conn, tables, BEHAVIOR_OBSERVATION_FEATURE_FLAG)
    affect_enabled = affect_installed and _feature_enabled(conn, tables, ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG)
    response_assessment_enabled = response_assessment_installed and _feature_enabled(
        conn, tables, RESPONSE_ASSESSMENT_FEATURE_FLAG,
    )
    return {
        "schema_version": 1,
        "observation": {
            "state": "enabled" if observation_enabled else "default_off" if observation_installed else "not_installed",
            "feature_enabled": observation_enabled,
            "observation_count": _count(conn, tables, BEHAVIOR_OBSERVATION_TABLE, assistant_id=assistant_id),
            "cluster_count": _count(conn, tables, BEHAVIOR_OBSERVATION_CLUSTER_TABLE, assistant_id=assistant_id),
            "body_free": True,
        },
        "assistant_affect_shadow": {
            "state": "enabled_shadow_only" if affect_enabled else "default_off" if affect_installed else "not_installed",
            "feature_enabled": affect_enabled,
            "shadow_count": _count(conn, tables, ASSISTANT_AFFECT_SHADOW_TABLE, assistant_id=assistant_id),
            "projection": "shadow_only",
            "is_user_affect": False,
        },
        "response_assessment": {
            "state": "enabled_body_free" if response_assessment_enabled else "default_off" if response_assessment_installed else "not_installed",
            "feature_enabled": response_assessment_enabled,
            "assessment_count": _count(conn, tables, RESPONSE_ASSESSMENT_TABLE, assistant_id=assistant_id),
            "visible_reply_template": False,
            "changes_delivery_or_permission": False,
        },
        "benchmark": behavior_benchmark_growth_summary(conn, assistant_id=assistant_id),
        "optimizer": {
            "state": "offline_only",
            "candidate_activation": "disabled",
        },
        "combined_shadow": combined_shadow_runtime_projection(conn, assistant_id=assistant_id),
        "canary": {
            "state": "owner_approval_required",
            "owner_approval_required": True,
            "stable": False,
        },
    }


__all__ = ["behavior_growth_summary"]
