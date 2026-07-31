"""Fixed AstrBot-to-Bridge health probe."""

from __future__ import annotations

from collections.abc import Callable

from bridge_ops_broker_client import OpsBrokerClientError


def probe_bridge(
    *,
    bridge_url: str,
    required: bool,
    broker_request: Callable,
    capture_command: Callable,
    safe_log_text: Callable,
    container: str,
) -> dict:
    if required:
        try:
            result = broker_request(
                "container_bridge_probe",
                container,
                {"timeout_seconds": 6},
            )
        except OpsBrokerClientError as exc:
            return {"ok": False, "url": bridge_url, "output": str(exc)}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        output = str(data.get("output") or result.get("error") or "")
        return {
            "ok": bool(result.get("ok") and data.get("ok")),
            "url": bridge_url,
            "output": safe_log_text(output),
        }
    target = bridge_url + "/health"
    code = (
        "import urllib.request;"
        f"print(urllib.request.urlopen({target!r}, timeout=3).read().decode('utf-8'))"
    )
    ok, output = capture_command(
        ["docker", "exec", container, "python", "-c", code],
        timeout=6,
    )
    return {
        "ok": ok and '"ok": true' in output.lower(),
        "url": bridge_url,
        "output": safe_log_text(output),
    }


__all__ = ["probe_bridge"]
