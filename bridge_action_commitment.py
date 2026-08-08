#!/usr/bin/env python3
"""Scoped persistence for server-rendered Owner action proposals."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import uuid

from bridge_action_commitment_schema import (
    action_commitment_feature_enabled,
    require_action_commitment_schema,
)
from bridge_action_registry import action_definition
from bridge_assistant_identity import current_assistant
from bridge_migrations import utc_now


_ALLOWED_RISK_LEVELS = {"low", "medium"}
_DECLINE_HINTS = ("不要", "不用", "取消", "算了", "不需要", "先别")
_ACCEPT_HINTS = ("需要", "可以", "行", "好的", "好", "确认", "同意", "照办", "就这么办", "继续")
_NON_ACCEPT_HINTS = ("不行", "不可以", "不太行", "先不", "不好")


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_after(value: str, seconds: int) -> str:
    return (_parse_timestamp(value) + timedelta(seconds=seconds)).isoformat()


def _public(row: Mapping[str, object]) -> dict:
    return {
        "id": str(row["id"]),
        "origin_plan_id": str(row["origin_plan_id"]),
        "action_type": str(row["action_type"]),
        "action_hash": str(row["action_hash"]),
        "approval_policy": str(row["approval_policy"]),
        "state": str(row["state"]),
        "expires_at": str(row["expires_at"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class ActionCommitmentRepository:
    """Persist only a time-bounded negotiation reference for a registered action."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], *, ttl_seconds: int = 900) -> None:
        self._connect = connect
        self._ttl_seconds = max(30, min(int(ttl_seconds), 3600))

    @staticmethod
    def _validate_action(action: Mapping[str, object]) -> tuple[dict, object]:
        payload = dict(action)
        action_type = str(payload.get("action_type") or "").strip()
        definition = action_definition(action_type)
        if (
            not definition.side_effect
            or definition.approval_policy != "owner_private"
            or definition.risk_level not in _ALLOWED_RISK_LEVELS
        ):
            raise ValueError("action_commitment_action_not_eligible")
        return payload, definition

    @staticmethod
    def _origin(conn: sqlite3.Connection, origin_plan_id: str, actor_id: str) -> dict:
        row = conn.execute(
            """
            SELECT p.id,p.assistant_id,p.owner_actor_id
            FROM interaction_plans p
            JOIN assistant_instances a ON a.id=p.assistant_id
            JOIN conversation_threads t ON t.id=p.thread_id
            WHERE p.id=? AND a.status='active'
              AND t.channel_type='qq_private'
              AND t.external_thread_ref=?
              AND t.legacy_user_id=?
            """,
            (str(origin_plan_id or ""), str(actor_id or ""), str(actor_id or "")),
        ).fetchone()
        if not row:
            raise ValueError("action_commitment_origin_plan_invalid")
        return dict(row)

    def propose(
        self,
        *,
        actor_id: str,
        thread_ref: str,
        origin_plan_id: str,
        action: Mapping[str, object],
        rendered_reply: str,
        now: str = "",
    ) -> dict:
        actor = str(actor_id or "").strip()
        thread = str(thread_ref or "").strip()
        if not actor or thread != f"qq:private:{actor}":
            raise ValueError("action_commitment_private_owner_required")
        rendered = str(rendered_reply or "").strip()
        if not rendered:
            raise ValueError("action_commitment_rendered_reply_required")
        payload, definition = self._validate_action(action)
        created_at = str(now or utc_now())
        expires_at = _timestamp_after(created_at, self._ttl_seconds)
        action_json = _canonical(payload)
        conn = self._connect()
        try:
            with conn:
                if not action_commitment_feature_enabled(conn):
                    raise ValueError("action_commitment_feature_disabled")
                require_action_commitment_schema(conn)
                origin = self._origin(conn, origin_plan_id, actor)
                current = current_assistant(conn)
                if not current or str(current["id"]) != str(origin["assistant_id"]):
                    raise ValueError("action_commitment_assistant_mismatch")
                commitment_id = "action-commitment-" + uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO interaction_action_commitments(
                        id,assistant_id,owner_actor_id,thread_ref,origin_plan_id,action_type,
                        action_json,action_hash,approval_policy,rendered_reply_hash,state,expires_at,
                        action_receipt_id,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'proposed',?,'',?,?)
                    """,
                    (
                        commitment_id,
                        origin["assistant_id"],
                        actor,
                        thread,
                        str(origin_plan_id),
                        definition.action_type,
                        action_json,
                        _digest(action_json),
                        definition.approval_policy,
                        _digest(rendered),
                        expires_at,
                        created_at,
                        created_at,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM interaction_action_commitments WHERE id=?",
                    (commitment_id,),
                ).fetchone()
        finally:
            conn.close()
        return _public(dict(row))

    def find_open(
        self,
        *,
        actor_id: str,
        thread_ref: str,
        now: str = "",
    ) -> dict | None:
        actor = str(actor_id or "").strip()
        thread = str(thread_ref or "").strip()
        if not actor or thread != f"qq:private:{actor}":
            return None
        current_time = str(now or utc_now())
        conn = self._connect()
        try:
            with conn:
                if not action_commitment_feature_enabled(conn):
                    return None
                conn.execute(
                    """
                    UPDATE interaction_action_commitments SET state='expired',updated_at=?
                    WHERE owner_actor_id=? AND thread_ref=? AND state='proposed' AND expires_at<=?
                    """,
                    (current_time, actor, thread, current_time),
                )
                row = conn.execute(
                    """
                    SELECT * FROM interaction_action_commitments
                    WHERE owner_actor_id=? AND thread_ref=? AND state='proposed' AND expires_at>?
                    ORDER BY created_at DESC,id DESC LIMIT 1
                    """,
                    (actor, thread, current_time),
                ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        result = _public(dict(row))
        result["action"] = json.loads(str(row["action_json"]))
        return result

    def expire_due(self, *, now: str = "") -> int:
        current_time = str(now or utc_now())
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE interaction_action_commitments SET state='expired',updated_at=?
                    WHERE state='proposed' AND expires_at<=?
                    """,
                    (current_time, current_time),
                )
        finally:
            conn.close()
        return int(cursor.rowcount or 0)

    def invalidate_unrendered(
        self,
        commitment_id: str,
        *,
        actor_id: str,
        thread_ref: str,
        now: str = "",
    ) -> dict | None:
        """Close an offer that could not be durably recorded for the Owner."""

        actor = str(actor_id or "").strip()
        thread = str(thread_ref or "").strip()
        if not actor or thread != f"qq:private:{actor}":
            return None
        current_time = str(now or utc_now())
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE interaction_action_commitments
                    SET state='expired',updated_at=?
                    WHERE id=? AND owner_actor_id=? AND thread_ref=? AND state='proposed'
                    """,
                    (current_time, str(commitment_id or ""), actor, thread),
                )
                row = conn.execute(
                    "SELECT * FROM interaction_action_commitments WHERE id=?",
                    (str(commitment_id or ""),),
                ).fetchone()
        finally:
            conn.close()
        return _public(dict(row)) if row else None

    def _transition(
        self,
        *,
        commitment_id: str,
        actor_id: str,
        thread_ref: str,
        next_state: str,
        action: Mapping[str, object] | None = None,
        now: str = "",
    ) -> dict | None:
        actor = str(actor_id or "").strip()
        thread = str(thread_ref or "").strip()
        if not actor or thread != f"qq:private:{actor}":
            return None
        current_time = str(now or utc_now())
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    """
                    SELECT * FROM interaction_action_commitments
                    WHERE id=? AND owner_actor_id=? AND thread_ref=?
                      AND state='proposed' AND expires_at>?
                    """,
                    (str(commitment_id or ""), actor, thread, current_time),
                ).fetchone()
                if not row:
                    return None
                original = json.loads(str(row["action_json"]))
                payload = original if action is None else dict(action)
                validated, definition = self._validate_action(payload)
                action_json = _canonical(validated)
                state = next_state
                if next_state == "accepted" and action_json != str(row["action_json"]):
                    state = "amended"
                conn.execute(
                    """
                    UPDATE interaction_action_commitments
                    SET state=?,action_type=?,action_json=?,action_hash=?,approval_policy=?,updated_at=?
                    WHERE id=? AND state='proposed'
                    """,
                    (
                        state,
                        definition.action_type,
                        action_json,
                        _digest(action_json),
                        definition.approval_policy,
                        current_time,
                        str(row["id"]),
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM interaction_action_commitments WHERE id=?",
                    (str(row["id"]),),
                ).fetchone()
        finally:
            conn.close()
        result = _public(dict(updated))
        result["action"] = json.loads(str(updated["action_json"]))
        return result

    def accept(
        self,
        commitment_id: str,
        *,
        actor_id: str,
        thread_ref: str,
        action: Mapping[str, object],
        now: str = "",
    ) -> dict | None:
        return self._transition(
            commitment_id=commitment_id,
            actor_id=actor_id,
            thread_ref=thread_ref,
            next_state="accepted",
            action=action,
            now=now,
        )

    def decline(
        self,
        commitment_id: str,
        *,
        actor_id: str,
        thread_ref: str,
        now: str = "",
    ) -> dict | None:
        return self._transition(
            commitment_id=commitment_id,
            actor_id=actor_id,
            thread_ref=thread_ref,
            next_state="declined",
            now=now,
        )

    def mark_execution(
        self,
        commitment_id: str,
        *,
        actor_id: str,
        thread_ref: str,
        receipt: Mapping[str, object] | None,
        now: str = "",
    ) -> dict | None:
        """Link an ActionReceipt after execution without declaring delivery success."""

        actor = str(actor_id or "").strip()
        thread = str(thread_ref or "").strip()
        if not actor or thread != f"qq:private:{actor}":
            return None
        current_time = str(now or utc_now())
        receipt_data = dict(receipt or {})
        succeeded = str(receipt_data.get("status") or "").lower() in {"completed", "no_op"}
        next_state = "executed" if succeeded else "failed"
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    """
                    SELECT * FROM interaction_action_commitments
                    WHERE id=? AND owner_actor_id=? AND thread_ref=?
                      AND state IN ('accepted','amended')
                    """,
                    (str(commitment_id or ""), actor, thread),
                ).fetchone()
                if not row:
                    return None
                conn.execute(
                    """
                    UPDATE interaction_action_commitments
                    SET state=?,action_receipt_id=?,updated_at=? WHERE id=?
                    """,
                    (
                        next_state,
                        str(receipt_data.get("receipt_id") or "")[:160],
                        current_time,
                        str(row["id"]),
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM interaction_action_commitments WHERE id=?",
                    (str(row["id"]),),
                ).fetchone()
        finally:
            conn.close()
        return _public(dict(updated))

def _resolution_kind(message: str) -> str:
    text = str(message or "").strip().lower()
    if not text or len(text) > 240 or "?" in text or "？" in text:
        return ""
    if any(hint in text for hint in _DECLINE_HINTS):
        return "decline"
    if any(hint in text for hint in _NON_ACCEPT_HINTS):
        return ""
    if any(
        text == hint or text.startswith(hint + marker)
        for hint in _ACCEPT_HINTS
        for marker in ("，", ",", "。", "！", "!", " ")
    ):
        return "accept"
    return ""


def _refine_registered_action(action: Mapping[str, object], message: str) -> dict:
    payload = dict(action)
    if str(payload.get("action_type") or "").startswith("qq_group_"):
        from bridge_qq_action_commitment import refine_qq_action_commitment

        return refine_qq_action_commitment(payload, message)
    return payload


def resolve_action_commitment(
    repository: ActionCommitmentRepository,
    *,
    actor_id: str,
    thread_ref: str,
    message: str,
    now: str = "",
) -> dict | None:
    """Resolve one valid Owner reply without executing its action."""

    commitment = repository.find_open(
        actor_id=actor_id,
        thread_ref=thread_ref,
        now=now,
    )
    if commitment is None:
        return None
    resolution = _resolution_kind(message)
    if resolution == "decline":
        declined = repository.decline(
            commitment["id"],
            actor_id=actor_id,
            thread_ref=thread_ref,
            now=now,
        )
        if declined is not None:
            declined["resolution"] = "declined"
        return declined
    if resolution != "accept":
        return None
    resolved = repository.accept(
        commitment["id"],
        actor_id=actor_id,
        thread_ref=thread_ref,
        action=_refine_registered_action(commitment["action"], message),
        now=now,
    )
    if resolved is not None:
        resolved["resolution"] = "accepted"
    return resolved


__all__ = ["ActionCommitmentRepository", "resolve_action_commitment"]
