#!/usr/bin/env python3
"""DSH-0: a zero-side-effect contract for a future DeepSeek Harness adapter.

This module deliberately does *not* start a Harness process, install its
dependencies, read credentials, or select it as an executor.  It only freezes
the Neko-owned envelope and proposal boundary that later gates must preserve.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import re


DSH_UPSTREAM_REPOSITORY = "https://github.com/deepseek-ai/deepseek-harness"
DSH_PINNED_REVISION = "47f943859bef60e4160492346772ded9b24f765a"
DSH_UPSTREAM_LICENSE = "MIT"
DSH0_STATUS = "contract_ready"

_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,119}$")
_FORBIDDEN_FIELD_EXACT = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "delivery",
        "filesystem",
        "knowledge",
        "memory",
        "network",
        "password",
        "private_key",
        "secret",
        "task",
        "approval",
        "url",
        "token",
        "message",
        "response_text",
        "raw_text",
        "prompt",
        "content",
        "body",
        "text",
    },
)


def _forbidden_field_name(value: object) -> bool:
    name = str(value or "").strip().lower()
    if name in _FORBIDDEN_FIELD_EXACT:
        return True
    return name.startswith(("delivery_", "filesystem_", "knowledge_", "memory_", "network_", "approval_", "task_")) or name.endswith(
        ("_api_key", "_authorization", "_body", "_content", "_credential", "_password", "_private_key", "_secret", "_text", "_token", "_url"),
    )


class DshContractError(ValueError):
    """Raised before an untrusted DSH proposal can cross a Neko boundary."""


def _parse_datetime(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DshContractError("dsh0_deadline_invalid") from exc
    if parsed.tzinfo is None:
        raise DshContractError("dsh0_deadline_invalid")
    return parsed.astimezone(timezone.utc)


def _opaque_ref(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise DshContractError(f"dsh0_{field}_invalid")
    return text


def _assert_no_forbidden_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key or "").strip().lower()
            if _forbidden_field_name(normalized):
                raise DshContractError("dsh0_forbidden_field")
            _assert_no_forbidden_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_forbidden_fields(item)


def _exact_keys(value: Mapping[str, object], allowed: frozenset[str]) -> None:
    if set(value) - allowed:
        raise DshContractError("dsh0_forbidden_field")


def validate_pinned_upstream(value: Mapping[str, object]) -> dict:
    """Accept only the reviewed, immutable upstream revision for DSH-0."""

    if not isinstance(value, Mapping):
        raise DshContractError("dsh_upstream_invalid")
    repository = str(value.get("repository") or "").strip()
    revision = str(value.get("revision") or "").strip()
    if repository != DSH_UPSTREAM_REPOSITORY:
        raise DshContractError("dsh_upstream_not_approved")
    if revision != DSH_PINNED_REVISION:
        raise DshContractError("dsh_upstream_revision_unpinned")
    return {"repository": DSH_UPSTREAM_REPOSITORY, "revision": DSH_PINNED_REVISION, "license": DSH_UPSTREAM_LICENSE}


def create_dsh_run_request(value: Mapping[str, object]) -> dict:
    """Validate a body-free, Neko-owned request before an eventual IPC hop."""

    if not isinstance(value, Mapping):
        raise DshContractError("dsh0_request_invalid")
    _assert_no_forbidden_fields(value)
    _exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "run_ref",
                "assistant_ref",
                "workspace_ref",
                "deadline_at",
                "idempotency_key",
                "model_gateway_ref",
                "context",
                "allowed_capability_ids",
            },
        ),
    )
    if value.get("schema_version") != 1:
        raise DshContractError("dsh0_schema_version_invalid")
    context = value.get("context")
    if not isinstance(context, Mapping):
        raise DshContractError("dsh0_context_invalid")
    _exact_keys(context, frozenset({"conversation_ref", "topic_ref", "topic_revision", "continuity_refs"}))
    if not isinstance(context.get("topic_revision"), int) or int(context["topic_revision"]) < 0:
        raise DshContractError("dsh0_topic_revision_invalid")
    continuity_refs = context.get("continuity_refs")
    if not isinstance(continuity_refs, list):
        raise DshContractError("dsh0_context_invalid")
    if len(continuity_refs) > 32:
        raise DshContractError("dsh0_context_invalid")
    capabilities = value.get("allowed_capability_ids")
    if not isinstance(capabilities, list) or len(capabilities) > 32:
        raise DshContractError("dsh0_capabilities_invalid")
    normalized_capabilities = sorted({str(item or "").strip() for item in capabilities})
    if not normalized_capabilities or any(not _CAPABILITY_RE.fullmatch(item) for item in normalized_capabilities):
        raise DshContractError("dsh0_capabilities_invalid")
    idempotency_key = str(value.get("idempotency_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{4,160}", idempotency_key):
        raise DshContractError("dsh0_idempotency_key_invalid")
    return {
        "schema_version": 1,
        "run_ref": _opaque_ref(value.get("run_ref"), field="run_ref"),
        "assistant_ref": _opaque_ref(value.get("assistant_ref"), field="assistant_ref"),
        "workspace_ref": _opaque_ref(value.get("workspace_ref"), field="workspace_ref"),
        "deadline_at": _parse_datetime(value.get("deadline_at")).isoformat(),
        "idempotency_key": idempotency_key,
        "model_gateway_ref": _opaque_ref(value.get("model_gateway_ref"), field="model_gateway_ref"),
        "context": {
            "conversation_ref": _opaque_ref(context.get("conversation_ref"), field="conversation_ref"),
            "topic_ref": _opaque_ref(context.get("topic_ref"), field="topic_ref"),
            "topic_revision": int(context["topic_revision"]),
            "continuity_refs": [_opaque_ref(item, field="continuity_ref") for item in continuity_refs],
        },
        "allowed_capability_ids": normalized_capabilities,
    }


def validate_capability_request(run_request: Mapping[str, object], value: Mapping[str, object]) -> dict:
    """Validate a proposal for the Neko Capability Gateway, never a direct tool call."""

    request = create_dsh_run_request(run_request)
    if not isinstance(value, Mapping):
        raise DshContractError("dsh0_capability_request_invalid")
    _assert_no_forbidden_fields(value)
    _exact_keys(value, frozenset({"schema_version", "run_ref", "capability_id", "request_ref"}))
    if value.get("schema_version") != 1:
        raise DshContractError("dsh0_schema_version_invalid")
    run_ref = _opaque_ref(value.get("run_ref"), field="run_ref")
    if run_ref != request["run_ref"]:
        raise DshContractError("dsh0_capability_run_mismatch")
    capability_id = str(value.get("capability_id") or "").strip()
    if capability_id not in request["allowed_capability_ids"]:
        raise DshContractError("dsh0_capability_not_granted")
    return {
        "schema_version": 1,
        "run_ref": run_ref,
        "capability_id": capability_id,
        "request_ref": _opaque_ref(value.get("request_ref"), field="capability_request_ref"),
    }


def assert_dsh0_proposal(value: Mapping[str, object]) -> dict:
    """Accept a reference-only output; final content remains outside DSH-0 storage."""

    if not isinstance(value, Mapping):
        raise DshContractError("dsh0_proposal_invalid")
    _assert_no_forbidden_fields(value)
    _exact_keys(value, frozenset({"schema_version", "run_ref", "proposal_kind", "proposal_ref", "evidence_refs"}))
    if value.get("schema_version") != 1:
        raise DshContractError("dsh0_schema_version_invalid")
    proposal_kind = str(value.get("proposal_kind") or "").strip()
    if proposal_kind not in {"response", "action_draft", "research_proposal", "affect_candidate"}:
        raise DshContractError("dsh0_proposal_kind_invalid")
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list) or len(evidence_refs) > 32:
        raise DshContractError("dsh0_proposal_invalid")
    return {
        "schema_version": 1,
        "run_ref": _opaque_ref(value.get("run_ref"), field="run_ref"),
        "proposal_kind": proposal_kind,
        "proposal_ref": _opaque_ref(value.get("proposal_ref"), field="proposal_ref"),
        "evidence_refs": [_opaque_ref(item, field="evidence_ref") for item in evidence_refs],
    }


class DshFakeGateway:
    """In-memory DSH-0 transport for contract tests; it has no external effects."""

    def __init__(self, *, max_inflight: int, now: Callable[[], datetime] | None = None):
        if not isinstance(max_inflight, int) or max_inflight < 1:
            raise ValueError("dsh0_max_inflight_invalid")
        self._max_inflight = max_inflight
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._runs: dict[str, dict] = {}
        self._by_idempotency: dict[str, str] = {}
        self.delivery_count = 0
        self.persistent_write_count = 0

    def submit(self, value: Mapping[str, object]) -> dict:
        request = create_dsh_run_request(value)
        known_ref = self._by_idempotency.get(request["idempotency_key"])
        if known_ref is not None:
            known = self._runs[known_ref]
            if known["run_ref"] != request["run_ref"]:
                raise DshContractError("dsh0_idempotency_conflict")
            return dict(known["receipt"])
        if _parse_datetime(request["deadline_at"]) <= self._now().astimezone(timezone.utc):
            return {"status": "deadline_expired", "run_ref": request["run_ref"]}
        active = sum(1 for item in self._runs.values() if item["status"] == "accepted")
        if active >= self._max_inflight:
            return {"status": "backpressure", "run_ref": request["run_ref"]}
        receipt = {
            "status": "accepted",
            "run_ref": request["run_ref"],
            "idempotency_key": request["idempotency_key"],
            "workspace_ref": request["workspace_ref"],
        }
        self._runs[request["run_ref"]] = {"run_ref": request["run_ref"], "status": "accepted", "receipt": receipt}
        self._by_idempotency[request["idempotency_key"]] = request["run_ref"]
        return dict(receipt)

    def cancel(self, run_ref: object) -> dict:
        normalized = _opaque_ref(run_ref, field="run_ref")
        item = self._runs.get(normalized)
        if item is None:
            return {"status": "not_found", "run_ref": normalized}
        if item["status"] == "accepted":
            item["status"] = "cancelled"
        return {"status": item["status"], "run_ref": normalized}


def dsh_runtime_enabled() -> bool:
    """DSH-0 intentionally has no runtime route or feature flag to turn on."""

    return False


__all__ = [
    "DSH_PINNED_REVISION",
    "DSH_UPSTREAM_LICENSE",
    "DSH_UPSTREAM_REPOSITORY",
    "DSH0_STATUS",
    "DshContractError",
    "DshFakeGateway",
    "assert_dsh0_proposal",
    "create_dsh_run_request",
    "dsh_runtime_enabled",
    "validate_capability_request",
    "validate_pinned_upstream",
]
