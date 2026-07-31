#!/usr/bin/env python3
"""Commit-bound invalidation wiring for the Assistant Home projection."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from bridge_sqlite_commit_hooks import CommitMutationConnection


ASSISTANT_HOME_ASSISTANT_TABLES = frozenset({
    "assistant_feature_flags",
    "assistant_instances",
    "continuity_turns",
    "conversation_messages",
    "conversation_threads",
    "learning_candidates",
    "persona_packs",
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

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_home_database(
    path: Path,
    tables: frozenset[str],
    callback: Callable[[], None],
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=10,
        factory=ClosingCommitMutationConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.configure_mutation_watch(tables, callback)
    return conn


__all__ = [
    "ASSISTANT_HOME_ASSISTANT_TABLES",
    "ASSISTANT_HOME_TASK_TABLES",
    "connect_home_database",
]
