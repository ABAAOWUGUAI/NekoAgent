"""Durable server-owned binding for Artifact revision tasks."""

from __future__ import annotations

import hashlib
import sqlite3


ARTIFACT_REVISION_BINDING_COLUMNS = (
    "artifact_revision_id",
    "artifact_revision_base_version_id",
)

ARTIFACT_REVISION_BINDING_MIGRATION_CHECKSUM = hashlib.sha256(
    b"agent-platform-v10-artifact-revision-binding-v1"
).hexdigest()


def apply_artifact_revision_binding_v1(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
    for name in ARTIFACT_REVISION_BINDING_COLUMNS:
        if name not in columns:
            conn.execute(
                f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )


__all__ = [
    "ARTIFACT_REVISION_BINDING_COLUMNS",
    "ARTIFACT_REVISION_BINDING_MIGRATION_CHECKSUM",
    "apply_artifact_revision_binding_v1",
]
