#!/usr/bin/env python3
"""Bounded recovery state machine for continuous private response cycles."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Mapping

from bridge_continuous_private_conversation import (
    ResponseCycleLeaseError,
    ResponseCycleRecoveryError,
    bind_prepared_response_delivery,
    claim_prepared_response_cycle,
    hold_response_cycle,
    load_prepared_response_cycle,
    mark_response_cycle_failure,
    reclaim_response_cycle,
)
from bridge_qq_delivery import enqueue_prepared_qq_response


def _expired(value: object) -> bool:
    token = str(value or "").strip()
    if not token:
        return True
    try:
        parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _active_cycles(connect, limit: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id,state,source_set_hash,generation_started_at,
                   prepared_delivery_json,prepared_delivery_sha256,
                   lease_owner,lease_token,lease_expires_at,
                   attempt_count,max_attempts,logical_response_id,outbox_dedupe_key
            FROM conversation_response_cycles
            WHERE blocks_thread=1 AND state IN ('processing','retryable','commit_ready')
            ORDER BY created_at,id LIMIT ?
            """,
            (max(1, min(int(limit), 64)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _existing_delivery(outbox, cycle: Mapping[str, object]) -> dict | None:
    logical_id = str(cycle.get("logical_response_id") or "").strip()
    dedupe_key = str(cycle.get("outbox_dedupe_key") or "").strip()
    by_logical = outbox.get_delivery_by_logical_response_id(logical_id) if logical_id else None
    by_dedupe = outbox.get_delivery_by_dedupe_key(dedupe_key) if dedupe_key else None
    if by_logical and by_dedupe and str(by_logical.get("id")) != str(by_dedupe.get("id")):
        raise ResponseCycleRecoveryError("response_cycle_outbox_identity_conflict")
    return by_logical or by_dedupe


def _hold(connect, cycle: Mapping[str, object], reason: str, *, effects: bool) -> bool:
    with connect() as conn:
        current = conn.execute(
            "SELECT state FROM conversation_response_cycles WHERE id=?",
            (str(cycle["id"]),),
        ).fetchone()
        if current is None or str(current[0]) == "manual_hold":
            return False
        try:
            hold_response_cycle(
                conn,
                cycle_id=str(cycle["id"]),
                source_set_hash=str(cycle["source_set_hash"]),
                expected_states=[str(current[0])],
                reason=reason,
                blocks_effects=effects,
                require_recoverable_lease=True,
            )
        except ResponseCycleRecoveryError:
            return False
    return True


def reconcile_response_cycles(
    connect,
    outbox,
    *,
    lease_owner: str,
    process_unstarted: Callable[[dict], None] | None = None,
    limit: int = 16,
) -> dict:
    """Recover prepared facts first; never replay uncertain generation/effects."""

    counters = {"scanned": 0, "reclaimed": 0, "bound": 0, "held": 0}
    for cycle in _active_cycles(connect, limit):
        counters["scanned"] += 1
        if (
            str(cycle.get("lease_owner") or "")
            and str(cycle.get("lease_token") or "")
            and not _expired(cycle.get("lease_expires_at"))
        ):
            continue
        try:
            existing = _existing_delivery(outbox, cycle)
        except (ValueError, ResponseCycleRecoveryError) as exc:
            if _hold(connect, cycle, str(exc), effects=True):
                counters["held"] += 1
            continue

        prepared = bool(
            str(cycle.get("prepared_delivery_json") or "")
            and str(cycle.get("prepared_delivery_sha256") or "")
        )
        if str(cycle["state"]) == "commit_ready" or prepared:
            if not prepared:
                if _hold(connect, cycle, "response_cycle_prepared_missing", effects=True):
                    counters["held"] += 1
                continue
            if (
                existing is None
                and int(cycle.get("attempt_count") or 0) >= int(cycle.get("max_attempts") or 0)
            ):
                if _hold(connect, cycle, "response_cycle_attempts_exhausted", effects=True):
                    counters["held"] += 1
                continue
            try:
                with connect() as conn:
                    claimed = claim_prepared_response_cycle(
                        conn,
                        cycle_id=str(cycle["id"]),
                        source_set_hash=str(cycle["source_set_hash"]),
                        lease_owner=lease_owner,
                        binding_only=existing is not None,
                    )
                with connect() as conn:
                    loaded = load_prepared_response_cycle(conn, str(cycle["id"]))
                delivery = existing or enqueue_prepared_qq_response(
                    outbox,
                    loaded["delivery"],
                    destination=loaded["destination"],
                )
                with connect() as conn:
                    bind_prepared_response_delivery(
                        conn,
                        cycle_id=str(cycle["id"]),
                        lease_token=str(claimed["lease_token"]),
                        source_set_hash=str(cycle["source_set_hash"]),
                        delivery=delivery,
                    )
                counters["bound"] += 1
            except ResponseCycleLeaseError:
                # A live/current worker still owns this exact prepared intent.
                continue
            except sqlite3.Error as exc:
                try:
                    with connect() as conn:
                        mark_response_cycle_failure(
                            conn,
                            cycle_id=str(cycle["id"]),
                            lease_token=str(claimed["lease_token"]),
                            error=type(exc).__name__,
                        )
                except Exception:
                    pass
            except Exception as exc:
                if _hold(
                    connect, cycle,
                    str(exc) or type(exc).__name__,
                    effects=True,
                ):
                    counters["held"] += 1
            continue

        if str(cycle.get("generation_started_at") or ""):
            if (
                str(cycle.get("lease_owner") or "")
                and str(cycle.get("lease_token") or "")
                and not _expired(cycle.get("lease_expires_at"))
            ):
                continue
            if _hold(
                connect, cycle,
                "response_cycle_generation_outcome_unknown",
                effects=True,
            ):
                counters["held"] += 1
            continue

        if int(cycle.get("attempt_count") or 0) >= int(cycle.get("max_attempts") or 0):
            if _hold(connect, cycle, "response_cycle_attempts_exhausted", effects=False):
                counters["held"] += 1
            continue
        if not _expired(cycle.get("lease_expires_at")) or not callable(process_unstarted):
            continue
        reclaimed = None
        try:
            with connect() as conn:
                reclaimed = reclaim_response_cycle(
                    conn,
                    cycle_id=str(cycle["id"]),
                    source_set_hash=str(cycle["source_set_hash"]),
                    lease_owner=lease_owner,
                )
            counters["reclaimed"] += 1
            process_unstarted(reclaimed)
        except ResponseCycleLeaseError:
            continue
        except Exception as exc:
            try:
                with connect() as conn:
                    mark_response_cycle_failure(
                        conn,
                        cycle_id=str(cycle["id"]),
                        lease_token=str((reclaimed or {}).get("lease_token") or ""),
                        error=type(exc).__name__,
                    )
            except Exception:
                pass
    return counters


__all__ = ["reconcile_response_cycles"]
