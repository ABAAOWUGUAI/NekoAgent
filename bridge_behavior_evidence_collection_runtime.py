#!/usr/bin/env python3
"""Owner-controlled, body-free Behavior Evolution evidence collection.

The three switches here are intentionally one atomic bundle.  They only
persist body-free observation, Assistant Affect Shadow, and response
assessment records.  They do not enable the optimizer, policy activation,
Shadow delivery, Canary, Stable, Knowledge, Memory, Tasks, Approvals, or any
channel delivery.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import sqlite3

from bridge_assistant_affect_shadow_schema import (
    ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG,
    assistant_affect_shadow_enabled,
    require_assistant_affect_shadow_schema,
)
from bridge_behavior_observation_schema import (
    BEHAVIOR_OBSERVATION_FEATURE_FLAG,
    behavior_observation_enabled,
    require_behavior_observation_assistant_isolation_schema,
)
from bridge_migrations import MigrationDriftError
from bridge_response_assessment_schema import (
    RESPONSE_ASSESSMENT_FEATURE_FLAG,
    require_response_assessment_schema,
    response_assessment_enabled,
)


EVIDENCE_COLLECTION_CUTOVER_PATH = "/assistant/behavior-growth/evidence-collection/cutover"
EVIDENCE_COLLECTION_FEATURE_FLAGS = (
    BEHAVIOR_OBSERVATION_FEATURE_FLAG,
    ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG,
    RESPONSE_ASSESSMENT_FEATURE_FLAG,
)
_FEATURE_METADATA = {
    BEHAVIOR_OBSERVATION_FEATURE_FLAG: {
        "label": "Behavior Observation",
        "body_free": True,
        "mode": "observation_only",
    },
    ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG: {
        "label": "Assistant Affect Shadow",
        "body_free": True,
        "mode": "shadow_only",
    },
    RESPONSE_ASSESSMENT_FEATURE_FLAG: {
        "label": "Response Assessment",
        "body_free": True,
        "mode": "assessment_only",
    },
}
_CONTRACT = {
    "schema_version": 1,
    "feature_flags": list(EVIDENCE_COLLECTION_FEATURE_FLAGS),
    "body_free": True,
    "default_enabled": False,
    "atomic_cutover": True,
    "affected_domains": [],
    "forbidden_transitions": [
        "optimizer_enable",
        "policy_activation",
        "shadow_delivery",
        "canary",
        "stable",
        "knowledge_write",
        "memory_write",
        "task_create",
        "approval_create",
    ],
}
EVIDENCE_COLLECTION_CONTRACT_CHECKSUM = "sha256:" + hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()


class BehaviorEvidenceCollectionError(ValueError):
    """The Owner control-plane request or local schema is invalid."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _plan_checksum(payload: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _schema_status(conn: sqlite3.Connection) -> tuple[bool, str]:
    try:
        # The observer writes Assistant-scoped clusters.  v51's aggregate
        # cluster schema is therefore not an eligible collection target.
        require_behavior_observation_assistant_isolation_schema(conn)
        require_assistant_affect_shadow_schema(conn)
        require_response_assessment_schema(conn)
    except (sqlite3.Error, MigrationDriftError, ValueError) as exc:
        return False, str(exc) or "behavior_evidence_collection_schema_unavailable"
    return True, ""


def _flag_values(conn: sqlite3.Connection, *, schema_ok: bool) -> dict[str, dict[str, object]]:
    readers = {
        BEHAVIOR_OBSERVATION_FEATURE_FLAG: behavior_observation_enabled,
        ASSISTANT_AFFECT_SHADOW_FEATURE_FLAG: assistant_affect_shadow_enabled,
        RESPONSE_ASSESSMENT_FEATURE_FLAG: response_assessment_enabled,
    }
    return {
        name: {
            **_FEATURE_METADATA[name],
            "enabled": bool(schema_ok and readers[name](conn)),
        }
        for name in EVIDENCE_COLLECTION_FEATURE_FLAGS
    }


def behavior_evidence_collection_plan(conn: sqlite3.Connection) -> dict[str, object]:
    """Return only body-free switch state plus a stale-write checksum."""

    schema_ok, schema_error = _schema_status(conn)
    flags = _flag_values(conn, schema_ok=schema_ok)
    enabled_values = [bool(flags[name]["enabled"]) for name in EVIDENCE_COLLECTION_FEATURE_FLAGS]
    fully_enabled = all(enabled_values)
    payload = {
        "schema_version": 1,
        "feature": "behavior_body_free_evidence_collection_v1",
        "contract_checksum": EVIDENCE_COLLECTION_CONTRACT_CHECKSUM,
        "state": "enabled" if fully_enabled else "default_off" if not any(enabled_values) else "inconsistent",
        "feature_enabled": fully_enabled,
        "flags": flags,
        "preconditions": {
            "state": "ready" if schema_ok else "blocked",
            "reason": "ready" if schema_ok else "schema_unavailable",
            "schema_error": "" if schema_ok else schema_error,
        },
        "body_free": True,
        "atomic_cutover": True,
        "delivery_enabled": False,
        "formal_domain_writes": [],
        "unaffected": {
            "optimizer": "unchanged_default_off",
            "combined_shadow": "unchanged_default_off",
            "canary": "owner_approval_required",
            "stable": False,
        },
        "reversible": True,
    }
    return {**payload, "plan_checksum": _plan_checksum(payload)}


def _atomic_set_flags(conn: sqlite3.Connection, *, enabled: bool) -> None:
    """Write exactly the three feature flags as one SQLite savepoint.

    A savepoint is used rather than relying on the caller's outer transaction:
    it preserves atomicity both for the production connection and for tests
    that are already inside a transaction.
    """

    savepoint = "behavior_evidence_collection_cutover"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for feature_name in EVIDENCE_COLLECTION_FEATURE_FLAGS:
            cursor = conn.execute(
                "UPDATE assistant_feature_flags "
                "SET enabled=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE name=?",
                (1 if enabled else 0, feature_name),
            )
            if cursor.rowcount != 1:
                raise BehaviorEvidenceCollectionError("behavior_evidence_collection_feature_flag_missing")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def set_behavior_evidence_collection_cutover(
    conn: sqlite3.Connection,
    *,
    enabled: object,
    expect_plan_checksum: object,
) -> dict[str, object]:
    """Atomically start or stop only the three body-free collection planes."""

    if type(enabled) is not bool:
        raise BehaviorEvidenceCollectionError("behavior_evidence_collection_enabled_boolean_required")
    plan = behavior_evidence_collection_plan(conn)
    if str(expect_plan_checksum or "") != plan["plan_checksum"]:
        raise BehaviorEvidenceCollectionError("stale_behavior_evidence_collection_plan")
    if not bool(plan["preconditions"]["state"] == "ready"):
        raise BehaviorEvidenceCollectionError("behavior_evidence_collection_prerequisite_required:schema_unavailable")
    previous = bool(plan["feature_enabled"])
    _atomic_set_flags(conn, enabled=enabled)
    result = behavior_evidence_collection_plan(conn)
    return {
        **result,
        "changed": previous != enabled,
        "action": "body_free_evidence_collection_enabled" if enabled else "body_free_evidence_collection_disabled",
        "delivery_enabled": False,
        "formal_domain_writes": [],
    }


__all__ = [
    "BehaviorEvidenceCollectionError",
    "EVIDENCE_COLLECTION_CONTRACT_CHECKSUM",
    "EVIDENCE_COLLECTION_CUTOVER_PATH",
    "EVIDENCE_COLLECTION_FEATURE_FLAGS",
    "behavior_evidence_collection_plan",
    "set_behavior_evidence_collection_cutover",
]
