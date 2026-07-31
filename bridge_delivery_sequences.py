#!/usr/bin/env python3
"""Reserve inbound order and cancel replies made stale before QQ send."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def reserve_response_sequence(
    outbox, channel: str, thread_ref: str, *, reservation_key: str = "",
) -> int:
    channel, thread_ref = _clip(channel, 80), _clip(thread_ref, 300)
    reservation_key = _clip(reservation_key, 180)
    if not channel or not thread_ref:
        raise ValueError("delivery_thread_identity_required")
    now = _now()
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if reservation_key:
                reserved = conn.execute(
                    """
                    SELECT response_sequence FROM delivery_response_reservations
                    WHERE channel=? AND thread_ref=? AND reservation_key=?
                    """,
                    (channel, thread_ref, reservation_key),
                ).fetchone()
                if reserved:
                    conn.commit()
                    return int(reserved[0])
            row = conn.execute(
                """
                SELECT MAX(sequence_value) FROM (
                    SELECT COALESCE(MAX(current_sequence),0) AS sequence_value
                    FROM delivery_thread_sequences WHERE channel=? AND thread_ref=?
                    UNION ALL
                    SELECT COALESCE(MAX(response_sequence),0) AS sequence_value
                    FROM delivery_outbox WHERE channel=? AND thread_ref=?
                )
                """,
                (channel, thread_ref, channel, thread_ref),
            ).fetchone()
            sequence = int(row[0] or 0) + 1
            conn.execute(
                """
                INSERT INTO delivery_thread_sequences(channel,thread_ref,current_sequence,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(channel,thread_ref) DO UPDATE SET
                    current_sequence=excluded.current_sequence,updated_at=excluded.updated_at
                """,
                (channel, thread_ref, sequence, now),
            )
            if reservation_key:
                conn.execute(
                    """
                    INSERT INTO delivery_response_reservations(
                        channel,thread_ref,reservation_key,response_sequence,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (channel, thread_ref, reservation_key, sequence, now, now),
                )
            stale = conn.execute(
                """
                SELECT id,attempt FROM delivery_outbox
                WHERE channel=? AND thread_ref=? AND delivery_class='social'
                  AND acked_at='' AND dead_letter=0 AND superseded_by=''
                  AND delivery_certainty IN ('pending','rejected','claimed')
                """,
                (channel, thread_ref),
            ).fetchall()
            marker = f"revision:{sequence}"
            for delivery in stale:
                conn.execute(
                    """
                    UPDATE delivery_outbox SET superseded_by=?,last_action='superseded',
                        lease_owner='',lease_token='',lease_expires_at='',updated_at=? WHERE id=?
                    """,
                    (marker, now, delivery["id"]),
                )
                if int(delivery["attempt"] or 0) > 0:
                    conn.execute(
                        """
                        UPDATE delivery_attempts SET state='superseded',certainty='not_sent',
                            finished_at=?,updated_at=? WHERE delivery_id=? AND attempt_no=?
                        """,
                        (now, now, delivery["id"], int(delivery["attempt"])),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return sequence


__all__ = ["reserve_response_sequence"]
