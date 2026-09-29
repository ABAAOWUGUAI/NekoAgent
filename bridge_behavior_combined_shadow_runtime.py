#!/usr/bin/env python3
"""BE-6 zero-send runtime hook for body-free Combined Shadow.

The hook reads existing Quality Receipt / decision facts, rechecks the QQ
group allowlist at execution time, and gives the pure contract only opaque
references.  A successful comparison may append one retention-bounded,
body-free Shadow receipt.  That receipt is the sole permitted persistence:
this module never calls Delivery, Knowledge, Memory, Task, Approval, or any
write API belonging to those formal domains.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading
from bridge_behavior_benchmark_registry import behavior_benchmark_growth_summary
from bridge_behavior_candidate_registry import (
    BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE,
    require_behavior_candidate_assistant_isolation_schema,
    require_behavior_candidate_registry_schema,
    resolve_shadow_eligible_pair_candidate,
)
from bridge_behavior_owner_authorization import (
    BehaviorOwnerAuthorizationError,
    behavior_owner_authorization_plan,
    verify_behavior_owner_authorization,
)
from bridge_behavior_owner_authorization_schema import (
    BEHAVIOR_OWNER_AUTHORIZATION_TABLE,
    require_behavior_owner_authorization_schema,
)
from bridge_behavior_paired_shadow_cutover import paired_shadow_runtime_config
from bridge_migrations import MigrationDriftError
from bridge_behavior_combined_shadow import (
    CombinedShadowContractError,
    assert_combined_shadow_zero_effect,
    run_combined_shadow,
)
from bridge_behavior_paired_shadow import PairedShadowError, run_paired_shadow_context


_MAX_VOLATILE_RECEIPTS = 200
_REQUIRED_METRICS = frozenset(
    {"total_score", "reply_obligation_recall", "ambient_intrusion_rate", "citation_completeness"},
)
_PAIRED_SHADOW_RUNTIME_FIELDS = frozenset(
    {
        "enabled", "assistant_id", "scope_refs", "candidate_ref",
        "candidate_policy_bundle_hash", "reference_policy_ref", "benchmark_ref",
        "benchmark_hash", "authorization_ref", "authorization_binding_hash",
        "expires_at", "plan_checksum",
    },
)


class CombinedShadowRuntimeError(ValueError):
    """Raised when the runtime boundary would stop being strictly read-only."""


def _opaque_ref(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return f"{prefix}:sha256-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _VolatileLedger:
    """A bounded observability cache; durable receipts remain canonical."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receipts: deque[dict[str, object]] = deque(maxlen=_MAX_VOLATILE_RECEIPTS)
        self._seen: set[str] = set()
        self._last_status = "default_off"
        self._last_reason = "runtime_config_missing"
        self._last_run_at = ""

    def note(self, *, status: str, reason: str) -> None:
        with self._lock:
            self._last_status = status
            self._last_reason = reason
            self._last_run_at = _utc_now()

    def append(self, record: Mapping[str, object]) -> bool:
        assert_combined_shadow_zero_effect(record)
        identifier = str(record["combined_shadow_ref"])
        # A process-local de-duplication guard only improves diagnostics.  The
        # durable receipt primary key is the restart-safe exactly-once fence.
        with self._lock:
            if identifier in self._seen:
                return False
            if len(self._receipts) == self._receipts.maxlen:
                evicted = self._receipts.popleft()
                self._seen.discard(str(evicted.get("combined_shadow_ref") or ""))
            self._receipts.append(dict(record))
            self._seen.add(identifier)
            self._last_status = "shadow_only_durable"
            self._last_reason = "durable_receipt_recorded"
            self._last_run_at = _utc_now()
            return True

    def summary(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._last_status,
                "delivery_enabled": False,
                "formal_domain_writes": False,
                "receipt_persistence": "unknown_without_database",
                "audit_status": "runtime_status_only",
                "last_reason": self._last_reason,
                "last_run_at": self._last_run_at,
                "volatile_receipt_count": len(self._receipts),
            }

    def receipts(self, *, limit: object = 50) -> list[dict[str, object]]:
        try:
            safe_limit = max(1, min(int(limit), _MAX_VOLATILE_RECEIPTS))
        except (TypeError, ValueError):
            safe_limit = 50
        with self._lock:
            return [dict(item) for item in list(self._receipts)[-safe_limit:][::-1]]

    def reset_for_tests(self) -> None:
        with self._lock:
            self._receipts.clear()
            self._seen.clear()
            self._last_status = "default_off"
            self._last_reason = "runtime_config_missing"
            self._last_run_at = ""


_LEDGER = _VolatileLedger()


def _safe_limit(value: object, *, maximum: int = _MAX_VOLATILE_RECEIPTS) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return 50


def combined_shadow_runtime_projection(conn: sqlite3.Connection | None = None, *, assistant_id: object = "") -> dict[str, object]:
    """Return an Owner-visible status without chat body, group ID, or policy text."""

    projection = _LEDGER.summary()
    if conn is None:
        return projection
    try:
        require_behavior_candidate_registry_schema(conn)
        require_behavior_candidate_assistant_isolation_schema(conn)
        owner = str(assistant_id or "").strip()
        if not owner:
            raise ValueError("combined_shadow_assistant_id_required")
        count = int(conn.execute(
            f"SELECT COUNT(*) FROM {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE} WHERE assistant_id=?", (owner,),
        ).fetchone()[0])
    except (sqlite3.Error, MigrationDriftError):
        return {
            **projection,
            "state": "default_off",
            "runtime_state": projection["state"],
            "receipt_persistence": "not_installed",
            "audit_status": "receipt_schema_unavailable",
            "durable_receipt_count": 0,
        }
    return {
        **projection,
        # The durable row count is authoritative across worker restarts and
        # database connections.  The process-local cache remains diagnostic
        # only; allowing it to determine ``state`` would show a stale Shadow
        # result on a different database/read model.
        "state": "shadow_only_durable" if count else "default_off",
        "runtime_state": projection["state"],
        "receipt_persistence": "durable_body_free",
        "audit_status": "durable_shadow_receipts",
        "durable_receipt_count": count,
    }


def list_combined_shadow_receipts(
    conn: sqlite3.Connection,
    *,
    assistant_id: object,
    limit: object = 50,
) -> list[dict[str, object]]:
    """Read retention-bounded, body-free receipts from the canonical store."""

    require_behavior_candidate_registry_schema(conn)
    require_behavior_candidate_assistant_isolation_schema(conn)
    owner = str(assistant_id or "").strip()
    if not owner:
        raise CombinedShadowRuntimeError("combined_shadow_assistant_id_required")
    rows = conn.execute(
        f"""SELECT combined_shadow_ref,assistant_id,scope_ref,source_event_ref,trace_ref,evidence_refs_json,
                   benchmark_ref,benchmark_hash,candidate_ref,reference_hard_failure_count,
                   candidate_hard_failure_count,metric_delta_json,created_at,expires_at,state
              FROM {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE}
             WHERE assistant_id=? ORDER BY created_at DESC,combined_shadow_ref DESC LIMIT ?""",
        (owner, _safe_limit(limit)),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        value = dict(row)
        try:
            evidence_refs = json.loads(str(value.pop("evidence_refs_json")))
            metric_delta = json.loads(str(value.pop("metric_delta_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise CombinedShadowRuntimeError("combined_shadow_receipt_json_invalid")
        receipt = {
            **value,
            "evidence_refs": evidence_refs,
            "metric_delta": metric_delta,
            "delivery_count": 0,
            "formal_writes": [],
            "side_effects": [],
        }
        paired_execution = metric_delta.pop("paired_execution", None)
        if paired_execution is not None:
            if not isinstance(paired_execution, Mapping):
                raise CombinedShadowRuntimeError("combined_shadow_receipt_json_invalid")
            receipt["paired_execution"] = dict(paired_execution)
        assert_combined_shadow_zero_effect(receipt)
        result.append(receipt)
    return result


def _reference(conn, assistant_id: str) -> dict[str, object] | None:
    summary = behavior_benchmark_growth_summary(conn, assistant_id=assistant_id)
    if summary.get("state") != "baseline_recorded" or summary.get("reference_baseline") != "recorded":
        return None
    metrics = summary.get("reference_metrics")
    if not isinstance(metrics, Mapping) or set(_REQUIRED_METRICS) - set(metrics):
        return None
    return {
        "assistant_id": assistant_id,
        "benchmark_ref": summary.get("benchmark_ref"),
        "benchmark_hash": summary.get("benchmark_hash"),
        "reference_policy_ref": summary.get("reference_policy_ref"),
        "reference_metrics": {name: metrics[name] for name in sorted(_REQUIRED_METRICS)},
        "hard_failure_count": summary.get("hard_failure_count"),
    }


def _persist_receipt(conn, record: Mapping[str, object], *, assistant_id: str) -> bool:
    """Persist only the inert, body-free BE-6 receipt; never a formal domain."""

    assert_combined_shadow_zero_effect(record)
    metric_delta = dict(record["metric_delta"])
    # v51 stored the benchmark-level deltas in this JSON column.  v52 keeps
    # those fields stable and adds an explicitly named body-free pair result,
    # avoiding a second durable domain/schema solely for a retention-bounded
    # Shadow audit record.
    if isinstance(record.get("paired_execution"), Mapping):
        metric_delta["paired_execution"] = dict(record["paired_execution"])
    conn.execute(
        f"""INSERT INTO {BEHAVIOR_COMBINED_SHADOW_RECEIPT_TABLE}(
            combined_shadow_ref,assistant_id,scope_ref,source_event_ref,trace_ref,evidence_refs_json,
            benchmark_ref,benchmark_hash,candidate_ref,reference_hard_failure_count,
            candidate_hard_failure_count,metric_delta_json,created_at,expires_at,state
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'shadow_only')
        ON CONFLICT(combined_shadow_ref) DO NOTHING""",
        (
            record["combined_shadow_ref"], assistant_id, record["scope_ref"], record["source_event_ref"], record["trace_ref"],
            json.dumps(record["evidence_refs"], ensure_ascii=True, separators=(",", ":")), record["benchmark_ref"],
            record["benchmark_hash"], record["candidate_ref"], record["reference_hard_failure_count"],
            record["candidate_hard_failure_count"], json.dumps(metric_delta, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            _utc_now(),
            (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)).isoformat(),
        ),
    )
    return bool(conn.execute("SELECT changes()").fetchone()[0])


def _utc(value: object, *, error: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CombinedShadowRuntimeError(error) from exc
    if parsed.tzinfo is None:
        raise CombinedShadowRuntimeError(error)
    return parsed.astimezone(timezone.utc).isoformat()


def _paired_shadow_binding(config: Mapping[str, object]) -> dict[str, object]:
    """Rebuild the exact v53 binding from the body-free v54 snapshot."""

    return {
        "assistant_id": config["assistant_id"],
        "candidate_ref": config["candidate_ref"],
        "policy_bundle_hash": config["candidate_policy_bundle_hash"],
        "reference_policy_ref": config["reference_policy_ref"],
        "benchmark_ref": config["benchmark_ref"],
        "benchmark_hash": config["benchmark_hash"],
        "scope_refs": config["scope_refs"],
        "cutover_plan_checksum": config["plan_checksum"],
    }


def _live_runtime_config(
    runtime: Mapping[str, object],
    conn: sqlite3.Connection,
    *,
    assistant_id: str,
    now: str,
) -> dict[str, object]:
    """Accept only the exact, current v54 database snapshot for this Assistant."""

    if set(runtime) != _PAIRED_SHADOW_RUNTIME_FIELDS:
        raise CombinedShadowRuntimeError("paired_shadow_runtime_config_invalid")
    if runtime.get("enabled") is not True:
        raise CombinedShadowRuntimeError("paired_shadow_runtime_config_disabled")
    config = dict(runtime)
    if str(config.get("assistant_id") or "").strip() != assistant_id:
        raise CombinedShadowRuntimeError("paired_shadow_runtime_assistant_mismatch")
    scope_refs = config.get("scope_refs")
    if not isinstance(scope_refs, list) or not scope_refs or scope_refs != sorted(set(scope_refs)):
        raise CombinedShadowRuntimeError("paired_shadow_runtime_scope_invalid")
    if any(not isinstance(value, str) or not value.strip() for name, value in config.items() if name not in {"enabled", "scope_refs"}):
        raise CombinedShadowRuntimeError("paired_shadow_runtime_config_invalid")
    if _utc(config["expires_at"], error="paired_shadow_runtime_expiry_invalid") <= now:
        raise CombinedShadowRuntimeError("paired_shadow_runtime_expired")
    # The Bridge invokes this loader in the same inbound path.  Reading it
    # again makes a disable or changed Cutover between hand-off and execution
    # a no-op, and ensures a caller-supplied mapping is never an authority.
    current = paired_shadow_runtime_config(conn, assistant_id=assistant_id)
    if current != config:
        raise CombinedShadowRuntimeError("paired_shadow_runtime_stale")
    return config


def _reverify_owner_authorization(
    conn: sqlite3.Connection,
    *,
    config: Mapping[str, object],
    assistant_id: str,
    now: str,
) -> None:
    """Verify the linked v53 authorization without making a durable state change."""

    require_behavior_owner_authorization_schema(conn)
    authorization_ref = str(config["authorization_ref"])
    row = conn.execute(
        f"SELECT state,expires_at FROM {BEHAVIOR_OWNER_AUTHORIZATION_TABLE} WHERE authorization_ref=? AND assistant_id=?",
        (authorization_ref, assistant_id),
    ).fetchone()
    if row is None or str(row["state"] or "") != "active":
        raise CombinedShadowRuntimeError("paired_shadow_owner_authorization_inactive")
    # ``verify_behavior_owner_authorization`` records expiry transitions for
    # state-changing paths.  Reject a known-expired authorization before it
    # can append an Owner-Authorization audit row on this no-op path.
    if _utc(row["expires_at"], error="paired_shadow_owner_authorization_expiry_invalid") <= now:
        raise CombinedShadowRuntimeError("paired_shadow_owner_authorization_expired")
    binding = _paired_shadow_binding(config)
    plan = behavior_owner_authorization_plan(
        conn,
        purpose="paired_shadow_enable",
        binding=binding,
        now=now,
    )
    if str(config["authorization_binding_hash"]) != str(plan["binding_hash"]):
        raise CombinedShadowRuntimeError("paired_shadow_authorization_binding_hash_mismatch")
    conn.execute("SAVEPOINT combined_shadow_owner_authorization")
    try:
        verify_behavior_owner_authorization(
            conn,
            authorization_ref=authorization_ref,
            purpose="paired_shadow_enable",
            assistant_id=assistant_id,
            binding=binding,
            now=now,
            consume=False,
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT combined_shadow_owner_authorization")
        conn.execute("RELEASE SAVEPOINT combined_shadow_owner_authorization")
        raise
    conn.execute("RELEASE SAVEPOINT combined_shadow_owner_authorization")


def _live_context(
    *,
    group_id: object,
    event: object,
    decision: Mapping[str, object] | None,
    conversation_frame: Mapping[str, object] | None,
) -> tuple[str, str, dict[str, object], dict[str, object]]:
    """Project a current in-memory group turn without retaining its body/IDs."""

    assistant_id = str(getattr(event, "assistant_id", "") or "").strip()
    event_id = str(getattr(event, "event_id", "") or "").strip()
    raw_group = str(group_id or "").strip()
    if not assistant_id or not event_id or not raw_group:
        raise CombinedShadowRuntimeError("combined_shadow_live_context_invalid")
    frame = conversation_frame if isinstance(conversation_frame, Mapping) else {}
    raw_decision = decision if isinstance(decision, Mapping) else {}
    attention = str(frame.get("attention") or "unknown")
    if attention not in {"explicit_mention", "reply_to_assistant", "ambient_optional"}:
        attention = "unknown"
    grounding = str(raw_decision.get("grounding_status") or "not_required")
    if grounding not in {"grounded", "not_required", "unknown", "blocked"}:
        grounding = "unknown"
    research = str(raw_decision.get("research_disposition") or "not_needed")
    if research not in {"not_needed", "research_answer", "research_blocked"}:
        research = "not_needed"
    work_lifecycle = str(raw_decision.get("work_lifecycle") or "")
    task_continuation = "preserve_due_work" if work_lifecycle in {"continue", "active"} else "not_applicable"
    scope_ref = _opaque_ref("scope", raw_group)
    source_event_ref = _opaque_ref("event", event_id)
    trace_ref = _opaque_ref("trace", event_id)
    contract_event = {
        "schema_version": 1,
        "scope_type": "group",
        "scope_ref": scope_ref,
        "admitted_scope": True,
        "source_event_ref": source_event_ref,
        "trace_ref": trace_ref,
        "evidence_refs": [source_event_ref, trace_ref],
    }
    temporary = {
        "scope_ref": scope_ref,
        "source_event_ref": source_event_ref,
        "trace_ref": trace_ref,
        "attention": attention,
        "topic_active": bool(frame.get("topic_active")),
        "reference_should_reply": bool(raw_decision.get("should_reply")),
        "grounding_status": grounding,
        "research_disposition": research,
        "task_continuation": task_continuation,
    }
    return assistant_id, raw_group, contract_event, temporary


def process_combined_shadow_live_context(
    runtime: Mapping[str, object],
    conn: sqlite3.Connection,
    *,
    group_id: object,
    event: object,
    decision: Mapping[str, object] | None,
    conversation_frame: Mapping[str, object] | None,
) -> dict[str, object]:
    """Pair Reference/Candidate in the current inbound stack, never later.

    This is the only BE-6 persistence entrypoint.  It receives no body and
    runs no model/network.  The existing delivery result is not mutated; an
    admission/baseline/candidate failure is a non-authoritative no-op.
    """

    if not isinstance(runtime, Mapping):
        _LEDGER.note(status="default_off", reason="runtime_invalid")
        return {**_LEDGER.summary(), "processed": 0}
    try:
        require_behavior_candidate_registry_schema(conn)
        require_behavior_candidate_assistant_isolation_schema(conn)
        assistant_id, raw_group, contract_event, temporary = _live_context(
            group_id=group_id, event=event, decision=decision, conversation_frame=conversation_frame,
        )
        now = _utc_now()
        config = _live_runtime_config(runtime, conn, assistant_id=assistant_id, now=now)
        if contract_event["scope_ref"] not in config["scope_refs"]:
            raise CombinedShadowRuntimeError("paired_shadow_runtime_scope_mismatch")
        admitted = conn.execute(
            "SELECT enabled FROM qq_access_entries WHERE subject_type='qq_group' AND subject_id=?",
            (raw_group,),
        ).fetchone()
        if admitted is None or int(admitted[0]) != 1:
            raise CombinedShadowRuntimeError("combined_shadow_scope_not_admitted")
        reference = _reference(conn, assistant_id)
        if reference is None:
            raise CombinedShadowRuntimeError("reference_baseline_missing")
        candidate = resolve_shadow_eligible_pair_candidate(conn, reference)
        if candidate is None or str(candidate.get("assistant_id") or "") != assistant_id:
            raise CombinedShadowRuntimeError("candidate_registry_unavailable")
        current_binding = {
            "assistant_id": assistant_id,
            "candidate_ref": str(candidate.get("candidate_ref") or ""),
            "candidate_policy_bundle_hash": str(candidate.get("policy_bundle_hash") or ""),
            "reference_policy_ref": str(reference.get("reference_policy_ref") or ""),
            "benchmark_ref": str(reference.get("benchmark_ref") or ""),
            "benchmark_hash": str(reference.get("benchmark_hash") or ""),
        }
        if any(str(config[field]) != value for field, value in current_binding.items()):
            raise CombinedShadowRuntimeError("paired_shadow_runtime_binding_drift")
        _reverify_owner_authorization(conn, config=config, assistant_id=assistant_id, now=now)
        candidate_metrics = candidate.get("candidate_metrics")
        if not isinstance(candidate_metrics, Mapping) or set(_REQUIRED_METRICS) - set(candidate_metrics):
            raise CombinedShadowRuntimeError("candidate_registry_unavailable")
        # Candidate Registry also carries offline cost/latency observations.
        # The existing pure Combined Shadow contract intentionally accepts
        # only its four comparison metrics, so retain the current inbound
        # projection without changing the authoritative Candidate record.
        paired_candidate = {
            **candidate,
            "candidate_metrics": {name: candidate_metrics[name] for name in sorted(_REQUIRED_METRICS)},
        }
        record = run_paired_shadow_context(contract_event, reference, paired_candidate, temporary)
        if _persist_receipt(conn, record, assistant_id=assistant_id):
            _LEDGER.append(record)
            return {**combined_shadow_runtime_projection(conn, assistant_id=assistant_id), "processed": 1, "rejected": 0}
        return {**combined_shadow_runtime_projection(conn, assistant_id=assistant_id), "processed": 0, "rejected": 0, "reason": "duplicate_live_context"}
    except (
        sqlite3.Error, MigrationDriftError, BehaviorOwnerAuthorizationError,
        CombinedShadowContractError, PairedShadowError, CombinedShadowRuntimeError, TypeError, ValueError,
    ) as exc:
        _LEDGER.note(status="default_off", reason=str(exc) or "live_context_rejected")
        return {**_LEDGER.summary(), "processed": 0, "rejected": 1}


def _group_from_thread(value: object) -> str:
    thread = str(value or "").strip()
    prefix = "qq:group:"
    if not thread.startswith(prefix):
        return ""
    group = thread[len(prefix):].strip()
    return group[:180] if group else ""


def _read_admitted_events(conn, *, limit: int) -> list[dict[str, object]]:
    """Read limited Quality Receipt facts and recheck admission before shadowing.

    The raw group ID exists only in this short-lived authorization comparison;
    the returned event contains its one-way opaque scope ref.
    """

    try:
        rows = conn.execute(
            """
            SELECT q.id AS receipt_id,q.decision_id,q.created_at,d.thread_id,d.assistant_id
              FROM qq_quality_receipts q
              JOIN engagement_decisions d ON d.id=q.decision_id
             ORDER BY q.created_at DESC,q.id DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        return []
    events: list[dict[str, object]] = []
    for row in rows:
        receipt_id = str(row["receipt_id"] or "").strip()
        decision_id = str(row["decision_id"] or "").strip()
        assistant_id = str(row["assistant_id"] or "").strip()
        group_id = _group_from_thread(row["thread_id"])
        if not receipt_id or not decision_id or not assistant_id or not group_id:
            continue
        try:
            admitted = conn.execute(
                """
                SELECT enabled FROM qq_access_entries
                 WHERE subject_type='qq_group' AND subject_id=?
                """,
                (group_id,),
            ).fetchone()
        except Exception:
            continue
        if admitted is None or not bool(int(admitted[0])):
            continue
        # Keep all returned identifiers within the strict opaque-ref grammar.
        events.append(
            {
                "schema_version": 1,
                "assistant_id": assistant_id,
                "scope_type": "group",
                "scope_ref": _opaque_ref("scope", group_id),
                "admitted_scope": True,
                "source_event_ref": _opaque_ref("receipt", receipt_id),
                "trace_ref": _opaque_ref("trace", decision_id),
                "evidence_refs": [
                    _opaque_ref("receipt", receipt_id),
                    _opaque_ref("decision", decision_id),
                ],
            },
        )
    return events


def process_combined_shadow_pass(runtime: Mapping[str, object], conn) -> dict[str, object]:
    """Refuse the obsolete receipt-scan path.

    A past Quality Receipt cannot recreate the original inbound context.  The
    former implementation produced an aggregate comparison that looked like a
    paired Shadow but never executed Reference/Candidate on one turn.  BE-6
    therefore runs only through :func:`process_combined_shadow_live_context`.
    """

    del runtime, conn
    _LEDGER.note(status="default_off", reason="live_context_required")
    return {**_LEDGER.summary(), "processed": 0, "rejected": 0}


def _reset_combined_shadow_runtime_for_tests() -> None:
    """Test-only reset for the non-canonical observability cache."""

    _LEDGER.reset_for_tests()


__all__ = [
    "CombinedShadowRuntimeError",
    "combined_shadow_runtime_projection",
    "list_combined_shadow_receipts",
    "process_combined_shadow_live_context",
    "process_combined_shadow_pass",
]
