#!/usr/bin/env python3
"""Reserve inbound order without taking ownership of finalized Delivery."""

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
            # Reservations establish inbound ordering only.  A candidate may
            # be replaced in group_participation_queue, but a finalized
            # Delivery (especially a claimed one) is never cancelled or has
            # its lease rewritten merely because a later inbound turn arrived.
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return sequence


__all__ = ["reserve_response_sequence"]
