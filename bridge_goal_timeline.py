#!/usr/bin/env python3
"""User-language timeline projection for Goal continuity records."""

from __future__ import annotations

import sqlite3

from bridge_goal_continuity import get_goal_continuity


FEEDBACK_LABELS = {
    "accepted": "你已确认这一版目标",
    "needs_change": "你提出了修改要求",
    "corrected": "你纠正了这一版目标",
    "rejected": "这一版目标未采用",
}

CHECKPOINT_LABELS = {
    "pending": "等待执行",
    "running": "正在执行",
    "succeeded": "已完成",
    "failed": "执行失败",
    "skipped": "已跳过",
}


def _record_id(prefix: str, item: dict, *fallback_keys: str) -> str:
    identifier = str(item.get("id") or "")
    if not identifier:
        identifier = ":".join(str(item.get(key) or "") for key in fallback_keys).strip(":")
    return f"{prefix}:{identifier or 'unknown'}"


def project_goal_continuity_timeline(
    conn: sqlite3.Connection,
    goal_id: str,
    *,
    limit: int = 200,
) -> dict:
    """Return additive timeline events and a Run-to-Revision lookup.

    The projection deliberately omits raw revision instructions and feedback
    messages.  The task timeline is a progress surface, not a prompt viewer.
    """

    bounded_limit = max(1, min(int(limit or 200), 500))
    try:
        continuity = get_goal_continuity(conn, str(goal_id), limit=bounded_limit)
    except (sqlite3.OperationalError, ValueError):
        return {"events": [], "run_revisions": {}}

    revisions = {
        str(item.get("id") or ""): item
        for item in continuity.get("revisions", [])
        if isinstance(item, dict) and item.get("id")
    }
    run_revisions = {}
    for binding in continuity.get("run_bindings", []):
        if not isinstance(binding, dict):
            continue
        revision_id = str(binding.get("revision_id") or "")
        revision = revisions.get(revision_id, {})
        run_revisions[str(binding.get("run_id") or "")] = {
            "revision_id": revision_id,
            "revision_number": int(revision.get("revision_number") or 0),
        }

    events = []
    for revision_id, revision in revisions.items():
        number = int(revision.get("revision_number") or 0)
        events.append({
            "id": f"revision:{revision_id}",
            "run_id": str(revision.get("source_run_id") or ""),
            "kind": "revision",
            "label": f"第{number}版已建立" if number else "新的目标版本已建立",
            "event_type": "goal.revision_created",
            "revision_id": revision_id,
            "revision_number": number,
            "created_at": str(revision.get("created_at") or ""),
        })

    for feedback in continuity.get("feedback", []):
        if not isinstance(feedback, dict):
            continue
        revision_id = str(feedback.get("revision_id") or "")
        number = int(revisions.get(revision_id, {}).get("revision_number") or 0)
        label = FEEDBACK_LABELS.get(str(feedback.get("kind") or ""), "目标反馈已记录")
        events.append({
            "id": _record_id("feedback", feedback, "created_at", "run_id"),
            "run_id": str(feedback.get("run_id") or ""),
            "kind": "feedback",
            "label": f"第{number}版：{label}" if number else label,
            "event_type": "goal.feedback",
            "revision_id": revision_id,
            "revision_number": number,
            "created_at": str(feedback.get("created_at") or ""),
        })

    for checkpoint in continuity.get("checkpoints", []):
        if not isinstance(checkpoint, dict):
            continue
        status = str(checkpoint.get("status") or "")
        step_key = str(checkpoint.get("step_key") or "步骤")[:120]
        events.append({
            "id": _record_id("checkpoint", checkpoint, "run_id", "step_key"),
            "run_id": str(checkpoint.get("run_id") or ""),
            "kind": "checkpoint",
            "label": f"{step_key}：{CHECKPOINT_LABELS.get(status, status or '状态已更新')}",
            "event_type": "run.checkpoint",
            "created_at": str(checkpoint.get("updated_at") or checkpoint.get("created_at") or ""),
        })

    for artifact in continuity.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        title = str(artifact.get("title") or "未命名成品")[:160]
        version_number = int(artifact.get("version_number") or 0)
        revision_id = str(artifact.get("revision_id") or "")
        events.append({
            "id": _record_id("artifact", artifact, "version_id", "created_at"),
            "run_id": str(artifact.get("source_run_id") or ""),
            "kind": "artifact",
            "label": (
                f"生成成品《{title}》第{version_number}版"
                if version_number else f"生成成品《{title}》"
            ),
            "event_type": "artifact.version_created",
            "revision_id": revision_id,
            "revision_number": int(revisions.get(revision_id, {}).get("revision_number") or 0),
            "artifact_id": str(artifact.get("artifact_id") or ""),
            "version_id": str(artifact.get("version_id") or ""),
            "version_number": version_number,
            "created_at": str(artifact.get("created_at") or ""),
        })
    return {"events": events, "run_revisions": run_revisions}


__all__ = ["project_goal_continuity_timeline"]
