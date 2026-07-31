#!/usr/bin/env python3
"""SQLite repository for the shadow Goal/Run domain model.

This module deliberately does not import or mutate ``codex_qq_bridge``.  It can
be introduced in shadow mode and reconciled against legacy task dictionaries
before it becomes an authoritative write path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Iterable, Mapping

from bridge_goal_run import (
    TaskProjection,
    goal_status_for_run,
    legacy_goal_id,
    project_legacy_task,
    utc_now,
)
from bridge_migrations import Migration, MigrationDriftError, apply_migrations, applied_migrations


PLATFORM_MIGRATION_NAMESPACE = "agent-platform"

PLATFORM_MIGRATIONS = (
    Migration(
        version=1,
        name="goal_run_shadow_model",
        statements=(
            """
            CREATE TABLE goals (
                id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                conversation_ref TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'draft', 'active', 'waiting_user', 'completed', 'failed', 'cancelled', 'superseded'
                )),
                completion_policy TEXT NOT NULL CHECK(completion_policy IN ('auto', 'user_confirm', 'manual')),
                legacy_root_task_id TEXT NOT NULL DEFAULT '',
                current_run_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE UNIQUE INDEX idx_goals_legacy_root ON goals(legacy_root_task_id) WHERE legacy_root_task_id <> ''",
            "CREATE INDEX idx_goals_status_updated ON goals(status, updated_at DESC)",
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
                legacy_task_id TEXT NOT NULL DEFAULT '',
                source_run_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted'
                )),
                strategy TEXT NOT NULL CHECK(strategy IN ('direct', 'grounded', 'action', 'workflow', 'sandbox')),
                capability_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE UNIQUE INDEX idx_runs_legacy_task ON runs(legacy_task_id) WHERE legacy_task_id <> ''",
            "CREATE INDEX idx_runs_goal_created ON runs(goal_id, created_at DESC)",
            "CREATE INDEX idx_runs_status_updated ON runs(status, updated_at DESC)",
            """
            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                from_status TEXT NOT NULL DEFAULT '',
                to_status TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_run_events_run_id ON run_events(run_id, id)",
            """
            CREATE TABLE evidence (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                source_name TEXT NOT NULL DEFAULT '',
                source_uri TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                retrieved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                excerpt TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_evidence_run_id ON evidence(run_id, retrieved_at DESC)",
            "CREATE INDEX idx_evidence_content_hash ON evidence(content_hash)",
        ),
    ),
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: object) -> object:
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("metadata_json", "input_json", "output_json", "error_json", "payload_json"):
        if key in item:
            item[key.removesuffix("_json")] = _decode_json(item.pop(key))
    return item


def ensure_platform_schema(conn: sqlite3.Connection) -> list[int]:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    applied = applied_migrations(conn, PLATFORM_MIGRATION_NAMESPACE)
    applied_version_set = {m["version"] for m in applied}
    pending = [m for m in PLATFORM_MIGRATIONS if m.version not in applied_version_set]
    # Known versions: v1 shadow model plus structured Task DB migrations.
    known = {m.version for m in PLATFORM_MIGRATIONS} | {2, 3, 4, 5, 6, 7, 8, 9, 10}
    unknown = applied_version_set - known
    if unknown:
        raise MigrationDriftError(
            "database_has_unknown_migrations:" + ",".join(str(v) for v in sorted(unknown))
        )
    if not pending:
        return []
    return apply_migrations(
        conn,
        pending,
        namespace=PLATFORM_MIGRATION_NAMESPACE,
    )


class PlatformRepository:
    """Repository boundary for Goal/Run shadow state."""

    def __init__(self, conn: sqlite3.Connection, *, migrate: bool = True):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        if migrate:
            ensure_platform_schema(self.conn)

    @contextmanager
    def _write(self):
        if self.conn.in_transaction:
            savepoint = f"platform_{uuid.uuid4().hex}"
            self.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _task_lookup_item(
        self,
        task_lookup: Mapping[str, Mapping[str, object]] | None,
        task_id: str,
    ) -> Mapping[str, object] | None:
        return task_lookup.get(task_id) if task_lookup is not None else None

    def _resolve_goal(
        self,
        task: Mapping[str, object],
        task_lookup: Mapping[str, Mapping[str, object]] | None,
    ) -> tuple[str, str]:
        task_id = str(task.get("id") or "").strip()
        existing = self.conn.execute(
            "SELECT goal_id FROM runs WHERE legacy_task_id = ?",
            (task_id,),
        ).fetchone()
        if existing:
            goal_id = str(existing["goal_id"])
            goal = self.conn.execute("SELECT legacy_root_task_id FROM goals WHERE id = ?", (goal_id,)).fetchone()
            return goal_id, str(goal["legacy_root_task_id"] or task_id) if goal else task_id

        root_task_id = task_id
        source_task_id = str(task.get("source_task_id") or "").strip()
        seen = {task_id}
        while source_task_id and source_task_id not in seen:
            seen.add(source_task_id)
            source_run = self.conn.execute(
                "SELECT goal_id FROM runs WHERE legacy_task_id = ?",
                (source_task_id,),
            ).fetchone()
            if source_run:
                goal_id = str(source_run["goal_id"])
                goal = self.conn.execute(
                    "SELECT legacy_root_task_id FROM goals WHERE id = ?",
                    (goal_id,),
                ).fetchone()
                return goal_id, str(goal["legacy_root_task_id"] or source_task_id) if goal else source_task_id
            root_task_id = source_task_id
            source = self._task_lookup_item(task_lookup, source_task_id)
            source_task_id = str(source.get("source_task_id") or "").strip() if source else ""
        if source_task_id in seen:
            root_task_id = min(seen)
        return legacy_goal_id(root_task_id), root_task_id

    def _insert_event(
        self,
        run_id: str,
        event_type: str,
        *,
        from_status: str = "",
        to_status: str = "",
        payload: Mapping[str, object] | None = None,
        created_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO run_events(run_id, event_type, from_status, to_status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, event_type, from_status, to_status, _json(dict(payload or {})), created_at or utc_now()),
        )

    def _upsert_goal_shell(self, projection: TaskProjection) -> None:
        self.conn.execute(
            """
            INSERT INTO goals(
                id, actor_id, channel, conversation_ref, title, status,
                completion_policy, legacy_root_task_id, current_run_id,
                created_at, updated_at, completed_at, version, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, '', 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                actor_id = CASE WHEN goals.actor_id = '' THEN excluded.actor_id ELSE goals.actor_id END,
                channel = CASE WHEN goals.channel = '' THEN excluded.channel ELSE goals.channel END,
                conversation_ref = CASE
                    WHEN goals.conversation_ref = '' THEN excluded.conversation_ref ELSE goals.conversation_ref
                END,
                completion_policy = CASE
                    WHEN goals.completion_policy = 'auto' AND excluded.completion_policy <> 'auto'
                    THEN excluded.completion_policy
                    ELSE goals.completion_policy
                END
            """,
            (
                projection.goal_id,
                projection.actor_id,
                projection.channel,
                projection.conversation_ref,
                projection.title,
                projection.goal_status,
                projection.completion_policy,
                projection.root_task_id,
                projection.created_at,
                projection.updated_at,
                _json({"projection": "legacy_task_v1"}),
            ),
        )

    def _upsert_run(self, projection: TaskProjection) -> tuple[bool, str]:
        existing = self.conn.execute("SELECT * FROM runs WHERE id = ?", (projection.run_id,)).fetchone()
        values = {
            "goal_id": projection.goal_id,
            "legacy_task_id": projection.legacy_task_id,
            "source_run_id": projection.source_run_id,
            "status": projection.run_status,
            "strategy": projection.strategy,
            "capability_id": projection.capability_id,
            "summary": projection.summary,
            "input_json": _json(projection.input_data),
            "output_json": _json(projection.output_data),
            "error_json": _json(projection.error_data),
            "created_at": projection.created_at,
            "updated_at": projection.updated_at,
            "started_at": projection.started_at,
            "finished_at": projection.finished_at,
            "metadata_json": _json(projection.metadata),
        }
        if not existing:
            columns = tuple(values)
            self.conn.execute(
                f"INSERT INTO runs(id, {', '.join(columns)}, version) VALUES (?, {', '.join('?' for _ in columns)}, 1)",
                (projection.run_id, *(values[column] for column in columns)),
            )
            return True, ""

        previous_status = str(existing["status"])
        changed = any(existing[key] != value for key, value in values.items())
        if changed:
            assignments = ", ".join(f"{key} = ?" for key in values)
            self.conn.execute(
                f"UPDATE runs SET {assignments}, version = version + 1 WHERE id = ?",
                (*(values[key] for key in values), projection.run_id),
            )
        return changed, previous_status

    def _refresh_goal(self, goal_id: str) -> None:
        latest = self.conn.execute(
            """
            SELECT * FROM runs
            WHERE goal_id = ?
            ORDER BY created_at DESC, updated_at DESC, id DESC
            LIMIT 1
            """,
            (goal_id,),
        ).fetchone()
        if not latest:
            return
        goal = self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if not goal:
            return
        completion_policy = str(goal["completion_policy"])
        status = goal_status_for_run(str(latest["status"]), completion_policy)
        completed_at = str(latest["finished_at"] or latest["updated_at"]) if status == "completed" else ""
        values = {
            "status": status,
            "current_run_id": str(latest["id"]),
            "updated_at": str(latest["updated_at"]),
            "completed_at": completed_at,
        }
        if any(goal[key] != value for key, value in values.items()):
            self.conn.execute(
                """
                UPDATE goals
                SET status = ?, current_run_id = ?, updated_at = ?, completed_at = ?, version = version + 1
                WHERE id = ?
                """,
                (status, values["current_run_id"], values["updated_at"], completed_at, goal_id),
            )

    def _sync_evidence(self, projection: TaskProjection, task: Mapping[str, object]) -> None:
        items = task.get("evidence")
        if not isinstance(items, list):
            return
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            source_uri = str(raw.get("source_uri") or raw.get("url") or "").strip()
            excerpt = str(raw.get("excerpt") or raw.get("content") or "")[:50000]
            content_hash = str(raw.get("content_hash") or "").strip()
            if not content_hash:
                content_hash = hashlib.sha256(f"{source_uri}\n{excerpt}".encode("utf-8")).hexdigest()
            external_id = str(raw.get("id") or content_hash)
            evidence_key = hashlib.sha256(
                f"{projection.run_id}:{external_id}".encode("utf-8"),
            ).hexdigest()[:24]
            evidence_id = f"ev_{evidence_key}"
            now = str(raw.get("retrieved_at") or projection.updated_at or utc_now())
            self.conn.execute(
                """
                INSERT INTO evidence(
                    id, run_id, source_name, source_uri, published_at, retrieved_at,
                    expires_at, content_hash, excerpt, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_name = excluded.source_name,
                    source_uri = excluded.source_uri,
                    published_at = excluded.published_at,
                    retrieved_at = excluded.retrieved_at,
                    expires_at = excluded.expires_at,
                    content_hash = excluded.content_hash,
                    excerpt = excluded.excerpt,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    evidence_id,
                    projection.run_id,
                    str(raw.get("source_name") or raw.get("source") or "")[:300],
                    source_uri[:4000],
                    str(raw.get("published_at") or "")[:80],
                    now,
                    str(raw.get("expires_at") or "")[:80],
                    content_hash,
                    excerpt,
                    _json(dict(raw.get("metadata") or {})) if isinstance(raw.get("metadata"), Mapping) else "{}",
                    now,
                    now,
                ),
            )

    def sync_task(
        self,
        task: Mapping[str, object],
        *,
        task_lookup: Mapping[str, Mapping[str, object]] | None = None,
    ) -> dict:
        with self._write():
            goal_id, root_task_id = self._resolve_goal(task, task_lookup)
            projection = project_legacy_task(task, root_task_id=root_task_id, goal_id=goal_id)
            self._upsert_goal_shell(projection)
            changed, previous_status = self._upsert_run(projection)
            if not previous_status:
                self._insert_event(
                    projection.run_id,
                    "run.projected",
                    to_status=projection.run_status,
                    payload={"legacy_task_id": projection.legacy_task_id},
                    created_at=projection.updated_at,
                )
            elif changed and previous_status != projection.run_status:
                self._insert_event(
                    projection.run_id,
                    "run.status_changed",
                    from_status=previous_status,
                    to_status=projection.run_status,
                    payload={"legacy_task_id": projection.legacy_task_id},
                    created_at=projection.updated_at,
                )
            self._sync_evidence(projection, task)
            self._refresh_goal(projection.goal_id)
        return {
            "projection": projection.as_dict(),
            "goal": self.get_goal(projection.goal_id),
            "run": self.get_run(projection.run_id),
            "changed": changed,
        }

    @staticmethod
    def _expected_root_task_id(
        task: Mapping[str, object],
        lookup: Mapping[str, Mapping[str, object]],
    ) -> str:
        task_id = str(task.get("id") or "").strip()
        root_task_id = task_id
        source_task_id = str(task.get("source_task_id") or "").strip()
        seen = {task_id}
        while source_task_id and source_task_id not in seen:
            seen.add(source_task_id)
            root_task_id = source_task_id
            source = lookup.get(source_task_id)
            source_task_id = str(source.get("source_task_id") or "").strip() if source else ""
        if source_task_id in seen:
            return min(seen)
        return root_task_id

    @staticmethod
    def _task_depth(task: Mapping[str, object], lookup: Mapping[str, Mapping[str, object]]) -> int:
        depth = 0
        source_id = str(task.get("source_task_id") or "").strip()
        seen: set[str] = set()
        while source_id and source_id not in seen and source_id in lookup:
            seen.add(source_id)
            depth += 1
            source_id = str(lookup[source_id].get("source_task_id") or "").strip()
        return depth

    def sync_tasks(self, tasks: Iterable[Mapping[str, object]]) -> list[dict]:
        items = [dict(task) for task in tasks]
        lookup = {str(item.get("id") or "").strip(): item for item in items if str(item.get("id") or "").strip()}
        ordered = sorted(items, key=lambda item: (self._task_depth(item, lookup), str(item.get("created_at") or "")))
        return [self.sync_task(item, task_lookup=lookup) for item in ordered]

    def get_goal(self, goal_id: str) -> dict | None:
        return _row_dict(self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())

    def get_run(self, run_id: str) -> dict | None:
        return _row_dict(self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def list_goals(self, *, status: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        params: list[object] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status)
        params.extend((max(1, min(int(limit), 200)), max(0, int(offset))))
        rows = self.conn.execute(
            f"SELECT * FROM goals {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [_row_dict(row) for row in rows if row is not None]

    def list_runs(
        self,
        *,
        goal_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if goal_id:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend((max(1, min(int(limit), 200)), max(0, int(offset))))
        rows = self.conn.execute(
            f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [_row_dict(row) for row in rows if row is not None]

    def list_run_events(self, run_id: str, *, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY id DESC LIMIT ?",
            (run_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [_row_dict(row) for row in rows if row is not None]

    def list_evidence(self, run_id: str, *, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE run_id = ? ORDER BY retrieved_at DESC LIMIT ?",
            (run_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [_row_dict(row) for row in rows if row is not None]

    def overview(self) -> dict:
        def counts(table: str) -> dict[str, int]:
            rows = self.conn.execute(f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status").fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

        goal_counts = counts("goals")
        run_counts = counts("runs")
        evidence_count = int(self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
        event_count = int(self.conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0])
        return {
            "ok": True,
            "goals": {"total": sum(goal_counts.values()), "counts": goal_counts},
            "runs": {"total": sum(run_counts.values()), "counts": run_counts},
            "run_events": event_count,
            "evidence": evidence_count,
        }

    def reconcile_tasks(self, tasks: Iterable[Mapping[str, object]]) -> dict:
        items = [dict(task) for task in tasks]
        lookup = {str(item.get("id") or "").strip(): item for item in items if str(item.get("id") or "").strip()}
        expected_ids = set(lookup)
        missing: list[str] = []
        mismatches: list[dict] = []
        for task in items:
            task_id = str(task.get("id") or "").strip()
            root_task_id = self._expected_root_task_id(task, lookup)
            goal_id = legacy_goal_id(root_task_id)
            projection = project_legacy_task(task, root_task_id=root_task_id, goal_id=goal_id)
            row = self.conn.execute("SELECT * FROM runs WHERE legacy_task_id = ?", (task_id,)).fetchone()
            if not row:
                missing.append(task_id)
                continue
            for field, expected in (
                ("goal_id", projection.goal_id),
                ("status", projection.run_status),
                ("strategy", projection.strategy),
                ("source_run_id", projection.source_run_id),
            ):
                if row[field] != expected:
                    mismatches.append(
                        {"task_id": task_id, "field": field, "expected": expected, "actual": row[field]},
                    )
        orphan_rows = self.conn.execute(
            "SELECT legacy_task_id FROM runs WHERE legacy_task_id <> ''",
        ).fetchall()
        orphans = sorted(str(row["legacy_task_id"]) for row in orphan_rows if str(row["legacy_task_id"]) not in expected_ids)
        return {
            "ok": not missing and not mismatches and not orphans,
            "checked": len(items),
            "missing_runs": sorted(missing),
            "mismatches": mismatches,
            "orphan_runs": orphans,
        }


def sync_task_projection(
    conn: sqlite3.Connection,
    task: Mapping[str, object],
    *,
    task_lookup: Mapping[str, Mapping[str, object]] | None = None,
) -> dict:
    return PlatformRepository(conn).sync_task(task, task_lookup=task_lookup)


def platform_overview(conn: sqlite3.Connection) -> dict:
    return PlatformRepository(conn).overview()


def list_platform_goals(conn: sqlite3.Connection, **filters) -> list[dict]:
    return PlatformRepository(conn).list_goals(**filters)


def list_platform_runs(conn: sqlite3.Connection, **filters) -> list[dict]:
    return PlatformRepository(conn).list_runs(**filters)


def reconcile_task_projections(conn: sqlite3.Connection, tasks: Iterable[Mapping[str, object]]) -> dict:
    return PlatformRepository(conn).reconcile_tasks(tasks)
