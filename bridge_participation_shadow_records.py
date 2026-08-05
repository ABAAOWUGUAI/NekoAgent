"""Bounded persistence adapters for participation shadow decisions."""

from __future__ import annotations

import sqlite3

from bridge_conversation_participation import (
    decision_from_legacy,
    record_participation_decision,
    retention_for_decision,
    transition_participation_decision,
)


def record_group_shadow_decision(
    conn: sqlite3.Connection,
    event,
    *,
    allowed: bool,
    reason: str,
    group_id: str,
    source_message_id: str,
    model_role: str = "",
    model_id: str = "",
    conversation_frame: dict | None = None,
    interaction_decision: dict | None = None,
):
    decision = decision_from_legacy(
        event,
        allowed=allowed,
        legacy_reason=reason,
        model_role=model_role,
        model_id=model_id,
    )
    retention, _ = retention_for_decision(event, decision)
    return record_participation_decision(
        conn,
        decision,
        assistant_id=event.assistant_id,
        thread_id=f"qq:group:{group_id}",
        source_message_id=source_message_id,
        legacy_allowed=allowed,
        legacy_reason=reason,
        retention_class=retention,
        conversation_frame=conversation_frame,
        interaction_decision=interaction_decision,
    )


def transition_group_participation(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    stage: str,
    action: str | None = None,
    reason_code: str | None = None,
    model_role: str | None = None,
    model_id: str | None = None,
    confidence: float | None = None,
    superseded_by: str = "",
) -> dict | None:
    """Keep deferred natural-group candidates in their original record."""

    return transition_participation_decision(
        conn,
        decision_id=decision_id,
        stage=stage,
        action=action,
        reason_code=reason_code,
        model_role=model_role,
        model_id=model_id,
        confidence=confidence,
        superseded_by=superseded_by,
    )


__all__ = ["record_group_shadow_decision", "transition_group_participation"]
