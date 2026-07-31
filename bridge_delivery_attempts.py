#!/usr/bin/env python3
"""Transactional AC-3 delivery-attempt state transitions."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone


UTC = timezone.utc


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _utc(value=None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value=None) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _error_kind(value: object) -> str:
    text = str(value or "").strip()
    return text.split(":", 1)[0].split(" ", 1)[0][:120] or "unknown"


def begin_send(outbox, delivery_id: str, lease_token: str) -> dict | None:
    delivery_id, lease_token = _clip(delivery_id, 80), _clip(lease_token, 80)
    if not delivery_id or not lease_token:
        raise ValueError("delivery_send_identity_required")
    now_ts = _timestamp()
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            if row["lease_token"] != lease_token or row["last_action"] not in {"claim", "send_start"}:
                from bridge_delivery_outbox import LeaseLostError

                raise LeaseLostError("delivery_lease_lost")
            conn.execute(
                "UPDATE delivery_outbox SET delivery_certainty='sending',last_action='send_start',updated_at=? WHERE id=?",
                (now_ts, delivery_id),
            )
            conn.execute(
                """
                UPDATE delivery_attempts SET state='sending',send_started_at=?,updated_at=?
                WHERE delivery_id=? AND attempt_no=?
                """,
                (now_ts, now_ts, delivery_id, int(row["attempt"])),
            )
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return outbox._row_to_delivery(row)


def ack(outbox, delivery_id: str, lease_token: str, *, platform_message_id: str = "") -> dict | None:
    delivery_id, lease_token = _clip(delivery_id, 80), _clip(lease_token, 80)
    if not delivery_id or not lease_token:
        raise ValueError("delivery_ack_identity_required")
    now, now_ts = _utc(), _timestamp()
    platform_message_id = _clip(platform_message_id, 180)
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            if row["acked_at"]:
                conn.commit()
                return outbox._row_to_delivery(row, now=now)
            from bridge_delivery_outbox import LeaseLostError

            if row["dead_letter"]:
                raise LeaseLostError("delivery_is_dead_letter")
            if row["lease_token"] != lease_token or row["last_action"] not in {"claim", "send_start"}:
                raise LeaseLostError("delivery_lease_lost")
            conn.execute(
                """
                UPDATE delivery_outbox SET acked_at=?,lease_owner='',lease_expires_at='',
                    last_action='ack',delivery_certainty='confirmed',platform_message_id=?,
                    last_error='',updated_at=? WHERE id=?
                """,
                (now_ts, platform_message_id, now_ts, delivery_id),
            )
            conn.execute(
                """
                UPDATE delivery_attempts SET state='confirmed',certainty='confirmed',
                    platform_message_id=?,finished_at=?,updated_at=?
                WHERE delivery_id=? AND attempt_no=?
                """,
                (platform_message_id, now_ts, now_ts, delivery_id, int(row["attempt"])),
            )
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return outbox._row_to_delivery(row, now=now)


def retry(
    outbox,
    delivery_id: str,
    lease_token: str,
    *,
    error: str = "",
    delay_seconds: float = 0.0,
    available_at=None,
    now=None,
    known_not_sent: bool = False,
) -> dict | None:
    delivery_id, lease_token = _clip(delivery_id, 80), _clip(lease_token, 80)
    if not delivery_id or not lease_token:
        raise ValueError("delivery_retry_identity_required")
    try:
        delay_seconds = max(0.0, min(float(delay_seconds), 30 * 86400.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("delivery_retry_delay_invalid") from exc
    current, current_ts = _utc(now), _timestamp(now)
    next_at = _utc(available_at) if available_at is not None else current + timedelta(seconds=delay_seconds)
    changed = False
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            if row["acked_at"]:
                conn.commit()
                return outbox._row_to_delivery(row, now=current)
            from bridge_delivery_outbox import LeaseLostError

            if row["lease_token"] != lease_token:
                raise LeaseLostError("delivery_lease_lost")
            if row["last_action"] == "retry":
                conn.commit()
                return outbox._row_to_delivery(row, now=current)
            if row["last_action"] != "claim":
                if row["last_action"] != "send_start" or not known_not_sent:
                    raise LeaseLostError("delivery_outcome_uncertain")
            is_dead = int(row["attempt"]) >= int(row["max_attempts"])
            conn.execute(
                """
                UPDATE delivery_outbox SET available_at=?,lease_owner='',lease_expires_at='',
                    last_action='retry',delivery_certainty='rejected',last_error=?,dead_letter=?,
                    dead_lettered_at=?,updated_at=? WHERE id=?
                """,
                (
                    _timestamp(next_at),_clip(error, 2000),1 if is_dead else 0,
                    current_ts if is_dead else "",current_ts,delivery_id,
                ),
            )
            conn.execute(
                """
                UPDATE delivery_attempts SET state='rejected',certainty='not_sent',
                    error_kind=?,finished_at=?,updated_at=? WHERE delivery_id=? AND attempt_no=?
                """,
                (_error_kind(error), current_ts, current_ts, delivery_id, int(row["attempt"])),
            )
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            conn.commit()
            changed = True
        except Exception:
            conn.rollback()
            raise
    if changed and not row["dead_letter"]:
        outbox.notifier.notify()
    return outbox._row_to_delivery(row, now=current)


def defer_claim(outbox, delivery_id: str, lease_token: str, *, reason: str, available_at=None) -> dict | None:
    """Release a known-not-sent policy deferral without reducing send retries."""

    delivery_id, lease_token = _clip(delivery_id, 80), _clip(lease_token, 80)
    if not delivery_id or not lease_token:
        raise ValueError("delivery_defer_identity_required")
    current, current_ts = _utc(), _timestamp()
    next_at = _utc(available_at) if available_at is not None else current + timedelta(minutes=5)
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            from bridge_delivery_outbox import LeaseLostError

            if row["lease_token"] != lease_token or row["last_action"] != "claim":
                raise LeaseLostError("delivery_lease_lost")
            conn.execute(
                """
                UPDATE delivery_outbox SET available_at=?,max_attempts=max_attempts+1,
                    lease_owner='',lease_token='',lease_expires_at='',last_action='policy_defer',
                    delivery_certainty='pending',last_error=?,updated_at=? WHERE id=?
                """,
                (_timestamp(next_at), _clip(reason, 2000), current_ts, delivery_id),
            )
            conn.execute(
                """
                UPDATE delivery_attempts SET state='rejected',certainty='not_sent',
                    error_kind=?,finished_at=?,updated_at=? WHERE delivery_id=? AND attempt_no=?
                """,
                (_error_kind(f"policy_defer:{reason}"), current_ts, current_ts, delivery_id, int(row["attempt"])),
            )
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    outbox.notifier.notify()
    return outbox._row_to_delivery(row, now=current)


def cancel_claim(outbox, delivery_id: str, lease_token: str, *, reason: str) -> dict | None:
    """Permanently suppress a policy-controlled record before external send."""

    delivery_id, lease_token = _clip(delivery_id, 80), _clip(lease_token, 80)
    if not delivery_id or not lease_token:
        raise ValueError("delivery_cancel_identity_required")
    now_ts = _timestamp()
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            from bridge_delivery_outbox import LeaseLostError

            if row["lease_token"] != lease_token or row["last_action"] != "claim":
                raise LeaseLostError("delivery_lease_lost")
            marker = _clip(f"policy:{reason}", 180)
            conn.execute(
                """
                UPDATE delivery_outbox SET lease_owner='',lease_token='',lease_expires_at='',
                    last_action='policy_cancel',delivery_certainty='rejected',last_error=?,
                    superseded_by=?,updated_at=? WHERE id=?
                """,
                (_clip(reason, 2000), marker, now_ts, delivery_id),
            )
            conn.execute(
                """
                UPDATE delivery_attempts SET state='rejected',certainty='not_sent',
                    error_kind=?,finished_at=?,updated_at=? WHERE delivery_id=? AND attempt_no=?
                """,
                (_error_kind(f"policy_cancel:{reason}"), now_ts, now_ts, delivery_id, int(row["attempt"])),
            )
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return outbox._row_to_delivery(row)


def mark_ambiguous(outbox, delivery_id: str, lease_token: str, *, error: str = "") -> dict | None:
    delivery_id, lease_token = _clip(delivery_id, 80), _clip(lease_token, 80)
    if not delivery_id or not lease_token:
        raise ValueError("delivery_ambiguous_identity_required")
    now_ts = _timestamp()
    with closing(outbox._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                conn.commit()
                return None
            if row["acked_at"] or row["delivery_certainty"] == "ambiguous":
                conn.commit()
                return outbox._row_to_delivery(row)
            from bridge_delivery_outbox import LeaseLostError

            if row["lease_token"] != lease_token or row["last_action"] != "send_start":
                raise LeaseLostError("delivery_lease_lost")
            conn.execute(
                """
                UPDATE delivery_outbox SET delivery_certainty='ambiguous',dead_letter=1,
                    dead_lettered_at=?,lease_owner='',lease_expires_at='',last_action='ambiguous',
                    last_error=?,updated_at=? WHERE id=?
                """,
                (now_ts, _clip(error, 2000), now_ts, delivery_id),
            )
            conn.execute(
                """
                UPDATE delivery_attempts SET state='ambiguous',certainty='ambiguous',
                    error_kind=?,finished_at=?,updated_at=? WHERE delivery_id=? AND attempt_no=?
                """,
                (_error_kind(error), now_ts, now_ts, delivery_id, int(row["attempt"])),
            )
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return outbox._row_to_delivery(row)


def list_attempts(outbox, delivery_id: str, *, limit: int = 100) -> list[dict]:
    delivery_id = _clip(delivery_id, 80)
    if not delivery_id:
        raise ValueError("delivery_id_required")
    limit = max(1, min(int(limit), 500))
    with closing(outbox._connect()) as conn:
        rows = conn.execute(
            """
            SELECT id,delivery_id,attempt_no,worker_ref,state,certainty,platform_message_id,
                   error_kind,started_at,send_started_at,finished_at,created_at,updated_at
            FROM delivery_attempts WHERE delivery_id=? ORDER BY attempt_no DESC LIMIT ?
            """,
            (delivery_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "ack", "begin_send", "cancel_claim", "defer_claim", "list_attempts",
    "mark_ambiguous", "retry",
]
