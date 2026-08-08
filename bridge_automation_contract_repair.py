"""Deterministic repair of legacy automation execution contracts (A2).

Split out of ``bridge_automation.py`` to keep that legacy file inside its
non-growth budget.  Only a persisted ``capability_id=null / output_kind=
agent_task`` contract that re-derives to a known read-only capability may be
repaired; the transaction increments the job revision, updates the contract
and its hash, and preserves the old values plus the audit reason in
``automation_contract_repairs``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from bridge_automation_execution_contract import (
    audit_execution_contract_repair,
    execution_contract_hash,
)
from bridge_migrations import utc_now as _utc_now


def repair_legacy_execution_contract(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    parameters: dict | None = None,
    now: str = "",
) -> dict:
    """Deterministically repair one legacy generic contract in one transaction."""

    from bridge_automation_schema import ensure_automation_tables

    ensure_automation_tables(conn)
    job = conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError("automation_job_not_found")
    try:
        persisted = json.loads(str(job["execution_contract_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        persisted = {}
    if not isinstance(persisted, dict):
        raise ValueError("automation_job_contract_invalid")
    try:
        parameters_json = json.loads(str(job["parameters_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parameters_json = {}
    effective_parameters = dict(parameters if isinstance(parameters, dict) else parameters_json)
    audit = audit_execution_contract_repair(
        str(job["instruction"] or ""),
        effective_parameters,
        persisted_contract=persisted,
        action_type=str(job["action_type"] or "agent"),
    )
    if not audit.get("repairable"):
        return {"repaired": False, "reason": str(audit.get("reason") or "not_repairable")}
    repair = audit["repair"]
    if not isinstance(repair, dict):
        return {"repaired": False, "reason": "repair_contract_invalid"}
    repair_json = json.dumps(repair, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    persisted_json = json.dumps(persisted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    current_now = now or str(_utc_now())
    repair_id = uuid.uuid4().hex
    with conn:
        cursor = conn.execute(
            """UPDATE automation_jobs
               SET execution_contract_json=?, execution_contract_hash=?,
                   revision=revision+1, updated_at=?
               WHERE id=? AND execution_contract_json=?""",
            (repair_json, execution_contract_hash(repair), current_now, job_id, persisted_json),
        )
        if cursor.rowcount != 1:
            raise ValueError("automation_job_contract_changed_concurrently")
        conn.execute(
            """INSERT INTO automation_contract_repairs(
                   id, job_id, repaired_at, derivation_version, reason,
                   persisted_capability_id, derived_capability_id,
                   persisted_contract_json, derived_contract_json,
                   persisted_hash, derived_hash, network_required_rose
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                repair_id,
                job_id,
                current_now,
                int(audit.get("derivation_version") or 0),
                str(audit.get("reason") or ""),
                "",
                str(audit.get("derived_capability_id") or ""),
                persisted_json,
                repair_json,
                str(audit.get("persisted_hash") or ""),
                str(audit.get("derived_hash") or ""),
                1 if audit.get("network_required_rose") else 0,
            ),
        )
    updated = dict(conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone())
    return {
        "repaired": True,
        "job_id": job_id,
        "reason": str(audit.get("reason") or ""),
        "persisted_capability_id": "",
        "derived_capability_id": str(audit.get("derived_capability_id") or ""),
        "derived_contract": repair,
        "derived_hash": str(audit.get("derived_hash") or ""),
        "network_required_rose": bool(audit.get("network_required_rose")),
        "audit_id": repair_id,
        "revision": int(updated["revision"] or 1),
    }


__all__ = ["repair_legacy_execution_contract"]
