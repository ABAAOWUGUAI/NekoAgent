#!/usr/bin/env python3
"""Durable quiet-gap candidates for natural group participation.

The queue stores only message identifiers and delivery metadata.  Message text
remains in the governed group message store and is read only when a candidate
is claimed after the quiet gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from bridge_group_participation_schema import GROUP_PARTICIPATION_QUEUE_TABLE
from bridge_migrations import utc_now


def _parse(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        result = datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def enqueue_group_candidate(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    current: dict,
    session: str,
    sender_id: str,
    sender_name: str,
    external_message_id: str,
    quiet_gap_seconds: int | None = None,
    active_topic_window_seconds: int | None = None,
    message_kind: str = "text",
    has_text_anchor: bool | None = None,
) -> dict:
    now = _parse(current.get("created_at"))
    gap_seconds = 8 if quiet_gap_seconds is None else int(quiet_gap_seconds)
    quiet_due = now + timedelta(seconds=max(0, gap_seconds))
    group = str(group_id or "").strip()
    if not group:
        raise ValueError("group_participation_group_required")
    existing = conn.execute(
        f"SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?", (group,)
    ).fetchone()
    if existing and str(existing["state"] or "") == "claimed":
        lease_value = str(existing["lease_expires_at"] or "")
        lease = _parse(lease_value) if lease_value else None
        if lease is not None and lease > now:
            preserved = dict(existing)
            preserved["replaced_message_id"] = 0
            preserved["joined_active_topic"] = True
            preserved["candidate_due_reason"] = "active_topic_window"
            preserved["preserved_claimed_candidate"] = True
            return preserved

    topic_window = max(max(0, gap_seconds), min(int(active_topic_window_seconds or 45), 600))
    terminal_states = {"completed", "failed", "cancelled"}
    existing_state = str(existing["state"] or "") if existing else ""
    existing_first = _parse(existing["first_message_at"]) if existing else now
    starts_fresh = bool(
        not existing
        or existing_state in terminal_states
        or now - existing_first > timedelta(minutes=5)
    )
    first = now if starts_fresh else existing_first
    due = min(quiet_due, first + timedelta(seconds=topic_window))
    kind = str(message_kind or "").strip().lower()
    recognized_text_kind = kind in {"text", "mixed"}
    is_text_anchor = recognized_text_kind and (
        has_text_anchor is None or bool(has_text_anchor)
    )
    initial_anchor_id = int(current.get("id") or 0) if is_text_anchor else 0
    initial_anchor_external_id = str(external_message_id or "") if is_text_anchor else ""
    initial_anchor_sender_id = str(sender_id or "") if is_text_anchor else ""
    initial_latest_text_id = int(current.get("id") or 0) if is_text_anchor else 0
    replaced_message_id = (
        int(existing["latest_message_id"] or 0)
        if existing
        and starts_fresh
        and existing_state in {"pending", "claimed"}
        and int(existing["latest_message_id"] or 0) != int(current.get("id") or 0)
        else 0
    )
    conn.execute(
        f"""
        INSERT INTO {GROUP_PARTICIPATION_QUEUE_TABLE}(
            group_id,state,first_message_at,last_message_at,due_at,latest_message_id,
            latest_sender_id,latest_sender_name,latest_session,latest_external_message_id,
            anchor_message_id,anchor_external_message_id,anchor_sender_id,latest_text_message_id,
            candidate_revision,attempt,lease_expires_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,'',?)
        ON CONFLICT(group_id) DO UPDATE SET
            state='pending',
            first_message_at=excluded.first_message_at,
            last_message_at=excluded.last_message_at,
            due_at=excluded.due_at,
            latest_message_id=excluded.latest_message_id,
            latest_sender_id=excluded.latest_sender_id,
            latest_sender_name=excluded.latest_sender_name,
            latest_session=excluded.latest_session,
            latest_external_message_id=excluded.latest_external_message_id,
            anchor_message_id=CASE
                WHEN ? THEN excluded.anchor_message_id
                WHEN {GROUP_PARTICIPATION_QUEUE_TABLE}.anchor_message_id=0
                     AND excluded.anchor_message_id<>0
                THEN excluded.anchor_message_id
                ELSE {GROUP_PARTICIPATION_QUEUE_TABLE}.anchor_message_id
            END,
            anchor_external_message_id=CASE
                WHEN ? THEN excluded.anchor_external_message_id
                WHEN {GROUP_PARTICIPATION_QUEUE_TABLE}.anchor_message_id=0
                     AND excluded.anchor_message_id<>0
                THEN excluded.anchor_external_message_id
                ELSE {GROUP_PARTICIPATION_QUEUE_TABLE}.anchor_external_message_id
            END,
            anchor_sender_id=CASE
                WHEN ? THEN excluded.anchor_sender_id
                WHEN {GROUP_PARTICIPATION_QUEUE_TABLE}.anchor_message_id=0
                     AND excluded.anchor_message_id<>0
                THEN excluded.anchor_sender_id
                ELSE {GROUP_PARTICIPATION_QUEUE_TABLE}.anchor_sender_id
            END,
            latest_text_message_id=CASE
                WHEN ? OR ? THEN excluded.latest_text_message_id
                ELSE {GROUP_PARTICIPATION_QUEUE_TABLE}.latest_text_message_id
            END,
            candidate_revision={GROUP_PARTICIPATION_QUEUE_TABLE}.candidate_revision+1,
            attempt=0,
            lease_expires_at='',
            updated_at=excluded.updated_at
        """,
        (
            group, "pending", first.isoformat(), now.isoformat(), due.isoformat(),
            int(current.get("id") or 0), str(sender_id or ""), str(sender_name or ""),
            str(session or ""), str(external_message_id or ""), initial_anchor_id,
            initial_anchor_external_id, initial_anchor_sender_id, initial_latest_text_id,
            utc_now(), bool(starts_fresh or is_text_anchor), bool(starts_fresh or is_text_anchor), bool(starts_fresh or is_text_anchor),
            bool(starts_fresh), bool(is_text_anchor),
        ),
    )
    row = conn.execute(
        f"SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?", (group,)
    ).fetchone()
    result = dict(row)
    result["replaced_message_id"] = replaced_message_id
    result["preserved_claimed_candidate"] = False
    result["joined_active_topic"] = bool(
        existing and existing_state in {"pending", "claimed"} and not starts_fresh
    )
    result["candidate_due_reason"] = "quiet_gap" if due == quiet_due else "active_topic_window"
    return result


def claim_due_group_candidates(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    lease_seconds: int = 120,
    limit: int = 3,
) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    rows = conn.execute(
        f"""
        SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE}
        WHERE (state='pending' AND due_at<=?)
           OR (state='claimed' AND lease_expires_at<=?)
        ORDER BY due_at, group_id LIMIT ?
        """,
        (current.isoformat(), current.isoformat(), max(1, min(int(limit), 20))),
    ).fetchall()
    claimed: list[dict] = []
    lease = (current + timedelta(seconds=max(30, int(lease_seconds)))).isoformat()
    for row in rows:
        group = str(row["group_id"])
        # A claimed opportunity has one bounded lifetime even in a busy group.
        if current - _parse(row["first_message_at"]) > timedelta(minutes=5):
            conn.execute(
                f"UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE} SET state='cancelled',lease_expires_at='',updated_at=? "
                "WHERE group_id=? AND candidate_revision=? AND state=?",
                (utc_now(), group, int(row["candidate_revision"] or 0), str(row["state"])),
            )
            continue
        cursor = conn.execute(
            f"""UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE}
                SET state='claimed', attempt=attempt+1, candidate_revision=candidate_revision+1,
                    lease_expires_at=?, updated_at=?
                WHERE group_id=? AND candidate_revision=?
                  AND ((state='pending' AND due_at<=?)
                       OR (state='claimed' AND lease_expires_at<=?))""",
            (lease, utc_now(), group, int(row["candidate_revision"] or 0), current.isoformat(), current.isoformat()),
        )
        if not cursor.rowcount:
            continue
        refreshed = conn.execute(
            f"SELECT * FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?", (group,)
        ).fetchone()
        if (
            refreshed
            and refreshed["state"] == "claimed"
            and int(refreshed["candidate_revision"] or 0) == int(row["candidate_revision"] or 0) + 1
        ):
            claimed.append(dict(refreshed))
    return claimed


def finish_group_candidate(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    state: str = "completed",
    latest_message_id: int | None = None,
    candidate_revision: int | None = None,
) -> bool:
    if state not in {"completed", "failed", "cancelled"}:
        raise ValueError("group_participation_queue_state_invalid")
    sql = f"UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE} SET state=?, lease_expires_at='', updated_at=? WHERE group_id=?"
    values: list[object] = [state, utc_now(), str(group_id or "").strip()]
    if latest_message_id is not None:
        sql += " AND latest_message_id=?"
        values.append(int(latest_message_id))
    if candidate_revision is not None:
        sql += " AND candidate_revision=?"
        values.append(int(candidate_revision))
    cursor = conn.execute(sql, values)
    return bool(cursor.rowcount)


def group_candidate_is_current(
    conn: sqlite3.Connection,
    group_id: str,
    latest_message_id: int | None = None,
    *,
    candidate_revision: int | None = None,
) -> bool:
    row = conn.execute(
        f"""SELECT latest_message_id,candidate_revision,state
            FROM {GROUP_PARTICIPATION_QUEUE_TABLE} WHERE group_id=?""",
        (str(group_id or "").strip(),),
    ).fetchone()
    return bool(
        row
        and str(row["state"]) == "claimed"
        and (latest_message_id is not None and int(row["latest_message_id"] or 0) == int(latest_message_id)
             or latest_message_id is None and candidate_revision is not None)
        and (candidate_revision is None or int(row["candidate_revision"] or 0) == int(candidate_revision))
    )


def requeue_stale_claimed_candidate(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    expected_latest_message_id: int,
    expected_candidate_revision: int,
    replacement: dict,
    session: str,
    quiet_gap_seconds: int | None = None,
    now: datetime | None = None,
    allow_current_latest: bool = False,
    active_topic_window_seconds: int | None = None,
) -> bool:
    """Atomically hand an obsolete claim to a textual group message.

    A claimed candidate deliberately remains stable while the Worker is making
    its decision.  If that decision later fails the freshness fence because a
    newer message changed the topic, leaving the old row ``claimed`` creates a
    retry loop: the Worker keeps reclaiming the obsolete anchor while every
    later inbound is merely coalesced.  The default handoff preserves that
    newer-message contract.  ``allow_current_latest`` is narrower: before a
    plan starts, it may replace a stale old anchor with the candidate's already
    queued latest textual message.  In both cases the old claim is dropped and
    the Worker must make a fresh decision; this helper never sends a reply.
    """

    group = str(group_id or "").strip()
    try:
        expected_message = int(expected_latest_message_id)
        expected_revision = int(expected_candidate_revision)
        replacement_id = int(replacement.get("id") or 0)
    except (TypeError, ValueError):
        return False
    if (
        not group
        or expected_message <= 0
        or expected_revision <= 0
        or replacement_id < expected_message
        or (replacement_id == expected_message and not allow_current_latest)
    ):
        return False
    event_time = _parse(replacement.get("created_at"))
    current = now or datetime.now(timezone.utc)
    current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
    gap = max(0, int(quiet_gap_seconds or 0))
    original = conn.execute(
        f"SELECT first_message_at FROM {GROUP_PARTICIPATION_QUEUE_TABLE} "
        "WHERE group_id=? AND state='claimed' AND latest_message_id=? AND candidate_revision=?",
        (group, expected_message, expected_revision),
    ).fetchone()
    if not original:
        return False
    first = _parse(original["first_message_at"])
    if current - first > timedelta(minutes=5):
        return False
    window = max(gap, min(int(active_topic_window_seconds or 45), 600))
    due = max(current, min(event_time + timedelta(seconds=gap), first + timedelta(seconds=window)))
    sender_id = str(replacement.get("sender_id") or "").strip()
    external_message_id = str(replacement.get("external_message_id") or "").strip()
    if not sender_id:
        return False
    cursor = conn.execute(
        f"""UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE}
            SET state='pending',
                first_message_at=?,last_message_at=?,due_at=?,
                latest_message_id=?,latest_sender_id=?,latest_sender_name=?,
                latest_session=?,latest_external_message_id=?,
                anchor_message_id=?,anchor_external_message_id=?,anchor_sender_id=?,
                latest_text_message_id=?,candidate_revision=candidate_revision+1,
                attempt=0,lease_expires_at='',updated_at=?
            WHERE group_id=? AND state='claimed'
              AND latest_message_id=? AND candidate_revision=?""",
        (
            first.isoformat(), event_time.isoformat(), due.isoformat(),
            replacement_id, sender_id, str(replacement.get("sender_name") or ""),
            str(session or ""), external_message_id,
            replacement_id, external_message_id, sender_id, replacement_id,
            utc_now(), group, expected_message, expected_revision,
        ),
    )
    return bool(cursor.rowcount)


def reschedule_group_candidate(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    seconds: int = 15,
    latest_message_id: int | None = None,
    candidate_revision: int | None = None,
) -> bool:
    due = datetime.now(timezone.utc) + timedelta(seconds=max(5, int(seconds)))
    sql = (
        f"UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE} SET state='pending', due_at=?, "
        "lease_expires_at='', updated_at=? WHERE group_id=?"
    )
    values: list[object] = [due.isoformat(), utc_now(), str(group_id or "").strip()]
    if latest_message_id is not None:
        sql += " AND latest_message_id=?"
        values.append(int(latest_message_id))
    if candidate_revision is not None:
        sql += " AND candidate_revision=?"
        values.append(int(candidate_revision))
    cursor = conn.execute(sql, values)
    return bool(cursor.rowcount)


def renew_group_candidate_lease(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    expected_candidate_revision: int,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> bool:
    """Atomically renew the lease of a claim this worker still owns.

    The model-approved contribution still needs the reply generation and the
    Outbox path, which together can outlive the original claim lease in busy
    groups.  Renewing ownership (state='claimed' plus the optimistic revision)
    keeps the row stable against inbound traffic and prevents a second worker
    from reviving the same work, without weakening the optimistic-revision
    fence.  A failed renewal means another worker or an inbound UPSERT already
    owns a newer revision and this worker must not deliver.
    """

    current = now or datetime.now(timezone.utc)
    current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
    lease = current + timedelta(seconds=max(30, int(lease_seconds)))
    cursor = conn.execute(
        f"""UPDATE {GROUP_PARTICIPATION_QUEUE_TABLE}
            SET lease_expires_at=?, updated_at=?
            WHERE group_id=? AND state='claimed' AND candidate_revision=?""",
        (lease.isoformat(), utc_now(), str(group_id or "").strip(), int(expected_candidate_revision)),
    )
    return bool(cursor.rowcount)


__all__ = [
    "claim_due_group_candidates",
    "enqueue_group_candidate",
    "finish_group_candidate",
    "group_candidate_is_current",
    "renew_group_candidate_lease",
    "requeue_stale_claimed_candidate",
    "reschedule_group_candidate",
]
