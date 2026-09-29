#!/usr/bin/env python3
"""BE-5 durable, body-free registry for offline Behavior Policy Candidates.

The registry is intentionally not an executor.  It persists only an
allowlisted Policy Bundle, a binding to an already-frozen Reference receipt,
and aggregate evaluation evidence.  There is no route from this module to
Delivery, Knowledge, Memory, Task, Approval, model credentials, or runtime
feature configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3

try:  # Optional BE-4 verifier dependency; never make Bridge startup depend on it.
    from cryptography.hazmat.primitives import serialization
except ImportError:  # pragma: no cover - exercised by dependency-isolated import test.
    serialization = None  # type: ignore[assignment]

from bridge_behavior_benchmark_registry import BEHAVIOR_BENCHMARK_REFERENCE_TABLE
from bridge_behavior_candidate_evaluation_receipt import (
    BehaviorCandidateEvaluationReceiptError,
    verify_candidate_evaluation_receipt,
)
from bridge_behavior_evolution_contract import (
    BehaviorEvolutionContractError,
    evaluate_shadow_admission,
    make_policy_bundle_candidate,
)
from bridge_behavior_observation_schema import BEHAVIOR_OBSERVATION_CLUSTER_TABLE
from bridge_behavior_private_evaluator import OfflineCandidateEvaluation, is_private_candidate_evaluation
from bridge_behavior_owner_authorization import (
    BehaviorOwnerAuthorizationError,
    verify_behavior_owner_authorization,
)
from bridge_migrations import MigrationDriftError


BEHAVIOR_POLICY_OPTIMIZER_FEATURE_FLAG = "behavior_policy_optimizer_v1"
BEHAVIOR_POLICY_CANDIDATE_TABLE = "behavior_policy_candidates"
BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE = "behavior_policy_candidate_transitions"
BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE = "behavior_policy_reference_restores"
BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE = "behavior_combined_shadow_receipts"
BEHAVIOR_POLICY_CANDIDATE_COLUMNS = frozenset(
    {
        "candidate_id",
        "reference_policy_ref",
        "source_observation_refs_json",
        "patches_json",
        "policy_bundle_hash",
        "benchmark_ref",
        "benchmark_hash",
        "provider_ref",
        "model_ref",
        "thinking_ref",
        "case_count",
        "reference_metrics_json",
        "campaign_budget_json",
        "created_at",
        "state",
        "decision_reason",
        "candidate_metrics_json",
        "hard_failure_count",
        "runs_per_case",
        "evaluated_at",
    },
)
BEHAVIOR_POLICY_CANDIDATE_ISOLATED_COLUMNS = BEHAVIOR_POLICY_CANDIDATE_COLUMNS | frozenset({"assistant_id"})
BEHAVIOR_POLICY_CANDIDATE_TRANSITION_COLUMNS = frozenset(
    {"candidate_id", "sequence", "from_state", "to_state", "reason", "occurred_at"},
)
BEHAVIOR_POLICY_CANDIDATE_TRANSITION_ISOLATED_COLUMNS = BEHAVIOR_POLICY_CANDIDATE_TRANSITION_COLUMNS | frozenset({"assistant_id"})
BEHAVIOR_POLICY_REFERENCE_RESTORE_COLUMNS = frozenset(
    {"candidate_id", "reference_policy_ref", "restored_at"},
)
BEHAVIOR_POLICY_REFERENCE_RESTORE_ISOLATED_COLUMNS = BEHAVIOR_POLICY_REFERENCE_RESTORE_COLUMNS | frozenset({"assistant_id"})
BEHAVIOR_COMBINED_SHADOW_RECEIPT_COLUMNS = frozenset(
    {
        "combined_shadow_ref", "scope_ref", "source_event_ref", "trace_ref", "evidence_refs_json",
        "benchmark_ref", "benchmark_hash", "candidate_ref", "reference_hard_failure_count",
        "candidate_hard_failure_count", "metric_delta_json", "created_at", "expires_at", "state",
    },
)
BEHAVIOR_COMBINED_SHADOW_RECEIPT_ISOLATED_COLUMNS = BEHAVIOR_COMBINED_SHADOW_RECEIPT_COLUMNS | frozenset({"assistant_id"})
_CONTRACT = {
    "feature_flag": BEHAVIOR_POLICY_OPTIMIZER_FEATURE_FLAG,
    "candidate_table": BEHAVIOR_POLICY_CANDIDATE_TABLE,
    "candidate_columns": sorted(BEHAVIOR_POLICY_CANDIDATE_COLUMNS),
    "transition_table": BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE,
    "transition_columns": sorted(BEHAVIOR_POLICY_CANDIDATE_TRANSITION_COLUMNS),
    "restore_table": BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE,
    "restore_columns": sorted(BEHAVIOR_POLICY_REFERENCE_RESTORE_COLUMNS),
    "combined_shadow_receipt_table": BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE,
    "combined_shadow_receipt_columns": sorted(BEHAVIOR_COMBINED_SHADOW_RECEIPT_COLUMNS),
    "states": ["offline_evaluation", "rejected", "shadow_eligible"],
    "default_enabled": False,
    "body_free": True,
    "formal_domain_writes": False,
}
BEHAVIOR_POLICY_CANDIDATE_MIGRATION_CHECKSUM = hashlib.sha256(
    json.dumps(_CONTRACT, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
).hexdigest()
_METRICS = (
    "total_score",
    "reply_obligation_recall",
    "ambient_intrusion_rate",
    "citation_completeness",
    "mean_cost_microunits",
    "p95_latency_ms",
    "mean_output_tokens",
)
_STATES = frozenset({"offline_evaluation", "rejected", "shadow_eligible"})


class BehaviorCandidateRegistryError(ValueError):
    """An offline candidate was missing binding evidence or crossed a boundary."""


def _utc(value: object, *, error: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BehaviorCandidateRegistryError(error) from exc
    if parsed.tzinfo is None:
        raise BehaviorCandidateRegistryError(error)
    return parsed.astimezone(timezone.utc).isoformat()


def _json(value: object, *, error: str) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BehaviorCandidateRegistryError(error) from exc


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def apply_behavior_candidate_registry_v1(conn: sqlite3.Connection) -> None:
    """Create the default-off, offline-only candidate state machine."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_POLICY_CANDIDATE_TABLE} (
            candidate_id TEXT PRIMARY KEY,
            reference_policy_ref TEXT NOT NULL,
            source_observation_refs_json TEXT NOT NULL CHECK(json_valid(source_observation_refs_json)),
            patches_json TEXT NOT NULL CHECK(json_valid(patches_json)),
            policy_bundle_hash TEXT NOT NULL UNIQUE,
            benchmark_ref TEXT NOT NULL,
            benchmark_hash TEXT NOT NULL,
            provider_ref TEXT NOT NULL,
            model_ref TEXT NOT NULL,
            thinking_ref TEXT NOT NULL,
            case_count INTEGER NOT NULL CHECK(case_count>0),
            reference_metrics_json TEXT NOT NULL CHECK(json_valid(reference_metrics_json)),
            campaign_budget_json TEXT NOT NULL CHECK(json_valid(campaign_budget_json)),
            created_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('offline_evaluation','rejected','shadow_eligible')),
            decision_reason TEXT,
            candidate_metrics_json TEXT CHECK(candidate_metrics_json IS NULL OR json_valid(candidate_metrics_json)),
            hard_failure_count INTEGER CHECK(hard_failure_count IS NULL OR hard_failure_count>=0),
            runs_per_case INTEGER CHECK(runs_per_case IS NULL OR runs_per_case>=3),
            evaluated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_policy_candidates_state_recent
            ON {BEHAVIOR_POLICY_CANDIDATE_TABLE}(state,created_at DESC,candidate_id DESC);
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE} (
            candidate_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence>=1),
            from_state TEXT CHECK(from_state IS NULL OR from_state IN ('offline_evaluation','rejected','shadow_eligible')),
            to_state TEXT NOT NULL CHECK(to_state IN ('offline_evaluation','rejected','shadow_eligible')),
            reason TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id,sequence),
            FOREIGN KEY(candidate_id) REFERENCES {BEHAVIOR_POLICY_CANDIDATE_TABLE}(candidate_id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE} (
            candidate_id TEXT NOT NULL,
            reference_policy_ref TEXT NOT NULL,
            restored_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id,restored_at),
            FOREIGN KEY(candidate_id) REFERENCES {BEHAVIOR_POLICY_CANDIDATE_TABLE}(candidate_id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE} (
            combined_shadow_ref TEXT PRIMARY KEY,
            scope_ref TEXT NOT NULL,
            source_event_ref TEXT NOT NULL,
            trace_ref TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)),
            benchmark_ref TEXT NOT NULL,
            benchmark_hash TEXT NOT NULL,
            candidate_ref TEXT NOT NULL,
            reference_hard_failure_count INTEGER NOT NULL CHECK(reference_hard_failure_count>=0),
            candidate_hard_failure_count INTEGER NOT NULL CHECK(candidate_hard_failure_count=0),
            metric_delta_json TEXT NOT NULL CHECK(json_valid(metric_delta_json)),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state='shadow_only')
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_combined_shadow_recent
            ON {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE}(created_at DESC,combined_shadow_ref DESC);
        INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
        VALUES('{BEHAVIOR_POLICY_OPTIMIZER_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        """,
    )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(item[0]) for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})")}


def require_behavior_candidate_registry_schema(conn: sqlite3.Connection) -> dict:
    """Fail closed if candidate persistence or its default-off flag drifted."""

    tables = _tables(conn)
    expected = {
        BEHAVIOR_POLICY_CANDIDATE_TABLE: BEHAVIOR_POLICY_CANDIDATE_COLUMNS,
        BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE: BEHAVIOR_POLICY_CANDIDATE_TRANSITION_COLUMNS,
        BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE: BEHAVIOR_POLICY_REFERENCE_RESTORE_COLUMNS,
        BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE: BEHAVIOR_COMBINED_SHADOW_RECEIPT_COLUMNS,
    }
    missing_tables = sorted(set(expected) - tables)
    missing_columns = sorted(
        name
        for table, columns in expected.items()
        if table in tables
        for name in columns - _columns(conn, table)
    )
    flag = conn.execute(
        "SELECT 1 FROM assistant_feature_flags WHERE name=?",
        (BEHAVIOR_POLICY_OPTIMIZER_FEATURE_FLAG,),
    ).fetchone()
    if missing_tables or missing_columns or flag is None:
        raise MigrationDriftError(
            "behavior_candidate_registry_schema_drift:"
            + ",".join(missing_tables)
            + "|"
            + ",".join(missing_columns)
            + ("|feature_flag" if flag is None else ""),
        )
    return {"ok": True}


def require_behavior_candidate_assistant_isolation_schema(conn: sqlite3.Connection) -> dict:
    require_behavior_candidate_registry_schema(conn)
    expected = {
        BEHAVIOR_POLICY_CANDIDATE_TABLE: BEHAVIOR_POLICY_CANDIDATE_ISOLATED_COLUMNS,
        BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE: BEHAVIOR_POLICY_CANDIDATE_TRANSITION_ISOLATED_COLUMNS,
        BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE: BEHAVIOR_POLICY_REFERENCE_RESTORE_ISOLATED_COLUMNS,
        BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE: BEHAVIOR_COMBINED_SHADOW_RECEIPT_ISOLATED_COLUMNS,
    }
    missing = sorted(
        f"{table}.{column}"
        for table, columns in expected.items()
        for column in columns - _columns(conn, table)
    )
    if missing:
        raise MigrationDriftError("behavior_candidate_assistant_isolation_schema_drift:" + ",".join(missing))
    return {"ok": True}


def _assistant_id(value: object) -> str:
    assistant_id = str(value or "").strip()
    if not assistant_id:
        raise BehaviorCandidateRegistryError("behavior_candidate_assistant_id_required")
    return assistant_id


def behavior_optimizer_enabled(conn: sqlite3.Connection) -> bool:
    """Return feature state; an unavailable or drifted schema is disabled."""

    try:
        require_behavior_candidate_registry_schema(conn)
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?",
            (BEHAVIOR_POLICY_OPTIMIZER_FEATURE_FLAG,),
        ).fetchone()
        return bool(row and int(row[0]))
    except (sqlite3.Error, MigrationDriftError):
        return False


def set_behavior_optimizer_feature(conn: sqlite3.Connection, *, enabled: bool) -> dict:
    """Configure automatic candidate creation only; it never enables delivery."""

    require_behavior_candidate_registry_schema(conn)
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE name=?",
        (1 if enabled else 0, BEHAVIOR_POLICY_OPTIMIZER_FEATURE_FLAG),
    )
    return {"enabled": behavior_optimizer_enabled(conn), "delivery_enabled": False}


def _baseline_for_reference(conn: sqlite3.Connection, assistant_id: str, reference_policy_ref: str) -> dict:
    try:
        row = conn.execute(
        f"""SELECT benchmark_ref,benchmark_hash,owner_approval_ref,reference_policy_ref,provider_ref,model_ref,thinking_ref,case_count,
                       campaign_budget_json,reference_metrics_json,hard_failure_count
                FROM {BEHAVIOR_BENCHMARK_REFERENCE_TABLE}
                WHERE assistant_id=? AND reference_policy_ref=? AND state='reference_baseline'
                ORDER BY recorded_at DESC,benchmark_ref DESC LIMIT 1""",
            (assistant_id, reference_policy_ref),
        ).fetchone()
    except sqlite3.Error as exc:
        raise BehaviorCandidateRegistryError("behavior_candidate_reference_baseline_required") from exc
    if row is None:
        raise BehaviorCandidateRegistryError("behavior_candidate_reference_baseline_required")
    value = dict(row)
    metrics = _metrics(_json(value["reference_metrics_json"], error="behavior_candidate_baseline_invalid"), error="behavior_candidate_baseline_invalid")
    budget = _budget(_json(value["campaign_budget_json"], error="behavior_candidate_baseline_invalid"))
    return {
        "assistant_id": assistant_id,
        "benchmark_ref": str(value["benchmark_ref"]),
        "benchmark_hash": str(value["benchmark_hash"]),
        "owner_approval_ref": str(value["owner_approval_ref"]),
        "reference_policy_ref": str(value["reference_policy_ref"]),
        "provider_ref": str(value["provider_ref"]),
        "model_ref": str(value["model_ref"]),
        "thinking_ref": str(value["thinking_ref"]),
        "case_count": int(value["case_count"]),
        "reference_metrics": metrics,
        "campaign_budget": budget,
        "hard_failure_count": int(value["hard_failure_count"]),
    }


def _metrics(value: object, *, error: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(_METRICS):
        raise BehaviorCandidateRegistryError(error)
    normalized: dict[str, float] = {}
    for name in _METRICS:
        try:
            amount = float(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorCandidateRegistryError(error) from exc
        if not math.isfinite(amount) or amount < 0:
            raise BehaviorCandidateRegistryError(error)
        normalized[name] = amount
    return normalized


def _budget(value: object) -> dict[str, int]:
    names = {"max_mean_cost_microunits", "max_p95_latency_ms", "max_mean_output_tokens"}
    if not isinstance(value, Mapping) or set(value) != names:
        raise BehaviorCandidateRegistryError("behavior_candidate_baseline_invalid")
    result: dict[str, int] = {}
    for name in sorted(names):
        if isinstance(value[name], bool):
            raise BehaviorCandidateRegistryError("behavior_candidate_baseline_invalid")
        try:
            number = int(value[name])
        except (TypeError, ValueError) as exc:
            raise BehaviorCandidateRegistryError("behavior_candidate_baseline_invalid") from exc
        if number < 1 or str(number) != str(value[name]).strip():
            raise BehaviorCandidateRegistryError("behavior_candidate_baseline_invalid")
        result[name] = number
    return result


def _source_cluster_assistant_id(conn: sqlite3.Connection, refs: list[str]) -> str:
    if any(not item.startswith("behavior-cluster:") for item in refs):
        raise BehaviorCandidateRegistryError("behavior_candidate_observation_cluster_invalid")
    try:
        found = {
            str(row[0]): _assistant_id(row[1])
            for row in conn.execute(
                f"SELECT id,assistant_id FROM {BEHAVIOR_OBSERVATION_CLUSTER_TABLE} WHERE id IN ({','.join('?' for _ in refs)}) AND observation_count>0",
                tuple(refs),
            ).fetchall()
        }
    except sqlite3.Error as exc:
        raise BehaviorCandidateRegistryError("behavior_candidate_observation_cluster_invalid") from exc
    if set(found) != set(refs) or len(set(found.values())) != 1:
        raise BehaviorCandidateRegistryError("behavior_candidate_observation_cluster_invalid")
    return next(iter(found.values()))


def _stored_candidate(row: sqlite3.Row | Mapping[str, object] | None) -> dict | None:
    if row is None:
        return None
    value = dict(row)
    source_refs = _json(value.pop("source_observation_refs_json"), error="behavior_candidate_stored_json_invalid")
    patches = _json(value.pop("patches_json"), error="behavior_candidate_stored_json_invalid")
    reference_metrics = _metrics(_json(value.pop("reference_metrics_json"), error="behavior_candidate_stored_json_invalid"), error="behavior_candidate_stored_json_invalid")
    campaign_budget = _budget(_json(value.pop("campaign_budget_json"), error="behavior_candidate_stored_json_invalid"))
    candidate_metrics_json = value.pop("candidate_metrics_json")
    candidate_metrics = None if candidate_metrics_json is None else _metrics(
        _json(candidate_metrics_json, error="behavior_candidate_stored_json_invalid"),
        error="behavior_candidate_stored_json_invalid",
    )
    return {
        **value,
        "source_observation_refs": source_refs,
        "patches": patches,
        "reference_metrics": reference_metrics,
        "campaign_budget": campaign_budget,
        "candidate_metrics": candidate_metrics,
        "delivery_enabled": False,
        "formal_domain_writes": [],
    }


def create_offline_candidate(
    conn: sqlite3.Connection,
    candidate: Mapping[str, object],
    *,
    created_at: object,
) -> dict:
    """Persist a candidate only after binding it to a durable frozen baseline."""

    require_behavior_candidate_assistant_isolation_schema(conn)
    try:
        normalized = make_policy_bundle_candidate(candidate)
    except BehaviorEvolutionContractError as exc:
        raise BehaviorCandidateRegistryError(str(exc)) from exc
    source_refs = list(normalized["source_observation_refs"])
    assistant_id = _source_cluster_assistant_id(conn, source_refs)
    baseline = _baseline_for_reference(conn, assistant_id, str(normalized["reference_policy_ref"]))
    timestamp = _utc(created_at, error="behavior_candidate_created_at_invalid")
    existing = conn.execute(
        f"SELECT 1 FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE candidate_id=? OR (assistant_id=? AND policy_bundle_hash=?)",
        (normalized["candidate_id"], assistant_id, normalized["policy_bundle_hash"]),
    ).fetchone()
    if existing is not None:
        raise BehaviorCandidateRegistryError("behavior_candidate_already_recorded")
    conn.execute(
        f"""INSERT INTO {BEHAVIOR_POLICY_CANDIDATE_TABLE}(
                candidate_id,assistant_id,reference_policy_ref,source_observation_refs_json,patches_json,policy_bundle_hash,
                benchmark_ref,benchmark_hash,provider_ref,model_ref,thinking_ref,case_count,
                reference_metrics_json,campaign_budget_json,created_at,state,
                decision_reason,candidate_metrics_json,hard_failure_count,runs_per_case,evaluated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL)""",
        (
            normalized["candidate_id"], assistant_id, normalized["reference_policy_ref"], _canonical(source_refs), _canonical(normalized["patches"]),
            normalized["policy_bundle_hash"], baseline["benchmark_ref"], baseline["benchmark_hash"],
            baseline["provider_ref"], baseline["model_ref"], baseline["thinking_ref"], baseline["case_count"],
            _canonical(baseline["reference_metrics"]), _canonical(baseline["campaign_budget"]), timestamp, "offline_evaluation",
        ),
    )
    conn.execute(
        f"INSERT INTO {BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE}(candidate_id,assistant_id,sequence,from_state,to_state,reason,occurred_at) VALUES(?,?,1,NULL,'offline_evaluation','candidate_recorded',?)",
        (normalized["candidate_id"], assistant_id, timestamp),
    )
    return _stored_candidate(
        conn.execute(f"SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE candidate_id=?", (normalized["candidate_id"],)).fetchone(),
    ) or {}


def _evaluation(value: object) -> dict:
    fields = (
        "candidate_ref", "reference_policy_ref", "policy_bundle_hash", "benchmark_ref", "benchmark_hash",
        "provider_ref", "model_ref", "thinking_ref", "case_count", "runs_per_case", "campaign_budget",
        "candidate_metrics", "hard_failure_count",
    )
    if not isinstance(value, OfflineCandidateEvaluation) or not is_private_candidate_evaluation(value):
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_untrusted")
    source = {name: getattr(value, name, None) for name in fields}
    if set(source) != set(fields):
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_invalid")
    try:
        case_count = int(source["case_count"])
        runs = int(source["runs_per_case"])
        hard_failures = int(source["hard_failure_count"])
    except (TypeError, ValueError) as exc:
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_invalid") from exc
    if (
        isinstance(source["case_count"], bool)
        or isinstance(source["runs_per_case"], bool)
        or isinstance(source["hard_failure_count"], bool)
        or case_count < 1
        or runs < 3
        or hard_failures < 0
    ):
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_invalid")
    # These opaque IDs were normalized before the candidate was evaluated; we
    # compare them to the stored binding instead of accepting a second source.
    return {
        "candidate_ref": str(source["candidate_ref"]),
        "reference_policy_ref": str(source["reference_policy_ref"]),
        "policy_bundle_hash": str(source["policy_bundle_hash"]),
        "benchmark_ref": str(source["benchmark_ref"]),
        "benchmark_hash": str(source["benchmark_hash"]),
        "provider_ref": str(source["provider_ref"]),
        "model_ref": str(source["model_ref"]),
        "thinking_ref": str(source["thinking_ref"]),
        "case_count": case_count,
        "runs_per_case": runs,
        "campaign_budget": _budget(source["campaign_budget"]),
        "candidate_metrics": _metrics(source["candidate_metrics"], error="behavior_candidate_evaluation_invalid"),
        "hard_failure_count": hard_failures,
    }


def _record_offline_evaluation_value(
    conn: sqlite3.Connection,
    value: Mapping[str, object],
    *,
    evaluated_at: object,
) -> dict:
    """Persist a previously trusted body-free aggregate, never activate it."""

    require_behavior_candidate_assistant_isolation_schema(conn)
    row = conn.execute(
        f"SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE candidate_id=?",
        (value["candidate_ref"],),
    ).fetchone()
    stored = _stored_candidate(row)
    if stored is None:
        raise BehaviorCandidateRegistryError("behavior_candidate_not_found")
    if stored["state"] != "offline_evaluation":
        raise BehaviorCandidateRegistryError("behavior_candidate_not_offline")
    binding = (
        "reference_policy_ref", "policy_bundle_hash", "benchmark_ref", "benchmark_hash", "provider_ref", "model_ref",
        "thinking_ref", "case_count", "campaign_budget",
    )
    if any(value[name] != stored[name] for name in binding):
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_binding_mismatch")
    decision = evaluate_shadow_admission(
        reference=stored["reference_metrics"],
        candidate={
            **value["candidate_metrics"],
            "benchmark_frozen": True,
            "runs_per_case": value["runs_per_case"],
            "hard_failure_count": value["hard_failure_count"],
        },
        campaign_budget=stored["campaign_budget"],
    )
    state = str(decision["state"])
    if state not in _STATES - {"offline_evaluation"}:
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_invalid")
    timestamp = _utc(evaluated_at, error="behavior_candidate_evaluated_at_invalid")
    conn.execute(
        f"""UPDATE {BEHAVIOR_POLICY_CANDIDATE_TABLE}
            SET state=?,decision_reason=?,candidate_metrics_json=?,hard_failure_count=?,runs_per_case=?,evaluated_at=?
            WHERE candidate_id=? AND state='offline_evaluation'""",
        (
            state, str(decision["reason"]), _canonical(value["candidate_metrics"]), value["hard_failure_count"],
            value["runs_per_case"], timestamp, value["candidate_ref"],
        ),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise BehaviorCandidateRegistryError("behavior_candidate_not_offline")
    conn.execute(
        f"INSERT INTO {BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE}(candidate_id,assistant_id,sequence,from_state,to_state,reason,occurred_at) VALUES(?,?,2,'offline_evaluation',?,?,?)",
        (value["candidate_ref"], stored["assistant_id"], state, str(decision["reason"]), timestamp),
    )
    result = _stored_candidate(
        conn.execute(f"SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE candidate_id=?", (value["candidate_ref"],)).fetchone(),
    ) or {}
    return {**result, "reason": str(decision["reason"])}


def record_offline_evaluation(
    conn: sqlite3.Connection,
    evaluation: object,
    *,
    evaluated_at: object,
) -> dict:
    """In-process evaluator path retained for isolated test/private runners."""

    return _record_offline_evaluation_value(conn, _evaluation(evaluation), evaluated_at=evaluated_at)


def receive_signed_candidate_evaluation(
    conn: sqlite3.Connection,
    receipt: Mapping[str, object],
    *,
    verification_key_resolver: object,
    received_at: object,
) -> dict:
    """Accept one v2 signed Candidate receipt under its exact Owner authorization.

    ``verification_key_resolver`` must expose only the public verifier for the
    declared key reference.  The Bridge never receives a signing private key,
    and this function does not call a model, create a Candidate, change a
    Benchmark, or alter Delivery/Knowledge/Memory state.
    """

    if serialization is None:
        raise BehaviorCandidateRegistryError("behavior_candidate_receipt_crypto_unavailable")
    if not callable(verification_key_resolver) or not isinstance(receipt, Mapping):
        raise BehaviorCandidateRegistryError("behavior_candidate_receipt_verification_key_unavailable")
    try:
        key_ref = str(receipt.get("signing_key_ref") or "").strip()
        verification_key = verification_key_resolver(key_ref)
        value = verify_candidate_evaluation_receipt(receipt, verification_key=verification_key)
    except BehaviorCandidateEvaluationReceiptError as exc:
        raise BehaviorCandidateRegistryError(str(exc)) from exc
    except Exception as exc:
        raise BehaviorCandidateRegistryError("behavior_candidate_receipt_verification_key_unavailable") from exc
    try:
        public_key_bytes = verification_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except Exception as exc:
        raise BehaviorCandidateRegistryError("behavior_candidate_receipt_verification_key_unavailable") from exc
    require_behavior_candidate_assistant_isolation_schema(conn)
    row = conn.execute(
        f"SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE candidate_id=?",
        (str(value["candidate_ref"]),),
    ).fetchone()
    stored = _stored_candidate(row)
    if stored is None:
        raise BehaviorCandidateRegistryError("behavior_candidate_not_found")
    if str(stored["assistant_id"]) != str(value["assistant_id"]):
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_binding_mismatch")
    baseline = conn.execute(
        f"""SELECT owner_approval_ref FROM {BEHAVIOR_BENCHMARK_REFERENCE_TABLE}
            WHERE assistant_id=? AND benchmark_ref=? AND benchmark_hash=? AND reference_policy_ref=?
              AND state='reference_baseline' LIMIT 1""",
        (str(value["assistant_id"]), str(value["benchmark_ref"]), str(value["benchmark_hash"]), str(value["reference_policy_ref"])),
    ).fetchone()
    if baseline is None or str(baseline[0]) != str(value["owner_approval_ref"]):
        raise BehaviorCandidateRegistryError("behavior_candidate_evaluation_binding_mismatch")
    payload = {
        name: value[name]
        for name in (
            "candidate_ref", "reference_policy_ref", "policy_bundle_hash", "benchmark_ref", "benchmark_hash",
            "provider_ref", "model_ref", "thinking_ref", "case_count", "runs_per_case", "campaign_budget",
            "candidate_metrics", "hard_failure_count",
        )
    }
    try:
        timestamp = _utc(received_at, error="behavior_candidate_receipt_time_invalid")
    except BehaviorCandidateRegistryError:
        raise
    conn.execute("SAVEPOINT behavior_candidate_evaluation_receipt")
    try:
        try:
            verify_behavior_owner_authorization(
                conn,
                authorization_ref=value["owner_authorization_ref"],
                purpose="private_candidate_evaluation",
                assistant_id=value["assistant_id"],
                binding={
                    "assistant_id": value["assistant_id"],
                    "benchmark_ref": value["benchmark_ref"],
                    "benchmark_hash": value["benchmark_hash"],
                    "reference_policy_ref": value["reference_policy_ref"],
                    "evaluator_ref": value["evaluator_ref"],
                    "run_ref": value["run_ref"],
                    "signing_key_ref": value["signing_key_ref"],
                    "public_key_fingerprint": "sha256:" + hashlib.sha256(public_key_bytes).hexdigest(),
                    "candidate_ref": value["candidate_ref"],
                    "policy_bundle_hash": value["policy_bundle_hash"],
                },
                now=timestamp,
                consume=True,
            )
        except BehaviorOwnerAuthorizationError as exc:
            raise BehaviorCandidateRegistryError("behavior_candidate_owner_authorization_required") from exc
        result = _record_offline_evaluation_value(conn, payload, evaluated_at=value["completed_at"])
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT behavior_candidate_evaluation_receipt")
        conn.execute("RELEASE SAVEPOINT behavior_candidate_evaluation_receipt")
        raise
    conn.execute("RELEASE SAVEPOINT behavior_candidate_evaluation_receipt")
    return result


def restore_reference_from_candidate(
    conn: sqlite3.Connection,
    candidate_id: object,
    *,
    restored_at: object,
) -> dict:
    """Record a no-op rollback to the immutable Reference baseline.

    Candidates never replace Reference, so recovery is an auditable confirmation
    rather than a write to policy, production configuration, or any formal
    domain.  The candidate terminal state deliberately remains unchanged.
    """

    require_behavior_candidate_assistant_isolation_schema(conn)
    identifier = str(candidate_id or "").strip()
    row = conn.execute(
        f"SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE candidate_id=?",
        (identifier,),
    ).fetchone()
    stored = _stored_candidate(row)
    if stored is None:
        raise BehaviorCandidateRegistryError("behavior_candidate_not_found")
    timestamp = _utc(restored_at, error="behavior_candidate_restored_at_invalid")
    conn.execute(
        f"INSERT INTO {BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE}(candidate_id,assistant_id,reference_policy_ref,restored_at) VALUES(?,?,?,?)",
        (identifier, stored["assistant_id"], stored["reference_policy_ref"], timestamp),
    )
    return {
        "candidate_ref": identifier,
        "reference_policy_ref": stored["reference_policy_ref"],
        "state": stored["state"],
        "reference_modified": False,
        "production_modified": False,
        "delivery_enabled": False,
        "formal_domain_writes": [],
    }


def resolve_shadow_eligible_candidate(conn: sqlite3.Connection, reference: Mapping[str, object]) -> dict | None:
    """Return one already-evaluated candidate for BE-6, never its patches."""

    require_behavior_candidate_assistant_isolation_schema(conn)
    assistant_id = _assistant_id(reference.get("assistant_id"))
    row = conn.execute(
        f"""SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE}
            WHERE assistant_id=? AND state='shadow_eligible' AND benchmark_ref=? AND benchmark_hash=?
            ORDER BY evaluated_at DESC,candidate_id DESC LIMIT 1""",
        (assistant_id, str(reference.get("benchmark_ref") or ""), str(reference.get("benchmark_hash") or "")),
    ).fetchone()
    stored = _stored_candidate(row)
    if stored is None or int(stored.get("hard_failure_count") or -1) != 0 or not isinstance(stored.get("candidate_metrics"), Mapping):
        return None
    return {
        "assistant_id": stored["assistant_id"], "candidate_ref": stored["candidate_id"], "state": "shadow_eligible",
        "benchmark_ref": stored["benchmark_ref"], "benchmark_hash": stored["benchmark_hash"],
        "candidate_metrics": dict(stored["candidate_metrics"]), "hard_failure_count": 0,
    }


def load_offline_candidate_for_private_evaluation(
    conn: sqlite3.Connection,
    *,
    assistant_id: object,
    candidate_id: object,
) -> dict:
    """Load the exact body-free bundle for the isolated evaluator only.

    This is deliberately not an optimizer or public HTTP projection.  It
    rejects terminal/shadow states so a signed receipt can only be issued for
    a fresh offline Candidate bound to the current Assistant Instance.
    """

    require_behavior_candidate_assistant_isolation_schema(conn)
    owner = _assistant_id(assistant_id)
    identifier = str(candidate_id or "").strip()
    row = conn.execute(
        f"SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE} WHERE assistant_id=? AND candidate_id=?",
        (owner, identifier),
    ).fetchone()
    stored = _stored_candidate(row)
    if stored is None:
        raise BehaviorCandidateRegistryError("behavior_candidate_not_found")
    if str(stored.get("state") or "") != "offline_evaluation":
        raise BehaviorCandidateRegistryError("behavior_candidate_not_offline")
    candidate = {
        "schema_version": 1,
        "candidate_id": stored["candidate_id"],
        "reference_policy_ref": stored["reference_policy_ref"],
        "source_observation_refs": list(stored["source_observation_refs"]),
        "state": "offline_evaluation",
        "patches": list(stored["patches"]),
    }
    try:
        return make_policy_bundle_candidate(candidate)
    except BehaviorEvolutionContractError as exc:
        raise BehaviorCandidateRegistryError("behavior_candidate_stored_bundle_invalid") from exc


def resolve_shadow_eligible_pair_candidate(conn: sqlite3.Connection, reference: Mapping[str, object]) -> dict | None:
    """Return a registry-bound Candidate for an in-process BE-6 paired run.

    Unlike the legacy aggregate comparison helper, this narrow internal seam
    also returns the already allowlisted patch bundle.  It is intentionally
    unavailable to HTTP/optimizer callers and never returns benchmark cases,
    private Rubric/Gold, credentials, or any production configuration.
    """

    require_behavior_candidate_assistant_isolation_schema(conn)
    assistant_id = _assistant_id(reference.get("assistant_id"))
    reference_policy_ref = str(reference.get("reference_policy_ref") or "").strip()
    if not reference_policy_ref:
        return None
    row = conn.execute(
        f"""SELECT * FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE}
            WHERE assistant_id=? AND state='shadow_eligible' AND benchmark_ref=? AND benchmark_hash=?
              AND reference_policy_ref=?
            ORDER BY evaluated_at DESC,candidate_id DESC LIMIT 1""",
        (
            assistant_id,
            str(reference.get("benchmark_ref") or ""),
            str(reference.get("benchmark_hash") or ""),
            reference_policy_ref,
        ),
    ).fetchone()
    stored = _stored_candidate(row)
    if (
        stored is None
        or int(stored.get("hard_failure_count") if stored.get("hard_failure_count") is not None else -1) != 0
        or not isinstance(stored.get("candidate_metrics"), Mapping)
        or not isinstance(stored.get("patches"), list)
        or not stored.get("patches")
    ):
        return None
    return {
        "assistant_id": stored["assistant_id"],
        "candidate_ref": stored["candidate_id"],
        "state": "shadow_eligible",
        "benchmark_ref": stored["benchmark_ref"],
        "benchmark_hash": stored["benchmark_hash"],
        "candidate_metrics": dict(stored["candidate_metrics"]),
        "hard_failure_count": 0,
        "reference_policy_ref": stored["reference_policy_ref"],
        "policy_bundle_hash": stored["policy_bundle_hash"],
        "patches": [dict(item) for item in stored["patches"] if isinstance(item, Mapping)],
    }


def purge_expired_combined_shadow_receipts(
    conn: sqlite3.Connection,
    *,
    now: object | None = None,
) -> dict[str, int]:
    """Hard-delete expired, body-free Shadow receipts.

    These receipts are audit evidence for a zero-send comparison, not a new
    memory or knowledge domain.  Their retention therefore stays bounded and
    is enforced by the existing automation retention pass.
    """

    require_behavior_candidate_assistant_isolation_schema(conn)
    current = _utc(now if now is not None else datetime.now(timezone.utc), error="behavior_combined_shadow_time_invalid")
    deleted = conn.execute(
        f"DELETE FROM {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE} WHERE expires_at<=?",
        (current,),
    ).rowcount
    return {"deleted_combined_shadow_receipts": int(deleted)}


__all__ = [
    "BEHAVIOR_POLICY_CANDIDATE_MIGRATION_CHECKSUM",
    "BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE",
    "BEHAVIOR_POLICY_CANDIDATE_TABLE",
    "BEHAVIOR_POLICY_OPTIMIZER_FEATURE_FLAG",
    "BehaviorCandidateRegistryError",
    "apply_behavior_candidate_registry_v1",
    "behavior_optimizer_enabled",
    "create_offline_candidate",
    "load_offline_candidate_for_private_evaluation",
    "record_offline_evaluation",
    "receive_signed_candidate_evaluation",
    "purge_expired_combined_shadow_receipts",
    "resolve_shadow_eligible_candidate",
    "resolve_shadow_eligible_pair_candidate",
    "require_behavior_candidate_registry_schema",
    "require_behavior_candidate_assistant_isolation_schema",
    "restore_reference_from_candidate",
    "set_behavior_optimizer_feature",
]
