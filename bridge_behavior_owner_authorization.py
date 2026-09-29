#!/usr/bin/env python3
"""Body-free, Assistant-scoped Owner Authorization Registry operations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import uuid

from bridge_behavior_owner_authorization_schema import (
    BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE,
    BEHAVIOR_OWNER_AUTHORIZATION_TABLE,
    apply_behavior_owner_authorization_v1,
    require_behavior_owner_authorization_schema,
)


_PURPOSE_FIELDS = {
    "reference_baseline_freeze": frozenset(
        {
            "assistant_id", "benchmark_ref", "benchmark_hash", "reference_policy_ref",
            "evaluator_ref", "run_ref", "signing_key_ref", "public_key_fingerprint",
        },
    ),
    "private_candidate_evaluation": frozenset(
        {
            "assistant_id", "benchmark_ref", "benchmark_hash", "reference_policy_ref",
            "evaluator_ref", "run_ref", "signing_key_ref", "public_key_fingerprint",
            "candidate_ref", "policy_bundle_hash",
        },
    ),
    "paired_shadow_enable": frozenset(
        {
            "assistant_id", "candidate_ref", "policy_bundle_hash", "reference_policy_ref",
            "benchmark_ref", "benchmark_hash", "scope_refs", "cutover_plan_checksum",
        },
    ),
}
_HASH_FIELDS = frozenset(
    {"benchmark_hash", "policy_bundle_hash", "public_key_fingerprint", "cutover_plan_checksum"},
)
_REF_RE = re.compile(
    r"^(?:assistant-[0-9a-f]{32}|[a-z][a-z0-9_-]*:(?:[0-9a-f]{32}|sha256-[0-9a-f]{64}))$",
)
_FROZEN_BENCHMARK_REF_RE = re.compile(r"^benchmark:[0-9a-f]{24}$")
_ASSISTANT_ID_RE = re.compile(r"^(?:assistant-[0-9a-f]{32}|assistant:[A-Za-z0-9][A-Za-z0-9_-]*)$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCOPE_RE = re.compile(r"^scope:sha256-[0-9a-f]{64}$")
_BODY_KEY_RE = re.compile(
    r"(?:^|[_-])(body|content|gold|message|prompt|raw|rubric|scenario|text|output|private)(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_REF_TERM_RE = re.compile(
    r"(?:^|[^a-z0-9])(body|content|gold|message|prompt|raw|rubric|scenario|text|output|private)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class BehaviorOwnerAuthorizationError(ValueError):
    """An authorization was malformed, stale, out of scope, or inactive."""


def _utc(value: object, *, error: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BehaviorOwnerAuthorizationError(error) from exc
    if parsed.tzinfo is None:
        raise BehaviorOwnerAuthorizationError(error)
    return parsed.astimezone(timezone.utc).isoformat()


def _ref(value: object, *, error: str) -> str:
    result = str(value or "").strip()
    if _SENSITIVE_REF_TERM_RE.search(result):
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_body_free_violation")
    if not _REF_RE.fullmatch(result):
        raise BehaviorOwnerAuthorizationError(error)
    return result


def _benchmark_ref(value: object, *, error: str) -> str:
    """Accept the evaluator's fixed-size frozen Benchmark descriptor only."""

    result = str(value or "").strip()
    if _SENSITIVE_REF_TERM_RE.search(result):
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_body_free_violation")
    if not (_REF_RE.fullmatch(result) or _FROZEN_BENCHMARK_REF_RE.fullmatch(result)):
        raise BehaviorOwnerAuthorizationError(error)
    return result


def _assistant_id(conn: sqlite3.Connection, value: object, *, error: str) -> str:
    """Accept established Assistant forms and use the authoritative registry when present."""

    result = str(value or "").strip()
    if _SENSITIVE_REF_TERM_RE.search(result):
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_body_free_violation")
    if not _ASSISTANT_ID_RE.fullmatch(result):
        raise BehaviorOwnerAuthorizationError(error)
    registry_present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_instances'",
    ).fetchone()
    if registry_present and conn.execute(
        "SELECT 1 FROM assistant_instances WHERE id=?",
        (result,),
    ).fetchone() is None:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_assistant_not_found")
    return result


def _purpose(value: object) -> str:
    result = str(value or "").strip()
    if result not in _PURPOSE_FIELDS:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_purpose_invalid")
    return result


def _reject_body_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            name = str(key or "").strip()
            if not name or _BODY_KEY_RE.search(name):
                raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_body_free_violation")
            _reject_body_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_body_keys(nested)


def _canonical_binding(conn: sqlite3.Connection, purpose: object, binding: object) -> tuple[str, dict, str, str]:
    selected_purpose = _purpose(purpose)
    if not isinstance(binding, Mapping):
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_binding_invalid")
    _reject_body_keys(binding)
    if set(binding) != _PURPOSE_FIELDS[selected_purpose]:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_binding_invalid")
    normalized: dict[str, object] = {}
    for field in sorted(_PURPOSE_FIELDS[selected_purpose]):
        value = binding[field]
        if field == "assistant_id":
            normalized[field] = _assistant_id(
                conn,
                value,
                error="behavior_owner_authorization_binding_invalid",
            )
        elif field == "benchmark_ref":
            normalized[field] = _benchmark_ref(
                value,
                error="behavior_owner_authorization_binding_invalid",
            )
        elif field == "scope_refs":
            if not isinstance(value, list) or not value:
                raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_binding_invalid")
            scopes = [_ref(item, error="behavior_owner_authorization_binding_invalid") for item in value]
            if any(not _SCOPE_RE.fullmatch(item) for item in scopes) or scopes != sorted(set(scopes)):
                raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_binding_invalid")
            normalized[field] = scopes
        elif field in _HASH_FIELDS:
            digest = str(value or "").strip()
            if not _HASH_RE.fullmatch(digest):
                raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_binding_invalid")
            normalized[field] = digest
        else:
            normalized[field] = _ref(value, error="behavior_owner_authorization_binding_invalid")
    assistant_id = str(normalized["assistant_id"])
    canonical = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    binding_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return selected_purpose, normalized, canonical, binding_hash


def _record(cursor: sqlite3.Cursor) -> dict | None:
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in cursor.description]
    value = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row))
    binding = json.loads(str(value.pop("binding_json")))
    return {**value, "binding": binding}


def _authorization(conn: sqlite3.Connection, authorization_ref: str, assistant_id: str) -> dict | None:
    return _record(
        conn.execute(
            f"SELECT * FROM {BEHAVIOR_OWNER_AUTHORIZATION_TABLE} WHERE authorization_ref=? AND assistant_id=?",
            (authorization_ref, assistant_id),
        ),
    )


def _append_audit(
    conn: sqlite3.Connection,
    *,
    authorization_ref: str,
    assistant_id: str,
    event: str,
    occurred_at: str,
    actor_ref: str | None,
) -> None:
    sequence = int(
        conn.execute(
            f"SELECT COALESCE(MAX(sequence),0)+1 FROM {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE} WHERE authorization_ref=?",
            (authorization_ref,),
        ).fetchone()[0],
    )
    conn.execute(
        f"""INSERT INTO {BEHAVIOR_OWNER_AUTHORIZATION_AUDIT_TABLE}(
                authorization_ref,assistant_id,sequence,event,occurred_at,actor_ref
            ) VALUES(?,?,?,?,?,?)""",
        (authorization_ref, assistant_id, sequence, event, occurred_at, actor_ref),
    )


def behavior_owner_authorization_plan(
    conn: sqlite3.Connection,
    *,
    purpose: object,
    binding: object,
    now: object,
) -> dict:
    """Validate an exact body-free authorization without writing state."""

    require_behavior_owner_authorization_schema(conn)
    planned_at = _utc(now, error="behavior_owner_authorization_now_invalid")
    selected_purpose, normalized, _canonical, binding_hash = _canonical_binding(conn, purpose, binding)
    checksum_payload = json.dumps(
        {
            "assistant_id": normalized["assistant_id"],
            "binding_hash": binding_hash,
            "purpose": selected_purpose,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "purpose": selected_purpose,
        "assistant_id": normalized["assistant_id"],
        "binding": normalized,
        "binding_hash": binding_hash,
        "planned_at": planned_at,
        "plan_checksum": "sha256:" + hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest(),
    }


def create_behavior_owner_authorization(
    conn: sqlite3.Connection,
    *,
    purpose: object,
    binding: object,
    plan_checksum: object,
    issuer_ref: object,
    expires_at: object,
    now: object,
) -> dict:
    """Persist a named authorization after its exact plan checksum is confirmed."""

    plan = behavior_owner_authorization_plan(conn, purpose=purpose, binding=binding, now=now)
    if str(plan_checksum or "").strip() != plan["plan_checksum"]:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_plan_checksum_stale")
    issuer = _ref(issuer_ref, error="behavior_owner_authorization_issuer_invalid")
    issued_at = _utc(now, error="behavior_owner_authorization_now_invalid")
    expiry = _utc(expires_at, error="behavior_owner_authorization_expiry_invalid")
    if expiry <= issued_at:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_expiry_invalid")
    authorization_ref = "owner-authorization:" + uuid.uuid4().hex
    canonical = json.dumps(plan["binding"], ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    conn.execute(
        f"""INSERT INTO {BEHAVIOR_OWNER_AUTHORIZATION_TABLE}(
                authorization_ref,assistant_id,purpose,binding_json,binding_hash,state,
                issuer_ref,issued_at,expires_at,revoked_at,consumed_at
            ) VALUES(?,?,?,?,?,?,?, ?,?,?,?)""",
        (
            authorization_ref, plan["assistant_id"], plan["purpose"], canonical, plan["binding_hash"], "active",
            issuer, issued_at, expiry, None, None,
        ),
    )
    _append_audit(
        conn,
        authorization_ref=authorization_ref,
        assistant_id=str(plan["assistant_id"]),
        event="issued",
        occurred_at=issued_at,
        actor_ref=issuer,
    )
    return _authorization(conn, authorization_ref, str(plan["assistant_id"])) or {}


def verify_behavior_owner_authorization(
    conn: sqlite3.Connection,
    *,
    authorization_ref: object,
    purpose: object,
    assistant_id: object,
    binding: object,
    now: object,
    consume: bool,
) -> dict:
    """Check an exact Owner authorization and consume only one-shot BE-4/BE-5 actions."""

    require_behavior_owner_authorization_schema(conn)
    ref = _ref(authorization_ref, error="behavior_owner_authorization_not_found")
    selected_purpose, normalized, _canonical, binding_hash = _canonical_binding(conn, purpose, binding)
    requested_assistant = _assistant_id(
        conn,
        assistant_id,
        error="behavior_owner_authorization_assistant_invalid",
    )
    timestamp = _utc(now, error="behavior_owner_authorization_now_invalid")
    record = _authorization(conn, ref, requested_assistant)
    if record is None:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_not_found")
    if record["purpose"] != selected_purpose:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_purpose_mismatch")
    if record["binding_hash"] != binding_hash or record["binding"] != normalized:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_binding_mismatch")
    if record["state"] == "active" and timestamp >= record["expires_at"]:
        conn.execute(
            f"UPDATE {BEHAVIOR_OWNER_AUTHORIZATION_TABLE} SET state='expired' WHERE authorization_ref=? AND state='active'",
            (ref,),
        )
        _append_audit(
            conn,
            authorization_ref=ref,
            assistant_id=requested_assistant,
            event="expired",
            occurred_at=timestamp,
            actor_ref=None,
        )
        record = _authorization(conn, ref, requested_assistant) or record
    if record["state"] != "active":
        raise BehaviorOwnerAuthorizationError(f"behavior_owner_authorization_not_active:{record['state']}")
    if selected_purpose in {"reference_baseline_freeze", "private_candidate_evaluation"}:
        if consume:
            updated = conn.execute(
                f"""UPDATE {BEHAVIOR_OWNER_AUTHORIZATION_TABLE}
                    SET state='consumed',consumed_at=? WHERE authorization_ref=? AND state='active'""",
                (timestamp, ref),
            )
            if updated.rowcount != 1:
                raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_concurrent_transition")
            _append_audit(
                conn,
                authorization_ref=ref,
                assistant_id=requested_assistant,
                event="consumed",
                occurred_at=timestamp,
                actor_ref=None,
            )
    elif consume:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_not_consumable")
    return _authorization(conn, ref, requested_assistant) or {}


def preflight_behavior_owner_authorization(
    conn: sqlite3.Connection,
    *,
    authorization_ref: object,
    purpose: object,
    assistant_id: object,
    binding: object,
    now: object,
) -> dict:
    """Read-only exact check before an expensive evaluator invocation.

    The actual receiver still consumes one-shot authorizations in the same
    transaction as the durable receipt.  This helper has no audit write and
    therefore cannot turn a preflight into an authorization bypass.
    """

    require_behavior_owner_authorization_schema(conn)
    ref = _ref(authorization_ref, error="behavior_owner_authorization_not_found")
    selected_purpose, normalized, _canonical, binding_hash = _canonical_binding(conn, purpose, binding)
    requested_assistant = _assistant_id(
        conn,
        assistant_id,
        error="behavior_owner_authorization_assistant_invalid",
    )
    timestamp = _utc(now, error="behavior_owner_authorization_now_invalid")
    record = _authorization(conn, ref, requested_assistant)
    if record is None:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_not_found")
    if record["purpose"] != selected_purpose:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_purpose_mismatch")
    if record["binding_hash"] != binding_hash or record["binding"] != normalized:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_binding_mismatch")
    if record["state"] != "active":
        raise BehaviorOwnerAuthorizationError(f"behavior_owner_authorization_not_active:{record['state']}")
    if timestamp >= record["expires_at"]:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_not_active:expired")
    return record


def revoke_behavior_owner_authorization(
    conn: sqlite3.Connection,
    *,
    authorization_ref: object,
    assistant_id: object,
    issuer_ref: object,
    now: object,
) -> dict:
    """Revoke an active authorization only for its issuing Owner reference."""

    require_behavior_owner_authorization_schema(conn)
    ref = _ref(authorization_ref, error="behavior_owner_authorization_not_found")
    requested_assistant = _assistant_id(
        conn,
        assistant_id,
        error="behavior_owner_authorization_assistant_invalid",
    )
    issuer = _ref(issuer_ref, error="behavior_owner_authorization_issuer_invalid")
    timestamp = _utc(now, error="behavior_owner_authorization_now_invalid")
    record = _authorization(conn, ref, requested_assistant)
    if record is None:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_not_found")
    if record["issuer_ref"] != issuer:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_issuer_mismatch")
    if record["state"] == "active" and timestamp >= record["expires_at"]:
        conn.execute(
            f"UPDATE {BEHAVIOR_OWNER_AUTHORIZATION_TABLE} SET state='expired' WHERE authorization_ref=? AND state='active'",
            (ref,),
        )
        _append_audit(
            conn,
            authorization_ref=ref,
            assistant_id=requested_assistant,
            event="expired",
            occurred_at=timestamp,
            actor_ref=None,
        )
        record = _authorization(conn, ref, requested_assistant) or record
    if record["state"] != "active":
        raise BehaviorOwnerAuthorizationError(f"behavior_owner_authorization_not_active:{record['state']}")
    updated = conn.execute(
        f"""UPDATE {BEHAVIOR_OWNER_AUTHORIZATION_TABLE}
            SET state='revoked',revoked_at=? WHERE authorization_ref=? AND state='active'""",
        (timestamp, ref),
    )
    if updated.rowcount != 1:
        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_concurrent_transition")
    _append_audit(
        conn,
        authorization_ref=ref,
        assistant_id=requested_assistant,
        event="revoked",
        occurred_at=timestamp,
        actor_ref=issuer,
    )
    return _authorization(conn, ref, requested_assistant) or {}


__all__ = [
    "BehaviorOwnerAuthorizationError",
    "apply_behavior_owner_authorization_v1",
    "behavior_owner_authorization_plan",
    "create_behavior_owner_authorization",
    "preflight_behavior_owner_authorization",
    "require_behavior_owner_authorization_schema",
    "revoke_behavior_owner_authorization",
    "verify_behavior_owner_authorization",
]
