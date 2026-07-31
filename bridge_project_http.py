#!/usr/bin/env python3
"""Admin-only HTTP adapter for Product Gate C8 Project lifecycle."""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import unquote

from bridge_auth import PrincipalKind
from bridge_project_service import ProjectError, ProjectService


_ACTION_RE = re.compile(r"^/projects/([^/]+)/(rename|archive|restore)$")
_TASKS_RE = re.compile(r"^/projects/([^/]+)/tasks$")
_ADMIN = {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}


class ProjectHttpApi:
    def __init__(self, service: ProjectService, json_response) -> None:
        self.service = service
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path in {"/projects", "/projects/current"} or bool(_ACTION_RE.fullmatch(path))

    def _failure(self, request, exc: Exception) -> bool:
        error = str(exc) or type(exc).__name__
        if error.endswith("not_found"):
            status = 404
        elif any(token in error for token in ("already", "conflict", "stale", "current_archive")):
            status = 409
        else:
            status = 400
        self._json_response(request, status, {"ok": False, "error": error})
        return True

    @staticmethod
    def _query_bool(query: dict, key: str) -> bool:
        return str(query.get(key, [""])[0] or "").strip().lower() in {"1", "true", "yes", "on"}

    def handle_get(self, request, path: str, query: dict, principal: PrincipalKind) -> bool:
        if principal not in _ADMIN:
            return False
        try:
            if path == "/projects":
                result = self.service.list_projects(
                    include_archived=self._query_bool(query, "include_archived"),
                )
                self._json_response(request, 200, result)
                return True
            if path == "/projects/current":
                result = self.service.list_projects(include_archived=False)
                self._json_response(request, 200, {"ok": True, "project": result["current"]})
                return True
            match = _TASKS_RE.fullmatch(path)
            if match:
                limit = int(str(query.get("limit", ["5"])[0] or "5"))
                result = self.service.recent_tasks(unquote(match.group(1)), limit=limit)
                self._json_response(request, 200, result)
                return True
        except (ProjectError, ValueError, sqlite3.Error) as exc:
            return self._failure(request, exc)
        return False

    def handle_post(
        self,
        request,
        path: str,
        payload: dict,
        principal: PrincipalKind,
    ) -> bool:
        if principal not in _ADMIN:
            return False
        try:
            if path == "/projects":
                project = self.service.create(
                    payload.get("name", ""), payload.get("path", ""),
                    payload.get("description", ""), make_current=True,
                )
                self._json_response(
                    request, 201,
                    {"ok": True, "project": project, **self.service.list_projects(include_archived=True)},
                )
                return True
            if path == "/projects/current":
                project_id = str(payload.get("id") or payload.get("project_id") or "").strip()
                project = self.service.set_current(project_id)
                self._json_response(request, 200, {"ok": True, "project": project})
                return True
            match = _ACTION_RE.fullmatch(path)
            if not match:
                return False
            project_id, action = unquote(match.group(1)), match.group(2)
            expected = str(payload.get("expected_updated_at") or "")
            if action == "rename":
                project = self.service.rename(
                    project_id, name=payload.get("name", ""),
                    description=payload.get("description", ""),
                    expected_updated_at=expected,
                )
            elif action == "archive":
                if payload.get("confirm_archive") is not True:
                    raise ProjectError("project_archive_confirmation_required")
                project = self.service.archive(project_id, expected_updated_at=expected)
            else:
                project = self.service.restore(project_id, expected_updated_at=expected)
            self._json_response(request, 200, {"ok": True, "project": project})
            return True
        except (ProjectError, ValueError, OSError, sqlite3.Error) as exc:
            return self._failure(request, exc)


__all__ = ["ProjectHttpApi"]
