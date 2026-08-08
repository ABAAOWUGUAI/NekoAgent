"""Bounded ingestion scheduler hook (C5).

The knowledge ingestion worker reuses the existing Automation/Proactive worker
loop; it is not a second unsupervised process.  ``maybe_run_knowledge_ingestion``
is called from the bounded worker and only acts when at least one enabled,
Owner-configured source exists.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping


def list_enabled_ingestion_sources(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT id,source_type,root_path,enabled,config_revision,config_json
               FROM assistant_knowledge_sources WHERE enabled=1"""
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def maybe_run_knowledge_ingestion(
    assistant_connect: Callable[[], sqlite3.Connection],
    *,
    run_ingestion: Callable[..., dict],
) -> dict:
    """Run one pass per enabled source; never publishes; bounded and idempotent."""

    try:
        with assistant_connect() as conn:
            sources = list_enabled_ingestion_sources(conn)
    except (sqlite3.Error, ValueError):
        return {"ran": 0, "skipped": 0, "errors": []}
    results = []
    for source in sources:
        try:
            config = {
                "source_type": str(source.get("source_type") or ""),
                "root": str(source.get("root_path") or ""),
                "enabled": True,
                "config_revision": int(source.get("config_revision") or 1),
            }
            result = run_ingestion(assistant_connect, config)
        except Exception as exc:  # noqa: BLE001 - bounded worker keeps running
            results.append({"source_id": str(source.get("id") or ""), "error": str(exc)[:160]})
            continue
        results.append(result)
    return {
        "ran": sum(1 for item in results if item.get("ok")),
        "skipped": sum(1 for item in results if not item.get("ok") and item.get("reason") == "disabled"),
        "errors": [item for item in results if "error" in item],
        "sources": results,
    }


__all__ = ["list_enabled_ingestion_sources", "maybe_run_knowledge_ingestion"]
