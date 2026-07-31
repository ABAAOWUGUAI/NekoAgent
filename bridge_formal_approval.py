#!/usr/bin/env python3
"""Formal Approval domain service for paused Task/Run execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from bridge_formal_approval_schema import (
    FORMAL_APPROVAL_FEATURE_FLAG,
    require_formal_approval_schema,
)
from bridge_migrations import utc_after, utc_now
from bridge_platform_repository import PlatformRepository
from bridge_goal_timeline import project_goal_continuity_timeline


APPROVAL_STATUSES = {"pending", "approved", "rejected", "expired", "superseded"}
APPROVAL_DECISIONS = {"approve", "edit", "reject"}
ALLOWED_EDIT_FIELDS = {"timeout_seconds"}


class ApprovalError(ValueError):
    """Stable domain error that HTTP and QQ adapters can map safely."""


class _CommittedApprovalError(ApprovalError):
    """State was safely finalized and the caller should still receive an error."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_load(value: object, fallback: object) -> object:
    try:
        result = json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return fallback
    return result


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def task_action(task: Mapping[str, object], *, target_environment: str) -> dict:
    """Build the exact, secret-free action descriptor covered by approval."""

    task_id = str(task.get("id") or "").strip()
    prompt = str(task.get("prompt") or "")
    cwd = str(task.get("cwd") or "")
    if not task_id or not prompt or not cwd:
        raise ApprovalError("approval_task_snapshot_incomplete")
    return {
        "action_name": "enqueue_codex_task",
        "arguments": {
            "task_id": task_id,
            "prompt_sha256": _sha256(prompt),
            "sandbox": str(task.get("sandbox") or "read-only"),
            "cwd_sha256": _sha256(cwd),
            "timeout_seconds": int(task.get("timeout") or 0),
            "executor_provider_id": str(task.get("executor_provider_id") or ""),
            "executor_model_id": str(task.get("executor_model_id") or ""),
            "executor_adapter": str(task.get("executor_adapter") or ""),
            "executor_config_version": str(task.get("executor_config_version") or ""),
            "executor_profile_sha256": str(task.get("executor_profile_sha256") or ""),
        },
        "target_environment": str(target_environment or "server")[:80],
    }


def action_hash(action: Mapping[str, object]) -> str:
    return _sha256(_canonical_json(dict(action)))


def formal_approval_feature_enabled(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_feature_flags'",
    ).fetchone()
    if not table:
        return False
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?",
        (FORMAL_APPROVAL_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0]))


def _public(row: Mapping[str, object], *, include_action: bool = False) -> dict:
    allowed = _json_load(row.get("allowed_decisions_json"), [])
    edit_fields = _json_load(row.get("allowed_edit_fields_json"), [])
    arguments = _json_load(row.get("action_arguments_json"), {})
    safe_arguments = arguments if isinstance(arguments, dict) else {}
    item = {
        "id": str(row.get("id") or ""),
        "code": str(row.get("approval_code") or ""),
        "goal_id": str(row.get("goal_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "task_id": str(row.get("legacy_task_id") or ""),
        "action_name": str(row.get("action_name") or ""),
        "action_summary": str(row.get("action_summary") or ""),
        "action_hash": str(row.get("action_hash") or ""),
        "target_environment": str(row.get("target_environment") or ""),
        "version": int(row.get("approval_version") or 0),
        "status": str(row.get("status") or ""),
        "allowed_decisions": allowed if isinstance(allowed, list) else [],
        "allowed_edit_fields": edit_fields if isinstance(edit_fields, list) else [],
        "requested_channel": str(row.get("requested_channel") or ""),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "expires_at": str(row.get("expires_at") or ""),
        "decided_at": str(row.get("decided_at") or ""),
        "decision_channel": str(row.get("decision_channel") or ""),
        "decision_kind": str(row.get("decision_kind") or ""),
        "decision_reason": str(row.get("decision_reason") or ""),
        "timeout_seconds": int(safe_arguments.get("timeout_seconds") or 0),
    }
    if include_action:
        item["action"] = {
            "name": item["action_name"],
            "arguments": safe_arguments,
            "target_environment": item["target_environment"],
        }
    return item


class FormalApprovalRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        require_formal_approval_schema(self.conn)

    @contextmanager
    def _write(self):
        if self.conn.in_transaction:
            savepoint = "approval_" + uuid.uuid4().hex
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
        except _CommittedApprovalError:
            self.conn.commit()
            raise
        except Exception:
            self.conn.rollback()
            raise

    def _find(self, identifier: str, *, actor_id: str = "") -> dict | None:
        identifier = str(identifier or "").strip()
        if not identifier:
            return None
        params: list[object] = [identifier, identifier]
        where = "(id=? OR approval_code=?)"
        if actor_id:
            where += " AND actor_id=?"
            params.append(str(actor_id))
        rows = _rows(
            self.conn.execute(
                f"SELECT * FROM approval_requests WHERE {where} LIMIT 1",
                tuple(params),
            ),
        )
        return rows[0] if rows else None

    def get(self, identifier: str, *, actor_id: str = "", include_action: bool = False) -> dict | None:
        row = self._find(identifier, actor_id=actor_id)
        return _public(row, include_action=include_action) if row else None

    def list(
        self,
        *,
        actor_id: str = "",
        status: str = "",
        goal_id: str = "",
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if actor_id:
            clauses.append("actor_id=?")
            params.append(str(actor_id))
        if status:
            if status not in APPROVAL_STATUSES:
                raise ApprovalError("approval_status_invalid")
            clauses.append("status=?")
            params.append(status)
        if goal_id:
            clauses.append("goal_id=?")
            params.append(str(goal_id))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit or 50), 100)))
        rows = _rows(
            self.conn.execute(
                f"""
                SELECT * FROM approval_requests {where}
                ORDER BY created_at DESC,id DESC LIMIT ?
                """,
                tuple(params),
            ),
        )
        return [_public(row) for row in rows]

    def create_for_task(
        self,
        task: Mapping[str, object],
        *,
        goal_id: str,
        run_id: str,
        assistant_id: str,
        requested_channel: str,
        requested_by: str,
        target_environment: str,
        request_idempotency_key: str,
        action_summary: str,
        ttl_seconds: int = 1800,
    ) -> dict:
        action = task_action(task, target_environment=target_environment)
        digest = action_hash(action)
        request_key = str(request_idempotency_key or "").strip()
        if not request_key:
            raise ApprovalError("approval_request_idempotency_key_required")
        task_id = str(task.get("id") or "").strip()
        if str(task.get("status") or "") != "waiting_approval":
            raise ApprovalError("approval_task_not_paused")
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        expires = (now_value + timedelta(seconds=max(60, min(int(ttl_seconds), 86400)))).isoformat()
        approval_id = "approval-" + uuid.uuid4().hex
        code = uuid.uuid4().hex[:8]
        with self._write():
            existing = _rows(
                self.conn.execute(
                    "SELECT * FROM approval_requests WHERE request_idempotency_key=?",
                    (request_key,),
                ),
            )
            if existing:
                if str(existing[0]["action_hash"]) != digest:
                    raise ApprovalError("approval_request_idempotency_conflict")
                return _public(existing[0], include_action=True)
            pending = self.conn.execute(
                "SELECT id FROM approval_requests WHERE legacy_task_id=? AND status='pending'",
                (task_id,),
            ).fetchone()
            if pending:
                raise ApprovalError("approval_already_pending")
            self.conn.execute(
                """
                INSERT INTO approval_requests(
                    id,approval_code,goal_id,run_id,legacy_task_id,actor_id,assistant_id,
                    action_name,action_arguments_json,action_hash,target_environment,
                    action_summary,approval_version,request_idempotency_key,status,
                    allowed_decisions_json,allowed_edit_fields_json,requested_channel,
                    requested_by,created_at,updated_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,'pending',?,?,?,?,?,?,?)
                """,
                (
                    approval_id,
                    code,
                    str(goal_id),
                    str(run_id),
                    task_id,
                    str(task.get("user_id") or requested_by),
                    str(assistant_id or ""),
                    str(action["action_name"]),
                    _canonical_json(action["arguments"]),
                    digest,
                    str(action["target_environment"]),
                    str(action_summary or task.get("summary") or "待确认任务")[:500],
                    request_key,
                    _canonical_json(["approve", "edit", "reject"]),
                    _canonical_json(sorted(ALLOWED_EDIT_FIELDS)),
                    str(requested_channel or "unknown")[:40],
                    str(requested_by or "")[:200],
                    now,
                    now,
                    expires,
                ),
            )
            self._insert_run_event(
                str(run_id),
                "approval.requested",
                payload={
                    "approval_id": approval_id,
                    "approval_code": code,
                    "action_hash": digest,
                    "approval_version": 1,
                    "expires_at": expires,
                },
                created_at=now,
            )
        row = self._find(approval_id)
        if not row:
            raise ApprovalError("approval_create_failed")
        return _public(row, include_action=True)

    def _insert_run_event(
        self,
        run_id: str,
        event_type: str,
        *,
        payload: Mapping[str, object],
        created_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO run_events(run_id,event_type,from_status,to_status,payload_json,created_at)
            VALUES(?,?, '', '', ?, ?)
            """,
            (str(run_id), str(event_type), _canonical_json(dict(payload)), str(created_at)),
        )

    def _task_row(self, task_id: str) -> dict:
        rows = _rows(self.conn.execute("SELECT * FROM tasks WHERE id=?", (str(task_id),)))
        if not rows:
            raise ApprovalError("approval_task_not_found")
        return rows[0]

    def _decision_replay(
        self,
        idempotency_key: str,
        request_fingerprint: str,
        approval_id: str,
    ) -> dict | None:
        rows = _rows(
            self.conn.execute(
                "SELECT * FROM approval_decisions WHERE idempotency_key=?",
                (str(idempotency_key),),
            ),
        )
        if not rows:
            return None
        decision = rows[0]
        if (
            str(decision["approval_id"]) != str(approval_id)
            or str(decision["request_fingerprint"]) != str(request_fingerprint)
        ):
            raise ApprovalError("approval_idempotency_key_reused")
        approval = self._find(str(approval_id))
        if not approval:
            raise ApprovalError("approval_not_found")
        return {
            "approval": _public(approval, include_action=True),
            "decision": {
                "id": str(decision["id"]),
                "kind": str(decision["decision"]),
                "outcome": "replayed",
                "created_at": str(decision["created_at"]),
            },
            "task_id": str(approval["legacy_task_id"]),
            "task_status": str(self._task_row(str(approval["legacy_task_id"])).get("status") or ""),
            "idempotent_replay": True,
        }

    def decide(
        self,
        identifier: str,
        *,
        decision: str,
        expected_version: int,
        actor_id: str,
        channel: str,
        idempotency_key: str,
        edit_patch: Mapping[str, object] | None = None,
        reason: str = "",
        allow_admin: bool = False,
        now: str | None = None,
    ) -> dict:
        decision = str(decision or "").strip().lower()
        if decision not in APPROVAL_DECISIONS:
            raise ApprovalError("approval_decision_invalid")
        edit_patch = dict(edit_patch or {})
        if decision != "edit" and edit_patch:
            raise ApprovalError("approval_edit_patch_unexpected")
        unknown_edits = sorted(set(edit_patch) - ALLOWED_EDIT_FIELDS)
        if unknown_edits:
            raise ApprovalError("approval_edit_not_allowed:" + ",".join(unknown_edits))
        if decision == "edit" and not edit_patch:
            raise ApprovalError("approval_edit_patch_required")
        actor_id = str(actor_id or "").strip()
        channel = str(channel or "unknown").strip()[:40]
        idempotency_key = str(idempotency_key or "").strip()
        if not actor_id:
            raise ApprovalError("approval_actor_required")
        if not idempotency_key:
            raise ApprovalError("approval_idempotency_key_required")
        reason = str(reason or "").strip()[:1000]
        request_payload = {
            "identifier": str(identifier or ""),
            "decision": decision,
            "expected_version": int(expected_version),
            "actor_id": actor_id,
            "channel": channel,
            "edit_patch": edit_patch,
            "reason": reason,
        }
        request_fingerprint = _sha256(_canonical_json(request_payload))
        current_time = str(now or utc_now())

        with self._write():
            row = self._find(identifier)
            if not row:
                raise ApprovalError("approval_not_found")
            replay = self._decision_replay(
                idempotency_key,
                request_fingerprint,
                str(row["id"]),
            )
            if replay:
                return replay
            if not allow_admin and str(row["actor_id"]) != actor_id:
                raise ApprovalError("approval_actor_mismatch")
            if str(row["status"]) != "pending":
                raise ApprovalError("approval_not_pending")
            if int(row["approval_version"]) != int(expected_version):
                raise ApprovalError("approval_version_conflict")
            if _timestamp(str(row["expires_at"])) <= _timestamp(current_time):
                self._expire_row(row, current_time)
                raise _CommittedApprovalError("approval_expired")

            task = self._task_row(str(row["legacy_task_id"]))
            if str(task.get("status") or "") != "waiting_approval":
                self.conn.execute(
                    """
                    UPDATE approval_requests
                    SET status='superseded',updated_at=?,approval_version=approval_version+1,
                        decision_reason='task_state_changed'
                    WHERE id=? AND approval_version=?
                    """,
                    (current_time, str(row["id"]), int(row["approval_version"])),
                )
                raise _CommittedApprovalError("approval_task_state_changed")

            current_action = task_action(task, target_environment=str(row["target_environment"]))
            current_hash = action_hash(current_action)
            if current_hash != str(row["action_hash"]):
                self.conn.execute(
                    """
                    UPDATE approval_requests
                    SET status='superseded',updated_at=?,approval_version=approval_version+1,
                        decision_reason='action_snapshot_changed'
                    WHERE id=? AND approval_version=?
                    """,
                    (current_time, str(row["id"]), int(row["approval_version"])),
                )
                raise _CommittedApprovalError("approval_action_changed")

            original_hash = current_hash
            resulting_hash = current_hash
            next_status = "cancelled" if decision == "reject" else "queued"
            next_approval_status = "rejected" if decision == "reject" else "approved"
            if decision == "edit":
                timeout = int(edit_patch.get("timeout_seconds") or 0)
                if timeout < 30 or timeout > 900:
                    raise ApprovalError("approval_timeout_out_of_range")
                task["timeout"] = timeout
                current_action = task_action(task, target_environment=str(row["target_environment"]))
                resulting_hash = action_hash(current_action)

            task["status"] = next_status
            task["updated_at"] = current_time
            if next_status == "cancelled":
                task["cancel_requested"] = 1
                task["finished_at"] = current_time
                task["ok"] = 0
                task["returncode"] = 130
                task["output"] = "用户拒绝了待审批动作，任务未执行。"
            else:
                task["cancel_requested"] = 0
                task["finished_at"] = ""
            assignments = ",".join(f"{key}=?" for key in task)
            self.conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id=?",
                (*(task[key] for key in task), str(task["id"])),
            )

            next_version = int(row["approval_version"]) + 1
            self.conn.execute(
                """
                UPDATE approval_requests
                SET status=?,action_arguments_json=?,action_hash=?,approval_version=?,
                    updated_at=?,decided_at=?,decided_by=?,decision_channel=?,
                    decision_kind=?,decision_reason=?
                WHERE id=? AND approval_version=? AND status='pending'
                """,
                (
                    next_approval_status,
                    _canonical_json(current_action["arguments"]),
                    resulting_hash,
                    next_version,
                    current_time,
                    current_time,
                    actor_id,
                    channel,
                    decision,
                    reason,
                    str(row["id"]),
                    int(row["approval_version"]),
                ),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ApprovalError("approval_version_conflict")
            decision_id = "decision-" + uuid.uuid4().hex
            self.conn.execute(
                """
                INSERT INTO approval_decisions(
                    id,approval_id,approval_version,decision,actor_id,channel,
                    idempotency_key,request_fingerprint,edit_patch_json,
                    original_action_hash,resulting_action_hash,outcome,reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision_id,
                    str(row["id"]),
                    int(row["approval_version"]),
                    decision,
                    actor_id,
                    channel,
                    idempotency_key,
                    request_fingerprint,
                    _canonical_json(edit_patch),
                    original_hash,
                    resulting_hash,
                    "applied",
                    reason,
                    current_time,
                ),
            )
            projection = PlatformRepository(self.conn).sync_task(task)
            run_id = str((projection.get("projection") or {}).get("run_id") or row["run_id"])
            self._insert_run_event(
                run_id,
                "approval." + (
                    "edited_and_approved" if decision == "edit"
                    else "approved" if decision == "approve"
                    else "rejected"
                ),
                payload={
                    "approval_id": str(row["id"]),
                    "decision_id": decision_id,
                    "approval_version": next_version,
                    "original_action_hash": original_hash,
                    "resulting_action_hash": resulting_hash,
                    "decision_channel": channel,
                },
                created_at=current_time,
            )

        approval = self._find(str(row["id"]))
        if not approval:
            raise ApprovalError("approval_not_found")
        return {
            "approval": _public(approval, include_action=True),
            "decision": {
                "id": decision_id,
                "kind": decision,
                "outcome": "applied",
                "created_at": current_time,
            },
            "task_id": str(task["id"]),
            "task_status": next_status,
            "idempotent_replay": False,
        }

    def _expire_row(self, row: Mapping[str, object], now: str) -> None:
        task = self._task_row(str(row["legacy_task_id"]))
        if str(task.get("status") or "") == "waiting_approval":
            task.update({
                "status": "cancelled",
                "updated_at": now,
                "finished_at": now,
                "cancel_requested": 1,
                "ok": 0,
                "returncode": 124,
                "error_kind": "approval_expired",
                "output": "审批已过期，任务未执行。",
            })
            assignments = ",".join(f"{key}=?" for key in task)
            self.conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id=?",
                (*(task[key] for key in task), str(task["id"])),
            )
            PlatformRepository(self.conn).sync_task(task)
        self.conn.execute(
            """
            UPDATE approval_requests
            SET status='expired',updated_at=?,decided_at=?,decision_kind='expired',
                decision_reason='approval_expired',approval_version=approval_version+1
            WHERE id=? AND status='pending'
            """,
            (now, now, str(row["id"])),
        )
        self._insert_run_event(
            str(row["run_id"]),
            "approval.expired",
            payload={"approval_id": str(row["id"])},
            created_at=now,
        )

    def expire_due(self, *, now: str | None = None, limit: int = 100) -> list[str]:
        current_time = str(now or utc_now())
        expired: list[str] = []
        with self._write():
            rows = _rows(
                self.conn.execute(
                    """
                    SELECT * FROM approval_requests
                    WHERE status='pending' AND expires_at<=?
                    ORDER BY expires_at LIMIT ?
                    """,
                    (current_time, max(1, min(int(limit or 100), 500))),
                ),
            )
            for row in rows:
                self._expire_row(row, current_time)
                expired.append(str(row["legacy_task_id"]))
        return expired

    def timeline(self, goal_id: str, *, limit: int = 200) -> dict:
        goal = PlatformRepository(self.conn).get_goal(str(goal_id))
        if not goal:
            raise ApprovalError("task_goal_not_found")
        bounded_limit = max(1, min(int(limit or 200), 500))
        runs = PlatformRepository(self.conn).list_runs(
            goal_id=str(goal_id),
            limit=bounded_limit,
        )
        continuity_timeline = project_goal_continuity_timeline(
            self.conn, str(goal_id), limit=bounded_limit,
        )
        run_revisions = continuity_timeline["run_revisions"]
        events: list[dict] = []
        labels = {
            "run.projected": ("received", "已收到任务"),
            "run.status_changed": ("status", "执行状态已更新"),
            "approval.requested": ("approval", "等待你确认"),
            "approval.approved": ("approval", "你已批准，任务进入队列"),
            "approval.edited_and_approved": ("approval", "你修改后批准，任务进入队列"),
            "approval.rejected": ("approval", "你已拒绝，任务未执行"),
            "approval.expired": ("approval", "确认已过期，任务未执行"),
        }
        for run in runs:
            for event in PlatformRepository(self.conn).list_run_events(
                str(run["id"]),
                limit=bounded_limit,
            ):
                event_type = str(event.get("event_type") or "")
                if event_type == "goal.feedback":
                    # Feedback has a richer first-class projection below; do
                    # not render the supporting Run event a second time.
                    continue
                kind, label = labels.get(event_type, ("status", "任务记录已更新"))
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                revision = run_revisions.get(str(run["id"]), {})
                revision_id = str(revision.get("revision_id") or "")
                revision_number = int(revision.get("revision_number") or 0)
                if revision_number and event_type == "run.projected":
                    label = f"第{revision_number}版已收到任务"
                events.append({
                    "id": str(event.get("id") or ""),
                    "run_id": str(run["id"]),
                    "kind": kind,
                    "label": label,
                    "event_type": event_type,
                    "from_status": str(event.get("from_status") or ""),
                    "to_status": str(event.get("to_status") or ""),
                    "approval_id": str(payload.get("approval_id") or ""),
                    "revision_id": revision_id,
                    "revision_number": revision_number,
                    "created_at": str(event.get("created_at") or ""),
                })
        events.extend(continuity_timeline["events"])
        events.sort(key=lambda item: (item["created_at"], item["id"]))
        return {
            "goal": {
                "id": str(goal["id"]),
                "title": str(goal.get("title") or "未命名任务")[:240],
                "status": str(goal.get("status") or ""),
                "updated_at": str(goal.get("updated_at") or ""),
            },
            "events": events[-bounded_limit:],
        }


def formal_approval_cutover_plan(
    assistant_conn: sqlite3.Connection,
    task_conn: sqlite3.Connection,
) -> dict:
    schema = require_formal_approval_schema(task_conn)
    flags = {
        str(row[0]): bool(int(row[1]))
        for row in assistant_conn.execute("SELECT name,enabled FROM assistant_feature_flags")
    }
    now = utc_now()
    legacy_live = int(
        assistant_conn.execute(
            """
            SELECT count(*) FROM pending_approvals
            WHERE status='pending' AND expires_at>?
            """,
            (now,),
        ).fetchone()[0],
    )
    counts = task_conn.execute(
        """
        SELECT count(*),
               coalesce(sum(CASE WHEN status='pending' THEN 1 ELSE 0 END),0),
               coalesce(sum(CASE WHEN status='approved' THEN 1 ELSE 0 END),0)
        FROM approval_requests
        """,
    ).fetchone()
    prerequisites = {
        "identity_enabled": bool(flags.get("assistant_identity_v2")),
        "memory_scope_enabled": bool(flags.get("memory_scope_v2")),
        "daily_shell_enabled": bool(flags.get("daily_shell_v2")),
        "interaction_plan_enabled": bool(flags.get("interaction_plan_v2")),
        "legacy_live_pending_zero": legacy_live == 0,
    }
    result = {
        "ok": bool(schema["ok"] and all(prerequisites.values())),
        "feature_enabled": bool(flags.get(FORMAL_APPROVAL_FEATURE_FLAG)),
        "schema": schema,
        "prerequisites": prerequisites,
        "legacy_live_pending": legacy_live,
        "approval_count": int(counts[0]),
        "pending_count": int(counts[1]),
        "approved_count": int(counts[2]),
        "rollback": "disable_formal_approval_v2_keep_additive_audit_rows",
    }
    result["plan_checksum"] = _sha256(_canonical_json(result))
    return result


def set_formal_approval_feature(
    assistant_conn: sqlite3.Connection,
    task_conn: sqlite3.Connection,
    enabled: bool,
    *,
    expect_plan_checksum: str,
) -> dict:
    plan = formal_approval_cutover_plan(assistant_conn, task_conn)
    if str(expect_plan_checksum or "") != str(plan["plan_checksum"]):
        raise ApprovalError("stale_formal_approval_cutover_plan")
    if enabled and not plan["ok"]:
        raise ApprovalError("formal_approval_cutover_prerequisite_failed")
    assistant_conn.execute(
        """
        INSERT INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,?,?)
        ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (FORMAL_APPROVAL_FEATURE_FLAG, 1 if enabled else 0, utc_now()),
    )
    return formal_approval_cutover_plan(assistant_conn, task_conn)


__all__ = [
    "ALLOWED_EDIT_FIELDS",
    "ApprovalError",
    "FormalApprovalRepository",
    "action_hash",
    "formal_approval_cutover_plan",
    "formal_approval_feature_enabled",
    "set_formal_approval_feature",
    "task_action",
]
