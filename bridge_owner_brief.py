"""Bounded, public B2 Owner Brief projection.

This module intentionally does not own workflow state.  It turns already
authorized domain rows into a small daily read model and never promotes an old
or terminal record into an Owner decision merely because it still exists.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping

from bridge_artifact_repository import ArtifactRepository
from bridge_assistant_identity import current_assistant
from bridge_migrations import utc_now
from bridge_platform_repository import PlatformRepository
from bridge_formal_approval import FormalApprovalRepository


MAX_SECTION_ITEMS = 6
MAX_CONTINUATION_ITEMS = 2
OUTCOME_FRESHNESS = timedelta(days=3)
ACTIVE_GOAL_STATES = frozenset({"active"})
ACTIVE_RUN_STATES = frozenset({"queued", "running", "waiting_approval"})
MATERIAL_QUALITY_OUTCOMES = frozenset({"blocked", "failed", "error"})
MATERIAL_DELIVERY_STATES = frozenset({"dead_letter", "ambiguous"})


def _safe_limit(value: int) -> int:
    return max(1, min(int(value or MAX_SECTION_ITEMS), MAX_SECTION_ITEMS))


def _text(value: object, *, limit: int = 120) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def _timestamp(value: object) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fresh(value: object, *, now: str) -> bool:
    return _timestamp(value) >= _timestamp(now) - OUTCOME_FRESHNESS


def _sort_recent(records: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return sorted(records, key=lambda item: _timestamp(item.get("updated_at") or item.get("created_at")), reverse=True)


def _public_assistant(assistant: Mapping[str, object] | None) -> dict | None:
    if not assistant:
        return None
    return {
        "id": _text(assistant.get("id"), limit=160),
        "display_name": _text(assistant.get("display_name"), limit=80),
        "status": _text(assistant.get("status"), limit=40),
        "updated_at": _text(assistant.get("updated_at"), limit=64),
    }


def _continuation(web_threads: Iterable[Mapping[str, object]], *, limit: int, now: str) -> list[dict]:
    records: list[dict] = []
    for thread in _sort_recent(web_threads):
        if str(thread.get("channel_type") or "") != "web":
            continue
        if not _fresh(thread.get("updated_at") or thread.get("created_at"), now=now):
            continue
        thread_id = _text(thread.get("id"), limit=160)
        if not thread_id:
            continue
        records.append({
            "source_type": "conversation",
            "source_id": thread_id,
            "owner": "chat",
            "allowed_action": "continue_web_conversation",
            "updated_at": _text(thread.get("updated_at"), limit=64),
        })
        if len(records) >= min(limit, MAX_CONTINUATION_ITEMS):
            break
    return records


def _decisions(approvals: Iterable[Mapping[str, object]], *, limit: int) -> list[dict]:
    records: list[dict] = []
    for approval in _sort_recent(approvals):
        if str(approval.get("status") or "") != "pending":
            continue
        approval_id = _text(approval.get("id"), limit=160)
        if not approval_id:
            continue
        records.append({
            "source_type": "approval",
            "source_id": approval_id,
            "owner": "work",
            "allowed_action": "review_approval",
            "title": _text(approval.get("action_summary") or "待审核操作"),
            "updated_at": _text(approval.get("updated_at") or approval.get("created_at"), limit=64),
        })
        if len(records) >= limit:
            break
    return records


def _in_progress(goals: Iterable[Mapping[str, object]], runs: Iterable[Mapping[str, object]], *, limit: int) -> list[dict]:
    active_runs: dict[str, Mapping[str, object]] = {}
    for run in _sort_recent(runs):
        if str(run.get("status") or "") not in ACTIVE_RUN_STATES:
            continue
        goal_id = _text(run.get("goal_id"), limit=160)
        if goal_id and goal_id not in active_runs:
            active_runs[goal_id] = run

    records: list[dict] = []
    for goal in _sort_recent(goals):
        if str(goal.get("status") or "") not in ACTIVE_GOAL_STATES:
            continue
        goal_id = _text(goal.get("id"), limit=160)
        if not goal_id:
            continue
        run = active_runs.get(goal_id)
        records.append({
            "source_type": "goal",
            "source_id": goal_id,
            "owner": "work",
            "allowed_action": "open_work",
            "title": _text(goal.get("title") or "未命名委托"),
            "state": _text((run or {}).get("status") or "active", limit=40),
            "updated_at": _text((run or {}).get("updated_at") or goal.get("updated_at"), limit=64),
        })
        if len(records) >= limit:
            break
    return records


def _recent_outcomes(
    *,
    artifacts: Iterable[Mapping[str, object]],
    deliveries: Iterable[Mapping[str, object]],
    quality_events: Iterable[Mapping[str, object]],
    runs: Iterable[Mapping[str, object]],
    limit: int,
    now: str,
) -> list[dict]:
    records: list[dict] = []
    for artifact in _sort_recent(artifacts):
        updated_at = artifact.get("updated_at") or artifact.get("created_at")
        if not _fresh(updated_at, now=now):
            continue
        artifact_id = _text(artifact.get("id"), limit=160)
        if artifact_id:
            records.append({
                "source_type": "artifact",
                "source_id": artifact_id,
                "owner": "artifacts",
                "allowed_action": "open_artifact",
                "title": _text(artifact.get("title") or "新成果"),
                "state": _text(artifact.get("state") or "available", limit=40),
                "updated_at": _text(updated_at, limit=64),
            })
    for delivery in _sort_recent(deliveries):
        updated_at = delivery.get("updated_at") or delivery.get("created_at")
        state = str(delivery.get("state") or "")
        if state not in MATERIAL_DELIVERY_STATES or not _fresh(updated_at, now=now):
            continue
        delivery_id = _text(delivery.get("id"), limit=160)
        if delivery_id:
            records.append({
                "source_type": "delivery",
                "source_id": delivery_id,
                "owner": "qq",
                "allowed_action": "open_delivery",
                "state": _text(state, limit=40),
                "updated_at": _text(updated_at, limit=64),
            })
    for event in _sort_recent(quality_events):
        updated_at = event.get("updated_at") or event.get("created_at")
        outcome = str(event.get("outcome") or "")
        if outcome not in MATERIAL_QUALITY_OUTCOMES or not _fresh(updated_at, now=now):
            continue
        event_id = _text(event.get("id"), limit=160)
        if event_id:
            records.append({
                "source_type": "quality_receipt",
                "source_id": event_id,
                "owner": "qq",
                "allowed_action": "open_qq_event",
                "state": _text(outcome, limit=40),
                "reason_code": _text(event.get("reason_code"), limit=80),
                "updated_at": _text(updated_at, limit=64),
            })
    for run in _sort_recent(runs):
        updated_at = run.get("updated_at") or run.get("created_at")
        recovery_action = str(run.get("recovery_action") or "")
        if str(run.get("status") or "") not in {"failed", "timed_out"} or not recovery_action:
            continue
        if not _fresh(updated_at, now=now):
            continue
        run_id = _text(run.get("id"), limit=160)
        if run_id:
            records.append({
                "source_type": "run",
                "source_id": run_id,
                "owner": "work",
                "allowed_action": _text(recovery_action, limit=80),
                "state": _text(run.get("status"), limit=40),
                "updated_at": _text(updated_at, limit=64),
            })
    return sorted(records, key=lambda item: _timestamp(item["updated_at"]), reverse=True)[:limit]


def build_owner_brief(
    *,
    assistant: Mapping[str, object] | None,
    web_threads: Iterable[Mapping[str, object]],
    goals: Iterable[Mapping[str, object]],
    runs: Iterable[Mapping[str, object]],
    approvals: Iterable[Mapping[str, object]],
    artifacts: Iterable[Mapping[str, object]],
    deliveries: Iterable[Mapping[str, object]],
    quality_events: Iterable[Mapping[str, object]],
    limit: int = MAX_SECTION_ITEMS,
    now: str,
) -> dict:
    """Return the bounded public B2 Owner Brief projection."""

    safe_limit = _safe_limit(limit)
    goals = list(goals)
    runs = list(runs)
    return {
        "generated_at": _text(now, limit=64),
        "assistant": _public_assistant(assistant),
        "continuation": _continuation(web_threads, limit=safe_limit, now=now),
        "decisions": _decisions(approvals, limit=safe_limit),
        "in_progress": _in_progress(goals, runs, limit=safe_limit),
        "recent_outcomes": _recent_outcomes(
            artifacts=artifacts,
            deliveries=deliveries,
            quality_events=quality_events,
            runs=runs,
            limit=safe_limit,
            now=now,
        ),
    }


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [str(column[0]) for column in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def collect_owner_brief_sources(
    *,
    assistant_connect: Callable[[], sqlite3.Connection],
    task_connect: Callable[[], sqlite3.Connection],
    delivery_reader: Callable[[int], list[dict]],
    quality_reader: Callable[[int], list[dict]] | None = None,
    limit: int,
) -> dict:
    """Read the smallest existing collections needed for one Owner Brief."""

    safe_limit = _safe_limit(limit)
    with assistant_connect() as assistant_conn:
        assistant = current_assistant(assistant_conn, integrity_scope="identity")
        web_threads: list[dict] = []
        if assistant:
            web_threads = _rows(assistant_conn.execute(
                """
                SELECT id,channel_type,status,created_at,updated_at
                FROM conversation_threads
                WHERE owner_actor_id=? AND assistant_id=? AND channel_type='web'
                ORDER BY updated_at DESC,id DESC LIMIT ?
                """,
                (
                    str(assistant.get("owner_actor_id") or ""),
                    str(assistant.get("id") or ""),
                    min(safe_limit, MAX_CONTINUATION_ITEMS),
                ),
            ))

    with task_connect() as task_conn:
        repository = PlatformRepository(task_conn)
        approvals = FormalApprovalRepository(task_conn).list(status="pending", limit=safe_limit)
        goals = repository.list_goals(status="active", limit=safe_limit)
        runs = _rows(task_conn.execute(
            """
            SELECT * FROM runs
            WHERE status IN ('queued','running','waiting_approval')
            ORDER BY updated_at DESC,id DESC LIMIT ?
            """,
            (safe_limit,),
        ))
        owner_id = str((assistant or {}).get("owner_actor_id") or "")
        artifacts = ArtifactRepository(task_conn).list_artifacts(owner_id=owner_id, limit=safe_limit)

    return {
        "assistant": assistant,
        "web_threads": web_threads,
        "goals": goals,
        "runs": runs,
        "approvals": approvals,
        "artifacts": artifacts,
        "deliveries": list(delivery_reader(safe_limit)),
        "quality_events": list((quality_reader or (lambda _limit: []))(safe_limit)),
    }


class OwnerBriefService:
    """Small cached facade around a bounded, caller-owned source reader."""

    def __init__(
        self,
        source_reader: Callable[[int], Mapping[str, object]],
        *,
        now: Callable[[], str] = utc_now,
        cache_ttl_seconds: float = 2.0,
    ) -> None:
        self._source_reader = source_reader
        self._now = now
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._cache: dict[int, tuple[float, dict]] = {}
        self._lock = threading.RLock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def brief(self, *, limit: int = MAX_SECTION_ITEMS, force: bool = False) -> dict:
        safe_limit = _safe_limit(limit)
        with self._lock:
            cached = self._cache.get(safe_limit)
            if cached and not force and time.monotonic() - cached[0] < self._cache_ttl_seconds:
                return cached[1]
        sources = dict(self._source_reader(safe_limit))
        result = build_owner_brief(
            assistant=sources.get("assistant"),
            web_threads=sources.get("web_threads") or (),
            goals=sources.get("goals") or (),
            runs=sources.get("runs") or (),
            approvals=sources.get("approvals") or (),
            artifacts=sources.get("artifacts") or (),
            deliveries=sources.get("deliveries") or (),
            quality_events=sources.get("quality_events") or (),
            limit=safe_limit,
            now=self._now(),
        )
        with self._lock:
            self._cache[safe_limit] = (time.monotonic(), result)
        return result


__all__ = [
    "ACTIVE_GOAL_STATES",
    "ACTIVE_RUN_STATES",
    "MAX_CONTINUATION_ITEMS",
    "MAX_SECTION_ITEMS",
    "OUTCOME_FRESHNESS",
    "OwnerBriefService",
    "build_owner_brief",
    "collect_owner_brief_sources",
]
