#!/usr/bin/env python3
"""DSH-1 preflight-only sandbox and authenticated IPC contracts.

This module is deliberately incapable of starting DeepSeek Harness.  It
defines the reviewable plan a future, separately approved launcher must prove
before it can be introduced to any real Work Canary.
"""

from __future__ import annotations

from collections.abc import Mapping
import re

from bridge_deepseek_harness_contract import DSH_PINNED_REVISION


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9._-]+)+$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._-]{4,160}$")
_IPC_BODY_FIELDS = frozenset(
    {"api_key", "body", "content", "credential", "message", "message_text", "prompt", "raw_text", "text", "token"}
)


class DshSandboxContractError(ValueError):
    """A no-egress DSH sandbox or IPC boundary was invalid."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise DshSandboxContractError(error)
    return text


def _assert_body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key or "").lower()
            if name in _IPC_BODY_FIELDS or name.startswith(("raw_", "message_")) or name.endswith(("_body", "_content", "_text", "_token")):
                raise DshSandboxContractError("dsh1_ipc_body_free_violation")
            _assert_body_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_body_free(item)


def make_dsh_sandbox_plan(value: Mapping[str, object]) -> dict:
    """Create an inert, auditable launch plan; it is never a launch command."""

    if not isinstance(value, Mapping):
        raise DshSandboxContractError("dsh1_sandbox_plan_invalid")
    allowed = {
        "schema_version",
        "run_ref",
        "workspace_ref",
        "upstream_revision",
        "ipc_auth_ref",
        "log_key_ref",
        "log_retention_seconds",
    }
    if set(value) != allowed or value.get("schema_version") != 1:
        raise DshSandboxContractError("dsh1_sandbox_plan_invalid")
    if str(value.get("upstream_revision") or "") != DSH_PINNED_REVISION:
        raise DshSandboxContractError("dsh1_upstream_revision_unpinned")
    try:
        retention = int(value["log_retention_seconds"])
    except (TypeError, ValueError) as exc:
        raise DshSandboxContractError("dsh1_log_retention_invalid") from exc
    if not 1 <= retention <= 86400:
        raise DshSandboxContractError("dsh1_log_retention_invalid")
    plan = {
        "schema_version": 1,
        "run_ref": _ref(value.get("run_ref"), error="dsh1_sandbox_plan_invalid"),
        "workspace_ref": _ref(value.get("workspace_ref"), error="dsh1_sandbox_plan_invalid"),
        "upstream_revision": DSH_PINNED_REVISION,
        "ipc_auth_ref": _ref(value.get("ipc_auth_ref"), error="dsh1_sandbox_plan_invalid"),
        "log_key_ref": _ref(value.get("log_key_ref"), error="dsh1_sandbox_plan_invalid"),
        "log_retention_seconds": retention,
        "network": "none",
        "mounts": [],
        "workspace_lifecycle": "per_run_delete_on_terminal",
        "direct_model_credentials": False,
        "capability_gateway_only": True,
        "session_logs": "encrypted_ephemeral",
        "runtime_start_permitted": False,
    }
    assert_dsh_sandbox_plan(plan)
    return plan


def assert_dsh_sandbox_plan(plan: Mapping[str, object]) -> None:
    """Prove a plan has no egress, production mount, credential or launcher."""

    if not isinstance(plan, Mapping):
        raise DshSandboxContractError("dsh1_sandbox_plan_invalid")
    if plan.get("network") != "none":
        raise DshSandboxContractError("dsh1_network_must_be_none")
    mounts = plan.get("mounts")
    if not isinstance(mounts, list) or mounts:
        raise DshSandboxContractError("dsh1_production_mount_forbidden")
    if plan.get("runtime_start_permitted") is not False:
        raise DshSandboxContractError("dsh1_runtime_start_forbidden")
    if plan.get("direct_model_credentials") is not False or plan.get("capability_gateway_only") is not True:
        raise DshSandboxContractError("dsh1_gateway_boundary_invalid")
    if plan.get("workspace_lifecycle") != "per_run_delete_on_terminal" or plan.get("session_logs") != "encrypted_ephemeral":
        raise DshSandboxContractError("dsh1_retention_boundary_invalid")
    if str(plan.get("upstream_revision") or "") != DSH_PINNED_REVISION:
        raise DshSandboxContractError("dsh1_upstream_revision_unpinned")
    try:
        retention = int(plan.get("log_retention_seconds"))
    except (TypeError, ValueError) as exc:
        raise DshSandboxContractError("dsh1_log_retention_invalid") from exc
    if not 1 <= retention <= 86400:
        raise DshSandboxContractError("dsh1_log_retention_invalid")


def make_authenticated_ipc_envelope(value: Mapping[str, object]) -> dict:
    """Normalize a reference-only local IPC message without a secret value."""

    if not isinstance(value, Mapping):
        raise DshSandboxContractError("dsh1_ipc_invalid")
    _assert_body_free(value)
    allowed = {
        "schema_version",
        "run_ref",
        "ipc_auth_ref",
        "deadline_ref",
        "cancel_ref",
        "idempotency_key",
        "backpressure_ref",
        "context_refs",
    }
    if set(value) != allowed or value.get("schema_version") != 1:
        raise DshSandboxContractError("dsh1_ipc_invalid")
    contexts = value.get("context_refs")
    if not isinstance(contexts, list) or not contexts or len(contexts) > 32:
        raise DshSandboxContractError("dsh1_ipc_invalid")
    key = str(value.get("idempotency_key") or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise DshSandboxContractError("dsh1_idempotency_invalid")
    return {
        "schema_version": 1,
        "transport": "authenticated_local_ipc",
        "run_ref": _ref(value.get("run_ref"), error="dsh1_ipc_invalid"),
        "ipc_auth_ref": _ref(value.get("ipc_auth_ref"), error="dsh1_ipc_invalid"),
        "deadline_ref": _ref(value.get("deadline_ref"), error="dsh1_ipc_invalid"),
        "cancel_ref": _ref(value.get("cancel_ref"), error="dsh1_ipc_invalid"),
        "idempotency_key": key,
        "backpressure_ref": _ref(value.get("backpressure_ref"), error="dsh1_ipc_invalid"),
        "context_refs": [_ref(item, error="dsh1_ipc_invalid") for item in contexts],
    }


__all__ = [
    "DshSandboxContractError",
    "assert_dsh_sandbox_plan",
    "make_authenticated_ipc_envelope",
    "make_dsh_sandbox_plan",
]
