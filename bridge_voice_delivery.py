#!/usr/bin/env python3
"""Lease-bound media resolver for QQ voice-message delivery."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from bridge_artifact_service import ArtifactService
from bridge_voice_output import MAX_AUDIO_BYTES, VoiceOutputError


class VoiceDeliveryRuntime:
    def __init__(self, outbox_factory, artifact_service: ArtifactService) -> None:
        self.outbox_factory = outbox_factory
        self.artifact_service = artifact_service

    @staticmethod
    def _lease_current(delivery: dict, lease_token: str) -> bool:
        expected = str(delivery.get("lease_token") or "")
        if not expected or not lease_token or not hmac.compare_digest(expected, lease_token):
            return False
        try:
            expires = datetime.fromisoformat(str(delivery.get("lease_expires_at") or ""))
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires.astimezone(timezone.utc) > datetime.now(timezone.utc)

    def media(self, delivery_id: str, lease_token: str) -> tuple[bytes, str, str]:
        delivery = self.outbox_factory().get_delivery(delivery_id)
        if not delivery or not self._lease_current(delivery, lease_token):
            raise VoiceOutputError("voice_delivery_lease_invalid")
        payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
        if payload.get("kind") != "assistant_voice_reply":
            raise VoiceOutputError("voice_delivery_media_not_found")
        media = payload.get("voice_media") if isinstance(payload.get("voice_media"), dict) else {}
        required = {
            "artifact_version_id",
            "artifact_owner_id",
            "relative_path",
            "sha256",
            "size_bytes",
        }
        if not required.issubset(media):
            raise VoiceOutputError("voice_delivery_media_invalid")
        audio, content_type, _ = self.artifact_service.file_payload(
            str(media["artifact_version_id"]),
            str(media["relative_path"]),
            owner_id=str(media["artifact_owner_id"]),
        )
        expected_size = int(media["size_bytes"])
        expected_hash = str(media["sha256"])
        if (
            content_type != "audio/wav"
            or expected_size <= 0
            or expected_size > MAX_AUDIO_BYTES
            or len(audio) != expected_size
            or hashlib.sha256(audio).hexdigest() != expected_hash
        ):
            raise VoiceOutputError("voice_delivery_media_integrity_failed")
        return audio, content_type, expected_hash


__all__ = ["VoiceDeliveryRuntime"]
