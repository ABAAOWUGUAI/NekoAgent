#!/usr/bin/env python3
"""Cross-database read reconciliation for dispatched Automation runs."""

from __future__ import annotations

from bridge_automation import settle_automation_dispatch
from bridge_continuity_kernel import ContinuityKernel
from bridge_continuity_reconciliation import reconcile_continuity_state


FINAL_SUCCESS = {"done"}
FINAL_FAILURE = {"failed", "timeout", "cancelled"}


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
            with task_connect() as task_conn:
                delivery = task_conn.execute(
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
            with task_connect() as task_conn:
                task = task_conn.execute("SELECT status,error FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or str(task[0]) not in FINAL_SUCCESS | FINAL_FAILURE:
                continue
            success = str(task[0]) in FINAL_SUCCESS
            error = "" if success else str(task[1] or task[0])
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
