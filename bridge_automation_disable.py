#!/usr/bin/env python3
"""Owner-only, audit-preserving disable operation for durable automations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import sqlite3

from bridge_automation import ensure_automation_tables


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def execute_automation_disable(
    connect: Callable[[], sqlite3.Connection],
    *,
    actor_id: str,
    action: dict,
    trace_id: str,
    authorise_owner: Callable[[sqlite3.Connection, str], None],
    receipt: Callable[..., dict],
) -> dict:
    """Disable the latest context-selected schedule without deleting audit history."""

    target_source = str(action.get("target_source") or "latest")
    source_filter = ""
    if target_source == "github":
        source_filter = """
          AND (
              lower(j.instruction) LIKE '%github%'
              OR lower(j.instruction) LIKE '%githu%'
              OR lower(j.parameters_json) LIKE '%github%'
          )
        """
    try:
        with connect() as conn:
            authorise_owner(conn, actor_id)
            ensure_automation_tables(conn)
            row = conn.execute(
                f"""
                SELECT j.*,
                       COALESCE((
                           SELECT MAX(r.finished_at) FROM automation_runs r
                           WHERE r.job_id=j.id AND r.status='completed'
                       ),'') AS last_completed_at,
                       COALESCE((
                           SELECT COUNT(*) FROM automation_runs r
                           WHERE r.job_id=j.id AND r.status IN ('running','dispatched')
                       ),0) AS active_run_count
                FROM automation_jobs j
                WHERE j.user_id=?
                {source_filter}
                ORDER BY last_completed_at DESC,j.updated_at DESC LIMIT 1
                """,
                (actor_id,),
            ).fetchone()
            if row is None:
                return {
                    "ok": True,
                    "dispatch": "automation_disable_missing",
                    "reply": "没有找到你引用的定时任务，本轮没有停用其他任务。",
                    "action_receipts": [
                        receipt(
                            "automation.schedule.disable",
                            "not_found",
                            reason="target_missing",
                        ),
                    ],
                }
            job = dict(row)
            if not int(job.get("enabled") or 0):
                return {
                    "ok": True,
                    "dispatch": "automation_disable",
                    "reply": "这条定时任务已经停用，不会再次触发；历史运行记录仍保留用于审计。",
                    "automation_job": {
                        "id": job["id"],
                        "enabled": False,
                        "state": "disabled",
                    },
                    "action_receipts": [
                        receipt(
                            "automation.schedule.disable",
                            "no_op",
                            job["id"],
                            already_disabled=True,
                            audit_retained=True,
                            trace_id=_clip(trace_id, 80),
                        ),
                    ],
                }
            if int(job.get("active_run_count") or 0) > 0 or str(job.get("state") or "") in {
                "running",
                "dispatched",
            }:
                return {
                    "ok": True,
                    "dispatch": "automation_disable_busy",
                    "reply": "这条定时任务当前仍有一次运行未结束，本轮没有修改它；请等本次运行结束后再停用。",
                    "automation_job": {
                        "id": job["id"],
                        "enabled": True,
                        "state": job["state"],
                    },
                    "action_receipts": [
                        receipt(
                            "automation.schedule.disable",
                            "blocked",
                            job["id"],
                            reason="run_already_active",
                        ),
                    ],
                }
            updated_at = datetime.now(timezone.utc).isoformat()
            updated = conn.execute(
                """
                UPDATE automation_jobs
                SET enabled=0,state='disabled',next_due_at='',lease_until='',updated_at=?
                WHERE id=? AND user_id=? AND enabled=1
                  AND state NOT IN ('running','dispatched')
                """,
                (updated_at, job["id"], actor_id),
            )
            if updated.rowcount != 1:
                raise ValueError("automation_disable_conflict")
            conn.commit()
        return {
            "ok": True,
            "dispatch": "automation_disable",
            "reply": "已停用你引用的定时任务，以后不会再触发；历史运行和投递记录仅保留用于审计。",
            "automation_job": {
                "id": job["id"],
                "enabled": False,
                "state": "disabled",
                "next_due_at": "",
            },
            "action_receipts": [
                receipt(
                    "automation.schedule.disable",
                    "completed",
                    job["id"],
                    disabled_at=updated_at,
                    previous_next_due_at=str(job.get("next_due_at") or ""),
                    audit_retained=True,
                    trace_id=_clip(trace_id, 80),
                ),
            ],
        }
    except PermissionError:
        return {
            "ok": True,
            "dispatch": "automation_denied",
            "reply": "停用长期自动化需要 Owner 权限，本轮没有修改任何任务。",
            "action_receipts": [
                receipt("automation.schedule.disable", "denied", reason="owner_required"),
            ],
        }
    except (sqlite3.Error, ValueError, KeyError) as exc:
        reason = _clip(exc, 160) or "unknown_error"
        return {
            "ok": True,
            "dispatch": "automation_failed",
            "reply": f"定时任务没有停用成功：{reason}。系统没有把它当作已完成。",
            "action_receipts": [
                receipt("automation.schedule.disable", "failed", reason=reason),
            ],
        }
