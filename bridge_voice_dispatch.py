#!/usr/bin/env python3
"""Bridge-owned handoff from ephemeral voice transcript to unified QQ flow."""

from __future__ import annotations

import uuid

from bridge_inbound_context import inbound_exchange_context
from bridge_voice_input import VoiceInputError, complete_voice_dispatch


class VoiceDispatchRuntime:
    def __init__(
        self,
        connect,
        access_check,
        execute_once,
        transport_metadata,
        dispatch_response,
        assistant_dispatch,
        observe_private,
        bind_response,
        outbox,
        timeout: int,
        source: str,
    ):
        self.connect = connect
        self.access_check = access_check
        self.execute_once = execute_once
        self.transport_metadata = transport_metadata
        self.dispatch_response = dispatch_response
        self.assistant_dispatch = assistant_dispatch
        self.observe_private = observe_private
        self.bind_response = bind_response
        self.outbox = outbox
        self.timeout = timeout
        self.source = source

    def dispatch(self, request, payload: dict, receipt: dict) -> dict:
        actor_id = str(request.headers.get("X-QQ-Actor-ID", "") or "").strip()
        header_message_id = str(
            request.headers.get("X-QQ-Message-ID", "") or ""
        ).strip()
        external_message_id = str(payload.get("external_message_id") or "").strip()
        session = str(payload.get("session") or "").strip()
        transcript = str(receipt.get("transcript") or "").strip()
        receipt_id = str(receipt.get("id") or "").strip()
        if (
            not actor_id
            or not header_message_id
            or header_message_id != external_message_id
            or not session
            or not transcript
            or not receipt_id
        ):
            raise VoiceInputError("voice_dispatch_context_invalid")
        if self.access_check(self.connect, actor_id, "chat"):
            raise VoiceInputError("voice_dispatch_access_denied")
        trace_id = "voice-" + uuid.uuid4().hex[:16]
        safe_payload = {
            "user_id": actor_id,
            "session": session,
            "trace_id": trace_id,
            "external_message_id": external_message_id,
        }
        transport = self.transport_metadata(
            safe_payload,
            request.headers,
            default_actor=actor_id,
        )
        provenance = {
            **transport,
            "source_type_override": "qq_voice_transcript",
            "message_kind": "voice_transcript",
        }
        try:
            with inbound_exchange_context(provenance):
                result = self.execute_once(
                    self.connect,
                    header_message_id,
                    actor_id,
                    actor_id,
                    {
                        "schema_version": 1,
                        "receipt_id": receipt_id,
                        "external_message_id": external_message_id,
                    },
                    lambda: self.dispatch_response(
                        lambda: self.assistant_dispatch(
                            user_id=actor_id,
                            message=transcript,
                            timeout=self.timeout,
                            trace_id=trace_id,
                            force="auto",
                            source=self.source,
                            delivery_recipient_id=actor_id,
                            delivery_session=session,
                            inbound_context=provenance,
                        ),
                        transport,
                        scope="private",
                    ),
                )
            observation = self.observe_private(self.connect, provenance, result)
            self.bind_response(self.outbox(), result, observation)
            delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
            delivery_id = str(delivery.get("id") or "").strip()
            if not result.get("delivery_queued") or not delivery_id:
                raise VoiceInputError("voice_delivery_not_queued")
            with self.connect() as conn:
                message_row = conn.execute(
                    "SELECT id FROM conversation_messages WHERE external_message_id=? "
                    "AND role='user' AND source_type='qq_voice_transcript' "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (external_message_id,),
                ).fetchone()
                if not message_row:
                    raise VoiceInputError("voice_conversation_evidence_missing")
                complete_voice_dispatch(
                    conn,
                    receipt_id,
                    conversation_message_id=str(message_row[0]),
                    delivery_id=delivery_id,
                )
            return result
        except Exception as exc:
            try:
                with self.connect() as conn:
                    complete_voice_dispatch(
                        conn,
                        receipt_id,
                        error_kind=(
                            str(exc)
                            if str(exc).startswith("voice_")
                            else "voice_dispatch_failed"
                        ),
                    )
            except Exception:
                pass
            raise


def bridge_voice_dispatch(namespace: dict):
    """Bind the legacy Bridge module without growing its orchestration surface."""

    keys = (
        "_assistant_db_connect", "qq_private_access_http_error",
        "execute_inbound_once", "with_qq_transport_metadata",
        "_dispatch_qq_response_if_enabled", "_assistant_dispatch",
        "observe_private_participation", "bind_qq_response_decision",
        "_phase2_outbox", "DISPATCH_CHAT_TIMEOUT", "QQ_TASK_SOURCE",
    )
    return VoiceDispatchRuntime(*(namespace[key] for key in keys)).dispatch


__all__ = ["VoiceDispatchRuntime", "bridge_voice_dispatch"]
