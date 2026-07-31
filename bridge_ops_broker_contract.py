"""Structured Root Ops Broker contract.

This module intentionally contains validation only.  It does not execute
systemctl, docker, journalctl, or proxy operations.  A future root broker can
reuse the normalized request after validating the caller with SO_PEERCRED.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from bridge_admin_token import TOKEN_MAX_LENGTH, TOKEN_MIN_LENGTH, TOKEN_PATTERN


OPS_CONTRACT_VERSION = 1
READ_ACTIONS = frozenset({
    "service_status", "service_logs", "container_status", "container_list",
    "container_logs", "container_env", "container_file_exists", "qq_login_probe",
    "container_bridge_probe", "config_test", "qq_qrcode_info", "qq_qrcode_png",
})
WRITE_ACTIONS = frozenset({
    "service_restart", "container_restart", "proxy_reload",
    "astrbot_plugin_set_enabled", "astrbot_plugin_operate",
    "admin_token_rotate",
})
ALL_ACTIONS = READ_ACTIONS | WRITE_ACTIONS
SERVICE_TARGETS = frozenset({
    "codex-qq-bridge", "codex-deepseek-proxy", "docker", "mihomo",
    "astrbot", "napcat", "llbot", "maim-bot-core",
})
CONTAINER_TARGETS = frozenset({"astrbot", "maim-bot-napcat", "mihomo", "maim-bot-core"})
ROOT_FIELDS = frozenset({"contract_version", "action", "target", "args", "approval"})
ARG_FIELDS = frozenset({
    "lines", "timeout_seconds", "name", "path", "plugin_id", "enabled", "operation",
    "new_token",
})
APPROVAL_FIELDS = frozenset({"action_hash", "version", "idempotency_key", "expires_at"})


class OpsBrokerContractError(ValueError):
    """A request cannot be safely represented as an allowlisted operation."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OpsBrokerContractError(f"{label}_object_required")
    return value


def _unknown_fields(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OpsBrokerContractError(f"{label}_field_forbidden:{','.join(unknown)}")


def _string(value: Any, label: str, *, max_length: int = 160) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpsBrokerContractError(f"{label}_required")
    value = value.strip()
    if len(value) > max_length:
        raise OpsBrokerContractError(f"{label}_too_long")
    if any(char in value for char in ("\x00", "\n", "\r")):
        raise OpsBrokerContractError(f"{label}_contains_control")
    return value


def canonical_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only the immutable operation portion used for action hashing."""

    return {
        "contract_version": int(payload.get("contract_version") or OPS_CONTRACT_VERSION),
        "action": str(payload.get("action") or ""),
        "target": str(payload.get("target") or ""),
        "args": dict(payload.get("args") or {}),
    }


def action_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(canonical_action(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_expiry(value: Any, now: datetime) -> str:
    expiry = _string(value, "approval.expires_at", max_length=64)
    try:
        parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpsBrokerContractError("approval.expires_at_invalid") from exc
    if parsed.tzinfo is None:
        raise OpsBrokerContractError("approval.expires_at_timezone_required")
    if parsed.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
        raise OpsBrokerContractError("approval_expired")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_request(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Validate and normalize a broker request; never returns executable argv."""

    root = _object(payload, "request")
    _unknown_fields(root, ROOT_FIELDS, "request")
    version = root.get("contract_version", OPS_CONTRACT_VERSION)
    if version != OPS_CONTRACT_VERSION:
        raise OpsBrokerContractError("contract_version_unsupported")
    action = _string(root.get("action"), "action", max_length=64)
    if action not in ALL_ACTIONS:
        raise OpsBrokerContractError("action_forbidden")
    target = _string(root.get("target"), "target", max_length=96)
    if action in {"service_status", "service_restart"} and target not in SERVICE_TARGETS:
        raise OpsBrokerContractError("service_target_forbidden")
    if action == "service_logs" and target not in SERVICE_TARGETS:
        raise OpsBrokerContractError("service_logs_target_forbidden")
    if action in {
        "container_status", "container_logs", "container_restart", "container_bridge_probe",
    } and target not in CONTAINER_TARGETS:
        raise OpsBrokerContractError("container_target_forbidden")
    if action == "container_list" and target != "docker":
        raise OpsBrokerContractError("container_list_target_forbidden")
    if action == "config_test" and target != "mihomo":
        raise OpsBrokerContractError("config_test_target_forbidden")
    if action == "qq_login_probe" and target != "maim-bot-napcat":
        raise OpsBrokerContractError("qq_login_probe_target_forbidden")
    if action in {"qq_qrcode_info", "qq_qrcode_png"} and target != "maim-bot-napcat":
        raise OpsBrokerContractError("qq_qrcode_target_forbidden")
    if action == "container_bridge_probe" and target != "astrbot":
        raise OpsBrokerContractError("container_bridge_probe_target_forbidden")
    if action == "proxy_reload" and target != "mihomo":
        raise OpsBrokerContractError("proxy_reload_target_forbidden")
    if action in {"astrbot_plugin_set_enabled", "astrbot_plugin_operate"} and target != "astrbot":
        raise OpsBrokerContractError("astrbot_plugin_target_forbidden")
    if action == "admin_token_rotate" and target != "bridge-admin-token":
        raise OpsBrokerContractError("admin_token_target_forbidden")

    args = _object(root.get("args") or {}, "args")
    _unknown_fields(args, ARG_FIELDS, "args")
    normalized_args: dict[str, Any] = {}
    if "lines" in args:
        lines = args["lines"]
        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 500:
            raise OpsBrokerContractError("args.lines_out_of_range")
        normalized_args["lines"] = lines
    if "timeout_seconds" in args:
        timeout = args["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30:
            raise OpsBrokerContractError("args.timeout_seconds_out_of_range")
        normalized_args["timeout_seconds"] = timeout
    if action == "container_env":
        name = _string(args.get("name"), "args.name", max_length=80)
        if name != "ASSISTANT_PLATFORM_BRIDGE_URL":
            raise OpsBrokerContractError("args.name_forbidden")
        normalized_args["name"] = name
    elif "name" in args:
        raise OpsBrokerContractError("args.name_not_supported")
    if action == "container_file_exists":
        path = _string(args.get("path"), "args.path", max_length=240)
        if not (
            path.startswith("/app/napcat/")
            or path.startswith("/app/.config/QQ/NapCat/")
            or path.startswith("/tmp/")
        ):
            raise OpsBrokerContractError("args.path_forbidden")
        normalized_args["path"] = path
    elif "path" in args:
        raise OpsBrokerContractError("args.path_not_supported")
    if action in {"astrbot_plugin_set_enabled", "astrbot_plugin_operate"}:
        plugin_id = _string(args.get("plugin_id"), "args.plugin_id", max_length=128)
        if not all(char.isalnum() or char in "_.-" for char in plugin_id):
            raise OpsBrokerContractError("args.plugin_id_invalid")
        normalized_args["plugin_id"] = plugin_id
    elif "plugin_id" in args:
        raise OpsBrokerContractError("args.plugin_id_not_supported")
    if action == "astrbot_plugin_set_enabled":
        enabled = args.get("enabled")
        if not isinstance(enabled, bool):
            raise OpsBrokerContractError("args.enabled_boolean_required")
        normalized_args["enabled"] = enabled
    elif "enabled" in args:
        raise OpsBrokerContractError("args.enabled_not_supported")
    if action == "astrbot_plugin_operate":
        operation = _string(args.get("operation"), "args.operation", max_length=16).lower()
        if operation not in {"install", "update", "uninstall"}:
            raise OpsBrokerContractError("args.operation_invalid")
        normalized_args["operation"] = operation
    elif "operation" in args:
        raise OpsBrokerContractError("args.operation_not_supported")
    if action == "admin_token_rotate":
        new_token = _string(
            args.get("new_token"),
            "args.new_token",
            max_length=TOKEN_MAX_LENGTH,
        )
        if len(new_token) < TOKEN_MIN_LENGTH or not TOKEN_PATTERN.fullmatch(new_token):
            raise OpsBrokerContractError("args.new_token_invalid")
        normalized_args["new_token"] = new_token
    elif "new_token" in args:
        raise OpsBrokerContractError("args.new_token_not_supported")
    if action not in {"container_logs", "service_logs"} and normalized_args.get("lines") is not None:
        raise OpsBrokerContractError("args.lines_not_supported")

    normalized: dict[str, Any] = {
        "contract_version": OPS_CONTRACT_VERSION,
        "action": action,
        "target": target,
        "args": normalized_args,
    }
    if action in WRITE_ACTIONS:
        approval = _object(root.get("approval"), "approval")
        _unknown_fields(approval, APPROVAL_FIELDS, "approval")
        expected_hash = action_hash(normalized)
        if _string(approval.get("action_hash"), "approval.action_hash", max_length=64) != expected_hash:
            raise OpsBrokerContractError("approval_action_hash_mismatch")
        approval_version = approval.get("version")
        if isinstance(approval_version, bool) or not isinstance(approval_version, int) or approval_version < 1:
            raise OpsBrokerContractError("approval.version_invalid")
        normalized["approval"] = {
            "action_hash": expected_hash,
            "version": approval_version,
            "idempotency_key": _string(approval.get("idempotency_key"), "approval.idempotency_key", max_length=160),
            "expires_at": _parse_expiry(approval.get("expires_at"), now or datetime.now(timezone.utc)),
        }
    elif "approval" in root and root["approval"] is not None:
        raise OpsBrokerContractError("approval_not_allowed_for_read")
    return normalized


__all__ = [
    "ALL_ACTIONS",
    "CONTAINER_TARGETS",
    "OPS_CONTRACT_VERSION",
    "OpsBrokerContractError",
    "READ_ACTIONS",
    "SERVICE_TARGETS",
    "WRITE_ACTIONS",
    "action_hash",
    "canonical_action",
    "validate_request",
]
