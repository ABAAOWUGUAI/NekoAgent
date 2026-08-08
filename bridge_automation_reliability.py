#!/usr/bin/env python3
"""Cross-database read reconciliation for dispatched Automation runs."""

from __future__ import annotations

from bridge_automation import settle_automation_dispatch
from bridge_automation_business_gate import automation_leak_gate
from bridge_continuity_kernel import ContinuityKernel
from bridge_continuity_reconciliation import reconcile_continuity_state


FINAL_SUCCESS = {"done"}
FINAL_FAILURE = {"failed", "timeout", "cancelled"}


def _task_is_business_success(task: dict | None) -> tuple[bool, str]:
    """A ``done`` task is a business success only with no failure marker.

    The 2026-08-08 defect was ``status=done`` from a completed turn being
    treated as automation success even though the body was an internal
    sandbox/limitation note.  Here we additionally require: the row is a
    terminal ``done``, ``ok`` is truthy, there is no error_kind, and the final
    body is not internal runtime prose.
    """

    if task is None:
        return False, "task_missing"
    status = str(task.get("status") or "")
    if status not in FINAL_SUCCESS | FINAL_FAILURE:
        return False, "task_not_terminal"
    if status not in FINAL_SUCCESS:
        return False, str(task.get("error") or status)
    if not task.get("ok"):
        return False, "task_ok_false"
    error_kind = str(task.get("error_kind") or "")
    if error_kind:
        return False, f"task_error_kind_{error_kind}"
    output = str(task.get("output") or "")
    leak = automation_leak_gate(output)
    if not leak.get("ok"):
        return False, "no_business_evidence"
    return True, ""


def reconcile_automation_tasks(assistant_connect, task_connect, *, limit: int = 50) -> dict:
    with assistant_connect() as conn:
        rows = conn.execute(
            """SELECT task_id,delivery_id FROM automation_runs
               WHERE status='dispatched' AND (task_id<>'' OR delivery_id<>'')
               ORDER BY started_at LIMIT ?""",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    completed = failed = 0
    for row in rows:
        task_id = str(row[0] or "")
        delivery_id = str(row[1] or "")
        success = False
        error = ""
        if delivery_id:
            with task_connect() as conn:
                delivery = conn.execute(
                    """SELECT acked_at,dead_letter,last_error,delivery_certainty
                       FROM delivery_outbox WHERE id=?""",
                    (delivery_id,),
                ).fetchone()
            if not delivery:
                continue
            if str(delivery[0] or "") or str(delivery[3] or "") == "confirmed":
                success = True
            elif int(delivery[1] or 0):
                error = str(delivery[2] or "delivery_dead_letter")
            else:
                continue
        else:
            with task_connect() as conn:
                task = conn.execute(
                    """SELECT status,error,ok,output,error_kind FROM tasks WHERE id=?""",
                    (task_id,),
                ).fetchone()
            if not task or str(task[0]) not in FINAL_SUCCESS | FINAL_FAILURE:
                continue
            success, task_error = _task_is_business_success(
                {
                    "status": str(task[0] or ""),
                    "error": str(task[1] or ""),
                    "ok": task[2],
                    "output": str(task[3] or ""),
                    "error_kind": str(task[4] or ""),
                }
            )
            if not success:
                error = task_error or str(task[1] or task[0])
        with assistant_connect() as conn:
            settle_automation_dispatch(
                conn,
                delivery_id=delivery_id,
                task_id=task_id,
                status="completed" if success else "failed",
                error="" if success else error,
            )
        completed += int(success)
        failed += int(not success)
    reconcile_continuity_state(
        ContinuityKernel(assistant_connect),
        task_connect,
        limit=max(50, min(int(limit) * 4, 1000)),
    )
    return {"completed": completed, "failed": failed}


__all__ = ["reconcile_automation_tasks"]
