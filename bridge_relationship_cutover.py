#!/usr/bin/env python3
"""Gate 8 cutover planning and reversible feature switching."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import uuid
from typing import Iterator

from bridge_assistant_identity import current_assistant
from bridge_migrations import utc_now
from bridge_relationship_proactive_schema import (
    RELATIONSHIP_PROACTIVE_FEATURE_FLAG,
    require_relationship_proactive_schema,
)


REQUIRED_FEATURE_FLAGS = (
    "assistant_identity_v2",
    "memory_scope_v2",
    "daily_shell_v2",
    "interaction_plan_v2",
    "formal_approval_v2",
    "artifact_preview_v2",
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checksum(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _active_assistant(conn: sqlite3.Connection) -> dict:
    assistant = current_assistant(conn)
    if not assistant:
        raise ValueError("active_assistant_missing")
    return assistant


def _feature_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (RELATIONSHIP_PROACTIVE_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        name = f"gate8_cutover_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def relationship_proactive_cutover_plan(conn: sqlite3.Connection) -> dict:
    schema = require_relationship_proactive_schema(conn)
    assistant = _active_assistant(conn)
    feature_rows = {
        str(row[0]): bool(int(row[1]))
        for row in conn.execute(
            "SELECT name,enabled FROM assistant_feature_flags",
        ).fetchall()
    }
    prerequisites = {
        name: bool(feature_rows.get(name))
        for name in REQUIRED_FEATURE_FLAGS
    }
    counts = {
        "relationship_states": int(
            conn.execute(
                "SELECT count(*) FROM relationship_states",
            ).fetchone()[0],
        ),
        "notification_policies": int(
            conn.execute(
                "SELECT count(*) FROM operational_notification_policies",
            ).fetchone()[0],
        ),
        "social_policies": int(
            conn.execute(
                "SELECT count(*) FROM proactive_policies "
                "WHERE policy_kind='social'",
            ).fetchone()[0],
        ),
        "unbound_policies": int(schema["unbound_policies"]),
        "unbound_events": int(schema["unbound_events"]),
    }
    result = {
        "ok": bool(schema["ok"] and all(prerequisites.values())),
        "feature_enabled": _feature_enabled(conn),
        "assistant_id": assistant["id"],
        "schema": schema,
        "prerequisites": prerequisites,
        "counts": counts,
        "rollback": "disable_relationship_proactive_v2_keep_additive_rows",
    }
    result["plan_checksum"] = _checksum(result)
    return result


def set_relationship_proactive_feature(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    with _write_transaction(conn):
        plan = relationship_proactive_cutover_plan(conn)
        if _clip(expect_plan_checksum, 128) != plan["plan_checksum"]:
            raise ValueError("stale_relationship_proactive_cutover_plan")
        if enabled and not plan["ok"]:
            raise ValueError(
                "relationship_proactive_cutover_prerequisite_failed",
            )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO assistant_feature_flags(name,enabled,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                enabled=excluded.enabled,updated_at=excluded.updated_at
            """,
            (
                RELATIONSHIP_PROACTIVE_FEATURE_FLAG,
                1 if enabled else 0,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO assistant_instance_events(
                assistant_id,event_type,actor_type,channel,detail_json,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                plan["assistant_id"],
                (
                    "relationship_proactive_feature_enabled"
                    if enabled
                    else "relationship_proactive_feature_disabled"
                ),
                "operator",
                "cli",
                _canonical_json(
                    {
                        "schema_checksum": plan["schema"]["contract_checksum"],
                        "rollback": plan["rollback"],
                    },
                ),
                now,
            ),
        )
    return relationship_proactive_cutover_plan(conn)


__all__ = [
    "REQUIRED_FEATURE_FLAGS",
    "relationship_proactive_cutover_plan",
    "set_relationship_proactive_feature",
]
