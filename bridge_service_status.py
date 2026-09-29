"""Service status aggregation with optional Broker shadow comparison."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

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
    def collect_one(spec: dict) -> dict:
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
            return {
                **spec,
                "status": data.get("status") or "unknown",
                "ok": bool(broker_result and broker_result.get("ok") and data.get("ok")),
                "ops_broker": True,
                **({} if broker_result and broker_result.get("ok") else {
                    "error": (broker_result or {}).get("error", "broker_unavailable"),
                }),
            }
        command = (
            ["systemctl", "is-active", spec["target"]]
            if spec["type"] == "systemd"
            else ["docker", "inspect", "-f", "{{.State.Status}}", spec["target"]]
        )
        try:
            ok, status = direct_status(command, timeout=5)
        except (OSError, RuntimeError, ValueError) as exc:
            ok, status = False, type(exc).__name__
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
        return item

    # Each probe has an independent bounded timeout.  Running the fixed
    # service list sequentially made an ordinary six-service status page wait
    # up to 30–48 seconds, longer than the browser read deadline.
    if not specs:
        services: list[dict] = []
    else:
        with ThreadPoolExecutor(max_workers=min(len(specs), 8), thread_name_prefix="service-status") as executor:
            services = list(executor.map(collect_one, specs))
    return {"ok": True, "duration": round(time.monotonic() - started, 2), "services": services}


__all__ = ["collect_service_status"]
