#!/usr/bin/env python3
"""BE-4 durable registry for body-free Reference baseline receipts only.

Private cases, Gold answers and Rubrics stay inside the separately operated
evaluator.  The Assistant database receives only immutable, aggregate evidence
that a named evaluator ran a frozen Reference repeatedly.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

try:  # A missing crypto dependency must close baseline intake, never downgrade it to HMAC.
    from cryptography.hazmat.primitives import serialization
except ImportError:  # pragma: no cover - exercised by isolated deployment preflight.
    serialization = None  # type: ignore[assignment]

from bridge_behavior_benchmark_receipt import (
    BehaviorBenchmarkReceiptError,
    verify_reference_baseline_receipt,
)
from bridge_behavior_owner_authorization import (
    BehaviorOwnerAuthorizationError,
    verify_behavior_owner_authorization,
)
from bridge_migrations import MigrationDriftError


BEHAVIOR_BENCHMARK_REFERENCE_TABLE = "behavior_benchmark_reference_baselines"
BEHAVIOR_BENCHMARK_REFERENCE_COLUMNS = frozenset(
    {
        "benchmark_ref",
        "benchmark_hash",
        "owner_approval_ref",
        "run_ref",
        "evaluator_ref",
        "signing_key_ref",
        "provider_ref",
        "model_ref",
        "thinking_ref",
        "reference_policy_ref",
        "case_count",
        "runs_per_case",
        "campaign_budget_json",
        "reference_metrics_json",
        "hard_failure_count",
        "recorded_at",
        "state",
    }
)
BEHAVIOR_BENCHMARK_REFERENCE_ISOLATED_COLUMNS = BEHAVIOR_BENCHMARK_REFERENCE_COLUMNS | frozenset({"assistant_id"})
_CONTRACT = {
    "table": BEHAVIOR_BENCHMARK_REFERENCE_TABLE,
    "columns": sorted(BEHAVIOR_BENCHMARK_REFERENCE_COLUMNS),
    "private_material_persisted": False,
    "mutable": False,
}
BEHAVIOR_BENCHMARK_REFERENCE_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()
_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_BODY_KEY = re.compile(r"(?:^|_)(?:body|content|gold|message|prompt|raw|rubric|scenario|text)(?:$|_)")


class BehaviorBenchmarkRegistryError(ValueError):
    """A Reference baseline receipt is incomplete, leaked, or mutable."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise BehaviorBenchmarkRegistryError(error)
    return text


def _utc(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_recorded_at_invalid") from exc
    if parsed.tzinfo is None:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_recorded_at_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def apply_behavior_benchmark_registry_v1(conn: sqlite3.Connection) -> None:
    """Create the immutable, body-free baseline receipt table."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_BENCHMARK_REFERENCE_TABLE} (
            benchmark_ref TEXT PRIMARY KEY,
            benchmark_hash TEXT NOT NULL UNIQUE,
            owner_approval_ref TEXT NOT NULL,
            run_ref TEXT NOT NULL UNIQUE,
            evaluator_ref TEXT NOT NULL,
            signing_key_ref TEXT NOT NULL,
            provider_ref TEXT NOT NULL,
            model_ref TEXT NOT NULL,
            thinking_ref TEXT NOT NULL,
            reference_policy_ref TEXT NOT NULL,
            case_count INTEGER NOT NULL CHECK(case_count>0),
            runs_per_case INTEGER NOT NULL CHECK(runs_per_case>=3),
            campaign_budget_json TEXT NOT NULL CHECK(json_valid(campaign_budget_json)),
            reference_metrics_json TEXT NOT NULL CHECK(json_valid(reference_metrics_json)),
            hard_failure_count INTEGER NOT NULL CHECK(hard_failure_count>=0),
            recorded_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state='reference_baseline')
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_benchmark_reference_recent
            ON {BEHAVIOR_BENCHMARK_REFERENCE_TABLE}(recorded_at DESC,benchmark_ref DESC);
        """,
    )


def require_behavior_benchmark_registry_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = (
        {str(row[1]) for row in conn.execute(f"PRAGMA table_info({BEHAVIOR_BENCHMARK_REFERENCE_TABLE})")}
        if BEHAVIOR_BENCHMARK_REFERENCE_TABLE in tables
        else set()
    )
    missing = sorted(BEHAVIOR_BENCHMARK_REFERENCE_COLUMNS - columns)
    if BEHAVIOR_BENCHMARK_REFERENCE_TABLE not in tables or missing:
        raise MigrationDriftError(
            "behavior_benchmark_registry_schema_drift:"
            + ("table" if BEHAVIOR_BENCHMARK_REFERENCE_TABLE not in tables else "")
            + "|"
            + ",".join(missing),
        )
    return {"ok": True}


def require_behavior_benchmark_assistant_isolation_schema(conn: sqlite3.Connection) -> dict:
    require_behavior_benchmark_registry_schema(conn)
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({BEHAVIOR_BENCHMARK_REFERENCE_TABLE})")}
    missing = sorted(BEHAVIOR_BENCHMARK_REFERENCE_ISOLATED_COLUMNS - columns)
    if missing:
        raise MigrationDriftError("behavior_benchmark_assistant_isolation_schema_drift:" + ",".join(missing))
    return {"ok": True}


def _assistant_id(value: object) -> str:
    assistant_id = str(value or "").strip()
    if not assistant_id:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_assistant_id_required")
    return assistant_id


def _body_free_metrics(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_metrics_invalid")
    result: dict[str, float] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip()
        if not name or _BODY_KEY.search(name.lower()):
            raise BehaviorBenchmarkRegistryError("behavior_benchmark_body_free_violation")
        try:
            metric = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise BehaviorBenchmarkRegistryError("behavior_benchmark_metrics_invalid") from exc
        if metric < 0:
            raise BehaviorBenchmarkRegistryError("behavior_benchmark_metrics_invalid")
        result[name] = metric
    return {name: result[name] for name in sorted(result)}


def _campaign_budget(value: object) -> dict[str, int]:
    expected = {"max_mean_cost_microunits", "max_p95_latency_ms", "max_mean_output_tokens"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_budget_invalid")
    result: dict[str, int] = {}
    for name in sorted(expected):
        try:
            amount = int(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorBenchmarkRegistryError("behavior_benchmark_budget_invalid") from exc
        if amount < 1:
            raise BehaviorBenchmarkRegistryError("behavior_benchmark_budget_invalid")
        result[name] = amount
    return result


def _receipt(row: Mapping[str, object]) -> dict:
    metrics = _body_free_metrics(json.loads(str(row["reference_metrics_json"])))
    return {
        "assistant_id": str(row["assistant_id"]),
        "benchmark_ref": str(row["benchmark_ref"]),
        "benchmark_hash": str(row["benchmark_hash"]),
        "run_ref": str(row["run_ref"]),
        "evaluator_ref": str(row["evaluator_ref"]),
        "signing_key_ref": str(row["signing_key_ref"]),
        "provider_ref": str(row["provider_ref"]),
        "model_ref": str(row["model_ref"]),
        "thinking_ref": str(row["thinking_ref"]),
        "reference_policy_ref": str(row["reference_policy_ref"]),
        "case_count": int(row["case_count"]),
        "runs_per_case": int(row["runs_per_case"]),
        "campaign_budget": _campaign_budget(json.loads(str(row["campaign_budget_json"]))),
        "reference_metrics": metrics,
        "hard_failure_count": int(row["hard_failure_count"]),
        "recorded_at": str(row["recorded_at"]),
        "state": "reference_baseline",
        "private_material_exposed": False,
    }


def record_reference_baseline(
    conn: sqlite3.Connection,
    receipt: Mapping[str, object],
    *,
    verification_key: object,
    assistant_id: object,
    owner_authorization_ref: object,
    received_at: object,
    expected_reference_policy_ref: object = "",
) -> dict:
    """Persist only a verified private-evaluator baseline; rewrites fail closed."""

    require_behavior_benchmark_assistant_isolation_schema(conn)
    owner = _assistant_id(assistant_id)
    try:
        value = verify_reference_baseline_receipt(receipt, verification_key=verification_key)
    except BehaviorBenchmarkReceiptError as exc:
        raise BehaviorBenchmarkRegistryError(str(exc)) from exc
    expected = str(expected_reference_policy_ref or "").strip()
    if expected and _ref(expected, error="behavior_benchmark_identity_mismatch") != value["reference_policy_ref"]:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_identity_mismatch")
    if serialization is None:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_receipt_crypto_unavailable")
    try:
        verification_key_bytes = verification_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    except Exception as exc:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_receipt_verification_key_invalid") from exc
    fingerprint = "sha256:" + hashlib.sha256(verification_key_bytes).hexdigest()
    # Preserve the immutable-baseline result on idempotent/replayed ingestion;
    # there is no reason to consume another formal Owner authorization after a
    # matching baseline has already become authoritative.
    exists = conn.execute(
        f"SELECT 1 FROM {BEHAVIOR_BENCHMARK_REFERENCE_TABLE} WHERE assistant_id=? AND (benchmark_ref=? OR benchmark_hash=? OR run_ref=?)",
        (owner, value["benchmark_ref"], value["benchmark_hash"], value["run_ref"]),
    ).fetchone()
    if exists is not None:
        raise BehaviorBenchmarkRegistryError("behavior_benchmark_baseline_already_recorded")
    conn.execute("SAVEPOINT behavior_reference_baseline")
    try:
        try:
            verify_behavior_owner_authorization(
                conn,
                authorization_ref=owner_authorization_ref,
                purpose="reference_baseline_freeze",
                assistant_id=owner,
                binding={
                    "assistant_id": owner,
                    "benchmark_ref": value["benchmark_ref"],
                    "benchmark_hash": value["benchmark_hash"],
                    "reference_policy_ref": value["reference_policy_ref"],
                    "evaluator_ref": value["evaluator_ref"],
                    "run_ref": value["run_ref"],
                    "signing_key_ref": value["signing_key_ref"],
                    "public_key_fingerprint": fingerprint,
                },
                now=received_at,
                consume=True,
            )
        except BehaviorOwnerAuthorizationError as exc:
            raise BehaviorBenchmarkRegistryError("behavior_benchmark_owner_authorization_required") from exc
        conn.execute(
            f"""
            INSERT INTO {BEHAVIOR_BENCHMARK_REFERENCE_TABLE}(
                assistant_id,benchmark_ref,benchmark_hash,owner_approval_ref,run_ref,evaluator_ref,signing_key_ref,
                provider_ref,model_ref,thinking_ref,reference_policy_ref,
                case_count,runs_per_case,campaign_budget_json,reference_metrics_json,hard_failure_count,
                recorded_at,state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                owner, value["benchmark_ref"], value["benchmark_hash"], value["owner_approval_ref"], value["run_ref"],
                value["evaluator_ref"], value["signing_key_ref"],
                value["provider_ref"], value["model_ref"], value["thinking_ref"], value["reference_policy_ref"],
                value["case_count"], value["runs_per_case"],
                json.dumps(value["campaign_budget"], ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                json.dumps(value["reference_metrics"], ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                value["hard_failure_count"], value["completed_at"], "reference_baseline",
            ),
        )
        row = conn.execute(
            f"SELECT * FROM {BEHAVIOR_BENCHMARK_REFERENCE_TABLE} WHERE assistant_id=? AND benchmark_ref=?",
            (owner, value["benchmark_ref"]),
        ).fetchone()
        result = _receipt(dict(row))
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT behavior_reference_baseline")
        conn.execute("RELEASE SAVEPOINT behavior_reference_baseline")
        raise
    conn.execute("RELEASE SAVEPOINT behavior_reference_baseline")
    return result


def behavior_benchmark_growth_summary(conn: sqlite3.Connection, *, assistant_id: object) -> dict:
    """Return the latest formal baseline without private material or approval data."""

    try:
        require_behavior_benchmark_assistant_isolation_schema(conn)
        owner = _assistant_id(assistant_id)
    except (sqlite3.Error, MigrationDriftError, BehaviorBenchmarkRegistryError):
        return {
            "state": "not_configured",
            "private_material_exposed": False,
            "reference_baseline": "not_run",
        }
    row = conn.execute(
        f"SELECT * FROM {BEHAVIOR_BENCHMARK_REFERENCE_TABLE} WHERE assistant_id=? ORDER BY recorded_at DESC,benchmark_ref DESC LIMIT 1",
        (owner,),
    ).fetchone()
    if row is None:
        return {
            "state": "not_configured",
            "private_material_exposed": False,
            "reference_baseline": "not_run",
        }
    receipt = _receipt(dict(row))
    return {
        "state": "baseline_recorded",
        "private_material_exposed": False,
        "reference_baseline": "recorded",
        "benchmark_ref": receipt["benchmark_ref"],
        "benchmark_hash": receipt["benchmark_hash"],
        "provider_ref": receipt["provider_ref"],
        "model_ref": receipt["model_ref"],
        "thinking_ref": receipt["thinking_ref"],
        "reference_policy_ref": receipt["reference_policy_ref"],
        "case_count": receipt["case_count"],
        "runs_per_case": receipt["runs_per_case"],
        "campaign_budget": receipt["campaign_budget"],
        "reference_metrics": receipt["reference_metrics"],
        "hard_failure_count": receipt["hard_failure_count"],
        "recorded_at": receipt["recorded_at"],
    }


__all__ = [
    "BEHAVIOR_BENCHMARK_REFERENCE_MIGRATION_CHECKSUM",
    "BEHAVIOR_BENCHMARK_REFERENCE_TABLE",
    "BehaviorBenchmarkRegistryError",
    "apply_behavior_benchmark_registry_v1",
    "behavior_benchmark_growth_summary",
    "record_reference_baseline",
    "require_behavior_benchmark_assistant_isolation_schema",
    "require_behavior_benchmark_registry_schema",
]
