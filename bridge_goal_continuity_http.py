#!/usr/bin/env python3
"""Authenticated HTTP adapter for Goal revision, checkpoint and feedback continuity."""

from __future__ import annotations

from typing import Callable
from urllib.parse import unquote

from bridge_goal_continuity import (
    create_goal_revision,
    get_goal_continuity,
    list_run_checkpoints,
    record_goal_feedback,
    record_run_checkpoint,
)


def _status(error: str) -> int:
    if error in {"goal_not_found", "goal_revision_not_found", "run_not_found", "goal_feedback_run_not_found"}:
        return 404
    if error.endswith("_conflict") or error.endswith("_reused"):
        return 409
    return 400


class GoalContinuityHttpApi:
    def __init__(self, task_connect: Callable, json_response: Callable) -> None:
        self._task_connect = task_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        parts = path.strip("/").split("/")
        return (
            len(parts) == 4
            and parts[0] == "assistant"
            and ((parts[1] == "goals" and parts[3] in {"revisions", "feedback"}) or (parts[1] == "runs" and parts[3] == "checkpoints"))
        )

    def handle_get(self, request, path: str, query: dict) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "assistant":
            return False
        try:
            limit = int(query.get("limit", ["100"])[0])
            identifier = unquote(parts[2])
            with self._task_connect() as conn:
                if parts[1] == "goals" and parts[3] == "continuity":
                    result = get_goal_continuity(conn, identifier, limit=limit)
                    self._json_response(request, 200, {"ok": True, **result})
                    return True
                if parts[1] == "runs" and parts[3] == "checkpoints":
                    result = list_run_checkpoints(conn, identifier, limit=limit)
                    self._json_response(request, 200, {"ok": True, "items": result})
                    return True
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._json_response(request, _status(error), {"ok": False, "error": error})
            return True
        return False

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if not self.matches_post(path):
            return False
        parts = path.strip("/").split("/")
        identifier = unquote(parts[2])
        idempotency_key = str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip()
        try:
            with self._task_connect() as conn:
                if parts[1] == "goals" and parts[3] == "revisions":
                    result = create_goal_revision(
                        conn,
                        identifier,
                        str(payload.get("instruction") or ""),
                        actor_id="admin",
                        channel="web",
                        source_run_id=str(payload.get("source_run_id") or ""),
                        parent_revision_id=str(payload.get("parent_revision_id") or ""),
                        idempotency_key=idempotency_key,
                    )
                    self._json_response(request, 200, {"ok": True, "revision": result})
                    return True
                if parts[1] == "goals" and parts[3] == "feedback":
                    result = record_goal_feedback(
                        conn,
                        identifier,
                        str(payload.get("kind") or ""),
                        message=str(payload.get("message") or ""),
                        revision_id=str(payload.get("revision_id") or ""),
                        run_id=str(payload.get("run_id") or ""),
                        artifact_id=str(payload.get("artifact_id") or ""),
                        actor_id="admin",
                        channel="web",
                        idempotency_key=idempotency_key,
                    )
                    self._json_response(request, 200, {"ok": True, **result})
                    return True
                if parts[1] == "runs" and parts[3] == "checkpoints":
                    result = record_run_checkpoint(
                        conn,
                        identifier,
                        str(payload.get("step_key") or ""),
                        str(payload.get("status") or ""),
                        summary=str(payload.get("summary") or ""),
                        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                    )
                    self._json_response(request, 200, {"ok": True, "checkpoint": result})
                    return True
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._json_response(request, _status(error), {"ok": False, "error": error})
            return True
        return False


__all__ = ["GoalContinuityHttpApi"]
