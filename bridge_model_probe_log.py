"""Compact persistence helpers for model executor diagnostics."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def record_proxy_probe(
    conn: sqlite3.Connection, *, probe_type: str, result: dict,
    executor_id: str = "deepseek-proxy", triggered_by: str = "admin",
) -> dict:
    latency = None
    for section in ("upstream", "healthz", "tcp", "executor"):
        value = result.get(section)
        if isinstance(value, dict) and value.get("latency_ms") is not None:
            latency = float(value["latency_ms"])
            break
    error = str(result.get("error") or "")
    if not error:
        for section in ("executor", "upstream", "healthz", "tcp"):
            value = result.get(section)
            if isinstance(value, dict) and value.get("error"):
                error = str(value["error"])
                break
    item = {
        "id": str(uuid.uuid4()), "probe_type": _clip(probe_type, 40),
        "executor_id": _clip(executor_id, 64), "ok": 1 if result.get("ok") else 0,
        "latency_ms": latency, "error_message": _clip(error, 500),
        "triggered_by": _clip(triggered_by, 80) or "admin",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        """INSERT INTO proxy_probe_log(
            id, probe_type, executor_id, ok, latency_ms,
            error_message, triggered_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        tuple(item[key] for key in (
            "id", "probe_type", "executor_id", "ok", "latency_ms",
            "error_message", "triggered_by", "created_at",
        )),
    )
    return item


def list_proxy_probe_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM proxy_probe_log ORDER BY created_at DESC LIMIT ?",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    return [dict(row) for row in rows]
