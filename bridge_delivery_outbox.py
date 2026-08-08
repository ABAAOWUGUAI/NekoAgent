#!/usr/bin/env python3
"""Durable, lease-based delivery outbox for channel adapters.

A worker first claims a row and then marks ``send_start`` immediately before
the external side effect.  A lost pre-send lease is reclaimable; a lost lease
after ``send_start`` becomes ambiguous and stops automatic retry.  This avoids
pretending that a transport without an idempotency key can guarantee exactly
once delivery.

Long polling is process-local acceleration, not the source of truth.  SQLite
is always queried before sleeping and after waking.  The condition lock also
covers the first query and generation snapshot so a commit followed by
``notify()`` cannot be lost between those two operations.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, TypeVar

from bridge_sqlite_commit_hooks import connect_mutation_database

T = TypeVar("T")
UTC = timezone.utc
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 30.0

class LeaseLostError(RuntimeError):
    """Raised when a stale worker tries to finish a lease it no longer owns."""

def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return utc_now()
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime | str | None = None) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def ensure_delivery_outbox_table(conn: sqlite3.Connection) -> None:
    """Create the outbox schema on an existing SQLite connection."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_outbox (
            id TEXT PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL,
            destination TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            available_at TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            last_action TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            dead_letter INTEGER NOT NULL DEFAULT 0,
            dead_lettered_at TEXT NOT NULL DEFAULT '',
            acked_at TEXT NOT NULL DEFAULT '',
            logical_response_id TEXT NOT NULL DEFAULT '',
            source_message_id TEXT NOT NULL DEFAULT '',
            engagement_decision_id TEXT NOT NULL DEFAULT '',
            platform_message_id TEXT NOT NULL DEFAULT '',
            delivery_certainty TEXT NOT NULL DEFAULT 'pending',
            thread_ref TEXT NOT NULL DEFAULT '',
            response_sequence INTEGER NOT NULL DEFAULT 0,
            superseded_by TEXT NOT NULL DEFAULT '',
            delivery_class TEXT NOT NULL DEFAULT 'operational',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_delivery_outbox_claim
        ON delivery_outbox(dead_letter, acked_at, channel, available_at, lease_expires_at)
        """,
    )
    # Existing databases are upgraded by the registered task migration.  This
    # call also keeps standalone DeliveryOutbox users on the same schema.
    from bridge_delivery_continuity_schema import apply_delivery_continuity_v1

    apply_delivery_continuity_v1(conn)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_delivery_outbox_created
        ON delivery_outbox(created_at DESC)
        """,
    )


class OutboxNotifier:
    """Generation counter and race-free ``threading.Condition`` long poll."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    def notify(self) -> int:
        """Advance the generation and wake every in-process waiter."""

        with self._condition:
            self._generation += 1
            self._condition.notify_all()
            return self._generation

    def long_poll(
        self,
        query: Callable[[], T],
        *,
        timeout: float,
        next_wake_in: Callable[[], float | None] | None = None,
    ) -> T:
        """Query, snapshot generation, wait, then query again until timeout.

        ``query`` must return a false-y value when no work is available.  It is
        called while the condition lock is held.  Producers commit before
        calling :meth:`notify`; if a producer commits during ``query``, its
        notification waits on this lock and therefore cannot be missed.

        ``next_wake_in`` optionally caps the sleep so delayed deliveries and
        expired leases become visible even when no new notification arrives.
        """

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._condition:
                result = query()
                if result:
                    return result

                # Required ordering: query DB -> record generation -> wait.
                observed_generation = self._generation
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result

                wait_seconds = remaining
                if next_wake_in is not None:
                    scheduled = next_wake_in()
                    if scheduled is not None:
                        wait_seconds = min(wait_seconds, max(0.01, float(scheduled)))

                self._condition.wait_for(
                    lambda: self._generation != observed_generation,
                    timeout=wait_seconds,
                )
            # The next loop iteration re-queries SQLite after every wake or
            # timeout.  Notifications are only a latency optimization.


_NOTIFIERS: dict[str, OutboxNotifier] = {}
_NOTIFIERS_LOCK = threading.Lock()


def get_outbox_notifier(db_path: str | os.PathLike[str]) -> OutboxNotifier:
    """Return the shared in-process notifier for a database path."""

    key = os.path.normcase(os.path.abspath(os.fspath(db_path)))
    with _NOTIFIERS_LOCK:
        notifier = _NOTIFIERS.get(key)
        if notifier is None:
            notifier = OutboxNotifier()
            _NOTIFIERS[key] = notifier
        return notifier


class DeliveryOutbox:
    """SQLite-backed delivery queue with transactional leases."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        notifier: OutboxNotifier | None = None,
        busy_timeout_seconds: float = 10.0,
        mutation_callback: Callable[[], None] | None = None,
    ) -> None:
        self.db_path = os.fspath(db_path)
        self.busy_timeout_seconds = max(0.1, float(busy_timeout_seconds))
        self.notifier = notifier or get_outbox_notifier(self.db_path)
        self.mutation_callback = mutation_callback
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return connect_mutation_database(
            self.db_path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
            tables={"delivery_outbox"},
            callback=self.mutation_callback,
        )

    def ensure_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ensure_delivery_outbox_table(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @property
    def notify_generation(self) -> int:
        return self.notifier.generation

    def notify(self) -> int:
        """Wake waiters after an external writer commits to this outbox."""

        return self.notifier.notify()

    @staticmethod
    def _payload_json(payload: object) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("delivery_payload_must_be_json_serializable") from exc

    @staticmethod
    def _row_to_delivery(row: sqlite3.Row, *, now: datetime | None = None) -> dict:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["payload"] = None
        current = now or utc_now()
        lease_expires = item.get("lease_expires_at") or ""
        if item.get("superseded_by"):
            state = "superseded"
        elif item.get("acked_at"):
            state = "delivered"
        elif item.get("delivery_certainty") == "ambiguous":
            state = "ambiguous"
        elif item.get("dead_letter"):
            state = "dead_letter"
        elif lease_expires and _as_utc(lease_expires) > current:
            state = "leased"
        elif _as_utc(item["available_at"]) > current:
            state = "scheduled"
        else:
            state = "available"
        item["state"] = state
        item["dead_letter"] = bool(item.get("dead_letter"))
        return item

    def enqueue(
        self,
        *,
        dedupe_key: str,
        channel: str,
        destination: str,
        payload: object,
        available_at: datetime | str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        logical_response_id: str = "",
        source_message_id: str = "",
        engagement_decision_id: str = "",
        thread_ref: str = "",
        delivery_class: str = "operational",
        supersede_pending_social: bool = False,
        response_sequence: int = 0,
    ) -> dict:
        """Insert a delivery once and return the existing row on duplicates."""
        from bridge_delivery_enqueue import enqueue

        return enqueue(
            self,
            dedupe_key=dedupe_key,
            channel=channel,
            destination=destination,
            payload=payload,
            available_at=available_at,
            max_attempts=max_attempts,
            logical_response_id=logical_response_id,
            source_message_id=source_message_id,
            engagement_decision_id=engagement_decision_id,
            thread_ref=thread_ref,
            delivery_class=delivery_class,
            supersede_pending_social=supersede_pending_social,
            response_sequence=response_sequence,
        )

    def supersede_pending_dedupe_prefix(
        self,
        *,
        dedupe_prefix: str,
        superseded_by: str,
    ) -> int:
        """Supersede obsolete unsent rows from one internal dedupe namespace."""

        from bridge_delivery_enqueue import supersede_pending_dedupe_prefix

        return supersede_pending_dedupe_prefix(
            self,
            dedupe_prefix=dedupe_prefix,
            superseded_by=superseded_by,
        )

    def reserve_response_sequence(
        self, channel: str, thread_ref: str, *, reservation_key: str = "",
    ) -> int:
        from bridge_delivery_sequences import reserve_response_sequence

        return reserve_response_sequence(
            self, channel, thread_ref, reservation_key=reservation_key,
        )

    def claim(
        self,
        lease_owner: str,
        *,
        limit: int = 1,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        channel: str | None = None,
        now: datetime | str | None = None,
    ) -> list[dict]:
        """Transactionally claim available rows for a bounded lease."""

        lease_owner = _clip(lease_owner, 160)
        if not lease_owner:
            raise ValueError("delivery_lease_owner_required")
        try:
            limit = max(1, min(int(limit), 100))
            lease_seconds = max(0.01, min(float(lease_seconds), 86400.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("delivery_claim_options_invalid") from exc

        current = _as_utc(now)
        current_ts = _timestamp(current)
        expires_ts = _timestamp(current + timedelta(seconds=lease_seconds))
        channel_filter = _clip(channel, 80) if channel else ""

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # A crashed worker can exhaust the final attempt without ever
                # calling retry.  Once that lease expires, retire the row here.
                expired_sends = conn.execute(
                    """
                    SELECT id,attempt FROM delivery_outbox
                    WHERE acked_at='' AND dead_letter=0 AND delivery_certainty='sending'
                      AND lease_expires_at<>'' AND lease_expires_at<=?
                    """,
                    (current_ts,),
                ).fetchall()
                conn.execute(
                    """
                    UPDATE delivery_outbox
                    SET delivery_certainty='ambiguous',dead_letter=1,dead_lettered_at=?,
                        lease_owner='',lease_expires_at='',last_action='ambiguous',
                        last_error=CASE WHEN last_error='' THEN 'worker_lost_during_send' ELSE last_error END,
                        updated_at=?
                    WHERE acked_at='' AND dead_letter=0 AND delivery_certainty='sending'
                      AND lease_expires_at<>'' AND lease_expires_at<=?
                    """,
                    (current_ts, current_ts, current_ts),
                )
                for expired in expired_sends:
                    conn.execute(
                        """
                        UPDATE delivery_attempts
                        SET state='ambiguous',certainty='ambiguous',error_kind='worker_lost_during_send',
                            finished_at=?,updated_at=?
                        WHERE delivery_id=? AND attempt_no=?
                        """,
                        (current_ts, current_ts, expired["id"], int(expired["attempt"])),
                    )
                conn.execute(
                    """
                    UPDATE delivery_outbox
                    SET dead_letter = 1,
                        dead_lettered_at = ?,
                        lease_owner = '',
                        lease_expires_at = '',
                        last_action = 'dead_letter',
                        last_error = CASE
                            WHEN last_error = '' THEN 'lease_expired_after_max_attempts'
                            ELSE last_error
                        END,
                        updated_at = ?
                    WHERE acked_at = ''
                      AND dead_letter = 0
                      AND attempt >= max_attempts
                      AND available_at <= ?
                      AND (lease_expires_at = '' OR lease_expires_at <= ?)
                    """,
                    (current_ts, current_ts, current_ts, current_ts),
                )

                where_channel = " AND channel = ?" if channel_filter else ""
                params: list[object] = [current_ts, current_ts]
                if channel_filter:
                    params.append(channel_filter)
                params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT candidate.id
                    FROM delivery_outbox AS candidate
                    WHERE candidate.acked_at = ''
                      AND candidate.dead_letter = 0
                      AND candidate.superseded_by = ''
                      AND candidate.delivery_certainty NOT IN ('ambiguous','confirmed','sending')
                      AND candidate.attempt < candidate.max_attempts
                      AND candidate.available_at <= ?
                      AND (candidate.lease_expires_at = '' OR candidate.lease_expires_at <= ?)
                      AND (
                          candidate.thread_ref = '' OR NOT EXISTS (
                              SELECT 1 FROM delivery_outbox AS earlier
                              WHERE earlier.channel=candidate.channel
                                AND earlier.thread_ref=candidate.thread_ref
                                AND earlier.response_sequence<candidate.response_sequence
                                AND earlier.acked_at=''
                                AND earlier.superseded_by=''
                                AND (earlier.dead_letter=0 OR earlier.delivery_certainty='ambiguous')
                          )
                      )
                      {where_channel}
                    ORDER BY candidate.available_at ASC,candidate.created_at ASC,candidate.id ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()

                claimed: list[sqlite3.Row] = []
                for candidate in rows:
                    token = uuid.uuid4().hex
                    cursor = conn.execute(
                        """
                        UPDATE delivery_outbox
                        SET attempt = attempt + 1,
                            lease_owner = ?,
                            lease_token = ?,
                            lease_expires_at = ?,
                            delivery_certainty = 'claimed',
                            last_action = 'claim',
                            updated_at = ?
                        WHERE id = ?
                          AND acked_at = ''
                          AND dead_letter = 0
                          AND attempt < max_attempts
                          AND available_at <= ?
                          AND (lease_expires_at = '' OR lease_expires_at <= ?)
                        """,
                        (
                            lease_owner,
                            token,
                            expires_ts,
                            current_ts,
                            candidate["id"],
                            current_ts,
                            current_ts,
                        ),
                    )
                    if cursor.rowcount == 1:
                        attempt_row = conn.execute(
                            "SELECT attempt,lease_token,lease_owner FROM delivery_outbox WHERE id=?",
                            (candidate["id"],),
                        ).fetchone()
                        token_hash = hashlib.sha256(str(attempt_row["lease_token"]).encode("utf-8")).hexdigest()
                        conn.execute(
                            """
                            INSERT INTO delivery_attempts(
                                id,delivery_id,attempt_no,lease_token_hash,worker_ref,state,certainty,
                                started_at,created_at,updated_at
                            ) VALUES(?,?,?,?,?,'claimed','pending',?,?,?)
                            """,
                            (
                                uuid.uuid4().hex,candidate["id"],int(attempt_row["attempt"]),token_hash,
                                str(attempt_row["lease_owner"]),current_ts,current_ts,current_ts,
                            ),
                        )
                        claimed.append(
                            conn.execute(
                                "SELECT * FROM delivery_outbox WHERE id = ?",
                                (candidate["id"],),
                            ).fetchone(),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return [self._row_to_delivery(row, now=current) for row in claimed]

    def begin_send(self, delivery_id: str, lease_token: str) -> dict | None:
        """Mark the narrow external-side-effect window before calling QQ."""
        from bridge_delivery_attempts import begin_send

        return begin_send(self, delivery_id, lease_token)

    def get_delivery(self, delivery_id: str) -> dict | None:
        """Read one delivery without changing its lease."""

        delivery_id = _clip(delivery_id, 80)
        if not delivery_id:
            raise ValueError("delivery_id_required")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
        return self._row_to_delivery(row) if row is not None else None

    def bind_engagement_decision(
        self,
        delivery_id: str,
        engagement_decision_id: str,
        *,
        source_message_id: str = "",
    ) -> dict | None:
        """Idempotently attach the authoritative participation decision.

        QQ response enqueue deliberately happens before the post-dispatch
        participation observation.  This narrow projection closes that timing
        gap without changing send ownership or creating a second delivery
        record.
        """

        delivery_id = _clip(delivery_id, 80)
        engagement_decision_id = _clip(engagement_decision_id, 160)
        source_message_id = _clip(source_message_id, 180)
        if not delivery_id:
            raise ValueError("delivery_id_required")
        if not engagement_decision_id:
            raise ValueError("engagement_decision_id_required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                if source_message_id and str(row["source_message_id"] or "") != source_message_id:
                    raise ValueError("delivery_source_message_mismatch")
                current = str(row["engagement_decision_id"] or "")
                if current and current != engagement_decision_id:
                    raise ValueError("delivery_engagement_decision_conflict")
                if not current:
                    now = _timestamp()
                    conn.execute(
                        """UPDATE delivery_outbox
                           SET engagement_decision_id=?,updated_at=? WHERE id=?""",
                        (engagement_decision_id, now, delivery_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM delivery_outbox WHERE id=?",
                        (delivery_id,),
                    ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._row_to_delivery(row)

    def defer_claim(self, delivery_id: str, lease_token: str, *, reason: str, available_at=None) -> dict | None:
        from bridge_delivery_attempts import defer_claim

        return defer_claim(self, delivery_id, lease_token, reason=reason, available_at=available_at)

    def cancel_claim(self, delivery_id: str, lease_token: str, *, reason: str) -> dict | None:
        from bridge_delivery_attempts import cancel_claim

        return cancel_claim(self, delivery_id, lease_token, reason=reason)

    def ack(self, delivery_id: str, lease_token: str, *, platform_message_id: str = "") -> dict | None:
        """Acknowledge a delivery; repeating a successful ack is a no-op."""
        from bridge_delivery_attempts import ack

        return ack(self, delivery_id, lease_token, platform_message_id=platform_message_id)

    def retry(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        error: str = "",
        delay_seconds: float = 0.0,
        available_at: datetime | str | None = None,
        now: datetime | str | None = None,
        known_not_sent: bool = False,
    ) -> dict | None:
        """Release a failed lease or move it to the dead-letter state.

        A repeated retry with the same token returns the already-updated row
        without changing ``available_at`` or incrementing any counter.
        """
        from bridge_delivery_attempts import retry

        return retry(
            self,
            delivery_id,
            lease_token,
            error=error,
            delay_seconds=delay_seconds,
            available_at=available_at,
            now=now,
            known_not_sent=known_not_sent,
        )

    def mark_ambiguous(self, delivery_id: str, lease_token: str, *, error: str = "") -> dict | None:
        """Stop automatic retries when the platform may already have sent."""
        from bridge_delivery_attempts import mark_ambiguous

        return mark_ambiguous(self, delivery_id, lease_token, error=error)

    def requeue_dead_letter(self, delivery_id: str) -> dict | None:
        """Explicit operator action that makes one dead letter claimable again."""

        delivery_id = _clip(delivery_id, 80)
        if not delivery_id:
            raise ValueError("delivery_id_required")
        now = utc_now()
        now_ts = _timestamp(now)
        changed = False
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
                if row is None:
                    conn.commit()
                    return None
                if not row["dead_letter"] or row["acked_at"]:
                    raise ValueError("delivery_not_requeueable")
                conn.execute(
                    """
                    UPDATE delivery_outbox
                    SET available_at=?,max_attempts=MAX(max_attempts,attempt+?),
                        lease_owner='',lease_token='',lease_expires_at='',last_error='',
                        last_action='operator_requeue',delivery_certainty='pending',
                        dead_letter=0,dead_lettered_at='',updated_at=?
                    WHERE id=? AND dead_letter=1 AND acked_at=''
                    """,
                    (now_ts, DEFAULT_MAX_ATTEMPTS, now_ts, delivery_id),
                )
                row = conn.execute("SELECT * FROM delivery_outbox WHERE id=?", (delivery_id,)).fetchone()
                conn.commit()
                changed = True
            except Exception:
                conn.rollback()
                raise
        if changed:
            self.notifier.notify()
        return self._row_to_delivery(row, now=now)

    def list_deliveries(
        self,
        *,
        state: str = "all",
        channel: str | None = None,
        limit: int = 100,
        now: datetime | str | None = None,
    ) -> list[dict]:
        """Read deliveries without changing leases or attempts."""

        state = str(state or "all").strip().lower()
        if state not in {
            "all",
            "pending",
            "available",
            "scheduled",
            "leased",
            "delivered",
            "dead_letter",
            "ambiguous",
            "superseded",
        }:
            raise ValueError("delivery_state_invalid")
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError) as exc:
            raise ValueError("delivery_list_limit_invalid") from exc
        current = _as_utc(now)
        current_ts = _timestamp(current)
        clauses: list[str] = []
        params: list[object] = []
        if state == "pending":
            clauses.append("acked_at = '' AND dead_letter = 0 AND superseded_by = ''")
        elif state == "available":
            clauses.append(
                "acked_at = '' AND dead_letter = 0 AND superseded_by = '' AND available_at <= ? "
                "AND (lease_expires_at = '' OR lease_expires_at <= ?)"
            )
            params.extend((current_ts, current_ts))
        elif state == "scheduled":
            clauses.append("acked_at = '' AND dead_letter = 0 AND superseded_by = '' AND available_at > ?")
            params.append(current_ts)
        elif state == "leased":
            clauses.append("acked_at = '' AND dead_letter = 0 AND superseded_by = '' AND lease_expires_at > ?")
            params.append(current_ts)
        elif state == "delivered":
            clauses.append("acked_at <> ''")
        elif state == "dead_letter":
            clauses.append("dead_letter = 1 AND delivery_certainty <> 'ambiguous'")
        elif state == "ambiguous":
            clauses.append("delivery_certainty = 'ambiguous'")
        elif state == "superseded":
            clauses.append("superseded_by <> ''")
        if channel:
            clauses.append("channel = ?")
            params.append(_clip(channel, 80))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM delivery_outbox
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_delivery(row, now=current) for row in rows]

    def list_attempts(self, delivery_id: str, *, limit: int = 100) -> list[dict]:
        """Return metadata-only attempt history; payload and destination never appear."""
        from bridge_delivery_attempts import list_attempts

        return list_attempts(self, delivery_id, limit=limit)

    def _next_claimable_delay(self, *, channel: str | None = None) -> float | None:
        current = utc_now()
        clauses = [
            "acked_at = ''", "dead_letter = 0", "superseded_by = ''",
            "delivery_certainty NOT IN ('ambiguous','confirmed','sending')",
            "attempt < max_attempts",
        ]
        params: list[object] = []
        if channel:
            clauses.append("channel = ?")
            params.append(_clip(channel, 80))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT available_at, lease_expires_at
                FROM delivery_outbox
                WHERE {' AND '.join(clauses)}
                """,
                params,
            ).fetchall()
        if not rows:
            return None
        delays: list[float] = []
        for row in rows:
            due = _as_utc(row["available_at"])
            if row["lease_expires_at"]:
                due = max(due, _as_utc(row["lease_expires_at"]))
            delays.append(max(0.0, (due - current).total_seconds()))
        return min(delays)

    def claim_or_wait(
        self,
        lease_owner: str,
        *,
        wait_seconds: float = 20.0,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        limit: int = 1,
        channel: str | None = None,
    ) -> list[dict]:
        """Long poll SQLite and transactionally claim work when it appears."""

        return self.notifier.long_poll(
            lambda: self.claim(
                lease_owner,
                limit=limit,
                lease_seconds=lease_seconds,
                channel=channel,
            ),
            timeout=wait_seconds,
            next_wake_in=lambda: self._next_claimable_delay(channel=channel),
        )


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DeliveryOutbox",
    "LeaseLostError",
    "OutboxNotifier",
    "ensure_delivery_outbox_table",
    "get_outbox_notifier",
]
