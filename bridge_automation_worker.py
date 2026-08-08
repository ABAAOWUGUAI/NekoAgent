#!/usr/bin/env python3
"""Long-running automation, reconciliation and proactive work loop."""

from __future__ import annotations


def run_automation_worker(runtime: dict) -> None:
    while True:
        health = runtime["WORKER_HEALTH"]
        health.begin("automation")
        try:
            assistant_connect = runtime["_assistant_db_connect"]
            task_connect = runtime["_db_connect"]
            runtime["drain_action_outbox"](
                assistant_connect, runtime["_phase2_outbox"](), limit=10,
            )
            with assistant_connect() as conn:
                runtime["expire_stale_memories"](conn)
            runtime["process_group_participation_queue"](runtime)
            runtime["_process_automation_jobs"]()
            runtime["reconcile_automation_tasks"](
                assistant_connect, task_connect, limit=50,
            )
            runtime["process_proactive_policies"](runtime)
            # Knowledge ingestion reuses this same bounded worker loop.  It only
            # scans Owner-configured enabled sources and never publishes; a
            # missing/disabled config is a no-op.  A fatal worker failure is
            # already reflected in WORKER_HEALTH and logged by the worker; the
            # loop itself keeps running.
            process_knowledge_ingestion = runtime.get("process_knowledge_ingestion")
            if process_knowledge_ingestion is not None:
                try:
                    process_knowledge_ingestion(runtime)
                except Exception as exc:
                    print("knowledge:unexpected:" + type(exc).__name__, flush=True)
            with assistant_connect() as conn:
                wait_seconds = runtime["seconds_until_next_event"](
                    conn, maximum=60.0,
                )
            health.success("automation")
        except Exception as exc:
            health.failure("automation", exc)
            failures = health.snapshot()["automation"]["consecutive_failures"]
            print(type(exc).__name__, flush=True)
            wait_seconds = min(
                300.0,
                15.0 * (2 ** min(int(failures) - 1, 4)),
            )
        runtime["AUTOMATION_EVENT"].wait(timeout=wait_seconds)
        runtime["AUTOMATION_EVENT"].clear()


__all__ = ["run_automation_worker"]
