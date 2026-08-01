#!/usr/bin/env python3
"""Bounded, local-only speech transcription for QQ voice messages."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import wave


class VoiceTranscriptionError(ValueError):
    pass


_TRANSCRIPTION_SLOT = threading.BoundedSemaphore(1)


def transcription_policy(conn) -> dict:
    row = conn.execute(
        "SELECT voice_transcription_adapter,voice_transcription_executable,"
        "voice_transcription_model_path,voice_transcription_model_sha256,"
        "voice_transcription_ffmpeg,voice_transcription_threads,"
        "voice_transcription_timeout_seconds,voice_transcription_max_duration_seconds "
        "FROM qq_channel_settings WHERE channel_id='qq-main'"
    ).fetchone()
    if not row or str(row[0]) != "whisper_cpp":
        raise VoiceTranscriptionError("voice_transcription_policy_invalid")
    policy = {
        "adapter": str(row[0]), "executable": str(row[1]), "model_path": str(row[2]),
        "model_sha256": str(row[3]).lower(), "ffmpeg": str(row[4]),
        "threads": int(row[5]), "timeout_seconds": int(row[6]),
        "max_duration_seconds": int(row[7]),
    }
    if not 1 <= policy["threads"] <= 2 or not 10 <= policy["timeout_seconds"] <= 120:
        raise VoiceTranscriptionError("voice_transcription_policy_invalid")
    if not 1 <= policy["max_duration_seconds"] <= 60:
        raise VoiceTranscriptionError("voice_transcription_policy_invalid")
    if len(policy["model_sha256"]) != 64:
        raise VoiceTranscriptionError("voice_transcription_policy_invalid")
    return policy


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_runtime(policy: dict) -> None:
    executable = Path(policy["executable"])
    model = Path(policy["model_path"])
    ffmpeg = Path(policy["ffmpeg"])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise VoiceTranscriptionError("voice_transcription_executable_missing")
    if not ffmpeg.is_file() or not os.access(ffmpeg, os.X_OK):
        raise VoiceTranscriptionError("voice_transcription_ffmpeg_missing")
    if not model.is_file() or _sha256(model) != policy["model_sha256"]:
        raise VoiceTranscriptionError("voice_transcription_model_invalid")


def _transcribe_voice_file(source: Path, policy: dict, *, temp_root: str | None = None) -> dict:
    validate_runtime(policy)
    if not source.is_file():
        raise VoiceTranscriptionError("voice_source_missing")
    with tempfile.TemporaryDirectory(prefix="qq-voice-asr-", dir=temp_root) as directory:
        wav_path = Path(directory) / "input.wav"
        command = [
            policy["ffmpeg"], "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-t", str(policy["max_duration_seconds"] + 1), str(wav_path),
        ]
        try:
            decoded = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceTranscriptionError("voice_decode_failed") from exc
        if decoded.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size > 2_100_000:
            raise VoiceTranscriptionError("voice_decode_failed")
        try:
            with wave.open(str(wav_path), "rb") as audio:
                if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                    raise VoiceTranscriptionError("voice_decode_format_invalid")
                duration_ms = audio.getnframes() * 1000 // audio.getframerate()
        except (wave.Error, EOFError) as exc:
            raise VoiceTranscriptionError("voice_decode_failed") from exc
        if not 100 <= duration_ms <= policy["max_duration_seconds"] * 1000:
            raise VoiceTranscriptionError("voice_duration_invalid")
        asr = [policy["executable"], "-m", policy["model_path"], "--no-gpu", "-f", str(wav_path),
               "-l", "zh", "-t", str(policy["threads"]), "-nt", "-np"]
        try:
            result = subprocess.run(asr, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace", capture_output=True,
                                    timeout=policy["timeout_seconds"], check=False,
                                    env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceTranscriptionError("voice_transcription_failed") from exc
        text = " ".join((result.stdout or "").split())
        if result.returncode != 0:
            raise VoiceTranscriptionError("voice_transcription_failed")
        if not text or len(text) > 6000:
            raise VoiceTranscriptionError("voice_transcript_invalid")
        return {"text": text, "duration_ms": duration_ms, "language": "zh", "role": "speech_transcription"}


def transcribe_voice_file(source: Path, policy: dict, *, temp_root: str | None = None) -> dict:
    if not _TRANSCRIPTION_SLOT.acquire(blocking=False):
        raise VoiceTranscriptionError("voice_transcription_busy")
    try:
        return _transcribe_voice_file(source, policy, temp_root=temp_root)
    finally:
        _TRANSCRIPTION_SLOT.release()


__all__ = ["VoiceTranscriptionError", "transcribe_voice_file", "transcription_policy", "validate_runtime"]
