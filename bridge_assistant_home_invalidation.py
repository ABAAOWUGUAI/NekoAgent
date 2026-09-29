#!/usr/bin/env python3
"""Commit-bound invalidation wiring for the Assistant Home projection."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from bridge_sqlite_commit_hooks import CommitMutationConnection


ASSISTANT_HOME_ASSISTANT_TABLES = frozenset({
    "assistant_feature_flags",
    "assistant_instances",
    "assistant_voice_response_policies",
    "continuity_turns",
    "conversation_messages",
    "conversation_threads",
    "learning_candidates",
    "persona_packs",
    "persona_presets",
    "persona_versions",
    "pet_packs",
    "voice_packs",
})
ASSISTANT_HOME_TASK_TABLES = frozenset({
    "approval_requests",
    "artifact_publications",
    "artifact_version_files",
    "artifact_versions",
    "artifacts",
    "delivery_outbox",
    "goals",
    "runs",
})


class ClosingCommitMutationConnection(CommitMutationConnection):
    """Commit or roll back a context block, then release the file handle."""

    # This reports only slow transaction mechanics. SQL text, parameters,
    # message bodies and database paths must never enter the journal.
    _slow_stage_seconds = 1.0

    def _write_site(self) -> str:
        caller = sys._getframe(3)
        return (
            f"{Path(caller.f_code.co_filename).name}:"
            f"{caller.f_code.co_name}:{caller.f_lineno}"
        )

    def _observe_write(self, statement, operation):
        before = time.monotonic()
        was_writing = getattr(self, "_writer_started_at", None) is not None
        verb = str(statement or "").lstrip().split(None, 1)
        potential_write = bool(
            verb and verb[0].upper() in {
                "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP", "WITH"
            }
        )
        site = self._write_site() if was_writing or potential_write else ""
        if was_writing:
            previous = getattr(self, "_last_statement_done_at", before)
            gap = before - previous
            if gap >= self._slow_stage_seconds:
                print(
                    "assistant_db_txn_gap "
                    f"thread={threading.get_ident()} connection={id(self)} "
                    f"elapsed_ms={int(gap * 1000)} "
                    f"after={getattr(self, '_last_statement_site', '')} before={site}",
                    flush=True,
                )
        result = operation()
        after = time.monotonic()
        elapsed = after - before
        if was_writing and elapsed >= self._slow_stage_seconds:
            print(
                "assistant_db_txn_statement "
                f"thread={threading.get_ident()} connection={id(self)} "
                f"elapsed_ms={int(elapsed * 1000)} site={site}",
                flush=True,
            )
        if (
            getattr(self, "_writer_started_at", None) is None
            and self.in_transaction
            and potential_write
        ):
            # A first write can spend its busy timeout waiting for another
            # writer. Start at successful return, not before lock acquisition.
            self._writer_started_at = after
            self._writer_start_site = site
            if elapsed >= self._slow_stage_seconds:
                print(
                    "assistant_db_first_write_slow "
                    f"thread={threading.get_ident()} connection={id(self)} "
                    f"elapsed_ms={int(elapsed * 1000)} site={site}",
                    flush=True,
                )
        if self.in_transaction and getattr(self, "_writer_started_at", None) is not None:
            self._last_statement_done_at = after
            self._last_statement_site = site
        return result

    def execute(self, sql, parameters=(), /):
        return self._observe_write(sql, lambda: super(ClosingCommitMutationConnection, self).execute(sql, parameters))

    def executemany(self, sql, seq_of_parameters, /):
        return self._observe_write(sql, lambda: super(ClosingCommitMutationConnection, self).executemany(sql, seq_of_parameters))

    def _log_writer(self, status: str) -> None:
        started = getattr(self, "_writer_started_at", None)
        if started is None:
            return
        finished = time.monotonic()
        duration = finished - started
        if duration >= self._slow_stage_seconds:
            tail = finished - getattr(self, "_last_statement_done_at", finished)
            print(
                "assistant_db_writer "
                f"thread={threading.get_ident()} connection={id(self)} "
                f"elapsed_ms={int(duration * 1000)} tail_ms={int(tail * 1000)} "
                f"status={status} start={getattr(self, '_writer_start_site', '')} "
                f"last={getattr(self, '_last_statement_site', '')}",
                flush=True,
            )
        self._writer_started_at = None
        self._last_statement_done_at = None

    def _publish_committed_mutation(self) -> None:
        # CommitMutationConnection invokes this only after SQLite has committed,
        # so the writer duration excludes the subsequent cache callback.
        self._log_writer("committed")
        pending = bool(getattr(self, "_watched_mutation_pending", False))
        started = time.monotonic()
        super()._publish_committed_mutation()
        elapsed = time.monotonic() - started
        if pending and elapsed >= self._slow_stage_seconds:
            print(
                "assistant_db_callback_slow "
                f"thread={threading.get_ident()} connection={id(self)} "
                f"elapsed_ms={int(elapsed * 1000)}",
                flush=True,
            )

    def rollback(self) -> None:
        try:
            super().rollback()
        finally:
            self._log_writer("rolled_back")

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            result = super().__exit__(exc_type, exc_value, traceback)
            if getattr(self, "_writer_started_at", None) is not None:
                self._log_writer("rolled_back")
            return result
        except Exception:
            self._log_writer("exit_error")
            raise
        finally:
            self.close()


def connect_home_database(
    path: Path,
    tables: frozenset[str],
    callback: Callable[[], None],
    *,
    timeout_seconds: float = 10.0,
    query_only: bool = False,
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=max(0.05, float(timeout_seconds)),
        factory=ClosingCommitMutationConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {max(50, int(float(timeout_seconds) * 1000))}")
    conn.execute("PRAGMA foreign_keys = ON")
    if query_only:
        conn.execute("PRAGMA query_only = ON")
    conn.configure_mutation_watch(tables, callback)
    return conn


__all__ = [
    "ASSISTANT_HOME_ASSISTANT_TABLES",
    "ASSISTANT_HOME_TASK_TABLES",
    "connect_home_database",
]
