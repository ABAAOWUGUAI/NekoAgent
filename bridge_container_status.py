"""Container inventory with Broker-required and shadow modes."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from bridge_ops_broker_client import OpsBrokerClientError


def collect_containers(
    *,
    required: bool,
    shadow: bool,
    broker_request: Callable,
    capture_command: Callable,
) -> dict:
    started = time.monotonic()
    if required:
        try:
            result = broker_request("container_list", "docker")
        except OpsBrokerClientError as exc:
            return {
                "ok": False,
                "duration": round(time.monotonic() - started, 2),
                "error": str(exc),
                "containers": [],
                "ops_broker": True,
            }
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {
            "ok": bool(result.get("ok") and data.get("ok")),
            "duration": round(time.monotonic() - started, 2),
            "containers": data.get("containers") or [],
            "ops_broker": True,
            "error": result.get("error") or data.get("error", ""),
        }
    ok, output = capture_command(["docker", "ps", "--format", "{{json .}}"], timeout=8)
    if not ok:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": output,
            "containers": [],
        }
    containers = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append({
            "id": item.get("ID", ""),
            "name": item.get("Names", ""),
            "image": item.get("Image", ""),
            "status": item.get("Status", ""),
            "state": item.get("State", ""),
            "ports": item.get("Ports", ""),
            "running_for": item.get("RunningFor", ""),
        })
    result = {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "containers": containers,
    }
    if shadow:
        try:
            broker_result = broker_request("container_list", "docker")
        except OpsBrokerClientError as exc:
            result["ops_broker"] = {"ok": False, "error": str(exc)}
            result["shadow_match"] = False
        else:
            data = broker_result.get("data") if isinstance(broker_result.get("data"), dict) else {}
            result["ops_broker"] = broker_result
            result["shadow_match"] = bool(
                broker_result.get("ok") and data.get("containers") == containers,
            )
    return result


__all__ = ["collect_containers"]
