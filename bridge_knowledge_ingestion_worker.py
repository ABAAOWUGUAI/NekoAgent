"""Bounded ingestion scheduler hook (C5).

The knowledge ingestion worker reuses the existing Automation/Proactive worker
loop; it is not a second unsupervised process.  ``maybe_run_knowledge_ingestion``
is called from the bounded worker and only acts when at least one enabled,
Owner-configured source exists.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
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
    persist_errors: bool = True,
) -> dict:
    """Run one pass per enabled source; never publishes; bounded and idempotent.

    ``run_ingestion`` requires an open ``sqlite3.Connection`` (the runtime
    runner's contract).  Each source runs inside its own bounded connection
    from ``assistant_connect()``, mirroring the other worker hooks.  Per-source
    failures are recorded as errors in the returned summary and, when
    ``persist_errors`` is set, mirrored into the run-metadata table so a source
    that never reaches the runner stays observable instead of silently
    vanishing.
    """

    results = []
    try:
        with assistant_connect() as conn:
            sources = list_enabled_ingestion_sources(conn)
    except (sqlite3.Error, ValueError):
        return {"ran": 0, "skipped": 0, "errors": [], "sources": []}
    for source in sources:
        config = {
            "source_type": str(source.get("source_type") or ""),
            "root": str(source.get("root_path") or ""),
            "enabled": True,
            "config_revision": int(source.get("config_revision") or 1),
        }
        try:
            with assistant_connect() as conn:
                result = run_ingestion(conn, config)
        except Exception as exc:  # noqa: BLE001 - bounded worker keeps running
            results.append(
                {
                    "source_id": str(source.get("id") or ""),
                    "ok": False,
                    "error": str(exc)[:160],
                    "error_kind": type(exc).__name__[:80],
                },
            )
            continue
        results.append(result)
    summary = {
        "ran": sum(1 for item in results if item.get("ok")),
        "skipped": sum(1 for item in results if not item.get("ok") and item.get("reason") == "disabled"),
        "errors": [item for item in results if "error" in item],
        "sources": results,
        "summary_json": json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    if persist_errors and summary["errors"]:
        try:
            with assistant_connect() as conn:
                record_worker_error_runs(conn, summary)
        except Exception:  # noqa: BLE001 - observability must never break the loop
            pass
    return summary


def record_worker_error_runs(
    conn: sqlite3.Connection,
    summary: Mapping,
) -> int:
    """Persist bounded-worker connector errors into the run metadata table.

    A successful run already records itself via ``run_knowledge_ingestion``;
    this mirrors the same table for per-source worker failures so a source
    that never reaches the runner (connector/DB error) is still observable
    instead of disappearing.  Returns how many error rows were written.
    """

    from bridge_migrations import utc_now

    errors = [
        item for item in (summary.get("sources") or []) if isinstance(item, Mapping) and item.get("error")
    ]
    written = 0
    for item in errors:
        source_id = str(item.get("source_id") or "")
        error_kind = str(item.get("error_kind") or type(item.get("error") or "").__name__)[:80]
        detail = str(item.get("error") or "")[:160]
        now = utc_now()
        run_id = "knrun-" + uuid.uuid4().hex
        conn.execute(
            """INSERT INTO assistant_knowledge_ingestion_runs(
                   id,source_id,config_revision,started_at,finished_at,duration_seconds,
                   discovered,unchanged,changed,deleted,failed,chunks,candidates,drafts,
                   conflicts,rejected,stop_reason,error_kind
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, source_id, 1, now, now, 0.0,
                0, 0, 0, 0, 1, 0, 0, 0, 0, 0, "worker_error", error_kind,
            ),
        )
        written += 1
    if written:
        conn.commit()
    return written


__all__ = [
    "list_enabled_ingestion_sources",
    "maybe_run_knowledge_ingestion",
    "record_worker_error_runs",
]
