"""Validate and materialize one lease-bound WAV for AstrBot Record delivery."""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.parse
import wave
from contextlib import contextmanager
from pathlib import Path


MAX_VOICE_MEDIA_BYTES = 10 * 1024 * 1024


@contextmanager
def temporary_voice_file(payload: bytes, expected_sha256: str):
    if (
        len(payload) < 44
        or len(payload) > MAX_VOICE_MEDIA_BYTES
        or hashlib.sha256(payload).hexdigest() != expected_sha256
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WAVE"
    ):
        raise ValueError("voice_media_integrity_failed")
    descriptor, name = tempfile.mkstemp(prefix="codex-qq-voice-", suffix=".wav")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            with wave.open(str(path), "rb") as stream:
                if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
                    raise ValueError("voice_media_format_invalid")
                if not 8_000 <= stream.getframerate() <= 48_000 or stream.getnframes() <= 0:
                    raise ValueError("voice_media_format_invalid")
        except wave.Error as exc:
            raise ValueError("voice_media_format_invalid") from exc
        yield str(path)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def fetch_delivery_voice(delivery: dict, lease_token: str, fetch_bytes):
    delivery_id = str(delivery.get("id") or delivery.get("delivery_id") or "").strip()
    payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
    media = payload.get("voice_media") if isinstance(payload.get("voice_media"), dict) else {}
    expected_hash = str(media.get("sha256") or "")
    expected_size = int(media.get("size_bytes") or 0)
    if (
        not delivery_id
        or len(expected_hash) != 64
        or expected_size <= 0
        or expected_size > MAX_VOICE_MEDIA_BYTES
    ):
        raise ValueError("voice_media_descriptor_invalid")
    path = "/deliveries/" + urllib.parse.quote(delivery_id, safe="") + "/media"
    result = await fetch_bytes(
        path,
        headers={"X-Delivery-Lease-Token": lease_token},
        max_bytes=MAX_VOICE_MEDIA_BYTES,
    )
    body = result.get("body") if isinstance(result.get("body"), bytes) else b""
    if (
        not result.get("ok")
        or result.get("content_type") != "audio/wav"
        or len(body) != expected_size
        or str(result.get("etag") or "") != expected_hash
    ):
        raise RuntimeError(str(result.get("error") or "voice_media_fetch_failed"))
    return body, expected_hash


__all__ = ["MAX_VOICE_MEDIA_BYTES", "fetch_delivery_voice", "temporary_voice_file"]
