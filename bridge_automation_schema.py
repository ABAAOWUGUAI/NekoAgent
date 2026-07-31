#!/usr/bin/env python3
"""SQLite schema bootstrap for durable schedules and proactive decisions."""

from __future__ import annotations

import sqlite3


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
    if {
        "automation_jobs",
        "automation_runs",
        "automation_item_history",
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
            and {"lease_owner", "lease_until", "attempt_count", "terminal_source"}.issubset(run_columns)
            and "parameters_json" in job_columns
            and required_indexes.issubset(indexes)
        ):
            return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS automation_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            action_type TEXT NOT NULL,
            instruction TEXT NOT NULL,
            parameters_json TEXT NOT NULL DEFAULT '{}',
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
    )
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
    ):
        if name not in run_columns:
            conn.execute(f"ALTER TABLE automation_runs ADD COLUMN {name} {definition}")
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_jobs)").fetchall()}
    if "parameters_json" not in job_columns:
        conn.execute(
            "ALTER TABLE automation_jobs ADD COLUMN parameters_json TEXT NOT NULL DEFAULT '{}'",
        )


__all__ = ["ensure_automation_tables"]
