#!/usr/bin/env python3
"""Authenticated HTTP adapter for formal approvals and task timelines."""

from __future__ import annotations

from typing import Callable

from bridge_formal_approval import (
    ApprovalError,
    FormalApprovalRepository,
    formal_approval_cutover_plan,
    formal_approval_feature_enabled,
)


def _error_status(message: str) -> int:
    if message in {"approval_not_found", "task_goal_not_found"}:
        return 404
    if message == "approval_expired":
        return 410
    if message in {
        "approval_not_pending",
        "approval_version_conflict",
        "approval_idempotency_key_reused",
        "approval_task_state_changed",
        "approval_action_changed",
        "formal_approval_disabled",
    }:
        return 409
    return 400


class FormalApprovalHttpApi:
    def __init__(
        self,
        assistant_connect: Callable,
        task_connect: Callable,
        json_response: Callable,
        decision_applied: Callable[[dict], None],
    ) -> None:
        self._assistant_connect = assistant_connect
        self._task_connect = task_connect
        self._json_response = json_response
        self._decision_applied = decision_applied

    @staticmethod
    def matches_post(path: str) -> bool:
        parts = path.strip("/").split("/")
        return len(parts) == 4 and parts[:2] == ["assistant", "approvals"] and parts[3] == "decision"

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path == "/assistant/formal-approval/cutover-plan":
            try:
                with self._assistant_connect() as assistant_conn, self._task_connect() as task_conn:
                    result = formal_approval_cutover_plan(assistant_conn, task_conn)
            except Exception as exc:
                self._json_response(request, 400, {"ok": False, "error": str(exc) or type(exc).__name__})
                return True
            self._json_response(request, 200, {"ok": True, "result": result})
            return True

        if path == "/assistant/approvals":
            try:
                status = str(query.get("status", [""])[0] or "").strip()
                goal_id = str(query.get("goal_id", [""])[0] or "").strip()
                limit = int(query.get("limit", ["50"])[0])
                with self._task_connect() as conn:
                    items = FormalApprovalRepository(conn).list(
                        status=status,
                        goal_id=goal_id,
                        limit=limit,
                    )
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._json_response(request, _error_status(message), {"ok": False, "error": message})
                return True
            self._json_response(request, 200, {"ok": True, "items": items})
            return True

        if path.startswith("/assistant/approvals/") and path.count("/") == 3:
            identifier = path.rsplit("/", 1)[-1]
            try:
                with self._task_connect() as conn:
                    approval = FormalApprovalRepository(conn).get(identifier, include_action=True)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._json_response(request, _error_status(message), {"ok": False, "error": message})
                return True
            self._json_response(
                request,
                200 if approval else 404,
                {"ok": bool(approval), "approval": approval, "error": "" if approval else "approval_not_found"},
            )
            return True

        if path.startswith("/assistant/tasks/") and path.endswith("/timeline"):
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                return False
            try:
                limit = int(query.get("limit", ["200"])[0])
                with self._task_connect() as conn:
                    timeline = FormalApprovalRepository(conn).timeline(parts[2], limit=limit)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._json_response(request, _error_status(message), {"ok": False, "error": message})
                return True
            self._json_response(request, 200, {"ok": True, **timeline})
            return True
        return False

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if not self.matches_post(path):
            return False
        identifier = path.strip("/").split("/")[2]
        try:
            with self._assistant_connect() as assistant_conn:
                if not formal_approval_feature_enabled(assistant_conn):
                    raise ApprovalError("formal_approval_disabled")
            header_key = str(request.headers.get("Idempotency-Key") or "").strip()
            expected_version = int(payload.get("expected_version") or 0)
            if expected_version < 1:
                raise ApprovalError("approval_expected_version_required")
            with self._task_connect() as task_conn:
                result = FormalApprovalRepository(task_conn).decide(
                    identifier,
                    decision=str(payload.get("decision") or ""),
                    expected_version=expected_version,
                    actor_id="admin",
                    channel="web",
                    idempotency_key=header_key or str(payload.get("idempotency_key") or ""),
                    edit_patch=payload.get("edit_patch") if isinstance(payload.get("edit_patch"), dict) else {},
                    reason=str(payload.get("reason") or ""),
                    allow_admin=True,
                )
            self._decision_applied(result)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self._json_response(request, _error_status(message), {"ok": False, "error": message})
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["FormalApprovalHttpApi"]
