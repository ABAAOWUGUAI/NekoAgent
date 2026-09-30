#!/usr/bin/env python3
"""v54 durable, revocable Owner Cutover for zero-send paired Shadow.

This module owns only the body-free Cutover row.  It never creates an
executor, an HTTP control, Delivery state, or a Candidate/Baseline record.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from bridge_assistant_identity import current_assistant
from bridge_behavior_benchmark_registry import behavior_benchmark_growth_summary
from bridge_behavior_candidate_registry import resolve_shadow_eligible_pair_candidate
from bridge_behavior_owner_authorization import (
    BehaviorOwnerAuthorizationError,
    behavior_owner_authorization_plan,
    verify_behavior_owner_authorization,
)
from bridge_behavior_paired_shadow_schema import (
    PAIRED_SHADOW_CUTOVER_TABLE,
    require_behavior_paired_shadow_cutover_schema,
)
from bridge_migrations import MigrationDriftError


PAIRED_SHADOW_CUTOVER_PATH = "/assistant/behavior-growth/paired-shadow/cutover"
_RUNTIME_CONFIG_FIELDS = frozenset(
    {
        "enabled", "assistant_id", "scope_refs", "candidate_ref",
        "candidate_policy_bundle_hash", "reference_policy_ref", "benchmark_ref",
        "benchmark_hash", "authorization_ref", "authorization_binding_hash",
        "expires_at", "plan_checksum",
    },
)


class BehaviorPairedShadowCutoverError(ValueError):
    """The durable Cutover has no matching, current Owner authorization."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _scope_ref(group_id: object) -> str:
    return "scope:sha256-" + hashlib.sha256(str(group_id or "").encode("utf-8")).hexdigest()


def _utc(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_time_invalid") from exc
    if parsed.tzinfo is None:
        raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_time_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assistant_id(conn: sqlite3.Connection) -> str:
    return str((current_assistant(conn) or {}).get("id") or "").strip()


def _reference(conn: sqlite3.Connection, assistant_id: str) -> dict[str, object] | None:
    summary = behavior_benchmark_growth_summary(conn, assistant_id=assistant_id)
    hard_failures = summary.get("hard_failure_count")
    if summary.get("state") != "baseline_recorded" or int(hard_failures if hard_failures is not None else -1) != 0:
        return None
    required = ("benchmark_ref", "benchmark_hash", "reference_policy_ref")
    if any(not str(summary.get(name) or "").strip() for name in required):
        return None
    return {
        "assistant_id": assistant_id,
        "benchmark_ref": str(summary["benchmark_ref"]),
        "benchmark_hash": str(summary["benchmark_hash"]),
        "reference_policy_ref": str(summary["reference_policy_ref"]),
    }


def _admitted_scope_refs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT subject_id FROM qq_access_entries WHERE subject_type='qq_group' AND enabled=1",
    ).fetchall()
    return sorted({_scope_ref(row[0]) for row in rows if str(row[0] or "").strip()})


def _binding_from_plan(plan: Mapping[str, object]) -> dict[str, object]:
    reference = plan.get("reference")
    candidate = plan.get("candidate")
    if not isinstance(reference, Mapping) or not isinstance(candidate, Mapping):
        raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_prerequisite_required")
    return {
        "assistant_id": plan["assistant_id"],
        "candidate_ref": candidate["candidate_ref"],
        "policy_bundle_hash": candidate["policy_bundle_hash"],
        "reference_policy_ref": reference["reference_policy_ref"],
        "benchmark_ref": reference["benchmark_ref"],
        "benchmark_hash": reference["benchmark_hash"],
        "scope_refs": list(plan["admitted_scope_refs"]),
        "cutover_plan_checksum": plan["plan_checksum"],
    }


def _runtime_binding(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "assistant_id": value["assistant_id"],
        "candidate_ref": value["candidate_ref"],
        "policy_bundle_hash": value["candidate_policy_bundle_hash"],
        "reference_policy_ref": value["reference_policy_ref"],
        "benchmark_ref": value["benchmark_ref"],
        "benchmark_hash": value["benchmark_hash"],
        "scope_refs": value["scope_refs"],
        "cutover_plan_checksum": value["plan_checksum"],
    }


def _current_binding(existing: Mapping[str, object]) -> dict[str, object]:
    try:
        scope_refs = json.loads(str(existing.get("scope_refs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        scope_refs = []
    return {
        "candidate_ref": str(existing.get("candidate_ref") or ""),
        "candidate_policy_bundle_hash": str(existing.get("candidate_policy_bundle_hash") or ""),
        "reference_policy_ref": str(existing.get("reference_policy_ref") or ""),
        "benchmark_ref": str(existing.get("benchmark_ref") or ""),
        "benchmark_hash": str(existing.get("benchmark_hash") or ""),
        "authorization_ref": str(existing.get("authorization_ref") or ""),
        "authorization_binding_hash": str(existing.get("authorization_binding_hash") or ""),
        "expires_at": str(existing.get("expires_at") or ""),
        "admitted_scope_count": len(scope_refs) if isinstance(scope_refs, list) else 0,
    }


def behavior_paired_shadow_cutover_plan(conn: sqlite3.Connection) -> dict[str, object]:
    """Derive a body-free Cutover plan from current authoritative records."""

    require_behavior_paired_shadow_cutover_schema(conn)
    assistant_id = _assistant_id(conn)
    reference = _reference(conn, assistant_id) if assistant_id else None
    candidate = resolve_shadow_eligible_pair_candidate(conn, reference) if reference else None
    scope_refs = _admitted_scope_refs(conn)
    row = conn.execute(
        f"SELECT * FROM {PAIRED_SHADOW_CUTOVER_TABLE} WHERE assistant_id=?", (assistant_id,),
    ).fetchone() if assistant_id else None
    existing = dict(row) if row else {}
    ready = bool(assistant_id and reference and candidate and scope_refs)
    reason = (
        "ready" if ready else "assistant_required" if not assistant_id else
        "reference_baseline_required" if not reference else
        "shadow_eligible_candidate_required" if not candidate else "admitted_group_required"
    )
    payload = {
        "schema_version": 54,
        "feature": "behavior_paired_shadow_v1",
        "state": "enabled" if bool(existing.get("enabled")) else "default_off",
        "preconditions": {"state": "ready" if ready else "blocked", "reason": reason},
        "assistant_id": assistant_id,
        "admitted_scope_count": len(scope_refs),
        "admitted_scope_refs": scope_refs,
        "reference": reference if reference else {"state": "not_configured"},
        "candidate": (
            {
                "candidate_ref": candidate["candidate_ref"],
                "policy_bundle_hash": candidate["policy_bundle_hash"],
            }
            if candidate else {"state": "not_configured"}
        ),
        "current_binding": _current_binding(existing) if existing else {"state": "not_configured"},
        "delivery_enabled": False,
        "formal_domain_writes": [],
        "model_or_network_execution": False,
        "reversible": True,
    }
    return {**payload, "plan_checksum": _hash(_canonical(payload))}


def _disable_cutover(conn: sqlite3.Connection, *, assistant_id: str, plan_checksum: str, now: str) -> None:
    conn.execute(
        f"""INSERT INTO {PAIRED_SHADOW_CUTOVER_TABLE}(
                assistant_id,enabled,scope_refs_json,candidate_ref,candidate_policy_bundle_hash,reference_policy_ref,
                benchmark_ref,benchmark_hash,authorization_ref,authorization_binding_hash,expires_at,plan_checksum,created_at,updated_at
            ) VALUES(?,0,'[]','','','','','','','','',?,?,?)
            ON CONFLICT(assistant_id) DO UPDATE SET
                enabled=0,scope_refs_json='[]',candidate_ref='',candidate_policy_bundle_hash='',reference_policy_ref='',
                benchmark_ref='',benchmark_hash='',authorization_ref='',authorization_binding_hash='',expires_at='',
                plan_checksum=excluded.plan_checksum,updated_at=excluded.updated_at""",
        (assistant_id, plan_checksum, now, now),
    )


def set_behavior_paired_shadow_cutover(
    conn: sqlite3.Connection,
    *,
    enabled: object,
    expect_plan_checksum: object,
    authorization_ref: object | None = None,
    now: object | None = None,
) -> dict[str, object]:
    """Persist only an exact v53 authorization-bound Cutover snapshot."""

    if type(enabled) is not bool:
        raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_enabled_boolean_required")
    plan = behavior_paired_shadow_cutover_plan(conn)
    if str(expect_plan_checksum or "") != plan["plan_checksum"]:
        raise BehaviorPairedShadowCutoverError("stale_behavior_paired_shadow_plan")
    assistant_id = str(plan.get("assistant_id") or "")
    timestamp = _utc(now if now is not None else _now())
    if not assistant_id:
        raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_prerequisite_required:assistant_required")
    if not enabled:
        conn.execute("SAVEPOINT behavior_paired_shadow_cutover")
        try:
            _disable_cutover(conn, assistant_id=assistant_id, plan_checksum=str(plan["plan_checksum"]), now=timestamp)
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT behavior_paired_shadow_cutover")
            conn.execute("RELEASE SAVEPOINT behavior_paired_shadow_cutover")
            raise
        conn.execute("RELEASE SAVEPOINT behavior_paired_shadow_cutover")
        return {**behavior_paired_shadow_cutover_plan(conn), "changed": True, "action": "paired_shadow_disabled"}
    if plan["preconditions"] != {"state": "ready", "reason": "ready"}:
        raise BehaviorPairedShadowCutoverError(
            "behavior_paired_shadow_prerequisite_required:" + str(plan["preconditions"].get("reason") or "")
        )
    binding = _binding_from_plan(plan)
    conn.execute("SAVEPOINT behavior_paired_shadow_cutover")
    try:
        try:
            verified = verify_behavior_owner_authorization(
                conn,
                authorization_ref=authorization_ref,
                purpose="paired_shadow_enable",
                assistant_id=assistant_id,
                binding=binding,
                now=timestamp,
                consume=False,
            )
        except BehaviorOwnerAuthorizationError as exc:
            raise BehaviorPairedShadowCutoverError("behavior_paired_shadow_owner_authorization_required") from exc
        conn.execute(
            f"""INSERT INTO {PAIRED_SHADOW_CUTOVER_TABLE}(
                    assistant_id,enabled,scope_refs_json,candidate_ref,candidate_policy_bundle_hash,reference_policy_ref,
                    benchmark_ref,benchmark_hash,authorization_ref,authorization_binding_hash,expires_at,plan_checksum,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(assistant_id) DO UPDATE SET
                    enabled=excluded.enabled,scope_refs_json=excluded.scope_refs_json,candidate_ref=excluded.candidate_ref,
                    candidate_policy_bundle_hash=excluded.candidate_policy_bundle_hash,reference_policy_ref=excluded.reference_policy_ref,
                    benchmark_ref=excluded.benchmark_ref,benchmark_hash=excluded.benchmark_hash,authorization_ref=excluded.authorization_ref,
                    authorization_binding_hash=excluded.authorization_binding_hash,expires_at=excluded.expires_at,
                    plan_checksum=excluded.plan_checksum,updated_at=excluded.updated_at""",
            (
                assistant_id, 1, _canonical(binding["scope_refs"]), binding["candidate_ref"], binding["policy_bundle_hash"],
                binding["reference_policy_ref"], binding["benchmark_ref"], binding["benchmark_hash"],
                str(verified["authorization_ref"]), str(verified["binding_hash"]), str(verified["expires_at"]),
                binding["cutover_plan_checksum"], timestamp, timestamp,
            ),
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT behavior_paired_shadow_cutover")
        conn.execute("RELEASE SAVEPOINT behavior_paired_shadow_cutover")
        raise
    conn.execute("RELEASE SAVEPOINT behavior_paired_shadow_cutover")
    return {**behavior_paired_shadow_cutover_plan(conn), "changed": True, "action": "paired_shadow_enabled"}


def paired_shadow_runtime_config(conn: sqlite3.Connection, *, assistant_id: object) -> dict[str, object]:
    """Return an immutable body-free snapshot, or the hard default-off config."""

    try:
        require_behavior_paired_shadow_cutover_schema(conn)
        owner = str(assistant_id or "").strip()
        row = conn.execute(
            f"SELECT * FROM {PAIRED_SHADOW_CUTOVER_TABLE} WHERE assistant_id=? AND enabled=1", (owner,),
        ).fetchone()
        if row is None:
            return {"enabled": False}
        value = dict(row)
        scope_refs = json.loads(str(value["scope_refs_json"]))
        config = {
            "enabled": True,
            "assistant_id": owner,
            "scope_refs": scope_refs,
            "candidate_ref": str(value["candidate_ref"]),
            "candidate_policy_bundle_hash": str(value["candidate_policy_bundle_hash"]),
            "reference_policy_ref": str(value["reference_policy_ref"]),
            "benchmark_ref": str(value["benchmark_ref"]),
            "benchmark_hash": str(value["benchmark_hash"]),
            "authorization_ref": str(value["authorization_ref"]),
            "authorization_binding_hash": str(value["authorization_binding_hash"]),
            "expires_at": _utc(value["expires_at"]),
            "plan_checksum": str(value["plan_checksum"]),
        }
        if set(config) != _RUNTIME_CONFIG_FIELDS or not isinstance(scope_refs, list) or scope_refs != sorted(set(scope_refs)):
            return {"enabled": False}
        if _utc(_now()) >= config["expires_at"]:
            return {"enabled": False}
        authorization_plan = behavior_owner_authorization_plan(
            conn, purpose="paired_shadow_enable", binding=_runtime_binding(config), now=_now(),
        )
        if authorization_plan["binding_hash"] != config["authorization_binding_hash"]:
            return {"enabled": False}
        return config
    except (
        sqlite3.Error,
        MigrationDriftError,
        BehaviorOwnerAuthorizationError,
        BehaviorPairedShadowCutoverError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return {"enabled": False}


__all__ = [
    "PAIRED_SHADOW_CUTOVER_PATH", "BehaviorPairedShadowCutoverError",
    "behavior_paired_shadow_cutover_plan", "set_behavior_paired_shadow_cutover",
    "paired_shadow_runtime_config",
]
