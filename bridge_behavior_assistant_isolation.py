#!/usr/bin/env python3
"""v52 Assistant Instance isolation for Behavior Evolution state.

v46--v51 stored some aggregate Behavior Evolution records without the
Assistant Instance that produced them.  A profile switch could consequently
make one Assistant's observations, frozen reference, candidate, or Shadow
receipt visible to another.  This migration makes that ownership explicit.

The migration is deliberately conservative for legacy rows: when an old
body-free reference cannot be linked to exactly one Assistant Instance, it is
retained under a non-routable quarantine owner.  It is never attributed to the
currently active Assistant merely because that is convenient at migration
time.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import sqlite3

from bridge_behavior_benchmark_registry import BEHAVIOR_BENCHMARK_REFERENCE_TABLE
from bridge_behavior_candidate_registry import (
    BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE,
    BEHAVIOR_POLICY_CANDIDATE_TABLE,
    BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE,
    BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE,
)
from bridge_behavior_observation_schema import (
    BEHAVIOR_OBSERVATION_CLUSTER_TABLE,
    BEHAVIOR_OBSERVATION_TABLE,
)
from bridge_response_assessment_schema import RESPONSE_ASSESSMENT_TABLE
from bridge_migrations import MigrationDriftError


LEGACY_UNATTRIBUTED_ASSISTANT_ID = "assistant:legacy-unattributed-v51"
BEHAVIOR_ASSISTANT_ISOLATION_MIGRATION_CHECKSUM = "sha256:" + hashlib.sha256(
    b"behavior-assistant-isolation-v2:assistant-scoped-body-free-aggregates",
).hexdigest()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _cluster_id(assistant_id: str, problem_code: str, stage: str, policy_version: str) -> str:
    token = "|".join((assistant_id, problem_code, stage, policy_version))
    return "behavior-cluster:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def _assistant_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate or LEGACY_UNATTRIBUTED_ASSISTANT_ID


def _json_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _create_observation_cluster_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE {BEHAVIOR_OBSERVATION_CLUSTER_TABLE} (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            problem_code TEXT NOT NULL,
            stage TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            observation_count INTEGER NOT NULL CHECK(observation_count>=0),
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            UNIQUE(assistant_id,problem_code,stage,policy_version)
        );
        CREATE INDEX idx_behavior_observation_clusters_assistant_recent
            ON {BEHAVIOR_OBSERVATION_CLUSTER_TABLE}(assistant_id,last_observed_at DESC,id DESC);
        """,
    )


def _migrate_observation_clusters(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """Split old cross-profile aggregates and return old-to-owner evidence."""

    tables = _tables(conn)
    if not {BEHAVIOR_OBSERVATION_TABLE, BEHAVIOR_OBSERVATION_CLUSTER_TABLE} <= tables:
        return {}
    if "assistant_id" in _columns(conn, BEHAVIOR_OBSERVATION_CLUSTER_TABLE):
        rows = conn.execute(
            f"SELECT id,assistant_id FROM {BEHAVIOR_OBSERVATION_CLUSTER_TABLE}",
        ).fetchall()
        return {str(row[0]): {_assistant_id(row[1]): str(row[0])} for row in rows}

    legacy = BEHAVIOR_OBSERVATION_CLUSTER_TABLE + "_v51_legacy"
    conn.execute(f"ALTER TABLE {BEHAVIOR_OBSERVATION_CLUSTER_TABLE} RENAME TO {legacy}")
    _create_observation_cluster_table(conn)
    evidence: dict[str, dict[str, str]] = defaultdict(dict)
    rows = conn.execute(
        f"""SELECT cluster_id,assistant_id,problem_code,stage,policy_version,
                   COUNT(*),MIN(created_at),MAX(created_at)
              FROM {BEHAVIOR_OBSERVATION_TABLE}
             GROUP BY cluster_id,assistant_id,problem_code,stage,policy_version""",
    ).fetchall()
    for row in rows:
        old_id, raw_assistant, problem, stage, policy, count, first_seen, last_seen = row
        assistant = _assistant_id(raw_assistant)
        new_id = _cluster_id(assistant, str(problem), str(stage), str(policy))
        conn.execute(
            f"""INSERT INTO {BEHAVIOR_OBSERVATION_CLUSTER_TABLE}(
                    id,assistant_id,problem_code,stage,policy_version,observation_count,first_observed_at,last_observed_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
            (new_id, assistant, problem, stage, policy, count, first_seen, last_seen),
        )
        conn.execute(
            f"""UPDATE {BEHAVIOR_OBSERVATION_TABLE} SET cluster_id=?
                 WHERE cluster_id=? AND assistant_id=? AND problem_code=? AND stage=? AND policy_version=?""",
            (new_id, old_id, raw_assistant, problem, stage, policy),
        )
        evidence[str(old_id)][assistant] = new_id
    # Retain corrupt/expired legacy aggregates in a quarantine scope rather
    # than silently dropping audit evidence or assigning them to an active
    # Assistant Instance.
    for row in conn.execute(
        f"SELECT id,problem_code,stage,policy_version,observation_count,first_observed_at,last_observed_at FROM {legacy}",
    ).fetchall():
        old_id, problem, stage, policy, count, first_seen, last_seen = row
        if str(old_id) in evidence:
            continue
        assistant = LEGACY_UNATTRIBUTED_ASSISTANT_ID
        new_id = _cluster_id(assistant, str(problem), str(stage), str(policy))
        conn.execute(
            f"""INSERT OR IGNORE INTO {BEHAVIOR_OBSERVATION_CLUSTER_TABLE}(
                    id,assistant_id,problem_code,stage,policy_version,observation_count,first_observed_at,last_observed_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
            (new_id, assistant, problem, stage, policy, count, first_seen, last_seen),
        )
        evidence[str(old_id)][assistant] = new_id
    conn.execute(f"DROP TABLE {legacy}")
    return evidence


def _create_reference_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE {BEHAVIOR_BENCHMARK_REFERENCE_TABLE} (
            assistant_id TEXT NOT NULL,
            benchmark_ref TEXT NOT NULL,
            benchmark_hash TEXT NOT NULL,
            owner_approval_ref TEXT NOT NULL,
            run_ref TEXT NOT NULL,
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
            state TEXT NOT NULL CHECK(state='reference_baseline'),
            PRIMARY KEY(assistant_id,benchmark_ref),
            UNIQUE(assistant_id,benchmark_hash),
            UNIQUE(assistant_id,run_ref)
        );
        CREATE INDEX idx_behavior_benchmark_reference_assistant_recent
            ON {BEHAVIOR_BENCHMARK_REFERENCE_TABLE}(assistant_id,recorded_at DESC,benchmark_ref DESC);
        """,
    )


def _create_candidate_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE {BEHAVIOR_POLICY_CANDIDATE_TABLE} (
            candidate_id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            reference_policy_ref TEXT NOT NULL,
            source_observation_refs_json TEXT NOT NULL CHECK(json_valid(source_observation_refs_json)),
            patches_json TEXT NOT NULL CHECK(json_valid(patches_json)),
            policy_bundle_hash TEXT NOT NULL,
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
            evaluated_at TEXT,
            UNIQUE(assistant_id,policy_bundle_hash)
        );
        CREATE INDEX idx_behavior_policy_candidates_assistant_state_recent
            ON {BEHAVIOR_POLICY_CANDIDATE_TABLE}(assistant_id,state,created_at DESC,candidate_id DESC);
        CREATE TABLE {BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE} (
            candidate_id TEXT NOT NULL,
            assistant_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence>=1),
            from_state TEXT CHECK(from_state IS NULL OR from_state IN ('offline_evaluation','rejected','shadow_eligible')),
            to_state TEXT NOT NULL CHECK(to_state IN ('offline_evaluation','rejected','shadow_eligible')),
            reason TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id,sequence),
            FOREIGN KEY(candidate_id) REFERENCES {BEHAVIOR_POLICY_CANDIDATE_TABLE}(candidate_id) ON DELETE RESTRICT
        );
        CREATE INDEX idx_behavior_policy_candidate_transitions_assistant
            ON {BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE}(assistant_id,candidate_id,sequence);
        CREATE TABLE {BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE} (
            candidate_id TEXT NOT NULL,
            assistant_id TEXT NOT NULL,
            reference_policy_ref TEXT NOT NULL,
            restored_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id,restored_at),
            FOREIGN KEY(candidate_id) REFERENCES {BEHAVIOR_POLICY_CANDIDATE_TABLE}(candidate_id) ON DELETE RESTRICT
        );
        CREATE INDEX idx_behavior_policy_reference_restores_assistant
            ON {BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE}(assistant_id,restored_at DESC,candidate_id DESC);
        """,
    )


def _candidate_owner(source_refs: list[str], evidence: dict[str, dict[str, str]]) -> tuple[str, list[str], bool]:
    owners: set[str] = set()
    rewritten: list[str] = []
    for ref in source_refs:
        matched = evidence.get(ref, {})
        if len(matched) != 1:
            return LEGACY_UNATTRIBUTED_ASSISTANT_ID, source_refs, False
        owner = next(iter(matched))
        owners.add(owner)
        # Old refs are mapped to the deterministic v52 cluster identity only
        # after proving all source evidence belongs to this one Assistant.
        rewritten.append(str(matched[owner]))
    if len(owners) != 1:
        return LEGACY_UNATTRIBUTED_ASSISTANT_ID, source_refs, False
    return next(iter(owners)), rewritten, True


def _migrate_candidates(conn: sqlite3.Connection, evidence: dict[str, dict[str, str]]) -> dict[str, set[str]]:
    tables = _tables(conn)
    needed = {BEHAVIOR_POLICY_CANDIDATE_TABLE, BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE, BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE}
    if not needed <= tables:
        return {}
    if "assistant_id" in _columns(conn, BEHAVIOR_POLICY_CANDIDATE_TABLE):
        rows = conn.execute(
            f"SELECT benchmark_ref,assistant_id FROM {BEHAVIOR_POLICY_CANDIDATE_TABLE}",
        ).fetchall()
        result: dict[str, set[str]] = defaultdict(set)
        for benchmark_ref, assistant_id in rows:
            result[str(benchmark_ref)].add(_assistant_id(assistant_id))
        return result

    suffix = "_v51_legacy"
    legacy_candidate = BEHAVIOR_POLICY_CANDIDATE_TABLE + suffix
    legacy_transition = BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE + suffix
    legacy_restore = BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE + suffix
    conn.execute(f"ALTER TABLE {BEHAVIOR_POLICY_CANDIDATE_TABLE} RENAME TO {legacy_candidate}")
    conn.execute(f"ALTER TABLE {BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE} RENAME TO {legacy_transition}")
    conn.execute(f"ALTER TABLE {BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE} RENAME TO {legacy_restore}")
    _create_candidate_tables(conn)
    owners_by_candidate: dict[str, str] = {}
    owners_by_benchmark: dict[str, set[str]] = defaultdict(set)
    legacy_rows = conn.execute(f"SELECT * FROM {legacy_candidate}").fetchall()
    for row in legacy_rows:
        value = dict(row)
        source_refs = _json_list(value["source_observation_refs_json"])
        assistant, rewritten_refs, safe = _candidate_owner(source_refs, evidence)
        state = str(value["state"])
        reason = value["decision_reason"]
        if not safe or assistant == LEGACY_UNATTRIBUTED_ASSISTANT_ID:
            state = "rejected"
            reason = "legacy_cross_assistant_quarantined"
        conn.execute(
            f"""INSERT INTO {BEHAVIOR_POLICY_CANDIDATE_TABLE}(
                candidate_id,assistant_id,reference_policy_ref,source_observation_refs_json,patches_json,policy_bundle_hash,
                benchmark_ref,benchmark_hash,provider_ref,model_ref,thinking_ref,case_count,reference_metrics_json,
                campaign_budget_json,created_at,state,decision_reason,candidate_metrics_json,hard_failure_count,runs_per_case,evaluated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                value["candidate_id"], assistant, value["reference_policy_ref"], _canonical(rewritten_refs), value["patches_json"],
                value["policy_bundle_hash"], value["benchmark_ref"], value["benchmark_hash"], value["provider_ref"], value["model_ref"],
                value["thinking_ref"], value["case_count"], value["reference_metrics_json"], value["campaign_budget_json"],
                value["created_at"], state, reason, value["candidate_metrics_json"], value["hard_failure_count"],
                value["runs_per_case"], value["evaluated_at"],
            ),
        )
        owners_by_candidate[str(value["candidate_id"])] = assistant
        owners_by_benchmark[str(value["benchmark_ref"])].add(assistant)
    for row in conn.execute(f"SELECT * FROM {legacy_transition} ORDER BY candidate_id,sequence").fetchall():
        value = dict(row)
        assistant = owners_by_candidate.get(str(value["candidate_id"]), LEGACY_UNATTRIBUTED_ASSISTANT_ID)
        conn.execute(
            f"INSERT INTO {BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE}(candidate_id,assistant_id,sequence,from_state,to_state,reason,occurred_at) VALUES(?,?,?,?,?,?,?)",
            (value["candidate_id"], assistant, value["sequence"], value["from_state"], value["to_state"], value["reason"], value["occurred_at"]),
        )
    for row in conn.execute(f"SELECT * FROM {legacy_restore}").fetchall():
        value = dict(row)
        assistant = owners_by_candidate.get(str(value["candidate_id"]), LEGACY_UNATTRIBUTED_ASSISTANT_ID)
        conn.execute(
            f"INSERT INTO {BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE}(candidate_id,assistant_id,reference_policy_ref,restored_at) VALUES(?,?,?,?)",
            (value["candidate_id"], assistant, value["reference_policy_ref"], value["restored_at"]),
        )
    conn.execute(f"DROP TABLE {legacy_transition}")
    conn.execute(f"DROP TABLE {legacy_restore}")
    conn.execute(f"DROP TABLE {legacy_candidate}")
    return owners_by_benchmark


def _migrate_references(conn: sqlite3.Connection, owners_by_benchmark: dict[str, set[str]]) -> None:
    tables = _tables(conn)
    if BEHAVIOR_BENCHMARK_REFERENCE_TABLE not in tables:
        return
    if "assistant_id" in _columns(conn, BEHAVIOR_BENCHMARK_REFERENCE_TABLE):
        return
    legacy = BEHAVIOR_BENCHMARK_REFERENCE_TABLE + "_v51_legacy"
    conn.execute(f"ALTER TABLE {BEHAVIOR_BENCHMARK_REFERENCE_TABLE} RENAME TO {legacy}")
    _create_reference_table(conn)
    for row in conn.execute(f"SELECT * FROM {legacy}").fetchall():
        value = dict(row)
        owners = owners_by_benchmark.get(str(value["benchmark_ref"]), set()) or {LEGACY_UNATTRIBUTED_ASSISTANT_ID}
        for assistant in sorted(owners):
            conn.execute(
                f"""INSERT INTO {BEHAVIOR_BENCHMARK_REFERENCE_TABLE}(
                    assistant_id,benchmark_ref,benchmark_hash,owner_approval_ref,run_ref,evaluator_ref,signing_key_ref,
                    provider_ref,model_ref,thinking_ref,reference_policy_ref,case_count,runs_per_case,campaign_budget_json,
                    reference_metrics_json,hard_failure_count,recorded_at,state
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (assistant, *[value[name] for name in (
                    "benchmark_ref", "benchmark_hash", "owner_approval_ref", "run_ref", "evaluator_ref", "signing_key_ref",
                    "provider_ref", "model_ref", "thinking_ref", "reference_policy_ref", "case_count", "runs_per_case",
                    "campaign_budget_json", "reference_metrics_json", "hard_failure_count", "recorded_at", "state",
                )]),
            )
    conn.execute(f"DROP TABLE {legacy}")


def _migrate_combined_receipts(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    if BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE not in tables or "assistant_id" in _columns(conn, BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE):
        return
    legacy = BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE + "_v51_legacy"
    conn.execute(f"ALTER TABLE {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE} RENAME TO {legacy}")
    conn.executescript(
        f"""
        CREATE TABLE {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE} (
            combined_shadow_ref TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
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
        CREATE INDEX idx_behavior_combined_shadow_assistant_recent
            ON {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE}(assistant_id,created_at DESC,combined_shadow_ref DESC);
        """,
    )
    for row in conn.execute(f"SELECT * FROM {legacy}").fetchall():
        value = dict(row)
        # The v51 receipt intentionally contains only one-way event refs; it
        # cannot be reverse-joined to a decision without weakening privacy.
        conn.execute(
            f"""INSERT INTO {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE}(
                combined_shadow_ref,assistant_id,scope_ref,source_event_ref,trace_ref,evidence_refs_json,benchmark_ref,benchmark_hash,
                candidate_ref,reference_hard_failure_count,candidate_hard_failure_count,metric_delta_json,created_at,expires_at,state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (value["combined_shadow_ref"], LEGACY_UNATTRIBUTED_ASSISTANT_ID, *[value[name] for name in (
                "scope_ref", "source_event_ref", "trace_ref", "evidence_refs_json", "benchmark_ref", "benchmark_hash",
                "candidate_ref", "reference_hard_failure_count", "candidate_hard_failure_count", "metric_delta_json",
                "created_at", "expires_at", "state",
            )]),
        )
    conn.execute(f"DROP TABLE {legacy}")


def _migrate_response_assessments(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    if RESPONSE_ASSESSMENT_TABLE not in tables or "assistant_id" in _columns(conn, RESPONSE_ASSESSMENT_TABLE):
        return
    legacy = RESPONSE_ASSESSMENT_TABLE + "_v51_legacy"
    conn.execute(f"ALTER TABLE {RESPONSE_ASSESSMENT_TABLE} RENAME TO {legacy}")
    conn.executescript(
        f"""
        CREATE TABLE {RESPONSE_ASSESSMENT_TABLE} (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('group','private')),
            scope_ref TEXT NOT NULL,
            topic_ref TEXT NOT NULL,
            topic_revision INTEGER NOT NULL CHECK(topic_revision>=0),
            target_ref TEXT NOT NULL,
            reply_obligation TEXT NOT NULL,
            speech_act TEXT NOT NULL,
            grounding_status TEXT NOT NULL,
            stance_json TEXT NOT NULL CHECK(json_valid(stance_json)),
            research_disposition TEXT NOT NULL,
            task_continuation TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(assistant_id,source_kind,source_ref)
        );
        CREATE INDEX idx_response_assessments_assistant_recent
            ON {RESPONSE_ASSESSMENT_TABLE}(assistant_id,created_at DESC,id DESC);
        CREATE INDEX idx_response_assessments_assistant_topic
            ON {RESPONSE_ASSESSMENT_TABLE}(assistant_id,scope_ref,topic_ref,topic_revision,created_at DESC);
        """,
    )
    for row in conn.execute(f"SELECT * FROM {legacy}").fetchall():
        value = dict(row)
        conn.execute(
            f"""INSERT INTO {RESPONSE_ASSESSMENT_TABLE}(
                id,assistant_id,scope_type,scope_ref,topic_ref,topic_revision,target_ref,reply_obligation,speech_act,
                grounding_status,stance_json,research_disposition,task_continuation,policy_version,source_kind,source_ref,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (value["id"], LEGACY_UNATTRIBUTED_ASSISTANT_ID, *[value[name] for name in (
                "scope_type", "scope_ref", "topic_ref", "topic_revision", "target_ref", "reply_obligation", "speech_act",
                "grounding_status", "stance_json", "research_disposition", "task_continuation", "policy_version",
                "source_kind", "source_ref", "created_at",
            )]),
        )
    conn.execute(f"DROP TABLE {legacy}")


def apply_behavior_assistant_isolation_v2(conn: sqlite3.Connection) -> None:
    """Upgrade v51 body-free records to Assistant Instance scoped records."""

    evidence = _migrate_observation_clusters(conn)
    owners_by_benchmark = _migrate_candidates(conn, evidence)
    _migrate_references(conn, owners_by_benchmark)
    _migrate_combined_receipts(conn)
    _migrate_response_assessments(conn)


def require_behavior_assistant_isolation_schema(conn: sqlite3.Connection) -> dict:
    expected = {
        BEHAVIOR_OBSERVATION_CLUSTER_TABLE: {"assistant_id"},
        BEHAVIOR_BENCHMARK_REFERENCE_TABLE: {"assistant_id"},
        BEHAVIOR_POLICY_CANDIDATE_TABLE: {"assistant_id"},
        BEHAVIOR_POLICY_CANDIDATE_TRANSITION_TABLE: {"assistant_id"},
        BEHAVIOR_POLICY_REFERENCE_RESTORE_TABLE: {"assistant_id"},
        BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE: {"assistant_id"},
        RESPONSE_ASSESSMENT_TABLE: {"assistant_id"},
    }
    tables = _tables(conn)
    missing = [
        f"{table}.{column}"
        for table, columns in expected.items()
        for column in sorted(columns - (_columns(conn, table) if table in tables else set()))
    ]
    if missing:
        raise MigrationDriftError("behavior_assistant_isolation_schema_drift:" + ",".join(missing))
    return {"ok": True}


__all__ = [
    "BEHAVIOR_ASSISTANT_ISOLATION_MIGRATION_CHECKSUM",
    "LEGACY_UNATTRIBUTED_ASSISTANT_ID",
    "apply_behavior_assistant_isolation_v2",
    "require_behavior_assistant_isolation_schema",
]
