#!/usr/bin/env python3
"""Commit-bound mutation callbacks for SQLite-backed read projections."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable


_MUTATION_TABLE = re.compile(
    r"^\s*(?:"
    r"INSERT(?:\s+OR\s+\w+)?\s+INTO|"
    r"REPLACE(?:\s+OR\s+\w+)?\s+INTO|"
    r"UPDATE(?:\s+OR\s+\w+)?|"
    r"DELETE\s+FROM"
    r")\s+([^\s(]+)",
    re.IGNORECASE,
)
_ROLLBACK_STATEMENT = re.compile(r"^\s*ROLLBACK(?:\s+TRANSACTION)?(?:\s|$)", re.IGNORECASE)


def _table_name(statement: str) -> str:
    """Return the unqualified table targeted by one ordinary DML statement."""

    text = str(statement or "").lstrip()
    while text.startswith("--") and "\n" in text:
        text = text.split("\n", 1)[1].lstrip()
    match = _MUTATION_TABLE.match(text)
    if not match:
        return ""
    identifier = match.group(1).rsplit(".", 1)[-1].strip()
    if len(identifier) >= 2:
        pairs = {'"': '"', "'": "'", "`": "`", "[": "]"}
        closing = pairs.get(identifier[0])
        if closing and identifier.endswith(closing):
            identifier = identifier[1:-1]
    return identifier.casefold()


class CommitMutationConnection(sqlite3.Connection):
    """SQLite connection that publishes watched DML only after commit.

    The trace callback identifies the source table for every executed DML
    statement.  The callback is deferred until the surrounding transaction is
    committed; a rollback clears the pending marker.  Callback failures are
    intentionally fail-open because a cache notification must never turn an
    already committed domain write into an apparent request failure.
    """

    def configure_mutation_watch(
        self,
        tables: Iterable[str],
        callback: Callable[[], None] | None,
    ) -> "CommitMutationConnection":
        self._watched_mutation_tables = frozenset(
            str(table or "").strip().casefold()
            for table in tables
            if str(table or "").strip()
        )
        self._mutation_callback = callback if callable(callback) else None
        self._watched_mutation_pending = False
        self._watched_statement_change_baseline = None
        self.set_trace_callback(
            self._observe_statement if self._mutation_callback is not None else None,
        )
        return self

    def _finalize_watched_statement(self) -> None:
        baseline = getattr(self, "_watched_statement_change_baseline", None)
        if baseline is None:
            return
        self._watched_statement_change_baseline = None
        if self.total_changes > baseline:
            self._watched_mutation_pending = True

    def _observe_statement(self, statement: str) -> None:
        if _ROLLBACK_STATEMENT.match(str(statement or "")):
            self._watched_statement_change_baseline = None
            self._watched_mutation_pending = False
            return
        # A trace fires before the current statement. Settle the preceding
        # watched statement first so a zero-row UPDATE does not look like a
        # mutation merely because an unrelated statement changes rows later.
        self._finalize_watched_statement()
        table = _table_name(statement)
        if table and table in getattr(self, "_watched_mutation_tables", ()):
            self._watched_statement_change_baseline = self.total_changes

    def execute(self, sql, parameters=(), /):
        cursor = super().execute(sql, parameters)
        self._finalize_watched_statement()
        if not self.in_transaction:
            self._publish_committed_mutation()
        return cursor

    def executemany(self, sql, seq_of_parameters, /):
        cursor = super().executemany(sql, seq_of_parameters)
        self._finalize_watched_statement()
        if not self.in_transaction:
            self._publish_committed_mutation()
        return cursor

    def executescript(self, sql_script, /):
        try:
            cursor = super().executescript(sql_script)
        except Exception:
            self._finalize_watched_statement()
            if not self.in_transaction:
                self._publish_committed_mutation()
            raise
        self._finalize_watched_statement()
        if not self.in_transaction:
            self._publish_committed_mutation()
        return cursor

    def _publish_committed_mutation(self) -> None:
        if not getattr(self, "_watched_mutation_pending", False):
            return
        self._watched_mutation_pending = False
        callback = getattr(self, "_mutation_callback", None)
        if not callable(callback):
            return
        try:
            callback()
        except Exception:
            return

    def commit(self) -> None:
        self._finalize_watched_statement()
        super().commit()
        self._publish_committed_mutation()

    def rollback(self) -> None:
        try:
            super().rollback()
        finally:
            self._watched_statement_change_baseline = None
            self._watched_mutation_pending = False

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self._finalize_watched_statement()
        try:
            result = super().__exit__(exc_type, exc_value, traceback)
        except Exception:
            self._watched_statement_change_baseline = None
            self._watched_mutation_pending = False
            raise
        if exc_type is None:
            self._publish_committed_mutation()
        else:
            self._watched_statement_change_baseline = None
            self._watched_mutation_pending = False
        return result


def connect_mutation_database(
    path,
    *,
    timeout: float,
    isolation_level: str | None,
    tables: Iterable[str],
    callback: Callable[[], None] | None,
) -> CommitMutationConnection:
    conn = sqlite3.connect(
        path,
        timeout=timeout,
        isolation_level=isolation_level,
        factory=CommitMutationConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.configure_mutation_watch(tables, callback)
    return conn


__all__ = ["CommitMutationConnection", "connect_mutation_database"]
