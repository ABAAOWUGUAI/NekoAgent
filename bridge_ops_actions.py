"""Bridge-side helpers for approved, allowlisted Ops Broker writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import uuid

from bridge_ops_broker_client import OpsBrokerClient
from bridge_ops_broker_contract import OPS_CONTRACT_VERSION, action_hash


def admin_token_client_error(error: str) -> str:
    return {
        "admin_token_matches_channel_token": "token_matches_channel_secret",
        "args.new_token_invalid": "token_characters_invalid",
    }.get(str(error or ""), "")


def broker_write(
    action: str,
    target: str,
    args: dict | None = None,
    *,
    idempotency_key: str = "",
    approval_version: int = 1,
) -> dict:
    """Submit one short-lived write approval; never accepts executable text."""

    operation = {
        "contract_version": OPS_CONTRACT_VERSION,
        "action": action,
        "target": target,
        "args": dict(args or {}),
    }
    key = str(idempotency_key or f"bridge-{action}-{uuid.uuid4().hex}").strip()
    expires = datetime.now(timezone.utc) + timedelta(minutes=2)
    request = {
        **operation,
        "approval": {
            "action_hash": action_hash(operation),
            "version": max(1, int(approval_version)),
            "idempotency_key": key[:160],
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        },
    }
    response = OpsBrokerClient(
        os.environ.get("OPS_BROKER_SOCKET", "/run/agent-bridge/ops.sock"),
        timeout=30,
    ).request(request)
    if not response.get("ok"):
        response_data = response.get("data")
        nested_error = (
            response_data.get("error")
            if isinstance(response_data, dict)
            else ""
        )
        raise RuntimeError(str(
            nested_error
            or response.get("error")
            or "ops_broker_write_failed"
        ))
    data = response.get("data")
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(str((data or {}).get("error") or "ops_broker_write_failed"))
    return data


__all__ = ["admin_token_client_error", "broker_write"]
