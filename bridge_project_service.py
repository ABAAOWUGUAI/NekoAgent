#!/usr/bin/env python3
"""Project lifecycle repository/service for Product Gate C8."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from bridge_migrations import utc_after, utc_now
from bridge_project_schema import require_project_lifecycle_schema


class ProjectError(ValueError):
    """Stable Project lifecycle error."""


def _row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "path": str(row["path"]),
        "description": str(row["description"] or ""),
        "status": "active" if bool(row["active"]) else "archived",
        "active": bool(row["active"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "archived_at": str(row["archived_at"] or ""),
        "lifecycle_version": int(row["lifecycle_version"] or 1),
    }


class ProjectService:
    def __init__(
        self,
        assistant_connect: Callable[[], sqlite3.Connection],
        task_connect: Callable[[], sqlite3.Connection],
        *,
        workspace_base: Callable[[], Path],
        allowed_roots: Callable[[], tuple[Path, ...]],
        slugify: Callable[[str], str],
        ensure_codegraph: Callable[..., dict],
    ) -> None:
        self._assistant_connect = assistant_connect
        self._task_connect = task_connect
        self._workspace_base = workspace_base
        self._allowed_roots = allowed_roots
        self._slugify = slugify
        self._ensure_codegraph = ensure_codegraph

    @staticmethod
    def _validate_name(value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ProjectError("project_name_required")
        if len(name) > 80:
            raise ProjectError("project_name_too_long")
        return name

    @staticmethod
    def _validate_description(value: str) -> str:
        description = str(value or "").strip()
        if len(description) > 2000:
            raise ProjectError("project_description_too_long")
        return description

    def _resolve_path(self, raw: str, project_id: str) -> Path:
        if len(str(raw or "")) > 4096:
            raise ProjectError("project_path_too_long")
        workspace_base = Path(self._workspace_base())
        candidate = Path(raw).expanduser() if str(raw or "").strip() else workspace_base / project_id
        if not candidate.is_absolute():
            candidate = workspace_base / candidate
        resolved = candidate.resolve()
        if not any(resolved == root or root in resolved.parents for root in self._allowed_roots()):
            raise ProjectError("project_path_outside_allowed_roots")
        return resolved

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        project_id: str,
        event_type: str,
        *,
        previous_name: str = "",
        new_name: str = "",
        actor_type: str = "admin",
    ) -> None:
        conn.execute(
            """
            INSERT INTO project_lifecycle_events(
              project_id,event_type,actor_type,previous_name,new_name,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (project_id, event_type, actor_type[:40], previous_name, new_name, utc_now()),
        )

    @staticmethod
    def _write_agent_file(path: Path, name: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        agent_file = path / "AGENTS.md"
        if agent_file.exists():
            return
        agent_file.write_text(
            "\n".join(
                (
                    "# Project instructions",
                    "",
                    f"- Project name: {name}",
                    "- Default language: Chinese.",
                    "- Before broad code edits, inspect the existing structure and use CodeGraph when `.codegraph/` exists.",
                    "- Keep changes focused, run relevant checks, and report validation results.",
                    "",
                )
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _fetch(conn: sqlite3.Connection, project_id: str, *, include_archived: bool = True) -> dict | None:
        clause = "" if include_archived else " AND active=1"
        row = conn.execute(
            f"SELECT * FROM projects WHERE id=?{clause}",
            (project_id,),
        ).fetchone()
        return _row(row)

    @staticmethod
    def _current_id(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='current_project_id'",
        ).fetchone()
        return str(row[0]) if row else ""

    def _task_summary(self, path: str) -> dict:
        try:
            with self._task_connect() as conn:
                rows = conn.execute(
                    "SELECT status,count(*) FROM tasks WHERE cwd=? GROUP BY status",
                    (path,),
                ).fetchall()
            counts = {str(row[0] or "unknown"): int(row[1]) for row in rows}
            return {
                "available": True,
                "total": sum(counts.values()),
                "active": counts.get("queued", 0) + counts.get("running", 0),
                "counts": counts,
            }
        except sqlite3.Error:
            return {"available": False, "total": 0, "active": 0, "counts": {}}

    def list_projects(self, *, include_archived: bool = False) -> dict:
        with self._assistant_connect() as conn:
            require_project_lifecycle_schema(conn)
            where = "" if include_archived else "WHERE active=1"
            rows = conn.execute(
                f"SELECT * FROM projects {where} ORDER BY active DESC,updated_at DESC,name ASC",
            ).fetchall()
            current_id = self._current_id(conn)
        projects = []
        current = None
        for item_row in rows:
            item = _row(item_row)
            if not item:
                continue
            item["is_current"] = item["active"] and item["id"] == current_id
            item["task_summary"] = self._task_summary(item["path"])
            projects.append(item)
            if item["is_current"]:
                current = item
        return {"ok": True, "current": current, "project": current, "projects": projects}

    def create(
        self,
        name: str,
        path: str = "",
        description: str = "",
        *,
        make_current: bool = True,
        actor_type: str = "admin",
    ) -> dict:
        name = self._validate_name(name)
        description = self._validate_description(description)
        project_id = self._slugify(name)
        resolved = self._resolve_path(path, project_id)
        now = utc_now()
        with self._assistant_connect() as conn:
            require_project_lifecycle_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conflict = conn.execute(
                    """
                    SELECT id,name,path FROM projects
                    WHERE id=? OR lower(name)=lower(?) OR path=? LIMIT 1
                    """,
                    (project_id, name, str(resolved)),
                ).fetchone()
                if conflict:
                    if str(conflict["path"]) == str(resolved) and str(conflict["id"]) != project_id:
                        raise ProjectError("project_path_already_registered")
                    raise ProjectError("project_already_exists")
                conn.execute(
                    """
                    INSERT INTO projects(
                      id,name,path,description,active,created_at,updated_at,archived_at,lifecycle_version
                    ) VALUES(?,?,?,?,1,?,?, '',1)
                    """,
                    (project_id, name, str(resolved), description, now, now),
                )
                self._write_agent_file(resolved, name)
                self._event(conn, project_id, "created", new_name=name, actor_type=actor_type)
                if make_current:
                    conn.execute(
                        """
                        INSERT INTO settings(key,value,updated_at)
                        VALUES('current_project_id',?,?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                        """,
                        (project_id, now),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        project = self.get(project_id)
        if project:
            project["codegraph"] = self._ensure_codegraph(resolved, phase="project-create", force=True)
        return project or {}

    def get(self, project_id: str, *, include_archived: bool = True) -> dict | None:
        with self._assistant_connect() as conn:
            project = self._fetch(conn, str(project_id or "").strip(), include_archived=include_archived)
            current_id = self._current_id(conn)
        if project:
            project["is_current"] = project["active"] and project["id"] == current_id
            project["task_summary"] = self._task_summary(project["path"])
        return project

    def set_current(self, project_id: str) -> dict:
        with self._assistant_connect() as conn:
            project = self._fetch(conn, str(project_id or "").strip(), include_archived=False)
            if not project:
                raise ProjectError("project_not_found")
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('current_project_id',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (project["id"], utc_now()),
            )
        project["is_current"] = True
        project["task_summary"] = self._task_summary(project["path"])
        project["codegraph"] = self._ensure_codegraph(
            Path(project["path"]), phase="project-switch", force=True,
        )
        return project

    def rename(
        self,
        project_id: str,
        *,
        name: str,
        description: str,
        expected_updated_at: str,
        actor_type: str = "admin",
    ) -> dict:
        name = self._validate_name(name)
        description = self._validate_description(description)
        expected = str(expected_updated_at or "").strip()
        if not expected:
            raise ProjectError("project_expected_updated_at_required")
        with self._assistant_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._fetch(conn, project_id)
                if not current:
                    raise ProjectError("project_not_found")
                if current["updated_at"] != expected:
                    raise ProjectError("project_stale")
                conflict = conn.execute(
                    "SELECT id FROM projects WHERE lower(name)=lower(?) AND id<>? LIMIT 1",
                    (name, project_id),
                ).fetchone()
                if conflict:
                    raise ProjectError("project_name_already_exists")
                updated_at = utc_after(current["updated_at"])
                conn.execute(
                    """
                    UPDATE projects SET name=?,description=?,updated_at=?,lifecycle_version=lifecycle_version+1
                    WHERE id=?
                    """,
                    (name, description, updated_at, project_id),
                )
                self._event(
                    conn, project_id, "renamed", previous_name=current["name"],
                    new_name=name, actor_type=actor_type,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get(project_id) or {}

    def archive(
        self,
        project_id: str,
        *,
        expected_updated_at: str,
        actor_type: str = "admin",
    ) -> dict:
        expected = str(expected_updated_at or "").strip()
        if not expected:
            raise ProjectError("project_expected_updated_at_required")
        with self._assistant_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._fetch(conn, project_id)
                if not current:
                    raise ProjectError("project_not_found")
                if not current["active"]:
                    raise ProjectError("project_already_archived")
                if self._current_id(conn) == project_id:
                    raise ProjectError("project_current_archive_forbidden")
                if current["updated_at"] != expected:
                    raise ProjectError("project_stale")
                now = utc_after(current["updated_at"])
                conn.execute(
                    """
                    UPDATE projects SET active=0,archived_at=?,updated_at=?,
                      lifecycle_version=lifecycle_version+1 WHERE id=?
                    """,
                    (now, now, project_id),
                )
                self._event(conn, project_id, "archived", previous_name=current["name"], actor_type=actor_type)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get(project_id) or {}

    def restore(
        self,
        project_id: str,
        *,
        expected_updated_at: str,
        actor_type: str = "admin",
    ) -> dict:
        expected = str(expected_updated_at or "").strip()
        if not expected:
            raise ProjectError("project_expected_updated_at_required")
        with self._assistant_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._fetch(conn, project_id)
                if not current:
                    raise ProjectError("project_not_found")
                if current["active"]:
                    raise ProjectError("project_already_active")
                if current["updated_at"] != expected:
                    raise ProjectError("project_stale")
                now = utc_after(current["updated_at"])
                conn.execute(
                    """
                    UPDATE projects SET active=1,archived_at='',updated_at=?,
                      lifecycle_version=lifecycle_version+1 WHERE id=?
                    """,
                    (now, project_id),
                )
                self._event(conn, project_id, "restored", new_name=current["name"], actor_type=actor_type)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get(project_id) or {}

    def recent_tasks(self, project_id: str, *, limit: int = 5) -> dict:
        project = self.get(project_id)
        if not project:
            raise ProjectError("project_not_found")
        safe_limit = max(1, min(int(limit or 5), 20))
        with self._task_connect() as conn:
            rows = conn.execute(
                """
                SELECT id,status,summary,source,created_at,finished_at
                FROM tasks WHERE cwd=?
                ORDER BY COALESCE(created_at,updated_at) DESC,id DESC LIMIT ?
                """,
                (project["path"], safe_limit),
            ).fetchall()
        tasks = [
            {
                "id": str(row["id"]), "status": str(row["status"] or "unknown"),
                "summary": str(row["summary"] or "")[:240],
                "source": str(row["source"] or ""),
                "created_at": str(row["created_at"] or ""),
                "finished_at": str(row["finished_at"] or ""),
            }
            for row in rows
        ]
        return {"ok": True, "project": project, "tasks": tasks, "limit": safe_limit}


__all__ = ["ProjectError", "ProjectService"]
