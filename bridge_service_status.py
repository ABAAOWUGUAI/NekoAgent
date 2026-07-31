"""Service status aggregation with optional Broker shadow comparison."""

from __future__ import annotations

import time
from collections.abc import Callable

from bridge_ops_broker_client import OpsBrokerClientError


def collect_service_status(
    specs: list[dict],
    *,
    required: bool,
    shadow: bool,
    broker_request: Callable,
    direct_status: Callable,
) -> dict:
    started = time.monotonic()
    services = []
    for spec in specs:
        broker_result = None
        if required or shadow:
            action = "service_status" if spec["type"] == "systemd" else "container_status"
            try:
                broker_result = broker_request(action, spec["target"])
            except OpsBrokerClientError as exc:
                broker_result = {"ok": False, "error": str(exc)}
        if required:
            data = broker_result.get("data") if isinstance(broker_result, dict) and isinstance(
                broker_result.get("data"), dict,
            ) else {}
            services.append({
                **spec,
                "status": data.get("status") or "unknown",
                "ok": bool(broker_result and broker_result.get("ok") and data.get("ok")),
                "ops_broker": True,
                **({} if broker_result and broker_result.get("ok") else {
                    "error": (broker_result or {}).get("error", "broker_unavailable"),
                }),
            })
            continue
        command = (
            ["systemctl", "is-active", spec["target"]]
            if spec["type"] == "systemd"
            else ["docker", "inspect", "-f", "{{.State.Status}}", spec["target"]]
        )
        ok, status = direct_status(command, timeout=5)
        expected = "active" if spec["type"] == "systemd" else "running"
        item = {**spec, "status": status or "unknown", "ok": ok and status == expected}
        if broker_result is not None:
            data = broker_result.get("data") if isinstance(broker_result.get("data"), dict) else {}
            item.update({
                "ops_broker": broker_result,
                "shadow_match": bool(
                    data.get("status") == item["status"]
                    and bool(data.get("ok")) == bool(item["ok"])
                ),
            })
        services.append(item)
    return {"ok": True, "duration": round(time.monotonic() - started, 2), "services": services}


__all__ = ["collect_service_status"]
