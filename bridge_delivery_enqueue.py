#!/usr/bin/env python3
"""Transactional enqueue and stale-response replacement for Delivery Outbox."""

from __future__ import annotations

import uuid
from contextlib import closing
from datetime import datetime, timezone


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _timestamp(value=None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def enqueue(
    outbox,
    *,
    dedupe_key: str,
    channel: str,
    destination: str,
    payload: object,
    available_at=None,
    max_attempts: int,
    logical_response_id: str,
    source_message_id: str,
    engagement_decision_id: str,
    thread_ref: str,
    delivery_class: str,
    supersede_pending_social: bool,
    response_sequence: int,
) -> dict:
    dedupe_key, channel, destination = (
        _clip(dedupe_key, 300), _clip(channel, 80), _clip(destination, 300)
    )
    if not dedupe_key:
        raise ValueError("delivery_dedupe_key_required")
    if not channel:
        raise ValueError("delivery_channel_required")
    if not destination:
        raise ValueError("delivery_destination_required")
    try:
        max_attempts = max(1, min(int(max_attempts), 100))
        response_sequence = max(0, int(response_sequence))
    except (TypeError, ValueError) as exc:
        raise ValueError("delivery_enqueue_number_invalid") from exc
    now = datetime.now(timezone.utc)
    delivery_id = uuid.uuid4().hex
    logical_response_id = _clip(logical_response_id, 120)
    source_message_id = _clip(source_message_id, 180)
    engagement_decision_id = _clip(engagement_decision_id, 160)
    thread_ref = _clip(thread_ref, 300)
    delivery_class = _clip(delivery_class, 40) or "operational"
    created = False
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            initial_superseded_by = ""
            if thread_ref:
                latest = conn.execute(
                    """
                    SELECT MAX(sequence_value) FROM (
                        SELECT COALESCE(MAX(response_sequence),0) AS sequence_value
                        FROM delivery_outbox WHERE channel=? AND thread_ref=?
                        UNION ALL
                        SELECT COALESCE(MAX(current_sequence),0) AS sequence_value
                        FROM delivery_thread_sequences WHERE channel=? AND thread_ref=?
                    )
                    """,
                    (channel, thread_ref, channel, thread_ref),
                ).fetchone()
                latest_sequence = int(latest[0] or 0)
                if not response_sequence:
                    response_sequence = latest_sequence + 1
                elif response_sequence < latest_sequence:
                    newer = conn.execute(
                        """
                        SELECT id FROM delivery_outbox
                        WHERE channel=? AND thread_ref=? AND response_sequence=?
                          AND superseded_by='' ORDER BY created_at DESC LIMIT 1
                        """,
                        (channel, thread_ref, latest_sequence),
                    ).fetchone()
                    initial_superseded_by = str(newer[0]) if newer else f"revision:{latest_sequence}"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO delivery_outbox(
                    id,dedupe_key,channel,destination,payload_json,available_at,attempt,max_attempts,
                    lease_owner,lease_token,lease_expires_at,last_action,last_error,dead_letter,
                    dead_lettered_at,acked_at,logical_response_id,source_message_id,
                    engagement_decision_id,platform_message_id,delivery_certainty,thread_ref,
                    response_sequence,superseded_by,delivery_class,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,0,?,'','','','','',0,'','',?,?,?,?,'pending',?,?,?,?,?,?)
                """,
                (
                    delivery_id,dedupe_key,channel,destination,outbox._payload_json(payload),
                    _timestamp(available_at or now),max_attempts,logical_response_id,source_message_id,
                    engagement_decision_id,"",thread_ref,response_sequence,initial_superseded_by,
                    delivery_class,_timestamp(now),_timestamp(now),
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE dedupe_key=? OR (?<>'' AND logical_response_id=?)
                ORDER BY CASE WHEN dedupe_key=? THEN 0 ELSE 1 END LIMIT 1
                """,
                (dedupe_key, logical_response_id, logical_response_id, dedupe_key),
            ).fetchone()
            if created and thread_ref:
                conn.execute(
                    """
                    INSERT INTO delivery_thread_sequences(channel,thread_ref,current_sequence,updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(channel,thread_ref) DO UPDATE SET
                        current_sequence=MAX(current_sequence,excluded.current_sequence),
                        updated_at=excluded.updated_at
                    """,
                    (channel, thread_ref, response_sequence, _timestamp(now)),
                )
            if created and not initial_superseded_by and supersede_pending_social and thread_ref and delivery_class == "social":
                marker = f"revision:{response_sequence}"
                conn.execute(
                    """
                    UPDATE delivery_outbox SET superseded_by=?
                    WHERE channel=? AND thread_ref=? AND response_sequence<? AND superseded_by=?
                    """,
                    (str(row["id"]), channel, thread_ref, response_sequence, marker),
                )
                conn.execute(
                    """
                    UPDATE delivery_outbox SET superseded_by=?,last_action='superseded',updated_at=?
                    WHERE channel=? AND thread_ref=? AND id<>? AND delivery_class='social'
                      AND engagement_decision_id=''
                      AND acked_at='' AND dead_letter=0 AND superseded_by='' AND lease_owner=''
                      AND delivery_certainty IN ('pending','rejected')
                    """,
                    (str(row["id"]), _timestamp(now), channel, thread_ref, str(row["id"])),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if created and not row["superseded_by"]:
        outbox.notifier.notify()
    return outbox._row_to_delivery(row, now=now)


def supersede_pending_dedupe_prefix(
    outbox,
    *,
    dedupe_prefix: str,
    superseded_by: str,
) -> int:
    """Retire unsent obsolete deliveries selected by an internal dedupe namespace."""

    prefix = _clip(dedupe_prefix, 280)
    replacement = _clip(superseded_by, 120)
    if not prefix or not replacement:
        raise ValueError("delivery_supersede_selector_required")
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE delivery_outbox
                SET superseded_by=?,last_action='superseded',updated_at=?
                WHERE dedupe_key LIKE ? ESCAPE '\\'
                  AND id<>? AND acked_at='' AND dead_letter=0 AND superseded_by=''
                  AND lease_owner='' AND delivery_certainty IN ('pending','rejected')
                """,
                (
                    replacement,
                    _timestamp(),
                    prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
                    replacement,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if cursor.rowcount:
        outbox.notifier.notify()
    return int(cursor.rowcount)


__all__ = ["enqueue", "supersede_pending_dedupe_prefix"]
