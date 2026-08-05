#!/usr/bin/env python3
"""SQLite schema bootstrap for durable schedules and proactive decisions."""

from __future__ import annotations

import json
import sqlite3

from bridge_automation_contracts import DEFAULT_OUTPUT_CONTRACT, normalize_output_contract, output_contract_hash
from bridge_automation_execution_contract import (
    derive_execution_contract,
    execution_contract_hash,
    normalize_execution_contract,
)


def ensure_automation_tables(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    }
    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'",
        ).fetchall()
    }
    schema_complete = False
    if {
        "automation_jobs",
        "automation_runs",
        "automation_item_history",
        "automation_action_plans",
        "proactive_policies",
        "proactive_events",
    }.issubset(tables):
        policy_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(proactive_policies)",
            ).fetchall()
        }
        event_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(proactive_events)",
            ).fetchall()
        }
        run_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(automation_runs)").fetchall()
        }
        required_indexes = {
            "idx_automation_jobs_due",
            "idx_automation_runs_recent",
            "idx_automation_item_history_run",
            "idx_proactive_policies_due",
            "idx_proactive_events_user",
            "idx_proactive_events_delivery",
        }
        job_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(automation_jobs)").fetchall()
        }
        if (
            {
                "initiative_mode",
                "allowed_intents",
                "schedule_jitter_minutes",
                "topic_cooldown_minutes",
            }.issubset(policy_columns)
            and "intent" in event_columns
            and {"lease_owner", "lease_until", "attempt_count", "terminal_source", "job_revision", "config_hash", "output_contract_hash"}.issubset(run_columns)
            and {
                "parameters_json", "revision", "output_contract_json", "output_contract_hash",
                "execution_contract_json", "execution_contract_hash",
            }.issubset(job_columns)
            and required_indexes.issubset(indexes)
        ):
            # Keep the idempotent migration/backfill below active.  A prior
            # process may have created the new columns while leaving legacy
            # rows with an empty execution contract.  Do not rerun
            # executescript in this case: sqlite3 executescript implicitly
            # commits and would destroy a caller-owned transaction/savepoint.
            schema_complete = True
    if not schema_complete:
        schema_sql = """
        CREATE TABLE IF NOT EXISTS automation_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            action_type TEXT NOT NULL,
            instruction TEXT NOT NULL,
            parameters_json TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 1,
            output_contract_json TEXT NOT NULL DEFAULT '{}',
            output_contract_hash TEXT NOT NULL DEFAULT '',
            execution_contract_json TEXT NOT NULL DEFAULT '{}',
            execution_contract_hash TEXT NOT NULL DEFAULT '',
            schedule_type TEXT NOT NULL,
            run_at TEXT NOT NULL DEFAULT '',
            time_of_day TEXT NOT NULL DEFAULT '09:00',
            weekdays TEXT NOT NULL DEFAULT '0',
            interval_minutes INTEGER NOT NULL DEFAULT 1440,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            enabled INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'disabled',
            next_due_at TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            last_run_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            run_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_automation_jobs_due
        ON automation_jobs(enabled, next_due_at, lease_until);

        CREATE TABLE IF NOT EXISTS automation_runs (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL,
            dispatch TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            delivery_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            terminal_source TEXT NOT NULL DEFAULT '',
            job_revision INTEGER NOT NULL DEFAULT 0,
            config_hash TEXT NOT NULL DEFAULT '',
            output_contract_hash TEXT NOT NULL DEFAULT '',
            capability_id TEXT NOT NULL DEFAULT '',
            execution_contract_hash TEXT NOT NULL DEFAULT '',
            failure_stage TEXT NOT NULL DEFAULT '',
            UNIQUE(job_id, scheduled_for),
            FOREIGN KEY(job_id) REFERENCES automation_jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_automation_runs_recent
        ON automation_runs(started_at DESC);

        CREATE TABLE IF NOT EXISTS automation_item_history (
            job_id TEXT NOT NULL,
            item_key TEXT NOT NULL,
            run_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(job_id, item_key),
            FOREIGN KEY(job_id) REFERENCES automation_jobs(id),
            FOREIGN KEY(run_id) REFERENCES automation_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_automation_item_history_run
        ON automation_item_history(run_id, state);

        CREATE TABLE IF NOT EXISTS automation_action_plans (
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            thread_ref TEXT NOT NULL,
            source_message_id TEXT NOT NULL DEFAULT '',
            target_job_id TEXT,
            target_revision INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            receipts_json TEXT NOT NULL DEFAULT '[]',
            clarification_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(target_job_id) REFERENCES automation_jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_automation_action_plans_open
        ON automation_action_plans(actor_id,thread_ref,status,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_automation_action_plans_job
        ON automation_action_plans(target_job_id,updated_at DESC);

        CREATE TABLE IF NOT EXISTS proactive_policies (
            user_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            authorized INTEGER NOT NULL DEFAULT 0,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            quiet_start TEXT NOT NULL DEFAULT '23:30',
            quiet_end TEXT NOT NULL DEFAULT '09:00',
            min_silence_minutes INTEGER NOT NULL DEFAULT 180,
            min_gap_minutes INTEGER NOT NULL DEFAULT 360,
            daily_limit INTEGER NOT NULL DEFAULT 2,
            weekly_limit INTEGER NOT NULL DEFAULT 5,
            unanswered_limit INTEGER NOT NULL DEFAULT 2,
            evaluation_interval_minutes INTEGER NOT NULL DEFAULT 60,
            topic_notes TEXT NOT NULL DEFAULT '',
            include_meme INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'disabled',
            state_reason TEXT NOT NULL DEFAULT '',
            next_check_at TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            last_evaluated_at TEXT NOT NULL DEFAULT '',
            last_sent_at TEXT NOT NULL DEFAULT '',
            last_user_at TEXT NOT NULL DEFAULT '',
            consecutive_unanswered INTEGER NOT NULL DEFAULT 0,
            decision_count INTEGER NOT NULL DEFAULT 0,
            skip_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_proactive_policies_due
        ON proactive_policies(enabled, authorized, next_check_at, lease_until);

        CREATE TABLE IF NOT EXISTS proactive_events (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            topic_key TEXT NOT NULL DEFAULT '',
            scheduled_for TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            delivery_id TEXT NOT NULL DEFAULT '',
            delivered_at TEXT NOT NULL DEFAULT '',
            responded_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_proactive_events_user
        ON proactive_events(user_id, decision_at DESC);
        CREATE INDEX IF NOT EXISTS idx_proactive_events_delivery
        ON proactive_events(delivery_id);
        """
        if conn.in_transaction:
            for statement in schema_sql.split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(statement)
        else:
            conn.executescript(schema_sql)
    policy_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(proactive_policies)",
        ).fetchall()
    }
    for name, definition in (
        ("initiative_mode", "TEXT NOT NULL DEFAULT 'balanced'"),
        (
            "allowed_intents",
            "TEXT NOT NULL DEFAULT 'follow_up,share,check_in,celebrate,reminder'",
        ),
        ("schedule_jitter_minutes", "INTEGER NOT NULL DEFAULT 20"),
        ("topic_cooldown_minutes", "INTEGER NOT NULL DEFAULT 1440"),
    ):
        if name not in policy_columns:
            conn.execute(
                f"ALTER TABLE proactive_policies ADD COLUMN {name} {definition}",
            )
    event_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(proactive_events)",
        ).fetchall()
    }
    if "intent" not in event_columns:
        conn.execute(
            "ALTER TABLE proactive_events "
            "ADD COLUMN intent TEXT NOT NULL DEFAULT 'check_in'",
        )
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_runs)").fetchall()}
    for name, definition in (
        ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
        ("lease_until", "TEXT NOT NULL DEFAULT ''"),
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("terminal_source", "TEXT NOT NULL DEFAULT ''"),
        ("job_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("config_hash", "TEXT NOT NULL DEFAULT ''"),
        ("output_contract_hash", "TEXT NOT NULL DEFAULT ''"),
        ("capability_id", "TEXT NOT NULL DEFAULT ''"),
        ("execution_contract_hash", "TEXT NOT NULL DEFAULT ''"),
        ("failure_stage", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in run_columns:
            conn.execute(f"ALTER TABLE automation_runs ADD COLUMN {name} {definition}")
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_jobs)").fetchall()}
    if "parameters_json" not in job_columns:
        conn.execute(
            "ALTER TABLE automation_jobs ADD COLUMN parameters_json TEXT NOT NULL DEFAULT '{}'",
        )
    for name, definition in (
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ("output_contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("output_contract_hash", "TEXT NOT NULL DEFAULT ''"),
        ("execution_contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("execution_contract_hash", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in job_columns:
            conn.execute(f"ALTER TABLE automation_jobs ADD COLUMN {name} {definition}")
    contract = normalize_output_contract(DEFAULT_OUTPUT_CONTRACT)
    contract_json = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """UPDATE automation_jobs
           SET revision=CASE WHEN revision<1 THEN 1 ELSE revision END,
               output_contract_json=CASE WHEN output_contract_json='' OR output_contract_json='{}'
                                         THEN ? ELSE output_contract_json END,
               output_contract_hash=CASE WHEN output_contract_hash='' THEN ? ELSE output_contract_hash END""",
        (contract_json, output_contract_hash(contract)),
    )
    legacy_rows = conn.execute(
        """SELECT id,action_type,instruction,parameters_json
           FROM automation_jobs
           WHERE execution_contract_json='' OR execution_contract_json='{}'""",
    ).fetchall()
    for row in legacy_rows:
        try:
            parameters = json.loads(str(row[3] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parameters = {}
        try:
            execution_contract = normalize_execution_contract(
                derive_execution_contract(
                    str(row[2] or ""),
                    parameters if isinstance(parameters, dict) else {},
                    action_type=str(row[1] or "agent"),
                ),
            )
        except (TypeError, ValueError):
            execution_contract = {
                "schema_version": 1,
                "capability_id": None,
                "arguments": {},
                "status": "needs_clarification",
                "missing_inputs": ["execution_contract"],
                "network_required": False,
                "output_kind": "agent_task",
            }
        execution_json = json.dumps(execution_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if execution_contract.get("status") == "ready":
            conn.execute(
                """UPDATE automation_jobs
                   SET execution_contract_json=?,execution_contract_hash=?
                   WHERE id=? AND (execution_contract_json='' OR execution_contract_json='{}')""",
                (execution_json, execution_contract_hash(execution_contract), str(row[0])),
            )
        else:
            conn.execute(
                """UPDATE automation_jobs
                   SET execution_contract_json=?,execution_contract_hash=?,
                       enabled=0,state='disabled',next_due_at=''
                   WHERE id=? AND (execution_contract_json='' OR execution_contract_json='{}')""",
                (execution_json, execution_contract_hash(execution_contract), str(row[0])),
            )


__all__ = ["ensure_automation_tables"]
