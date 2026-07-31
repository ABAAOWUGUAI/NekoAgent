#!/usr/bin/env python3
"""HTTP/runtime adapter for Gate C2 QQ object authorization."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from bridge_auth import PrincipalKind
from bridge_qq_object_service import (
    GLOBAL_OBJECT_ROLES,
    GLOBAL_TASK_ROLES,
    actor_project_path,
    actor_projects,
    bind_actor_project,
    claim_actor_project,
    memory_allowed,
    normalize_qq_id,
    project_id_exists,
    qq_actor_role,
    qq_object_cutover_plan,
    qq_object_feature_enabled,
    set_qq_object_feature,
    task_allowed,
)


class QqObjectRuntime:
    def __init__(
        self,
        assistant_db_connect: Callable,
        task_db_connect: Callable,
        json_response: Callable,
        *,
        row_to_task: Callable,
        public_task: Callable,
        get_task: Callable,
        task_stats: Callable,
        list_tasks: Callable,
        cancel_task: Callable,
        retry_task: Callable,
        create_project: Callable,
        list_projects: Callable,
        current_project: Callable,
        set_current_project: Callable,
        slugify: Callable,
        list_memories: Callable,
        add_memory: Callable,
        delete_memory: Callable,
        channel_token_distinct: Callable[[], bool],
    ) -> None:
        self._assistant_db_connect = assistant_db_connect
        self._task_db_connect = task_db_connect
        self._json_response = json_response
        self._row_to_task = row_to_task
        self._public_task = public_task
        self._get_task = get_task
        self._task_stats_legacy = task_stats
        self._list_tasks_legacy = list_tasks
        self._cancel_task = cancel_task
        self._retry_task = retry_task
        self._create_project = create_project
        self._list_projects = list_projects
        self._current_project = current_project
        self._set_current_project = set_current_project
        self._slugify = slugify
        self._list_memories = list_memories
        self._add_memory = add_memory
        self._delete_memory = delete_memory
        self._channel_token_distinct = channel_token_distinct

    def _failure(self, request, exc: Exception) -> bool:
        message = str(exc) or type(exc).__name__
        if isinstance(exc, PermissionError) or "forbidden" in message:
            status = 403
        elif "not_found" in message:
            status = 404
        elif "required" in message or "missing" in message:
            status = 409
        elif "stale" in message or "already" in message:
            status = 409
        else:
            status = 400
        self._json_response(request, status, {"ok": False, "error": message})
        return True

    def _enabled(self) -> bool:
        with self._assistant_db_connect() as conn:
            return qq_object_feature_enabled(conn)

    def _actor(self, request) -> tuple[str, str, str]:
        actor = normalize_qq_id(request.headers.get("X-QQ-Actor-ID"))
        raw_group = str(request.headers.get("X-QQ-Group-ID") or "").strip()
        group = normalize_qq_id(raw_group, "qq_group_header_invalid") if raw_group else ""
        with self._assistant_db_connect() as conn:
            role = qq_actor_role(conn, actor, group=bool(group))
        if not role:
            raise PermissionError("qq_actor_forbidden")
        return actor, role, group

    @staticmethod
    def _query_value(query: dict, key: str, default: str = "") -> str:
        return str(query.get(key, [default])[0] or default).strip()

    def _task_row(self, task_id: str) -> dict | None:
        task = self._get_task(task_id)
        if task:
            return task
        with self._task_db_connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def _tasks(self, actor: str, role: str, *, status: str, limit: int, offset: int = 0) -> list[dict]:
        where, params = [], []
        if role not in GLOBAL_TASK_ROLES:
            where.append("user_id=?")
            params.append(actor)
        if status:
            where.append("status=?")
            params.append(status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.extend((limit, offset))
        with self._task_db_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks" + clause
                + " ORDER BY COALESCE(created_at,updated_at) DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [self._public_task(self._row_to_task(row), include_output=False) for row in rows]

    def _task_stats(self, actor: str, role: str) -> dict:
        params: tuple = ()
        where = ""
        if role not in GLOBAL_TASK_ROLES:
            where, params = " WHERE user_id=?", (actor,)
        with self._task_db_connect() as conn:
            rows = conn.execute(
                "SELECT status,count(*) AS count FROM tasks" + where + " GROUP BY status",
                params,
            ).fetchall()
        counts = {str(row[0] or "unknown"): int(row[1]) for row in rows}
        return {
            "ok": True,
            "total": sum(counts.values()),
            "active": counts.get("queued", 0) + counts.get("running", 0),
            "counts": counts,
        }

    def handle_get(self, request, path: str, query: dict, principal: PrincipalKind) -> bool:
        if path == "/qq/object-access/cutover":
            if principal not in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}:
                return self._failure(request, PermissionError("forbidden"))
            try:
                with self._assistant_db_connect() as conn:
                    result = qq_object_cutover_plan(
                        conn, channel_token_distinct=self._channel_token_distinct(),
                    )
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, **result})
            return True
        guarded = principal is PrincipalKind.QQ_CHANNEL and self._enabled()
        if not guarded:
            if path == "/projects":
                self._json_response(
                    request, 200,
                    {"ok": True, "current": self._current_project(), "projects": self._list_projects()},
                )
                return True
            if path == "/projects/current":
                self._json_response(request, 200, {"ok": True, "project": self._current_project()})
                return True
            if path == "/assistant/memories":
                user_id = self._query_value(query, "user_id", "web-console")
                memories = self._list_memories(
                    user_id=user_id,
                    query=self._query_value(query, "q"),
                    limit=max(1, min(int(self._query_value(query, "limit", "20")), 100)),
                    request_source="admin", owner_management=True,
                )
                self._json_response(request, 200, {"ok": True, "memories": memories})
                return True
            if path == "/tasks/stats":
                self._json_response(request, 200, self._task_stats_legacy())
                return True
            if path == "/tasks":
                status = self._query_value(query, "status") or None
                limit = max(1, min(int(self._query_value(query, "limit", "10")), 50))
                offset = max(0, int(self._query_value(query, "offset", "0")))
                stats = self._task_stats_legacy()
                total = stats["counts"].get(status, 0) if status else stats["total"]
                self._json_response(
                    request, 200,
                    {"ok": True, "status": status, "limit": limit, "offset": offset,
                     "total": total,
                     "tasks": self._list_tasks_legacy(limit=limit, status=status, offset=offset)},
                )
                return True
            if path.startswith("/tasks/") and path.count("/") == 2:
                task = self._get_task(unquote(path.rsplit("/", 1)[-1]))
                self._json_response(
                    request, 200 if task else 404,
                    {"ok": bool(task), "task": task, "error": "" if task else "task_not_found"},
                )
                return True
            return False
        object_get = path in {"/projects", "/projects/current", "/assistant/memories", "/tasks", "/tasks/stats"}
        object_get = object_get or (path.startswith("/tasks/") and path.count("/") == 2)
        if not object_get:
            return False
        try:
            actor, role, _ = self._actor(request)
            if path in {"/projects", "/projects/current"}:
                with self._assistant_db_connect() as conn:
                    result = actor_projects(conn, actor, role)
                if path.endswith("/current"):
                    result = {"ok": True, "project": result["current"]}
                self._json_response(request, 200, result)
                return True
            if path == "/assistant/memories":
                requested = self._query_value(query, "user_id", actor)
                user_id = requested if role in GLOBAL_OBJECT_ROLES else actor
                if role not in GLOBAL_OBJECT_ROLES and requested != actor:
                    raise PermissionError("qq_memory_forbidden")
                project_id = None
                with self._assistant_db_connect() as conn:
                    try:
                        project_id, _ = actor_project_path(conn, actor, role)
                    except ValueError:
                        pass
                memories = self._list_memories(
                    user_id=user_id,
                    query=self._query_value(query, "q"),
                    limit=max(1, min(int(self._query_value(query, "limit", "20")), 100)),
                    request_source="qq",
                    owner_management=False,
                    project_id=project_id,
                )
                self._json_response(request, 200, {"ok": True, "memories": memories})
                return True
            if path == "/tasks/stats":
                self._json_response(request, 200, self._task_stats(actor, role))
                return True
            if path == "/tasks":
                status = self._query_value(query, "status")
                limit = max(1, min(int(self._query_value(query, "limit", "10")), 50))
                offset = max(0, int(self._query_value(query, "offset", "0")))
                stats = self._task_stats(actor, role)
                total = stats["counts"].get(status, 0) if status else stats["total"]
                tasks = self._tasks(actor, role, status=status, limit=limit, offset=offset)
                self._json_response(
                    request, 200,
                    {"ok": True, "status": status or None, "limit": limit,
                     "offset": offset, "total": total, "tasks": tasks},
                )
                return True
            if path.startswith("/tasks/") and path.count("/") == 2:
                task = self._task_row(unquote(path.rsplit("/", 1)[-1]))
                if not task:
                    raise ValueError("task_not_found")
                if not task_allowed(task, actor, role):
                    raise PermissionError("qq_task_forbidden")
                self._json_response(
                    request, 200, {"ok": True, "task": self._public_task(task, include_output=True)},
                )
                return True
        except Exception as exc:
            return self._failure(request, exc)
        return False

    def handle_task_action(self, request, path: str, principal: PrincipalKind) -> bool:
        if not path.startswith("/tasks/") or not path.endswith(("/cancel", "/retry")):
            return False
        try:
            task_id = unquote(path.split("/")[-2])
            if principal is PrincipalKind.QQ_CHANNEL and self._enabled():
                actor, role, _ = self._actor(request)
                original = self._task_row(task_id)
                if not original:
                    raise ValueError("task_not_found")
                if not task_allowed(original, actor, role):
                    raise PermissionError("qq_task_forbidden")
            if path.endswith("/cancel"):
                task, error, status = self._cancel_task(task_id), "", 200
            else:
                task, error = self._retry_task(task_id)
                status = 202
            if not task:
                self._json_response(
                    request, 404 if error == "task_not_found" else 409,
                    {"ok": False, "error": error or "task_not_found"},
                )
                return True
            body = {"ok": True, "task": task}
            if path.endswith("/retry"):
                body["source_task_id"] = task_id
            self._json_response(request, status, body)
            return True
        except Exception as exc:
            return self._failure(request, exc)

    def _project_for_actor(self, actor: str, role: str) -> tuple[str, Path]:
        with self._assistant_db_connect() as conn:
            project_id, path = actor_project_path(conn, actor, role)
        return project_id, Path(path)

    def handle_post(
        self,
        request,
        path: str,
        payload: dict,
        principal: PrincipalKind,
    ) -> bool:
        if path == "/qq/object-access/cutover":
            if principal not in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}:
                return self._failure(request, PermissionError("forbidden"))
            try:
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("qq_object_enabled_boolean_required")
                with self._assistant_db_connect() as conn:
                    result = set_qq_object_feature(
                        conn,
                        bool(payload.get("enabled")),
                        expect_plan_checksum=str(payload.get("expect_plan_checksum") or ""),
                        changed_by="admin",
                        channel_token_distinct=self._channel_token_distinct(),
                    )
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, **result})
            return True
        guarded = principal is PrincipalKind.QQ_CHANNEL and self._enabled()
        if not guarded:
            try:
                if path == "/projects":
                    project = self._create_project(
                        str(payload.get("name") or "").strip(),
                        str(payload.get("path") or "").strip() or None,
                        str(payload.get("description") or "").strip(), True,
                    )
                    self._json_response(
                        request, 201,
                        {"ok": True, "project": project, "projects": self._list_projects()},
                    )
                    return True
                if path == "/projects/current":
                    identifier = str(
                        payload.get("id") or payload.get("project_id")
                        or payload.get("name") or payload.get("path") or "",
                    ).strip()
                    project = self._set_current_project(identifier)
                    self._json_response(request, 200, {"ok": True, "project": project})
                    return True
                if path == "/assistant/memories":
                    memory = self._add_memory(
                        str(payload.get("user_id") or "web-console").strip(),
                        str(payload.get("content") or "").strip(),
                        kind=str(payload.get("kind") or "fact").strip() or "fact",
                        source=str(payload.get("source") or "manual").strip() or "manual",
                        score=int(payload.get("score") or 7), request_source="admin",
                        scope_type=str(payload.get("scope_type") or "owner_private").strip(),
                        sensitivity=str(payload.get("sensitivity") or "private").strip(),
                        project_id=str(payload.get("project_id") or "").strip() or None,
                    )
                    self._json_response(request, 201, {"ok": True, "memory": memory})
                    return True
                if path == "/assistant/memories/delete":
                    memory_id = str(payload.get("id") or payload.get("memory_id") or "").strip()
                    user_id = str(payload.get("user_id") or "").strip() or None
                    deleted = self._delete_memory(memory_id, user_id=user_id)
                    self._json_response(request, 200, {"ok": deleted, "deleted": deleted})
                    return True
            except Exception as exc:
                return self._failure(request, exc)
            return False
        if path not in {
            "/projects", "/projects/current", "/assistant/memories",
            "/assistant/memories/delete", "/assistant/dispatch",
            "/assistant/group/dispatch", "/tasks",
        }:
            return False
        try:
            actor, role, group = self._actor(request)
            if path == "/projects":
                name = str(payload.get("name") or "").strip()
                project_id = self._slugify(name)
                with self._assistant_db_connect() as conn:
                    if project_id_exists(conn, project_id):
                        raise ValueError("project_already_exists")
                project = self._create_project(
                    name,
                    str(payload.get("path") or "").strip() or None,
                    str(payload.get("description") or "").strip(),
                    False,
                )
                with self._assistant_db_connect() as conn:
                    claim_actor_project(conn, actor, str(project["id"]))
                    result = actor_projects(conn, actor, role)
                self._json_response(request, 201, {**result, "project": project, "current": project})
                return True
            if path == "/projects/current":
                identifier = str(
                    payload.get("id") or payload.get("project_id")
                    or payload.get("name") or payload.get("path") or "",
                ).strip()
                with self._assistant_db_connect() as conn:
                    project = bind_actor_project(conn, actor, role, identifier)
                self._json_response(request, 200, {"ok": True, "project": project})
                return True
            if path == "/assistant/memories":
                requested = str(payload.get("user_id") or actor).strip()
                if role not in GLOBAL_OBJECT_ROLES and requested != actor:
                    raise PermissionError("qq_memory_forbidden")
                user_id = requested if role in GLOBAL_OBJECT_ROLES else actor
                project_id = None
                try:
                    project_id, _ = self._project_for_actor(actor, role)
                except ValueError:
                    pass
                memory = self._add_memory(
                    user_id,
                    str(payload.get("content") or "").strip(),
                    kind=str(payload.get("kind") or "fact").strip() or "fact",
                    source=str(payload.get("source") or "qq-manual").strip() or "qq-manual",
                    score=int(payload.get("score") or 7),
                    request_source="qq",
                    scope_type=str(payload.get("scope_type") or "").strip(),
                    sensitivity=str(payload.get("sensitivity") or "private").strip(),
                    project_id=project_id,
                )
                self._json_response(request, 201, {"ok": True, "memory": memory})
                return True
            if path == "/assistant/memories/delete":
                memory_id = str(payload.get("id") or payload.get("memory_id") or "").strip()
                requested = str(payload.get("user_id") or actor).strip()
                if role not in GLOBAL_OBJECT_ROLES and requested != actor:
                    raise PermissionError("qq_memory_forbidden")
                with self._assistant_db_connect() as conn:
                    if not memory_allowed(conn, memory_id, actor, role):
                        raise PermissionError("qq_memory_forbidden")
                deleted = self._delete_memory(memory_id, user_id=requested or None)
                self._json_response(request, 200, {"ok": deleted, "deleted": deleted})
                return True
            if path == "/assistant/dispatch":
                if str(payload.get("user_id") or actor).strip() != actor:
                    raise PermissionError("qq_actor_payload_mismatch")
                payload["user_id"] = actor
                payload["_qq_project_guard"] = True
                try:
                    _, cwd = self._project_for_actor(actor, role)
                    payload["_qq_cwd"] = str(cwd)
                except ValueError:
                    payload["_qq_cwd"] = ""
                return False
            if path == "/assistant/group/dispatch":
                if not group or str(payload.get("group_id") or "").strip() != group:
                    raise PermissionError("qq_group_payload_mismatch")
                if str(payload.get("sender_id") or "").strip() != actor:
                    raise PermissionError("qq_actor_payload_mismatch")
                payload["_qq_actor_id"] = actor
                try:
                    _, cwd = self._project_for_actor(actor, role)
                    payload["_qq_cwd"] = str(cwd)
                except ValueError:
                    payload["_qq_cwd"] = ""
                return False
            if path == "/tasks":
                _, cwd = self._project_for_actor(actor, role)
                payload.update({"user_id": actor, "source": "qq", "cwd": str(cwd)})
                return False
        except Exception as exc:
            return self._failure(request, exc)
        return False


__all__ = ["QqObjectRuntime"]
