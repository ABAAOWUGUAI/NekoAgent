#!/usr/bin/env python3
"""Small, transactional SQLite migration runner for the agent platform.

The existing bridge owns more than one SQLite database.  Migrations are
therefore namespaced so the same runner can safely be reused by each database.
The runner intentionally does not use ``executescript`` because that API may
commit implicitly and would weaken the all-or-nothing guarantee.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence


MigrationAction = Callable[[sqlite3.Connection], None]


class MigrationError(RuntimeError):
    """Base error for migration validation or execution failures."""


class MigrationDriftError(MigrationError):
    """Raised when an applied migration no longer matches its definition."""


@dataclass(frozen=True)
class Migration:
    """One ordered schema migration.

    SQL statements are preferred because their checksum can be derived.  A
    callable is supported for data migrations, but it must provide an explicit
    stable ``checksum``.
    """

    version: int
    name: str
    statements: Sequence[str] = ()
    apply: MigrationAction | None = None
    checksum: str = ""

    def resolved_checksum(self) -> str:
        if self.checksum:
            return self.checksum
        if self.apply is not None:
            raise MigrationError(f"migration_checksum_required:{self.version}")
        payload = "\n-- statement --\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_after(previous: str) -> str:
    """Return an ISO timestamp that is strictly newer than an optimistic-lock value."""

    current = datetime.now(timezone.utc)
    try:
        previous_value = datetime.fromisoformat(str(previous or ""))
        if previous_value.tzinfo is None:
            previous_value = previous_value.replace(tzinfo=timezone.utc)
        if current <= previous_value:
            current = previous_value + timedelta(microseconds=1)
    except ValueError:
        pass
    return current.isoformat()


def _validate_namespace(namespace: str) -> str:
    value = str(namespace or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value):
        raise MigrationError("invalid_migration_namespace")
    return value


def _ordered_migrations(migrations: Iterable[Migration]) -> list[Migration]:
    ordered = sorted(migrations, key=lambda item: item.version)
    versions: set[int] = set()
    for migration in ordered:
        if migration.version <= 0:
            raise MigrationError(f"invalid_migration_version:{migration.version}")
        if migration.version in versions:
            raise MigrationError(f"duplicate_migration_version:{migration.version}")
        if not str(migration.name or "").strip():
            raise MigrationError(f"migration_name_required:{migration.version}")
        if not migration.statements and migration.apply is None:
            raise MigrationError(f"migration_action_required:{migration.version}")
        migration.resolved_checksum()
        versions.add(migration.version)
    return ordered


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            namespace TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY(namespace, version)
        )
        """,
    )


def applied_migrations(conn: sqlite3.Connection, namespace: str) -> list[dict]:
    """Return applied migrations for a namespace.

    An unmigrated database returns an empty list without mutating it.
    """

    namespace = _validate_namespace(namespace)
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'",
    ).fetchone()
    if not table:
        return []
    rows = conn.execute(
        """
        SELECT namespace, version, name, checksum, applied_at
        FROM schema_migrations
        WHERE namespace = ?
        ORDER BY version
        """,
        (namespace,),
    ).fetchall()
    columns = ("namespace", "version", "name", "checksum", "applied_at")
    return [dict(zip(columns, tuple(row))) for row in rows]


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Iterable[Migration],
    *,
    namespace: str,
) -> list[int]:
    """Apply all pending migrations in one ``BEGIN IMMEDIATE`` transaction.

    Returning an empty list means the schema was already current.  Migration
    drift and execution failures are explicit; callers must not continue with
    a partially understood schema.
    """

    namespace = _validate_namespace(namespace)
    ordered = _ordered_migrations(migrations)
    if conn.in_transaction:
        raise MigrationError("connection_already_in_transaction")

    applied_versions: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_migration_table(conn)
        rows = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations WHERE namespace = ?",
            (namespace,),
        ).fetchall()
        existing = {
            int(row[0]): {"name": str(row[1]), "checksum": str(row[2])}
            for row in rows
        }

        known_versions = {item.version for item in ordered}
        unknown = sorted(set(existing) - known_versions)
        if unknown:
            raise MigrationDriftError(
                "database_has_unknown_migrations:" + ",".join(str(version) for version in unknown),
            )

        for migration in ordered:
            checksum = migration.resolved_checksum()
            previous = existing.get(migration.version)
            if previous:
                if previous["name"] != migration.name or previous["checksum"] != checksum:
                    raise MigrationDriftError(f"migration_drift:{migration.version}")
                continue

            for statement in migration.statements:
                sql = statement.strip()
                if sql:
                    conn.execute(sql)
            if migration.apply is not None:
                migration.apply(conn)
            conn.execute(
                """
                INSERT INTO schema_migrations(namespace, version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (namespace, migration.version, migration.name, checksum, utc_now()),
            )
            applied_versions.append(migration.version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied_versions


# === Migration v2: executor_snapshot ===

def _migration_v2_executor_snapshot() -> Migration:
    """Tasks 表新增 6 列执行器快照字段；回填历史未完成任务为原生 Codex 执行器。"""

    # 幂等 ALTER TABLE — 每列单独检查
    add_col_statements = []
    for name in (
        "executor_provider_id",
        "executor_model_id",
        "executor_model_name",
        "executor_adapter",
        "executor_config_version",
        "executor_profile_sha256",
    ):
        add_col_statements.append(
            f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
        )

    backfill = (
        "UPDATE tasks SET "
        "executor_provider_id='codex-login', "
        "executor_model_id='codex-default', "
        "executor_adapter='codex_login', "
        "executor_config_version='legacy-migration-v2' "
        "WHERE executor_adapter='' AND (status='queued' OR status='running')"
    )

    def _apply(conn: sqlite3.Connection) -> None:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for name in (
            "executor_provider_id",
            "executor_model_id",
            "executor_model_name",
            "executor_adapter",
            "executor_config_version",
            "executor_profile_sha256",
        ):
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        conn.execute(backfill)

    # checksum over the logical content
    content = "\n".join(add_col_statements) + "\n" + backfill
    return Migration(
        version=2,
        name="executor_snapshot",
        apply=_apply,
        checksum="eeb33e3536eaf1976ef0be5bed4c0fbfe3fadfbb2e89aef3982f3b51a978c48b",
    )


def _migration_v3_formal_approval() -> Migration:
    """Task/Run-side durable formal Approval records."""

    from bridge_formal_approval_schema import (
        FORMAL_APPROVAL_MIGRATION_CHECKSUM,
        apply_formal_approval_v2,
    )

    return Migration(
        version=3,
        name="formal_task_approval",
        apply=apply_formal_approval_v2,
        checksum=FORMAL_APPROVAL_MIGRATION_CHECKSUM,
    )


def _migration_v4_artifact_preview() -> Migration:
    """Immutable Artifact versions and isolated preview authorization."""

    from bridge_artifact_schema import ARTIFACT_MIGRATION_CHECKSUM, apply_artifact_preview_v2

    return Migration(
        version=4,
        name="artifact_static_preview",
        apply=apply_artifact_preview_v2,
        checksum=ARTIFACT_MIGRATION_CHECKSUM,
    )


def _migration_v5_project_task_lookup() -> Migration:
    """Index the stable task working-directory association used by Project lifecycle."""

    def _apply(conn: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
        if {"cwd", "created_at"}.issubset(columns):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_cwd_created ON tasks(cwd,created_at DESC,id)",
            )

    return Migration(
        version=5,
        name="project_task_lookup",
        apply=_apply,
        checksum="20d6c286165e54c183dd4b5a90a2658d12347fea0a6a1535dcb57c4321d26a1f",
    )


def _migration_v6_delivery_continuity() -> Migration:
    """Extend the existing Outbox; never create a parallel delivery queue."""

    from bridge_delivery_continuity_schema import (
        DELIVERY_CONTINUITY_MIGRATION_CHECKSUM,
        apply_delivery_continuity_v1,
    )

    return Migration(
        version=6,
        name="delivery_continuity_single_owner",
        apply=apply_delivery_continuity_v1,
        checksum=DELIVERY_CONTINUITY_MIGRATION_CHECKSUM,
    )


def _migration_v7_goal_continuity() -> Migration:
    """Goal revisions, durable run checkpoints and user feedback."""

    from bridge_goal_continuity_schema import (
        GOAL_CONTINUITY_MIGRATION_CHECKSUM,
        apply_goal_continuity_v1,
    )

    return Migration(
        version=7,
        name="goal_continuity_revision_feedback",
        apply=apply_goal_continuity_v1,
        checksum=GOAL_CONTINUITY_MIGRATION_CHECKSUM,
    )


def _migration_v8_goal_channel_scope() -> Migration:
    """Repair legacy QQ group Goals so private and group work cannot share a scope."""

    def _apply(conn: sqlite3.Connection) -> None:
        task_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
        required = {"id", "source", "delivery_recipient_id"}
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(task_columns) or not {"goals", "runs"}.issubset(tables):
            return
        rows = conn.execute(
            """SELECT DISTINCT r.goal_id,t.delivery_recipient_id
            FROM runs r JOIN tasks t ON t.id=r.legacy_task_id
            WHERE t.source='qq' AND t.delivery_recipient_id LIKE 'group:%'""",
        ).fetchall()
        for goal_id, conversation_ref in rows:
            conn.execute(
                "UPDATE goals SET channel='qq_group',conversation_ref=?,version=version+1 WHERE id=?",
                (str(conversation_ref), str(goal_id)),
            )

    return Migration(
        version=8,
        name="goal_channel_scope_repair",
        apply=_apply,
        checksum=hashlib.sha256(b"agent-platform-v8-goal-channel-scope-repair-v1").hexdigest(),
    )


def _migration_v9_goal_revision_run_link() -> Migration:
    """Bind every Run to the Goal Revision that it implements."""

    from bridge_goal_revision_link_schema import (
        GOAL_REVISION_LINK_MIGRATION_CHECKSUM,
        apply_goal_revision_link_v1,
    )

    return Migration(
        version=9,
        name="goal_revision_run_link",
        apply=apply_goal_revision_link_v1,
        checksum=GOAL_REVISION_LINK_MIGRATION_CHECKSUM,
    )


def _migration_v10_artifact_revision_binding() -> Migration:
    """Persist the Artifact and base Version selected by a revision request."""

    from bridge_artifact_revision_schema import (
        ARTIFACT_REVISION_BINDING_MIGRATION_CHECKSUM,
        apply_artifact_revision_binding_v1,
    )

    return Migration(
        version=10,
        name="artifact_revision_binding",
        apply=apply_artifact_revision_binding_v1,
        checksum=ARTIFACT_REVISION_BINDING_MIGRATION_CHECKSUM,
    )


def _agent_platform_migrations() -> list[Migration]:
    """Build the registry lazily so schema modules can import migration errors.

    ``bridge_formal_approval_schema`` needs ``MigrationDriftError`` from this
    module.  Constructing its migration while this module is still importing
    would otherwise create an order-dependent circular import for standalone
    cutover tools.
    """

    # v1 is already applied and therefore is not part of this registry.
    return [
        _migration_v2_executor_snapshot(),
        _migration_v3_formal_approval(),
        _migration_v4_artifact_preview(),
        _migration_v5_project_task_lookup(),
        _migration_v6_delivery_continuity(),
        _migration_v7_goal_continuity(),
        _migration_v8_goal_channel_scope(),
        _migration_v9_goal_revision_run_link(),
        _migration_v10_artifact_revision_binding(),
    ]


def _ensure_namespace_known(conn: sqlite3.Connection, namespace: str, known_versions: set[int]) -> None:
    """Verify that applied migrations are all known."""
    applied = applied_migrations(conn, namespace)
    applied_versions = {m["version"] for m in applied}
    unknown = applied_versions - known_versions
    if unknown:
        raise MigrationDriftError(
            "database_has_unknown_migrations:" + ",".join(str(v) for v in sorted(unknown))
        )


def ensure_agent_platform_migrations(conn: sqlite3.Connection) -> None:
    """Bridge 启动时调用；确保 agent-platform namespace 迁移已执行。

    v1 (goal_run_shadow_model) was applied by the legacy path.
    v2+ uses the structured Migration framework.
    """
    namespace = "agent-platform"
    _ensure_migration_table(conn)

    # All versions we know about (v1 is pre-existing, v2+ from our registry)
    LEGACY_KNOWN = {1}
    migrations = _agent_platform_migrations()
    known = LEGACY_KNOWN | {m.version for m in migrations}
    _ensure_namespace_known(conn, namespace, known)

    applied_versions = {m["version"] for m in applied_migrations(conn, namespace)}
    pending = [m for m in migrations if m.version not in applied_versions]

    for migration in sorted(pending, key=lambda m: m.version):
        checksum = migration.resolved_checksum()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                sql = statement.strip()
                if sql:
                    conn.execute(sql)
            if migration.apply is not None:
                migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations(namespace,version,name,checksum,applied_at) VALUES(?,?,?,?,?)",
                (namespace, migration.version, migration.name, checksum, utc_now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
