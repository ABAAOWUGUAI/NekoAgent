#!/usr/bin/env python3
"""Privacy-safe participation explanation projection for the admin console."""

from __future__ import annotations

import sqlite3
import json
from collections.abc import Mapping, Iterable

from bridge_conversation_participation import build_media_delivery_trace

from bridge_conversation_participation_routing_schema import DETERMINISTIC_PARTICIPATION_FEATURE_FLAG
from bridge_conversation_participation_schema import PARTICIPATION_SHADOW_FEATURE_FLAG


def project_media_diagnostics(rows: Iterable[Mapping[str, object]] | None) -> dict:
    """Aggregate media lifecycle facts without returning bodies or identifiers."""

    counts = {
        "total": 0,
        "delivery_confirmed": 0,
        "delivery_unconfirmed": 0,
        "delivery_pending": 0,
        "delivery_failed": 0,
        "delivery_ambiguous": 0,
    }
    categories = {
        "media_kind": {},
        "media_preflight_state": {},
        "visual_context_state": {},
        "media_observation_decision": {},
    }
    if rows is None:
        rows = ()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decision = row.get("decision_json")
        if isinstance(decision, str):
            try:
                decision = json.loads(decision)
            except (TypeError, ValueError, json.JSONDecodeError):
                decision = {}
        decision = decision if isinstance(decision, Mapping) else {}
        trace = build_media_delivery_trace(
            engagement_decision_id="",
            delivery_id=str(row.get("delivery_id") or ""),
            media_kind=str(decision.get("media_kind") or "none"),
            media_preflight_state=str(decision.get("media_preflight_state") or "none"),
            visual_context_state=str(decision.get("visual_context_state") or "none"),
            media_observation_decision=str(decision.get("media_observation_decision") or "none"),
            delivery_state=str(row.get("delivery_state") or row.get("delivery_certainty") or "pending"),
            ack_state=str(row.get("ack_state") or ""),
        )
        counts["total"] += 1
        outcome = trace["outcome_category"]
        counts[outcome] = counts.get(outcome, 0) + 1
        for key in categories:
            value = trace[key]
            bucket = categories[key]
            bucket[value] = int(bucket.get(value, 0)) + 1
    return {"counts": counts, "categories": categories}


def _delivery_projection_rows(conn, limit: int) -> list[dict]:
    """Project delivery facts from an outbox that lives beside decisions."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "delivery_outbox" not in tables:
        return []
    rows = conn.execute(
        """
        SELECT d.decision_json,
               o.id AS delivery_id,
               CASE
                 WHEN o.superseded_by<>'' THEN 'superseded'
                 WHEN o.dead_letter=1 THEN 'failed'
                 WHEN o.acked_at<>'' AND o.delivery_certainty='confirmed' THEN 'delivered'
                 WHEN o.delivery_certainty IN (
                   'pending','queued','claimed','sending','sent',
                   'failed','rejected','ambiguous'
                 ) THEN o.delivery_certainty
                 ELSE 'pending'
               END AS delivery_state,
               CASE
                 WHEN o.superseded_by<>'' THEN 'failed'
                 WHEN o.acked_at<>'' AND o.delivery_certainty='confirmed' THEN 'confirmed'
                 WHEN o.dead_letter=1 OR o.delivery_certainty IN ('failed','rejected') THEN 'failed'
                 WHEN o.delivery_certainty='ambiguous' THEN 'ambiguous'
                 ELSE 'pending'
               END AS ack_state
        FROM engagement_decisions AS d
        LEFT JOIN delivery_outbox AS o
          ON o.id = (
            SELECT o2.id
            FROM delivery_outbox AS o2
            WHERE o2.engagement_decision_id=d.id
            ORDER BY o2.updated_at DESC,o2.created_at DESC,o2.id DESC
            LIMIT 1
          )
        ORDER BY d.created_at DESC,d.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _cross_database_media_projection_rows(
    task_connect,
    decision_rows: list[sqlite3.Row],
) -> list[dict] | None:
    """Read delivery facts from the task DB without joining SQLite files.

    ``None`` means the task connector was unavailable or did not expose the
    expected Outbox schema; callers retain the assistant-only fallback.
    """

    if not callable(task_connect) or not decision_rows:
        return None
    decision_ids = [str(row[0]) for row in decision_rows]
    placeholders = ",".join("?" for _ in decision_ids)
    try:
        with task_connect() as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "delivery_outbox" not in tables:
                return None
            outbox_rows = conn.execute(
                f"""
                SELECT o.engagement_decision_id,
                       o.id AS delivery_id,
                       CASE
                         WHEN o.superseded_by<>'' THEN 'superseded'
                         WHEN o.dead_letter=1 THEN 'failed'
                         WHEN o.acked_at<>'' AND o.delivery_certainty='confirmed' THEN 'delivered'
                         WHEN o.delivery_certainty IN (
                           'pending','queued','claimed','sending','sent',
                           'failed','rejected','ambiguous'
                         ) THEN o.delivery_certainty
                         ELSE 'pending'
                       END AS delivery_state,
                       CASE
                         WHEN o.superseded_by<>'' THEN 'failed'
                         WHEN o.acked_at<>'' AND o.delivery_certainty='confirmed' THEN 'confirmed'
                         WHEN o.dead_letter=1 OR o.delivery_certainty IN ('failed','rejected') THEN 'failed'
                         WHEN o.delivery_certainty='ambiguous' THEN 'ambiguous'
                         ELSE 'pending'
                       END AS ack_state
                FROM delivery_outbox AS o
                WHERE o.engagement_decision_id IN ({placeholders})
                  AND o.id = (
                    SELECT o2.id
                    FROM delivery_outbox AS o2
                    WHERE o2.engagement_decision_id=o.engagement_decision_id
                    ORDER BY o2.updated_at DESC,o2.created_at DESC,o2.id DESC
                    LIMIT 1
                  )
                """,
                decision_ids,
            ).fetchall()
    except (AttributeError, OSError, TypeError, sqlite3.Error):
        return None

    outbox_by_decision = {
        str(row[0]): {
            "delivery_id": row[1],
            "delivery_state": row[2],
            "ack_state": row[3],
        }
        for row in outbox_rows
    }
    result: list[dict] = []
    for decision in decision_rows:
        decision_id = str(decision[0])
        delivery = outbox_by_decision.get(decision_id, {})
        result.append(
            {
                "decision_json": decision[1],
                "delivery_id": delivery.get("delivery_id", ""),
                "delivery_state": delivery.get("delivery_state", "pending"),
                "ack_state": delivery.get("ack_state", "pending"),
            },
        )
    return result


def participation_diagnostics(connect, *, task_connect=None, limit: int = 12) -> dict:
    empty = {"shadow_enabled": False, "deterministic_enabled": False, "decisions": []}
    try:
        with connect() as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "engagement_decisions" not in tables or "conversation_events" not in tables:
                return empty
            flags = {}
            if "assistant_feature_flags" in tables:
                flags = {
                    str(row[0]): bool(int(row[1]))
                    for row in conn.execute(
                        "SELECT name,enabled FROM assistant_feature_flags WHERE name IN (?,?)",
                        (PARTICIPATION_SHADOW_FEATURE_FLAG, DETERMINISTIC_PARTICIPATION_FEATURE_FLAG),
                    ).fetchall()
                }
            rows = conn.execute(
                """
                SELECT d.id,d.candidate_kind,d.action,d.reason_code,d.policy_version,
                       d.model_role,d.model_id,d.confidence,d.created_at,
                       e.channel_type,e.conversation_scope,e.message_kind,
                       e.reply_to_assistant,e.attachment_count,e.text_length
                FROM engagement_decisions d
                JOIN conversation_events e ON e.id=d.event_id
                ORDER BY d.created_at DESC,d.id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 50)),),
            ).fetchall()
            media_limit = max(1, min(int(limit), 50))
            decision_rows = conn.execute(
                """
                SELECT id,decision_json
                FROM engagement_decisions
                ORDER BY created_at DESC,id DESC LIMIT ?
                """,
                (media_limit,),
            ).fetchall()
            # Production keeps assistant decisions and the Delivery Outbox in
            # separate SQLite files.  Prefer that explicit task connector;
            # when it is absent/unavailable, retain the same-DB fallback.
            media_rows = _cross_database_media_projection_rows(
                task_connect, decision_rows,
            )
            if media_rows is None:
                media_rows = _delivery_projection_rows(conn, media_limit)
            if not media_rows:
                media_rows = [{"decision_json": row[1]} for row in decision_rows]
            media_projection = project_media_diagnostics(media_rows)
            return {
                "shadow_enabled": flags.get(PARTICIPATION_SHADOW_FEATURE_FLAG, False),
                "deterministic_enabled": flags.get(DETERMINISTIC_PARTICIPATION_FEATURE_FLAG, False),
                "decisions": [dict(row) for row in rows],
                "media": media_projection,
            }
    except (AttributeError, TypeError, sqlite3.Error, ValueError):
        return empty


__all__ = ["participation_diagnostics", "project_media_diagnostics"]
