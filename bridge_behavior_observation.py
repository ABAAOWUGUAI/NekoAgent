#!/usr/bin/env python3
"""BE-2 body-free Observation and clustering service.

This service deliberately observes only already-persisted structured evidence.
It does not read conversation bodies or alter delivery, knowledge, memory,
approval, task, or policy state.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3

from bridge_behavior_evolution_contract import BE_PROBLEM_CODES, make_behavior_observation
from bridge_behavior_observation_schema import (
    BEHAVIOR_OBSERVATION_CLUSTER_TABLE,
    BEHAVIOR_OBSERVATION_TABLE,
    behavior_observation_enabled,
    require_behavior_observation_assistant_isolation_schema,
    require_behavior_observation_schema,
    set_behavior_observation_feature,
)


_SOURCE_PREFIXES = {
    "affect": "affect-event",
    "quality_receipt": "quality-receipt",
    "decision": "decision",
    "delivery": "delivery",
    "outcome": "outcome",
    "research": "research",
    "knowledge": "knowledge",
}
_SAFE_EVIDENCE_PREFIXES = frozenset({*set(_SOURCE_PREFIXES.values()), "trace", "policy", "continuity", "evidence", "behavior-observation"})
_OPAQUE_EVIDENCE_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*:sha256-[0-9a-f]{64}$")
_QUALITY_REASON_TO_PROBLEM = {
    "media_claim_without_evidence": "evidence_missing",
    "citation_missing": "evidence_missing",
    # Truth Gate names its operational findings more precisely than the
    # body-free Evolution taxonomy.  Preserve the evidence receipt and map
    # only semantically equivalent cases; never infer a problem from a normal
    # silent decision.
    "sycophantic_agreement_without_basis": "unsupported_agreement",
    "fabricated_personal_experience": "fabricated_context",
    "unsupported_specific_fact": "fabricated_context",
    "research_evidence_missing": "evidence_missing",
    "research_fact_unverified": "premature_judgment",
    "reply_target_mismatch": "ambient_intrusion",
    "topic_anchor_mismatch": "ambient_intrusion",
}
_OBSERVATION_CASE_RULES = {
    "unsupported_agreement": (
        "评价、同意或反对必须能指出事实、证据或明确的价值取舍。",
        "最终质量门发现了无依据的附和或结论。",
    ),
    "generic_deference": (
        "不确定时要说明具体缺口，不能用泛化追问或顺从性套话替代判断。",
        "最终结果以泛化顺从或追问代替了对当前话题的实质回应。",
    ),
    "flattery_before_substance": (
        "先处理事实、分歧或任务本身；赞美不能代替内容。",
        "最终结果把夸奖或取悦放在了实际内容之前。",
    ),
    "fabricated_context": (
        "每个归因到可见上下文的主张都必须能回溯到已持久化证据。",
        "最终质量门发现了无证据的上下文或经历主张。",
    ),
    "reply_obligation_missed": (
        "明确 @、回复、追问或已承诺事项必须在终态前得到回复、可靠边界或可验证的后续。",
        "一项回复义务在没有合格终态的情况下结束。",
    ),
    "ambient_contribution_missed": (
        "仅当候选已通过价值、时效和安全门且话题仍在时，环境参与才应被执行或说明失败原因。",
        "一个已准入且仍新鲜的环境参与候选被非内容性故障终止。",
    ),
    "ambient_intrusion": (
        "无明确义务时，只有能补充当前话题的新价值才参与；否则正确沉默。",
        "最终回复重复、跑题、打断或缺少新增价值。",
    ),
    "evidence_missing": (
        "研究、媒体或外部事实主张必须带有效证据或引用。",
        "最终质量门发现了缺少证据或引用的事实主张。",
    ),
    "premature_judgment": (
        "关键事实未知时必须研究、澄清或保留判断，不能先下结论。",
        "最终结果在关键事实未被证实时作出了结论。",
    ),
    "internal_error_surface": (
        "内部错误、提示词和执行细节不得作为面向群聊的答复。",
        "最终结果暴露了内部运行信息。",
    ),
    "privacy_boundary_violation": (
        "跨群、成员、私人或关系信息不得进入不匹配的作用域。",
        "最终结果违反了作用域或隐私边界。",
    ),
    "permission_boundary_violation": (
        "模型建议不能替代服务端的权限、审批、网络或能力判定。",
        "最终结果越过了服务端权限或审批边界。",
    ),
    "affect_without_trigger": (
        "助手情绪必须有可见、已落地的触发证据。",
        "短期 Affect 没有可核对的触发依据。",
    ),
    "affect_target_leak": (
        "助手 Affect 必须绑定当前话题、对象、作用域和过期时间。",
        "短期 Affect 越过了它的对象、话题或作用域。",
    ),
    "affective_manipulation": (
        "情绪不得用于冷落、依赖、惩罚、占有或逼迫成员。",
        "最终表达出现了情绪操控或惩罚性行为。",
    ),
}
_CASE_REVIEW_PATH = "通过既有受权限和保留期约束的回执/会话域核对来源；成长域不复制正文。"
_DIRECTED_ATTENTION = frozenset({"explicit_mention", "reply_to_assistant"})
# This is deliberately narrow.  The worker emits it only after it selected an
# ambient contribution and the final handoff failed to queue Delivery.  Policy
# silences (cost window, safety, stale topic, media not ready) and model
# declines are not evidence of a missed contribution.
_AMBIENT_NON_CONTENT_FAILURE_REASONS = frozenset({"group_delivery_not_queued"})
_AFFECT_CONTRACT_ERROR_TO_PROBLEM = {
    "assistant_affect_trigger_missing": "affect_without_trigger",
    "assistant_affect_reason_invalid": "affect_without_trigger",
    "assistant_affect_scope_invalid": "affect_target_leak",
    "assistant_affect_target_invalid": "affect_target_leak",
    "assistant_affect_topic_invalid": "affect_target_leak",
    "assistant_affect_time_invalid": "affect_target_leak",
    "assistant_affect_prohibited": "affective_manipulation",
}


def _utc(value: object | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("behavior_observation_time_invalid")
    return parsed.astimezone(timezone.utc)


def _digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _scope_from_conversation(value: object) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if raw.startswith("group:") and len(raw) > len("group:"):
        return "group", "scope:sha256-" + _digest(raw)
    if raw.startswith("private:") and len(raw) > len("private:"):
        return "private", "scope:sha256-" + _digest(raw)
    return None


def _safe_ref(value: object) -> str:
    ref = str(value or "").strip()
    prefix = ref.split(":", 1)[0] if ":" in ref else ""
    if prefix not in _SAFE_EVIDENCE_PREFIXES or not _OPAQUE_EVIDENCE_REF_RE.fullmatch(ref):
        raise ValueError("behavior_observation_evidence_ref_unsafe")
    return ref


def _source_ref(source_kind: object, source_ref: object) -> tuple[str, str]:
    kind = str(source_kind or "").strip()
    expected_prefix = _SOURCE_PREFIXES.get(kind)
    ref = str(source_ref or "").strip()
    if not expected_prefix or not ref.startswith(expected_prefix + ":"):
        raise ValueError("behavior_observation_source_ref_unsafe")
    _safe_ref(ref)
    return kind, ref


def _assistant_for_decision(conn: sqlite3.Connection, decision_id: object) -> str:
    """Resolve the originating Assistant Instance; never borrow the active one.

    Quality receipts intentionally do not duplicate identity.  The existing
    engagement decision is the authoritative relationship between receipt and
    Assistant Instance.  If that link is absent, observation fails closed so a
    profile switch cannot misattribute behavior to a different assistant.
    """

    identifier = str(decision_id or "").strip()
    if not identifier:
        return ""
    try:
        row = conn.execute(
            "SELECT assistant_id FROM engagement_decisions WHERE id=?",
            (identifier,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row is not None else ""


def _cluster_id(observation: Mapping[str, object]) -> str:
    key = "|".join((
        str(observation["assistant_id"]),
        str(observation["problem_code"]),
        str(observation["stage"]),
        str(observation["policy_version"]),
    ))
    return "behavior-cluster:" + _digest(key)[:32]


def _required_assistant_id(value: object) -> str:
    assistant_id = str(value or "").strip()
    if not assistant_id:
        raise ValueError("behavior_observation_assistant_id_required")
    return assistant_id


def _stored_observation(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        evidence_refs = json.loads(str(item.pop("evidence_refs_json")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("behavior_observation_stored_json_invalid") from exc
    return {**item, "evidence_refs": evidence_refs}


def record_behavior_observation(
    conn: sqlite3.Connection,
    value: Mapping[str, object],
    *,
    source_kind: object,
    source_ref: object,
) -> dict | None:
    """Persist one valid observation and update its body-free cross-scope cluster."""

    if not behavior_observation_enabled(conn):
        return None
    require_behavior_observation_assistant_isolation_schema(conn)
    normalized = make_behavior_observation(value)
    evidence_refs = [_safe_ref(item) for item in normalized["evidence_refs"]]
    kind, source = _source_ref(source_kind, source_ref)
    cluster_id = _cluster_id(normalized)
    existing = conn.execute(
        f"SELECT * FROM {BEHAVIOR_OBSERVATION_TABLE} WHERE source_kind=? AND source_ref=? AND problem_code=?",
        (kind, source, normalized["problem_code"]),
    ).fetchone()
    if existing is not None:
        return _stored_observation(existing)
    conn.execute(
        f"""
        INSERT INTO {BEHAVIOR_OBSERVATION_TABLE}(
            id,assistant_id,scope_type,scope_ref,stage,problem_code,evidence_refs_json,trace_ref,policy_version,
            source_kind,source_ref,cluster_id,created_at,expires_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            normalized["observation_id"], normalized["assistant_id"], normalized["scope_type"], normalized["scope_ref"],
            normalized["stage"], normalized["problem_code"], json.dumps(evidence_refs, ensure_ascii=True, separators=(",", ":")),
            normalized["trace_ref"], normalized["policy_version"], kind, source, cluster_id,
            normalized["created_at"], normalized["expires_at"],
        ),
    )
    conn.execute(
        f"""
        INSERT INTO {BEHAVIOR_OBSERVATION_CLUSTER_TABLE}(
            id,assistant_id,problem_code,stage,policy_version,observation_count,first_observed_at,last_observed_at
        ) VALUES(?,?,?,?,?,1,?,?)
        ON CONFLICT(assistant_id,problem_code,stage,policy_version) DO UPDATE SET
            observation_count={BEHAVIOR_OBSERVATION_CLUSTER_TABLE}.observation_count+1,
            first_observed_at=MIN({BEHAVIOR_OBSERVATION_CLUSTER_TABLE}.first_observed_at,excluded.first_observed_at),
            last_observed_at=MAX({BEHAVIOR_OBSERVATION_CLUSTER_TABLE}.last_observed_at,excluded.last_observed_at)
        """,
        (cluster_id, normalized["assistant_id"], normalized["problem_code"], normalized["stage"], normalized["policy_version"], normalized["created_at"], normalized["created_at"]),
    )
    row = conn.execute(f"SELECT * FROM {BEHAVIOR_OBSERVATION_TABLE} WHERE id=?", (normalized["observation_id"],)).fetchone()
    return _stored_observation(row)


def _quality_problem_code(receipt: Mapping[str, object]) -> str | None:
    candidates = [str(receipt.get("rewrite_or_block_code") or "").strip(), str(receipt.get("reason_code") or "").strip()]
    for candidate in candidates:
        for code in (item.strip() for item in candidate.split(",") if item.strip()):
            if code in BE_PROBLEM_CODES:
                return code
            mapped = _QUALITY_REASON_TO_PROBLEM.get(code)
            if mapped is not None:
                return mapped
    return None


def observe_quality_receipt(conn: sqlite3.Connection, receipt: Mapping[str, object]) -> dict | None:
    """Observe an unambiguous Quality Receipt after it is already durable."""

    if not behavior_observation_enabled(conn) or not isinstance(receipt, Mapping):
        return None
    problem_code = _quality_problem_code(receipt)
    scope = _scope_from_conversation(receipt.get("conversation_ref"))
    receipt_id = str(receipt.get("id") or "").strip()
    decision_id = str(receipt.get("decision_id") or "").strip()
    assistant_id = _assistant_for_decision(conn, decision_id)
    if not problem_code or scope is None or not assistant_id or not receipt_id or not decision_id:
        return None
    try:
        created_at = _utc(receipt.get("created_at"))
    except (TypeError, ValueError):
        return None
    source_ref = "quality-receipt:sha256-" + _digest(receipt_id)
    source_digest = _digest(source_ref + "|" + problem_code)
    return record_behavior_observation(
        conn,
        {
            "schema_version": 1,
            "observation_id": "behavior-observation:" + source_digest[:32],
            "assistant_id": assistant_id,
            "scope_type": scope[0],
            "scope_ref": scope[1],
            "stage": "delivery_quality",
            "problem_code": problem_code,
            "evidence_refs": [source_ref, "decision:sha256-" + _digest(decision_id)],
            "trace_ref": "trace:sha256-" + _digest(decision_id),
            "policy_version": "policy:quality-receipt-v1",
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(days=14)).isoformat(),
        },
        source_kind="quality_receipt",
        source_ref=source_ref,
    )


def observe_reply_obligation_receipt(
    conn: sqlite3.Connection,
    receipt: Mapping[str, object],
    *,
    conversation_frame: Mapping[str, object] | None,
) -> dict | None:
    """Observe an actually missed *directed* reply, never an ordinary silence.

    The normal Truth/Persona/Delivery path remains authoritative.  This small
    observer consumes only its already-durable body-free receipt plus the
    category-only attention field from the current frame.  A direct message
    that was answered, and any optional ambient contribution, is intentionally
    invisible to this problem class.
    """

    if not behavior_observation_enabled(conn) or not isinstance(receipt, Mapping):
        return None
    frame = conversation_frame if isinstance(conversation_frame, Mapping) else {}
    if str(frame.get("attention") or "") not in _DIRECTED_ATTENTION:
        return None
    if str(receipt.get("outcome") or "") not in {"silent", "blocked", "error"}:
        return None
    scope = _scope_from_conversation(receipt.get("conversation_ref"))
    receipt_id = str(receipt.get("id") or "").strip()
    decision_id = str(receipt.get("decision_id") or "").strip()
    assistant_id = _assistant_for_decision(conn, decision_id)
    if scope is None or not assistant_id or not receipt_id or not decision_id:
        return None
    try:
        created_at = _utc(receipt.get("created_at"))
    except (TypeError, ValueError):
        return None
    source_ref = "quality-receipt:sha256-" + _digest(receipt_id)
    source_digest = _digest(source_ref + "|reply_obligation_missed")
    return record_behavior_observation(
        conn,
        {
            "schema_version": 1,
            "observation_id": "behavior-observation:" + source_digest[:32],
            "assistant_id": assistant_id,
            "scope_type": scope[0],
            "scope_ref": scope[1],
            "stage": "outcome",
            "problem_code": "reply_obligation_missed",
            "evidence_refs": [source_ref, "decision:sha256-" + _digest(decision_id)],
            "trace_ref": "trace:sha256-" + _digest(decision_id),
            "policy_version": "policy:reply-obligation-v1",
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(days=14)).isoformat(),
        },
        source_kind="quality_receipt",
        source_ref=source_ref,
    )


def observe_ambient_contribution_failure(
    conn: sqlite3.Connection,
    *,
    decision_id: object,
    stage: object,
) -> dict | None:
    """Observe a selected ambient turn lost to the final non-content handoff.

    It is not a silence counter: selection and the precise terminal reason are
    both required.  This keeps pricing windows, no-value decisions, stale
    topics, safety gates and unread media from training the optimizer as bugs.
    """

    if not behavior_observation_enabled(conn) or str(stage or "") != "delivery_failed":
        return None
    identifier = str(decision_id or "").strip()
    if not identifier:
        return None
    row = conn.execute(
        """SELECT assistant_id,thread_id,candidate_kind,reason_code,policy_version,created_at
           FROM engagement_decisions WHERE id=?""",
        (identifier,),
    ).fetchone()
    if row is None:
        return None
    candidate_kind = str(row[2] or "")
    reason_code = str(row[3] or "")
    thread_id = str(row[1] or "")
    if (
        candidate_kind != "ambient"
        or reason_code not in _AMBIENT_NON_CONTENT_FAILURE_REASONS
        or not thread_id.startswith("qq:group:")
    ):
        return None
    scope = _scope_from_conversation("group:" + thread_id[len("qq:group:"):])
    if scope is None or not str(row[0] or "").strip():
        return None
    try:
        created_at = _utc(row[5])
    except (TypeError, ValueError):
        return None
    source_ref = "outcome:sha256-" + _digest(identifier + "|" + reason_code)
    source_digest = _digest(source_ref + "|ambient_contribution_missed")
    return record_behavior_observation(
        conn,
        {
            "schema_version": 1,
            "observation_id": "behavior-observation:" + source_digest[:32],
            "assistant_id": str(row[0]),
            "scope_type": scope[0],
            "scope_ref": scope[1],
            "stage": "outcome",
            "problem_code": "ambient_contribution_missed",
            "evidence_refs": [source_ref, "decision:sha256-" + _digest(identifier)],
            "trace_ref": "trace:sha256-" + _digest(identifier),
            "policy_version": str(row[4] or "policy:natural-participation-v1"),
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(days=14)).isoformat(),
        },
        source_kind="outcome",
        source_ref=source_ref,
    )


def observe_assistant_affect_boundary_failure(
    conn: sqlite3.Connection,
    *,
    assistant_id: object,
    group_id: object,
    source_message_id: object,
    topic_revision: object,
    contract_error: object,
    now: object | None = None,
) -> dict | None:
    """Record a known Affect contract violation without retaining the trigger.

    The caller is allowed to pass only a validation code and identifiers that
    are immediately one-way hashed.  Unexpected observer/database failures
    deliberately do not become behaviour evidence: they need diagnostics,
    not an optimizer training signal.
    """

    if not behavior_observation_enabled(conn):
        return None
    problem_code = _AFFECT_CONTRACT_ERROR_TO_PROBLEM.get(str(contract_error or "").strip())
    owner = str(assistant_id or "").strip()
    group = str(group_id or "").strip()
    source_message = str(source_message_id or "").strip()
    try:
        revision = int(topic_revision)
    except (TypeError, ValueError):
        return None
    if not problem_code or not owner or not group or not source_message or revision < 0:
        return None
    try:
        created_at = _utc(now)
    except (TypeError, ValueError):
        return None
    source_ref = "affect-event:sha256-" + _digest(source_message)
    source_digest = _digest(source_ref + "|" + problem_code + "|" + str(revision))
    return record_behavior_observation(
        conn,
        {
            "schema_version": 1,
            "observation_id": "behavior-observation:" + source_digest[:32],
            "assistant_id": owner,
            "scope_type": "group",
            "scope_ref": "scope:sha256-" + _digest(group),
            "stage": "participation",
            "problem_code": problem_code,
            "evidence_refs": [source_ref],
            "trace_ref": "trace:sha256-" + _digest(source_ref + "|" + str(revision)),
            "policy_version": "policy:assistant-affect-shadow-v1",
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(days=14)).isoformat(),
        },
        source_kind="affect",
        source_ref=source_ref,
    )


def list_behavior_observations(conn: sqlite3.Connection, *, assistant_id: object, limit: int = 100) -> list[dict]:
    require_behavior_observation_assistant_isolation_schema(conn)
    owner = _required_assistant_id(assistant_id)
    safe_limit = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"SELECT * FROM {BEHAVIOR_OBSERVATION_TABLE} WHERE assistant_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
        (owner, safe_limit),
    ).fetchall()
    return [_stored_observation(row) for row in rows if row is not None]


def list_behavior_clusters(conn: sqlite3.Connection, *, assistant_id: object, limit: int = 100) -> list[dict]:
    require_behavior_observation_assistant_isolation_schema(conn)
    owner = _required_assistant_id(assistant_id)
    safe_limit = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"SELECT * FROM {BEHAVIOR_OBSERVATION_CLUSTER_TABLE} WHERE assistant_id=? ORDER BY last_observed_at DESC,id DESC LIMIT ?",
        (owner, safe_limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_behavior_cases(conn: sqlite3.Connection, *, assistant_id: object, limit: int = 100) -> list[dict]:
    """Return Owner-readable, body-free problem cases for the Growth surface.

    This intentionally provides the deterministic rule and the opaque source
    path instead of replaying a chat body into the Behavior Evolution domain.
    The normal channel/receipt domain remains the only place an authorised
    Owner may inspect retained conversation detail.
    """

    cases = []
    for observation in list_behavior_observations(conn, assistant_id=assistant_id, limit=limit):
        expected, actual = _OBSERVATION_CASE_RULES[observation["problem_code"]]
        cases.append(
            {
                "case_ref": observation["id"],
                "problem_code": observation["problem_code"],
                "stage": observation["stage"],
                "rule": {
                    "expected": expected,
                    "actual": actual,
                    "review_path": _CASE_REVIEW_PATH,
                },
                "source": {
                    "kind": observation["source_kind"],
                    "ref": observation["source_ref"],
                },
                "trace_ref": observation["trace_ref"],
                "evidence_refs": observation["evidence_refs"],
                "body_free": True,
            },
        )
    return cases


def purge_expired_behavior_observations(conn: sqlite3.Connection, *, now: object | None = None) -> dict:
    """Hard-delete expired records and make cluster counts reflect retained evidence only."""

    require_behavior_observation_assistant_isolation_schema(conn)
    current = _utc(now).isoformat()
    affected = [
        str(item[0])
        for item in conn.execute(
            f"SELECT DISTINCT cluster_id FROM {BEHAVIOR_OBSERVATION_TABLE} WHERE expires_at<=?",
            (current,),
        ).fetchall()
    ]
    deleted_observations = conn.execute(
        f"DELETE FROM {BEHAVIOR_OBSERVATION_TABLE} WHERE expires_at<=?",
        (current,),
    ).rowcount
    deleted_clusters = 0
    for cluster_id in affected:
        row = conn.execute(
            f"SELECT COUNT(*),MIN(created_at),MAX(created_at) FROM {BEHAVIOR_OBSERVATION_TABLE} WHERE cluster_id=?",
            (cluster_id,),
        ).fetchone()
        count = int(row[0]) if row is not None else 0
        if count == 0:
            deleted_clusters += conn.execute(
                f"DELETE FROM {BEHAVIOR_OBSERVATION_CLUSTER_TABLE} WHERE id=?",
                (cluster_id,),
            ).rowcount
        else:
            conn.execute(
                f"UPDATE {BEHAVIOR_OBSERVATION_CLUSTER_TABLE} SET observation_count=?,first_observed_at=?,last_observed_at=? WHERE id=?",
                (count, str(row[1]), str(row[2]), cluster_id),
            )
    return {"deleted_observations": int(deleted_observations), "deleted_clusters": int(deleted_clusters)}


__all__ = [
    "list_behavior_cases",
    "list_behavior_clusters",
    "list_behavior_observations",
    "observe_ambient_contribution_failure",
    "observe_assistant_affect_boundary_failure",
    "observe_quality_receipt",
    "observe_reply_obligation_receipt",
    "purge_expired_behavior_observations",
    "record_behavior_observation",
    "set_behavior_observation_feature",
]
