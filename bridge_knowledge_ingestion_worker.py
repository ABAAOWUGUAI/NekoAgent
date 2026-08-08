"""Bounded ingestion scheduler hook (C5).

The knowledge ingestion worker reuses the existing Automation/Proactive worker
loop; it is not a second unsupervised process.  ``maybe_run_knowledge_ingestion``
is called from the bounded worker and only acts when at least one enabled,
Owner-configured source exists.

Failure policy: every failure path returns a summary the caller can consume and
persist.  ``fatal`` is set when the worker could not even list sources or
persist its error rows — the caller must surface that (structured log + worker
health) instead of treating it as a normal empty run.  Errors never bring the
loop down, but they are never silently swallowed either.
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
    except sqlite3.Error as exc:
        raise ValueError("knowledge_source_listing_failed") from exc
    return [dict(row) for row in rows]


def _summary(results: list[dict]) -> dict:
    return {
        "ran": sum(1 for item in results if item.get("ok")),
        "skipped": sum(1 for item in results if not item.get("ok") and item.get("reason") == "disabled"),
        "errors": [item for item in results if "error" in item],
        "sources": results,
        "fatal": "",
        "summary_json": json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }


def maybe_run_knowledge_ingestion(
    assistant_connect: Callable[[], sqlite3.Connection],
    *,
    run_ingestion: Callable[..., dict],
    persist_errors: bool = True,
    log_event: Callable[[dict], None] | None = None,
) -> dict:
    """Run one pass per enabled source; never publishes; bounded and idempotent.

    ``run_ingestion`` requires an open ``sqlite3.Connection`` (the runtime
    runner's contract).  Each source runs inside its own bounded connection
    from ``assistant_connect()``, mirroring the other worker hooks.  Per-source
    failures are recorded as errors in the returned summary and, when
    ``persist_errors`` is set, mirrored into the run-metadata table so a source
    that never reaches the runner stays observable instead of silently
    vanishing.

    Returns a summary with ``fatal`` set (non-empty) when the worker could not
    even list sources or could not persist its own error rows.  ``log_event``
    receives sanitized structured failure records so the caller can emit logs
    and reflect failures into worker health without duplicating the message.
    """

    # Stage 1: source listing.  A connect/list failure is not a normal empty
    # run — it is the worker being unable to reach its own metadata table.
    try:
        with assistant_connect() as conn:
            sources = list_enabled_ingestion_sources(conn)
    except (sqlite3.Error, ValueError) as exc:
        failure = {
            "stage": "source_listing",
            "error": "knowledge_source_listing_failed",
            "detail": _safe_detail(exc),
        }
        if log_event is not None:
            log_event(failure)
        return {
            "ran": 0,
            "skipped": 0,
            "errors": [],
            "sources": [],
            "fatal": "source_listing_failed",
            "error_kind": "knowledge_source_listing_failed",
            "error": _safe_detail(exc),
            "summary_json": json.dumps(failure, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }
    results = []
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
    summary = _summary(results)
    if persist_errors and summary["errors"]:
        try:
            with assistant_connect() as conn:
                record_worker_error_runs(conn, summary)
        except Exception as exc:  # noqa: BLE001 - persistence failure surfaced below
            summary["fatal"] = "error_persistence_failed"
            summary["error_kind"] = "knowledge_worker_error_persistence_failed"
            summary["error"] = _safe_detail(exc)
            if log_event is not None:
                log_event(
                    {
                        "stage": "error_persistence",
                        "error": "knowledge_worker_error_persistence_failed",
                        "detail": _safe_detail(exc),
                    },
                )
    return summary


def _safe_detail(exc: Exception) -> str:
    """A short, sanitised error label — never raw paths, bodies or identifiers."""
    return str(type(exc).__name__)[:80]


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


def process_knowledge_ingestion_pass(runtime: Mapping) -> dict:
    """Run one bounded pass inside the automation worker with health reflection.

    The Bridge's automation loop invokes this; the worker module owns the
    orchestration so the legacy bridge file does not grow.  ``runtime`` must
    carry ``_assistant_db_connect`` and optionally ``WORKER_HEALTH``.  A fatal
    worker failure (source listing or error persistence) is surfaced through
    worker health and a sanitized structured log line; the loop itself keeps
    running.
    """

    from bridge_knowledge_ingestion_runtime import run_knowledge_ingestion

    connect = runtime.get("_assistant_db_connect")
    if connect is None:
        return {"fatal": "worker_unavailable", "ran": 0, "skipped": 0, "errors": [], "sources": []}
    health = runtime.get("WORKER_HEALTH")
    worker_id = "knowledge_ingestion"
    if health is not None:
        health.begin(worker_id)

    def log_failure(event: Mapping) -> None:
        # Sanitized structured log: stable error labels only, never raw paths,
        # bodies or identifiers.
        print(
            "knowledge:" + str(event.get("stage") or "") + ":" + str(event.get("error") or ""),
            flush=True,
        )

    summary = maybe_run_knowledge_ingestion(
        connect,
        run_ingestion=run_knowledge_ingestion,
        log_event=log_failure,
    )
    fatal = str(summary.get("fatal") or "")
    if health is not None:
        if fatal:
            health.failure(worker_id, RuntimeError(fatal))
        else:
            health.success(worker_id)
    return summary


__all__ = [
    "list_enabled_ingestion_sources",
    "maybe_run_knowledge_ingestion",
    "process_knowledge_ingestion_pass",
    "record_worker_error_runs",
]
