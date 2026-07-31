#!/usr/bin/env python3
"""Goal revision, Run checkpoint and user feedback lifecycle on task SQLite."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Mapping

from bridge_goal_continuity_schema import require_goal_continuity_schema
from bridge_goal_revision_link_schema import require_goal_revision_link_schema
from bridge_migrations import utc_now
from bridge_platform_repository import PlatformRepository


FEEDBACK_KINDS = {"accepted", "needs_change", "rejected", "corrected"}
CHECKPOINT_STATUSES = {"pending", "running", "succeeded", "failed", "skipped"}


@contextmanager
def _write(conn: sqlite3.Connection):
    """Serialize continuity mutations while preserving an existing outer transaction."""

    conn.execute("PRAGMA busy_timeout = 10000")
    if conn.in_transaction:
        savepoint = "goal_continuity_" + uuid.uuid4().hex
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("feedback_json", "payload_json", "experience_candidate_json"):
        if key in item:
            try:
                item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
            except (TypeError, json.JSONDecodeError):
                item[key.removesuffix("_json")] = {}
    return item


def _json(value: object) -> str:
    return json.dumps(value if isinstance(value, Mapping) else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _goal(conn: sqlite3.Connection, goal_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM goals WHERE id=?", (str(goal_id).strip(),)).fetchone()
    if row is None:
        raise ValueError("goal_not_found")
    return row


def _create_revision(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    instruction: str,
    actor_id: str,
    channel: str,
    source_run_id: str = "",
    parent_revision_id: str = "",
    feedback: Mapping[str, object] | None = None,
    idempotency_key: str = "",
) -> dict:
    require_goal_continuity_schema(conn)
    _goal(conn, goal_id)
    instruction = str(instruction or "").strip()[:12000]
    if not instruction:
        raise ValueError("goal_revision_instruction_required")
    key = str(idempotency_key or "").strip()[:300]
    if key:
        existing = conn.execute("SELECT * FROM goal_revisions WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            expected = {
                "goal_id": str(goal_id),
                "instruction": instruction,
                "actor_id": str(actor_id or "")[:200],
                "channel": str(channel or "")[:80],
                "source_run_id": str(source_run_id or "")[:200],
            }
            if any(str(existing[field]) != value for field, value in expected.items()):
                raise ValueError("goal_revision_idempotency_key_reused")
            return _row(existing) | {"replayed": True}
    latest = conn.execute(
        "SELECT * FROM goal_revisions WHERE goal_id=? ORDER BY revision_number DESC LIMIT 1",
        (goal_id,),
    ).fetchone()
    number = int(latest["revision_number"] or 0) + 1 if latest else 1
    parent = str(parent_revision_id or (latest["id"] if latest else "")).strip()
    now = utc_now()
    revision_id = "goal-revision-" + uuid.uuid4().hex
    conn.execute(
        "UPDATE goal_revisions SET status='superseded',updated_at=? WHERE goal_id=? AND status='active'",
        (now, goal_id),
    )
    conn.execute(
        """INSERT INTO goal_revisions(
            id,goal_id,revision_number,parent_revision_id,instruction,status,actor_id,channel,
            source_run_id,feedback_json,idempotency_key,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            revision_id, goal_id, number, parent, instruction, "active", str(actor_id or "")[:200],
            str(channel or "")[:80], str(source_run_id or "")[:200], _json(feedback), key, now, now,
        ),
    )
    conn.execute(
        "UPDATE goals SET status='active',completed_at='',updated_at=?,version=version+1 WHERE id=?",
        (now, goal_id),
    )
    return _row(conn.execute("SELECT * FROM goal_revisions WHERE id=?", (revision_id,)).fetchone())


def create_goal_revision(
    conn: sqlite3.Connection,
    goal_id: str,
    instruction: str,
    *,
    actor_id: str = "",
    channel: str = "",
    source_run_id: str = "",
    parent_revision_id: str = "",
    idempotency_key: str = "",
) -> dict:
    with _write(conn):
        return _create_revision(
            conn,
            goal_id=goal_id,
            instruction=instruction,
            actor_id=actor_id,
            channel=channel,
            source_run_id=source_run_id,
            parent_revision_id=parent_revision_id,
            idempotency_key=idempotency_key,
        )


def _bind_revision_run(
    conn: sqlite3.Connection,
    revision_id: str,
    run_id: str,
    binding_kind: str,
) -> dict:
    require_goal_continuity_schema(conn)
    require_goal_revision_link_schema(conn)
    binding_kind = str(binding_kind or "").strip()
    if binding_kind not in {"initial", "revision", "follow_up", "retry", "migration"}:
        raise ValueError("goal_revision_run_binding_kind_invalid")
    row = conn.execute(
        """SELECT gr.goal_id AS revision_goal_id,r.goal_id AS run_goal_id
        FROM goal_revisions gr CROSS JOIN runs r WHERE gr.id=? AND r.id=?""",
        (str(revision_id).strip(), str(run_id).strip()),
    ).fetchone()
    if row is None:
        raise ValueError("goal_revision_or_run_not_found")
    if str(row["revision_goal_id"]) != str(row["run_goal_id"]):
        raise ValueError("goal_revision_run_goal_mismatch")
    existing = conn.execute("SELECT * FROM goal_revision_runs WHERE run_id=?", (run_id,)).fetchone()
    if existing is not None:
        if str(existing["revision_id"]) != str(revision_id):
            raise ValueError("goal_revision_run_binding_conflict")
        return _row(existing) | {"replayed": True}
    conn.execute(
        "INSERT INTO goal_revision_runs(run_id,revision_id,binding_kind,created_at) VALUES(?,?,?,?)",
        (run_id, revision_id, binding_kind, utc_now()),
    )
    return _row(conn.execute("SELECT * FROM goal_revision_runs WHERE run_id=?", (run_id,)).fetchone()) | {"replayed": False}


def bind_revision_run(
    conn: sqlite3.Connection,
    revision_id: str,
    run_id: str,
    *,
    binding_kind: str = "revision",
) -> dict:
    with _write(conn):
        return _bind_revision_run(conn, revision_id, run_id, binding_kind)


def _ensure_task_revision_binding(conn: sqlite3.Connection, task: Mapping[str, object]) -> dict:
    """Idempotently bind a projected legacy Task/Run to one Goal Revision."""

    require_goal_continuity_schema(conn)
    require_goal_revision_link_schema(conn)
    run_id = str(task.get("run_id") or "").strip()
    goal_id = str(task.get("goal_id") or "").strip()
    if not run_id or not goal_id:
        raise ValueError("projected_goal_and_run_required")
    existing = conn.execute("SELECT * FROM goal_revision_runs WHERE run_id=?", (run_id,)).fetchone()
    if existing is not None:
        return _row(existing) | {"replayed": True}

    source_task_id = str(task.get("source_task_id") or "").strip()
    source_binding = None
    if source_task_id:
        source_binding = conn.execute(
            """SELECT l.* FROM goal_revision_runs l
            JOIN runs r ON r.id=l.run_id WHERE r.legacy_task_id=? LIMIT 1""",
            (source_task_id,),
        ).fetchone()
    latest = conn.execute(
        "SELECT * FROM goal_revisions WHERE goal_id=? ORDER BY revision_number DESC LIMIT 1",
        (goal_id,),
    ).fetchone()
    follow_up = bool(str(task.get("follow_up_source_task_id") or "").strip())
    artifact_revision = str(task.get("intent") or "").strip() == "artifact_revision"
    instruction = str(task.get("origin_message") or task.get("summary") or "继续当前目标").strip()[:12000]

    binding_kind = "initial"
    revision_id = ""
    if follow_up or artifact_revision:
        latest_is_unbound = bool(latest) and conn.execute(
            "SELECT 1 FROM goal_revision_runs WHERE revision_id=? LIMIT 1",
            (str(latest["id"]),),
        ).fetchone() is None
        if latest_is_unbound:
            revision_id = str(latest["id"])
        else:
            revision = _create_revision(
                conn,
                goal_id=goal_id,
                instruction=instruction,
                actor_id=str(task.get("user_id") or ""),
                channel=str(task.get("source") or ""),
                source_run_id=str(source_binding["run_id"] if source_binding else ""),
                parent_revision_id=str(latest["id"] if latest else ""),
            )
            revision_id = str(revision["id"])
        binding_kind = "revision" if artifact_revision else "follow_up"
    elif source_binding is not None:
        revision_id = str(source_binding["revision_id"])
        binding_kind = "retry"
    elif latest is not None:
        revision_id = str(latest["id"])
    else:
        revision = _create_revision(
            conn,
            goal_id=goal_id,
            instruction=instruction,
            actor_id=str(task.get("user_id") or ""),
            channel=str(task.get("source") or ""),
            source_run_id="",
        )
        revision_id = str(revision["id"])
    return _bind_revision_run(conn, revision_id, run_id, binding_kind)


def ensure_task_revision_binding(conn: sqlite3.Connection, task: Mapping[str, object]) -> dict:
    with _write(conn):
        return _ensure_task_revision_binding(conn, task)


def _record_run_checkpoint(
    conn: sqlite3.Connection,
    run_id: str,
    step_key: str,
    status: str,
    *,
    summary: str = "",
    payload: Mapping[str, object] | None = None,
) -> dict:
    require_goal_continuity_schema(conn)
    run = conn.execute("SELECT id FROM runs WHERE id=?", (str(run_id).strip(),)).fetchone()
    if run is None:
        raise ValueError("run_not_found")
    step_key = str(step_key or "").strip()[:200]
    status = str(status or "").strip().lower()
    if not step_key:
        raise ValueError("checkpoint_step_required")
    if status not in CHECKPOINT_STATUSES:
        raise ValueError("checkpoint_status_invalid")
    now = utc_now()
    checkpoint_id = "checkpoint-" + uuid.uuid4().hex
    conn.execute(
        """INSERT INTO run_checkpoints(id,run_id,step_key,status,summary,payload_json,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,step_key) DO UPDATE SET
          status=excluded.status,summary=excluded.summary,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
        (checkpoint_id, run_id, step_key, status, str(summary or "")[:2000], _json(payload), now, now),
    )
    return _row(conn.execute("SELECT * FROM run_checkpoints WHERE run_id=? AND step_key=?", (run_id, step_key)).fetchone())


def record_run_checkpoint(
    conn: sqlite3.Connection,
    run_id: str,
    step_key: str,
    status: str,
    *,
    summary: str = "",
    payload: Mapping[str, object] | None = None,
) -> dict:
    with _write(conn):
        return _record_run_checkpoint(
            conn, run_id, step_key, status, summary=summary, payload=payload,
        )


def _record_goal_feedback(
    conn: sqlite3.Connection,
    goal_id: str,
    kind: str,
    *,
    message: str = "",
    revision_id: str = "",
    run_id: str = "",
    artifact_id: str = "",
    actor_id: str = "",
    channel: str = "",
    idempotency_key: str = "",
) -> dict:
    require_goal_continuity_schema(conn)
    _goal(conn, goal_id)
    kind = str(kind or "").strip().lower()
    if kind not in FEEDBACK_KINDS:
        raise ValueError("goal_feedback_kind_invalid")
    message = str(message or "").strip()[:4000]
    key = str(idempotency_key or "").strip()[:300] or ("feedback-" + uuid.uuid4().hex)
    existing = conn.execute("SELECT * FROM goal_feedback WHERE idempotency_key=?", (key,)).fetchone()
    if existing:
        expected = {
            "goal_id": str(goal_id),
            "revision_id": str(revision_id or ""),
            "run_id": str(run_id or ""),
            "artifact_id": str(artifact_id or ""),
            "kind": kind,
            "message": message,
            "actor_id": str(actor_id or "")[:200],
            "channel": str(channel or "")[:80],
        }
        if any(str(existing[field]) != value for field, value in expected.items()):
            raise ValueError("goal_feedback_idempotency_key_reused")
        return {"feedback": _row(existing), "replayed": True}
    revision = None
    if revision_id:
        revision = conn.execute("SELECT * FROM goal_revisions WHERE id=? AND goal_id=?", (revision_id, goal_id)).fetchone()
        if revision is None:
            raise ValueError("goal_revision_not_found")
        if str(revision["status"]) != "active":
            raise ValueError("goal_feedback_revision_conflict")
    if run_id and conn.execute("SELECT 1 FROM runs WHERE id=? AND goal_id=?", (run_id, goal_id)).fetchone() is None:
        raise ValueError("goal_feedback_run_not_found")
    now = utc_now()
    if kind in {"needs_change", "corrected"} and not message:
        raise ValueError("goal_feedback_message_required")
    experience = {
        "kind": "experience",
        "source": "goal_feedback",
        "feedback_kind": kind,
        "goal_id": goal_id,
        "revision_id": revision_id,
        "run_id": run_id,
        "artifact_id": artifact_id,
        "requires_review": True,
    }
    feedback_id = "goal-feedback-" + uuid.uuid4().hex
    conn.execute(
        """INSERT INTO goal_feedback(
            id,goal_id,revision_id,run_id,artifact_id,kind,message,actor_id,channel,
            idempotency_key,experience_candidate_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (feedback_id, goal_id, revision_id, run_id, artifact_id, kind, message, str(actor_id or "")[:200], str(channel or "")[:80], key, _json(experience), now),
    )
    new_revision = None
    if kind in {"needs_change", "corrected"}:
        new_revision = _create_revision(
            conn,
            goal_id=goal_id,
            instruction=message,
            actor_id=actor_id,
            channel=channel,
            source_run_id=run_id,
            parent_revision_id=revision_id,
            feedback={"feedback_id": feedback_id, "kind": kind},
        )
        if revision is not None:
            conn.execute("UPDATE goal_revisions SET feedback_json=?,updated_at=? WHERE id=?", (_json({"feedback_id": feedback_id, "kind": kind}), now, revision_id))
    else:
        if revision_id:
            revision_status = "accepted" if kind == "accepted" else "rejected"
            conn.execute("UPDATE goal_revisions SET status=?,feedback_json=?,updated_at=? WHERE id=?", (revision_status, _json({"feedback_id": feedback_id, "kind": kind}), now, revision_id))
        if kind == "accepted":
            conn.execute("UPDATE goals SET status='completed',completed_at=?,updated_at=?,version=version+1 WHERE id=?", (now, now, goal_id))
        elif kind == "rejected":
            conn.execute(
                "UPDATE goals SET status='waiting_user',completed_at='',updated_at=?,version=version+1 WHERE id=?",
                (now, goal_id),
            )
    if run_id:
        conn.execute(
            "INSERT INTO run_events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (run_id, "goal.feedback", _json({"feedback_id": feedback_id, "goal_id": goal_id, "kind": kind, "revision_id": revision_id, "experience_candidate": True}), now),
        )
    return {
        "feedback": _row(conn.execute("SELECT * FROM goal_feedback WHERE id=?", (feedback_id,)).fetchone()),
        "revision": _row(conn.execute("SELECT * FROM goal_revisions WHERE id=?", (revision_id,)).fetchone()) if revision_id else None,
        "new_revision": new_revision,
        "experience_candidate": experience,
        "replayed": False,
    }


def record_goal_feedback(
    conn: sqlite3.Connection,
    goal_id: str,
    kind: str,
    *,
    message: str = "",
    revision_id: str = "",
    run_id: str = "",
    artifact_id: str = "",
    actor_id: str = "",
    channel: str = "",
    idempotency_key: str = "",
) -> dict:
    with _write(conn):
        return _record_goal_feedback(
            conn,
            goal_id,
            kind,
            message=message,
            revision_id=revision_id,
            run_id=run_id,
            artifact_id=artifact_id,
            actor_id=actor_id,
            channel=channel,
            idempotency_key=idempotency_key,
        )


def get_goal_continuity(conn: sqlite3.Connection, goal_id: str, *, limit: int = 100) -> dict:
    require_goal_continuity_schema(conn)
    require_goal_revision_link_schema(conn)
    goal = _goal(conn, goal_id)
    n = max(1, min(int(limit or 100), 500))
    revisions = [_row(row) for row in conn.execute("SELECT * FROM goal_revisions WHERE goal_id=? ORDER BY revision_number DESC LIMIT ?", (goal_id, n)).fetchall()]
    feedback = [_row(row) for row in conn.execute("SELECT * FROM goal_feedback WHERE goal_id=? ORDER BY created_at DESC LIMIT ?", (goal_id, n)).fetchall()]
    runs = PlatformRepository(conn).list_runs(goal_id=goal_id, limit=n)
    checkpoints = []
    for run in runs:
        checkpoints.extend(_row(row) for row in conn.execute("SELECT * FROM run_checkpoints WHERE run_id=? ORDER BY updated_at DESC", (run["id"],)).fetchall())
    bindings = [_row(row) for row in conn.execute(
        """SELECT l.*,r.status AS run_status,r.legacy_task_id,r.created_at AS run_created_at
        FROM goal_revision_runs l JOIN runs r ON r.id=l.run_id
        WHERE r.goal_id=? ORDER BY r.created_at DESC LIMIT ?""",
        (goal_id, n),
    ).fetchall()]
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    artifacts = []
    if {"artifacts", "artifact_versions"}.issubset(tables):
        artifacts = [_row(row) for row in conn.execute(
            """SELECT l.revision_id,av.source_run_id,a.id AS artifact_id,a.title,a.kind,
                   av.id AS version_id,av.version_number,av.state,av.created_at
            FROM goal_revision_runs l
            JOIN artifact_versions av ON av.source_run_id=l.run_id
            JOIN artifacts a ON a.id=av.artifact_id
            WHERE a.source_goal_id=? AND a.deleted_at=''
            ORDER BY av.created_at DESC,av.version_number DESC LIMIT ?""",
            (goal_id, n),
        ).fetchall()]
    return {
        "goal": dict(goal), "revisions": revisions, "run_bindings": bindings,
        "artifacts": artifacts, "feedback": feedback, "runs": runs,
        "checkpoints": checkpoints[:n],
    }


def list_run_checkpoints(conn: sqlite3.Connection, run_id: str, *, limit: int = 100) -> list[dict]:
    require_goal_continuity_schema(conn)
    if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
        raise ValueError("run_not_found")
    return [_row(row) for row in conn.execute("SELECT * FROM run_checkpoints WHERE run_id=? ORDER BY updated_at DESC LIMIT ?", (run_id, max(1, min(int(limit or 100), 500)))).fetchall()]


__all__ = [
    "CHECKPOINT_STATUSES",
    "FEEDBACK_KINDS",
    "bind_revision_run",
    "create_goal_revision",
    "ensure_task_revision_binding",
    "get_goal_continuity",
    "list_run_checkpoints",
    "record_goal_feedback",
    "record_run_checkpoint",
]
