#!/usr/bin/env python3
"""Long-running automation, reconciliation and proactive work loop."""

from __future__ import annotations

import sqlite3
import time

from bridge_migrations import MigrationError


def _assistant_db_stage(connect, stage: str, work):
    """Commit one independent maintenance phase before starting the next."""

    started = time.monotonic()
    status = "ok"
    try:
        with connect() as conn:
            return work(conn)
    except sqlite3.OperationalError as exc:
        status = "sqlite_busy" if "locked" in str(exc).lower() or "busy" in str(exc).lower() else "sqlite_error"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        print(
            f"automation_db_stage stage={stage} elapsed_ms={int((time.monotonic() - started) * 1000)} status={status}",
            flush=True,
        )


def _automation_loop_stage(stage: str, work):
    """Time existing serial loop work without changing its dispatch or errors."""

    started = time.monotonic()
    status = "ok"
    try:
        return work()
    except sqlite3.OperationalError as exc:
        status = "sqlite_busy" if "locked" in str(exc).lower() or "busy" in str(exc).lower() else "sqlite_error"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms >= 250 or status != "ok":
            print(
                f"automation_loop_stage stage={stage} elapsed_ms={elapsed_ms} status={status}",
                flush=True,
            )


def _purge_group_expired_bodies(conn) -> None:
    """Keep physical retention writes out of latency-sensitive group reads."""

    try:
        from bridge_group_message_store import purge_expired_group_bodies

        purge_expired_group_bodies(conn, limit=64)
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        print("group-retention:" + type(exc).__name__, flush=True)


def _capture_scoped_qq_memory(conn) -> None:
    """Best-effort bounded capture, independent of reply and QQ delivery."""

    try:
        from bridge_scoped_memory_auto import process_scoped_memory_pass

        result = process_scoped_memory_pass(conn, limit=80)
        if result.get("applied"):
            print("scoped-memory-applied:" + str(result["applied"]), flush=True)
    except (MigrationError, sqlite3.Error, ValueError, TypeError) as exc:
        print("scoped-memory-capture:" + type(exc).__name__, flush=True)


def _reconcile_group_ack_projections(runtime: dict) -> None:
    """Sweep confirmed QQ deliveries in bounded pages without sending QQ."""

    try:
        from bridge_delivery_settlement import reconcile_confirmed_group_delivery

        outbox = runtime["_phase2_outbox"]()
        offset = int(runtime.get("_GROUP_ACK_PROJECTION_SCAN_OFFSET", 0))
        deliveries = outbox.list_deliveries(
            state="delivered", channel="qq", limit=25, offset=offset,
        )
        runtime["_GROUP_ACK_PROJECTION_SCAN_OFFSET"] = 0 if len(deliveries) < 25 else offset + len(deliveries)
        candidates = []
        with runtime["_assistant_db_connect"]() as conn:
            for delivery in deliveries:
                payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
                if payload.get("kind") not in {"assistant_reply", "assistant_voice_reply", "proactive_chat"}:
                    continue
                group_id = str(payload.get("group_id") or "").strip()
                delivery_id = str(delivery.get("id") or "").strip()
                if not group_id or not delivery_id:
                    continue
                projected = conn.execute(
                    """SELECT 1 FROM group_messages WHERE group_id=? AND sender_id='bot'
                       AND instr(metadata_json, ?) > 0 LIMIT 1""",
                    (group_id, f'"delivery_id":"{delivery_id}"'),
                ).fetchone()
                if not projected:
                    candidates.append(delivery_id)
        if candidates:
            print("group-projection-pending:" + str(len(candidates)), flush=True)
        for delivery_id in candidates:
            repaired = reconcile_confirmed_group_delivery(
                outbox, delivery_id,
                assistant_db_connect=runtime["_assistant_db_connect"],
            )
            if repaired:
                project_state = runtime.get("project_delivery_state")
                if callable(project_state):
                    with runtime["_assistant_db_connect"]() as conn:
                        project_state(conn, delivery_id, "channel_acked")
                print("group-projection-repaired", flush=True)
    except (MigrationError, sqlite3.Error, ValueError, KeyError) as exc:
        print("group-projection-reconcile:" + type(exc).__name__, flush=True)


def _purge_optional_behavior_shadows(conn) -> None:
    """Delete expired body-free records without making retention a send gate."""

    try:
        from bridge_assistant_affect_shadow import purge_expired_assistant_affect_shadows
        from bridge_behavior_candidate_registry import purge_expired_combined_shadow_receipts
        from bridge_behavior_observation import purge_expired_behavior_observations

        purge_expired_behavior_observations(conn)
        purge_expired_assistant_affect_shadows(conn)
        purge_expired_combined_shadow_receipts(conn)
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        # A retention-plane problem must be visible to service logs, but it
        # cannot stop the established Delivery, task and approval worker loop.
        print("behavior-retention:" + type(exc).__name__, flush=True)


def _run_optional_combined_shadow(runtime: dict, conn) -> None:
    """Run the BE-6 read-only hook without making delivery depend on it."""

    try:
        from bridge_behavior_combined_shadow_runtime import process_combined_shadow_pass

        process_combined_shadow_pass(runtime, conn)
    except (MigrationError, sqlite3.Error, ValueError, TypeError) as exc:
        # Combined Shadow is deliberately non-authoritative.  Its absence,
        # registry drift, or a rejected body-free record cannot stall the
        # established automation, Delivery or approval loops.
        print("behavior-combined-shadow:" + type(exc).__name__, flush=True)


def _run_optional_behavior_policy_optimizer(conn) -> None:
    """Create at most one default-off offline candidate without coupling Delivery.

    The optimizer performs no model call or private evaluation.  Schema drift,
    a missing Benchmark, or a disabled feature is an observable no-op rather
    than a reason to stop the established automation loop.
    """

    try:
        from bridge_behavior_policy_optimization_runtime import run_behavior_policy_optimizer

        run_behavior_policy_optimizer(conn)
    except (MigrationError, sqlite3.Error, ValueError, TypeError) as exc:
        print("behavior-policy-optimizer:" + type(exc).__name__, flush=True)


def run_automation_worker(runtime: dict) -> None:
    while True:
        health = runtime["WORKER_HEALTH"]
        health.begin("automation")
        try:
            assistant_connect = runtime["_assistant_db_connect"]
            task_connect = runtime["_db_connect"]
            _automation_loop_stage(
                "outbox",
                lambda: runtime["drain_action_outbox"](
                    assistant_connect, runtime["_phase2_outbox"](), limit=10,
                ),
            )
            # A due natural-group candidate has a bounded topic-freshness window.
            # Run it before best-effort retention/shadow/optimizer work so an
            # otherwise fresh group contribution is not made stale by this loop.
            _automation_loop_stage(
                "group_candidate", lambda: runtime["process_group_participation_queue"](runtime),
            )
            _assistant_db_stage(assistant_connect, "memory_capture", _capture_scoped_qq_memory)
            _assistant_db_stage(assistant_connect, "memory_expire", runtime["expire_stale_memories"])
            _assistant_db_stage(assistant_connect, "behavior_purge", _purge_optional_behavior_shadows)
            _assistant_db_stage(assistant_connect, "group_body_purge", _purge_group_expired_bodies)
            _assistant_db_stage(assistant_connect, "behavior_optimizer", _run_optional_behavior_policy_optimizer)
            _assistant_db_stage(
                assistant_connect, "combined_shadow",
                lambda conn: _run_optional_combined_shadow(runtime, conn),
            )
            _automation_loop_stage("ack_reconcile", lambda: _reconcile_group_ack_projections(runtime))
            _automation_loop_stage("automation_jobs", runtime["_process_automation_jobs"])
            _automation_loop_stage(
                "task_reconcile",
                lambda: runtime["reconcile_automation_tasks"](
                    assistant_connect, task_connect, limit=50,
                ),
            )
            _automation_loop_stage(
                "proactive_policies", lambda: runtime["process_proactive_policies"](runtime),
            )
            # Knowledge ingestion reuses this same bounded worker loop.  It only
            # scans Owner-configured enabled sources and never publishes; a
            # missing/disabled config is a no-op.  A fatal worker failure is
            # already reflected in WORKER_HEALTH and logged by the worker; the
            # loop itself keeps running.
            process_knowledge_ingestion = runtime.get("process_knowledge_ingestion")
            if process_knowledge_ingestion is not None:
                try:
                    _automation_loop_stage(
                        "knowledge_ingestion", lambda: process_knowledge_ingestion(runtime),
                    )
                except Exception as exc:
                    print("knowledge:unexpected:" + type(exc).__name__, flush=True)
            with assistant_connect() as conn:
                wait_seconds = _automation_loop_stage(
                    "next_due",
                    lambda: runtime["seconds_until_next_event"](conn, maximum=60.0),
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
