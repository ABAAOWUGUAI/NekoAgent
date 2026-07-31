#!/usr/bin/env python3
"""Freshness and restart semantics for NapCat login QR codes."""

from __future__ import annotations

import time
from collections.abc import Callable


def qrcode_freshness(info: dict, max_age_seconds: int, *, now: int | None = None) -> tuple[bool, int | None]:
    mtime = int(info.get("mtime") or 0)
    age = max(0, int(time.time() if now is None else now) - mtime) if mtime else None
    return bool(info.get("available") and age is not None and age <= max(30, max_age_seconds)), age


def restart_napcat(*, broker_required: bool, broker_write: Callable, capture_command: Callable, container: str) -> tuple[bool, str]:
    if broker_required:
        try:
            result = broker_write("container_restart", container)
        except Exception as exc:
            return False, str(exc)
        return bool(result.get("restarted")), str(result.get("error") or "")
    return capture_command(["docker", "restart", container], timeout=30)


def refresh_napcat_qrcode(
    *,
    wait_seconds: int,
    qrcode_info: Callable[[], dict],
    restart: Callable[[], tuple[bool, str]],
    diagnostics: Callable[[], dict],
    safe_error: Callable[[str], str],
) -> dict:
    started = time.monotonic()
    refresh_started_at = int(time.time())
    wait_seconds = max(5, min(int(wait_seconds or 25), 45))
    previous_mtime = int(qrcode_info().get("mtime") or 0)
    ok, output = restart()
    if not ok:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": safe_error(output),
            "diagnostics": diagnostics(),
        }

    def is_new(value: dict) -> bool:
        mtime = int(value.get("mtime") or 0)
        return bool(value.get("available") and mtime > previous_mtime and mtime >= refresh_started_at - 2)

    deadline = time.monotonic() + wait_seconds
    qrcode = qrcode_info()
    while not is_new(qrcode) and time.monotonic() < deadline:
        time.sleep(1)
        qrcode = qrcode_info()
    refreshed = is_new(qrcode)
    return {
        "ok": refreshed,
        "duration": round(time.monotonic() - started, 2),
        "qrcode": qrcode,
        "diagnostics": diagnostics(),
        "error": "" if refreshed else "qrcode_not_refreshed",
    }


__all__ = ["qrcode_freshness", "refresh_napcat_qrcode", "restart_napcat"]
