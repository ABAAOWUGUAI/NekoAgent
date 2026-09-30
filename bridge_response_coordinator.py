#!/usr/bin/env python3
"""Bridge-owned scheduling for durable private-conversation messages.

The coordinator deliberately owns only response-cycle selection.  Conversation
messages remain authoritative in SQLite, while raw media stays process-local
and is erased as soon as its cycle finishes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Callable, Mapping

from bridge_continuous_private_conversation import (
    MAX_SOURCE_MESSAGES,
    ResponseCycleBusyError,
    acquire_response_cycle,
    continuous_private_conversation_enabled,
    mark_response_cycle_failure,
)
from bridge_media_contract import media_preflight
from bridge_response_cycle_recovery import reconcile_response_cycles
from bridge_visual_context import (
    MAX_VISUAL_IMAGES,
    MAX_VISUAL_IMAGE_BYTES,
    MAX_VISUAL_VIDEO_BYTES,
)


_VISUAL_COMPONENTS = frozenset({
    "image", "photo", "picture", "gif", "video", "mface", "marketface",
    "market_face", "dynamicface", "dynamic_face",
})


def _rows(cursor) -> list[dict]:
    names = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def _metadata(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _age_seconds(value: object) -> float:
    token = str(value or "").strip()
    if not token:
        return float("inf")
    try:
        created = datetime.fromisoformat(token.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
    except ValueError:
        return float("inf")


def _has_visual(metadata: Mapping[str, object]) -> bool:
    attachments = metadata.get("attachments")
    if isinstance(attachments, list) and attachments:
        return True
    components = metadata.get("message_components")
    return bool(
        isinstance(components, list)
        and any(
            isinstance(item, Mapping)
            and str(item.get("type") or "").strip().lower() in _VISUAL_COMPONENTS
            for item in components
        )
    )


class BridgeResponseCoordinator:
    """Scan durable pending input and freeze exactly one response cycle per thread."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        process_cycle: Callable[[dict, dict], None],
        *,
        debounce_seconds: float = 0.9,
        media_deadline_seconds: float = 15.0,
        media_only_grace_seconds: float = 0.0,
        scan_interval_seconds: float = 1.0,
        lease_owner: str = "bridge-response-coordinator",
        health_registry: object | None = None,
        health_worker_id: str = "continuous_private_response_coordinator",
        outbox=None,
    ) -> None:
        self._connect = connect
        self._process_cycle = process_cycle
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._media_deadline_seconds = max(0.0, float(media_deadline_seconds))
        # Kept only for rolling constructor compatibility with the brief r5
        # deployment.  A media-specific timer must not define conversation
        # membership; source freeze uses the same debounce for every message.
        del media_only_grace_seconds
        self._scan_interval_seconds = max(0.05, float(scan_interval_seconds))
        self._lease_owner = str(lease_owner or "bridge-response-coordinator")[:120]
        self._health_registry = health_registry
        self._health_worker_id = str(health_worker_id or "continuous_private_response_coordinator")[:80]
        self._outbox = outbox
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._process_lock = threading.Lock()
        self._media_lock = threading.Lock()
        self._transient_media: dict[str, dict] = {}

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def bind_media(
        self,
        *,
        source_message_id: str,
        external_message_id: str,
        actor_id: str,
        visual_media: object,
    ) -> dict:
        """Bind bounded raw media to one accepted, still-unassigned source."""

        source_id = str(source_message_id or "").strip()
        external_id = str(external_message_id or "").strip()
        actor = str(actor_id or "").strip()
        if not source_id or not external_id or not actor:
            raise ValueError("private_media_binding_required")
        if not isinstance(visual_media, list) or len(visual_media) > MAX_VISUAL_IMAGES:
            raise ValueError("private_media_payload_invalid")

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT m.external_message_id,t.external_thread_ref,m.metadata_json
                FROM conversation_messages m
                JOIN conversation_threads t ON t.id=m.thread_id
                WHERE m.id=? AND m.role='user'
                  AND NOT EXISTS(
                    SELECT 1 FROM conversation_response_cycle_sources s
                    WHERE s.source_message_id=m.id
                  )
                """,
                (source_id,),
            ).fetchone()
        if not row:
            raise ValueError("private_media_source_unavailable")
        if str(row[0]) != external_id or str(row[1]) != actor:
            raise ValueError("private_media_binding_mismatch")
        if int(_metadata(row[2]).get("response_coordinator_version") or 0) != 2:
            raise ValueError("private_media_protocol_mismatch")

        retained: list[dict] = []
        for item in visual_media:
            kind = str(item.get("type") or item.get("media_kind") or "").strip().lower() if isinstance(item, Mapping) else ""
            limit = MAX_VISUAL_VIDEO_BYTES if kind == "video" else MAX_VISUAL_IMAGE_BYTES
            check = media_preflight(item, transport_available=True, max_bytes=limit)
            if check.get("state") != "ready":
                continue
            retained.append({
                "type": "video" if check.get("media_kind") == "video" else "image",
                "mime": str(check.get("safe_mime") or "")[:80],
                "data_base64": item.get("data_base64"),
            })
        status = "ready" if retained else "unavailable"
        with self._media_lock:
            previous = self._transient_media.pop(source_id, None)
            self._clear_media_record(previous)
            self._transient_media[source_id] = {
                "status": status,
                "visual_media": retained,
            }
        self.wake()
        return {
            "ok": True,
            "accepted": True,
            "source_message_id": source_id,
            "visual_context_status": status,
            "media_count": len(retained),
        }

    @staticmethod
    def _clear_media_record(record: object) -> None:
        if not isinstance(record, dict):
            return
        media = record.get("visual_media")
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict):
                    item.clear()
            media.clear()
        record.clear()

    def _candidate_threads(self) -> list[dict]:
        with closing(self._connect()) as conn:
            if not continuous_private_conversation_enabled(conn):
                return []
            return _rows(conn.execute(
                """
                SELECT t.id,t.external_thread_ref,MIN(m.inbound_sequence) AS first_sequence
                FROM conversation_threads t
                JOIN conversation_messages m ON m.thread_id=t.id
                WHERE m.role='user' AND m.inbound_sequence IS NOT NULL
                  AND json_extract(m.metadata_json,'$.response_coordinator_version')=2
                  AND NOT EXISTS(
                    SELECT 1 FROM conversation_response_cycle_sources s
                    WHERE s.source_message_id=m.id
                  )
                  AND NOT EXISTS(
                    SELECT 1 FROM conversation_response_cycles c
                    WHERE c.thread_id=t.id
                      AND c.state IN ('pending','processing','commit_ready','retryable','manual_hold')
                      AND c.blocks_thread=1
                  )
                GROUP BY t.id,t.external_thread_ref
                ORDER BY first_sequence,t.id
                LIMIT 8
                """
            ))

    def _pending_messages(self, conn: sqlite3.Connection, thread_id: str) -> list[dict]:
        return _rows(conn.execute(
            """
            SELECT m.id,m.external_message_id,m.content,m.created_at,m.metadata_json,
                   m.inbound_sequence,m.reply_to_external_message_id
            FROM conversation_messages m
            WHERE m.thread_id=? AND m.role='user' AND m.inbound_sequence IS NOT NULL
              AND NOT EXISTS(
                SELECT 1 FROM conversation_response_cycle_sources s
                WHERE s.source_message_id=m.id
              )
            ORDER BY m.inbound_sequence,m.id
            LIMIT ?
            """,
            (str(thread_id), MAX_SOURCE_MESSAGES + 1),
        ))

    def _cycle_messages(self, conn: sqlite3.Connection, cycle_id: str) -> list[dict]:
        return _rows(conn.execute(
            """
            SELECT m.id,m.external_message_id,m.content,m.created_at,m.metadata_json,
                   m.inbound_sequence,m.reply_to_external_message_id
            FROM conversation_response_cycle_sources s
            JOIN conversation_messages m ON m.id=s.source_message_id
            WHERE s.cycle_id=? ORDER BY s.source_order
            """,
            (str(cycle_id),),
        ))

    def _media_ready_or_expired(self, messages: list[dict]) -> bool:
        latest_age = _age_seconds(messages[-1].get("created_at"))
        if latest_age < self._debounce_seconds:
            return False
        has_visual = any(
            _has_visual(_metadata(message.get("metadata_json")))
            for message in messages
        )
        with self._media_lock:
            for message in messages:
                metadata = _metadata(message.get("metadata_json"))
                if not _has_visual(metadata):
                    continue
                if str(message["id"]) in self._transient_media:
                    continue
                if _age_seconds(message.get("created_at")) < self._media_deadline_seconds:
                    return False
        return True

    def _build_context(self, cycle: dict, messages: list[dict], owner_actor_id: str) -> dict:
        components: list[dict] = []
        attachments: list[dict] = []
        text_parts: list[str] = []
        visual_index = 0
        transient: list[dict] = []

        for message in messages:
            metadata = _metadata(message.get("metadata_json"))
            had_text = bool(metadata.get("logical_turn_has_text"))
            content = str(message.get("content") or "").strip()
            if had_text and content:
                text_parts.append(content)
            raw_attachments = metadata.get("attachments")
            if isinstance(raw_attachments, list):
                attachments.extend(dict(item) for item in raw_attachments if isinstance(item, Mapping))

            raw_components = metadata.get("message_components")
            if not isinstance(raw_components, list):
                raw_components = []
            plain_added = False
            has_plain_spans = any(
                isinstance(raw, Mapping) and "text_start" in raw
                for raw in raw_components
            )
            for raw in raw_components:
                if not isinstance(raw, Mapping):
                    continue
                kind = str(raw.get("type") or "").strip().lower()
                if kind in {"plain", "text"}:
                    if has_plain_spans:
                        start, end = raw.get("text_start"), raw.get("text_end")
                        if (type(start) is not int or type(end) is not int
                                or start < 0 or end <= start or end > len(content)):
                            raise ValueError("private_ingress_plain_span_mismatch")
                        components.append({
                            "type": "plain",
                            "position": len(components),
                            "text": content[start:end],
                        })
                        plain_added = True
                        continue
                    if had_text and content and not plain_added:
                        components.append({
                            "type": "plain",
                            "position": len(components),
                            "text": content,
                        })
                        plain_added = True
                    continue
                if kind not in _VISUAL_COMPONENTS:
                    continue
                component = {
                    "type": kind,
                    "position": len(components),
                    "visual_index": visual_index,
                }
                for key in ("media_kind", "source_component"):
                    value = str(raw.get(key) or "").strip().lower()[:40]
                    if value:
                        component[key] = value
                components.append(component)
                visual_index += 1
            if had_text and content and not plain_added:
                components.append({
                    "type": "plain",
                    "position": len(components),
                    "text": content,
                })

            with self._media_lock:
                record = self._transient_media.get(str(message["id"]))
                raw_media = record.get("visual_media") if isinstance(record, dict) else None
                if isinstance(raw_media, list):
                    transient.extend(raw_media)

        if not components:
            components.append({
                "type": "plain",
                "position": 0,
                "text": "（发送了一项媒体内容）",
            })
        external_ids = [str(item["external_message_id"]) for item in messages]
        internal_ids = [str(item["id"]) for item in messages]
        return {
            "channel": "qq",
            "conversation_type": "private",
            "user_id": str(owner_actor_id),
            "_qq_actor_id": str(owner_actor_id),
            "_qq_actor_role": "owner",
            "session": f"qq:private:{owner_actor_id}",
            "message": "\n".join(text_parts) or "（发送了一项媒体内容）",
            "source_message_ids": external_ids,
            "source_internal_message_ids": internal_ids,
            "_external_message_id": external_ids[-1],
            "reply_to_external_message_id": str(
                messages[-1].get("reply_to_external_message_id") or ""
            ),
            "_response_cycle_id": str(cycle["id"]),
            "logical_turn_id": str(cycle["id"]),
            "logical_turn_has_text": bool(text_parts),
            "attachments": attachments,
            "message_components": components,
            "visual_media": transient,
            "trace_id": f"coordinator:{cycle['id']}",
        }

    def _clear_cycle_media(self, context: dict, source_ids: list[str]) -> None:
        media = context.get("visual_media")
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict):
                    item.clear()
            media.clear()
        with self._media_lock:
            for source_id in source_ids:
                self._clear_media_record(self._transient_media.pop(source_id, None))

    def _process_thread(self, candidate: Mapping[str, object]) -> bool:
        owner = str(candidate.get("external_thread_ref") or "").strip()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            messages = self._pending_messages(conn, str(candidate["id"]))
            if not messages or len(messages) > MAX_SOURCE_MESSAGES:
                conn.rollback()
                return False
            if not self._media_ready_or_expired(messages):
                conn.rollback()
                return False
            try:
                cycle = acquire_response_cycle(
                    conn,
                    legacy_user_id=owner,
                    source_external_message_ids=[str(item["external_message_id"]) for item in messages],
                    lease_owner=f"{self._lease_owner}:{uuid.uuid4().hex[:12]}",
                )
            except ResponseCycleBusyError:
                conn.rollback()
                return False
            conn.commit()

        context = self._build_context(cycle, messages, owner)
        source_ids = [str(item["id"]) for item in messages]
        try:
            self._process_cycle(cycle, context)
        except Exception as exc:
            try:
                with closing(self._connect()) as conn:
                    mark_response_cycle_failure(
                        conn,
                        cycle_id=str(cycle["id"]),
                        lease_token=str(cycle["lease_token"]),
                        error=type(exc).__name__,
                    )
                    conn.commit()
            except Exception:
                pass
        finally:
            self._clear_cycle_media(context, source_ids)
        return True

    def _recover_unstarted_cycle(self, cycle: dict) -> None:
        with closing(self._connect()) as conn:
            messages = self._cycle_messages(conn, str(cycle["id"]))
            row = conn.execute(
                "SELECT external_thread_ref FROM conversation_threads WHERE id=?",
                (str(cycle["thread_id"]),),
            ).fetchone()
        if not messages or row is None:
            raise RuntimeError("response_cycle_recovery_sources_missing")
        for message in messages:
            if not _has_visual(_metadata(message.get("metadata_json"))):
                continue
            with self._media_lock:
                record = self._transient_media.get(str(message["id"]))
                ready = isinstance(record, dict) and bool(record.get("visual_media"))
            if not ready:
                raise RuntimeError("response_cycle_recovery_media_unavailable")
        context = self._build_context(cycle, messages, str(row[0]))
        source_ids = [str(item["id"]) for item in messages]
        try:
            self._process_cycle(cycle, context)
        finally:
            self._clear_cycle_media(context, source_ids)

    def process_once(self) -> int:
        if not self._process_lock.acquire(blocking=False):
            return 0
        try:
            processed = 0
            if self._outbox is not None:
                recovered = reconcile_response_cycles(
                    self._connect,
                    self._outbox,
                    lease_owner=f"{self._lease_owner}:recovery:{uuid.uuid4().hex[:12]}",
                    process_unstarted=self._recover_unstarted_cycle,
                )
                processed += sum(int(recovered.get(key) or 0) for key in (
                    "reclaimed", "bound", "held",
                ))
            for candidate in self._candidate_threads():
                if self._process_thread(candidate):
                    processed += 1
            return processed
        finally:
            self._process_lock.release()

    def run(self) -> None:
        while not self._stop.is_set():
            health = self._health_registry
            if health is not None:
                health.begin(self._health_worker_id)
            try:
                self.process_once()
            except Exception as exc:
                # The caller owns structured worker health reporting.  Raw
                # message or media data must never enter this fallback path.
                if health is not None:
                    health.failure(self._health_worker_id, exc)
            else:
                if health is not None:
                    health.success(self._health_worker_id)
            self._wake.wait(self._scan_interval_seconds)
            self._wake.clear()


__all__ = ["BridgeResponseCoordinator"]
