"""Privilege-safe server summary built from the same broker facts as /services."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable

from bridge_ops_broker_client import OpsBrokerClient


def _human(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _broker(action: str, target: str) -> dict:
    try:
        response = OpsBrokerClient().request({"action": action, "target": target, "args": {}})
        return dict(response.get("data") or {}) if response.get("ok") else {"ok": False}
    except Exception:
        return {"ok": False}


def build_server_status(
    workspace: Path,
    executor_probe: Callable[[], dict],
    codegraph_probe: Callable[[], dict],
    *,
    include_runtime: bool = True,
) -> dict:
    started = time.monotonic()
    try:
        uptime_seconds = int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        uptime = f"{days}d {hours}h {minutes}m"
    except Exception:
        uptime = "unknown"
    try:
        loadavg = Path("/proc/loadavg").read_text(encoding="utf-8").strip()
    except Exception:
        loadavg = "unknown"
    memory: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            memory[key] = int(raw.strip().split()[0]) * 1024
    except Exception:
        pass
    total = memory.get("MemTotal", 0)
    available = memory.get("MemAvailable", 0)
    root_disk = shutil.disk_usage("/")
    workspace_disk = shutil.disk_usage(str(workspace))
    docker: dict = {}
    mihomo: dict = {}
    executor: dict = {}
    codegraph: dict = {}
    containers: list[str] = []
    if include_runtime:
        docker = _broker("service_status", "docker")
        mihomo = _broker("container_status", "mihomo")
        listed = _broker("container_list", "docker")
        executor = executor_probe()
        codegraph = codegraph_probe()
        containers = [
            f"{item.get('name', 'unknown')} | {item.get('status') or item.get('state') or 'unknown'}"
            for item in listed.get("containers", [])[:10]
        ] or ["(container inventory unavailable)"]
        ok = bool(docker.get("ok") and mihomo.get("ok") and executor.get("ok"))
    else:
        ok = bool(root_disk.total and workspace_disk.total)
    lines = [
        "Server quick health:",
        f"- overall: {'OK' if ok else 'CHECK'}",
        f"- uptime: {uptime}",
        f"- loadavg: {loadavg}",
        f"- memory: {_human(max(total - available, 0))} / {_human(total)} used, {_human(available)} available" if total else "- memory: unknown",
        f"- root disk: {_human(root_disk.used)} / {_human(root_disk.total)} used, free {_human(root_disk.free)}",
        f"- workspace disk: {_human(workspace_disk.used)} / {_human(workspace_disk.total)} used, free {_human(workspace_disk.free)}",
    ]
    if include_runtime:
        lines.extend(
            [
                f"- docker service: {docker.get('status', 'unknown')}",
                f"- mihomo proxy: {mihomo.get('status', 'unknown')}",
                f"- task executor: {'ready' if executor.get('ok') else executor.get('error', 'unavailable')} ({executor.get('adapter', 'unknown')})",
                f"- codegraph: {codegraph.get('status', 'unknown')}",
                "- containers:",
                *(f"  - {line}" for line in containers),
            ]
        )
    else:
        lines.append("- runtime checks: shown in the dedicated diagnostics below")
    return {"ok": ok, "duration": round(time.monotonic() - started, 2), "output": "\n".join(lines)}


__all__ = ["build_server_status"]
