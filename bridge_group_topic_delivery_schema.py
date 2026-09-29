#!/usr/bin/env python3
"""Migration contract for the reversible R7-A topic Delivery flag."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_group_topic_delivery import GROUP_TOPIC_DELIVERY_FEATURE_FLAG
from bridge_migrations import MigrationDriftError, utc_now


_CONTRACT = {
    "feature_flag": GROUP_TOPIC_DELIVERY_FEATURE_FLAG,
    "default_enabled": False,
    "behavior": "topic_social_send_time_validation",
}
GROUP_TOPIC_DELIVERY_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


def apply_group_topic_delivery_v1(conn: sqlite3.Connection) -> None:
    """Register disabled-by-default R7-A without modifying prior deliveries."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_feature_flags'",
    ).fetchone()
    if not table:
        raise MigrationDriftError("group_topic_delivery_feature_table_missing")
    conn.execute(
        """INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
           VALUES(?,?,?)""",
        (GROUP_TOPIC_DELIVERY_FEATURE_FLAG, 0, utc_now()),
    )


def require_group_topic_delivery_schema(conn: sqlite3.Connection) -> dict:
    """Fail closed if an applied R7-A migration lost its control flag."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_feature_flags'",
    ).fetchone()
    if not table:
        raise MigrationDriftError("group_topic_delivery_feature_table_missing")
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (GROUP_TOPIC_DELIVERY_FEATURE_FLAG,),
    ).fetchone()
    if row is None or int(row[0]) not in {0, 1}:
        raise MigrationDriftError("group_topic_delivery_feature_flag_drift")
    return {
        "ok": True,
        "feature_flag": GROUP_TOPIC_DELIVERY_FEATURE_FLAG,
        "enabled": bool(int(row[0])),
    }


__all__ = [
    "GROUP_TOPIC_DELIVERY_MIGRATION_CHECKSUM", "apply_group_topic_delivery_v1",
    "require_group_topic_delivery_schema",
]
