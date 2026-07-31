#!/usr/bin/env python3
"""Local audio primitives for the subscription-backed QQ call gateway.

The module uses only local processes: an energy VAD, whisper.cpp for speech to
text, and Piper for text to speech.  It never sends audio to a third party and
never stores source PCM after a turn completes.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class VoiceAudioError(RuntimeError):
    """Fail-closed media pipeline error."""


@dataclass(frozen=True)
class VoiceAudioFormat:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    frame_ms: int = 20

    def __post_init__(self) -> None:
        if self.sample_rate not in {8_000, 16_000, 24_000, 48_000}:
            raise ValueError("unsupported_voice_sample_rate")
        if self.channels != 1 or self.sample_width != 2:
            raise ValueError("voice_pcm16_mono_required")
        if self.frame_ms not in {10, 20, 30}:
            raise ValueError("unsupported_voice_frame_ms")

    @property
    def frame_bytes(self) -> int:
        return self.sample_rate * self.channels * self.sample_width * self.frame_ms // 1000


@dataclass(frozen=True)
class EnergyVadConfig:
    start_rms: int = 700
    continue_rms: int = 450
    pre_roll_ms: int = 200
    min_speech_ms: int = 240
    end_silence_ms: int = 560
    max_utterance_ms: int = 20_000

    def __post_init__(self) -> None:
        if self.start_rms <= 0 or self.continue_rms <= 0:
            raise ValueError("invalid_voice_vad_threshold")
        if self.continue_rms > self.start_rms:
            raise ValueError("voice_vad_continue_threshold_too_high")
        if min(self.pre_roll_ms, self.min_speech_ms, self.end_silence_ms) < 0:
            raise ValueError("invalid_voice_vad_duration")
        if self.max_utterance_ms <= self.min_speech_ms:
            raise ValueError("invalid_voice_max_utterance")


def pcm16_rms(frame: bytes) -> int:
    if len(frame) % 2:
        raise ValueError("pcm16_frame_alignment_required")
    if not frame:
        return 0
    samples = struct.unpack(f"<{len(frame) // 2}h", frame)
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return int(math.sqrt(mean_square))


class EnergyVadSegmenter:
    """Deterministic turn segmentation with bounded pre-roll and memory."""

    def __init__(
        self,
        audio_format: VoiceAudioFormat = VoiceAudioFormat(),
        config: EnergyVadConfig = EnergyVadConfig(),
    ) -> None:
        self.audio_format = audio_format
        self.config = config
        self._pre_roll = deque(maxlen=max(1, config.pre_roll_ms // audio_format.frame_ms))
        self._frames: list[bytes] = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def reset(self) -> None:
        self._pre_roll.clear()
        self._frames.clear()
        self._speech_ms = 0
        self._silence_ms = 0
        self._active = False

    def feed(self, frame: bytes) -> bytes | None:
        if len(frame) != self.audio_format.frame_bytes:
            raise ValueError("voice_pcm_frame_size_mismatch")
        level = pcm16_rms(frame)
        started_now = False
        if not self._active:
            self._pre_roll.append(frame)
            if level < self.config.start_rms:
                return None
            self._active = True
            self._frames = list(self._pre_roll)
            self._pre_roll.clear()
            started_now = True

        if not started_now:
            self._frames.append(frame)
        if level >= self.config.continue_rms:
            self._speech_ms += self.audio_format.frame_ms
            self._silence_ms = 0
        else:
            self._silence_ms += self.audio_format.frame_ms

        duration_ms = len(self._frames) * self.audio_format.frame_ms
        complete = duration_ms >= self.config.max_utterance_ms
        complete = complete or (
            self._speech_ms >= self.config.min_speech_ms
            and self._silence_ms >= self.config.end_silence_ms
        )
        if not complete:
            return None
        utterance = b"".join(self._frames)
        self.reset()
        return utterance


def write_pcm16_wav(path: Path, pcm: bytes, audio_format: VoiceAudioFormat) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(audio_format.channels)
        output.setsampwidth(audio_format.sample_width)
        output.setframerate(audio_format.sample_rate)
        output.writeframes(pcm)


class WhisperCppTranscriber:
    def __init__(
        self,
        *,
        executable: str,
        model_path: str,
        language: str = "zh",
        threads: int = 2,
        timeout_seconds: int = 45,
        temp_dir: str | None = None,
    ) -> None:
        self.executable = executable
        self.model_path = model_path
        self.language = language
        self.threads = max(1, min(int(threads), 8))
        self.timeout_seconds = max(5, min(int(timeout_seconds), 120))
        self.temp_dir = temp_dir

    def build_command(self, wav_path: Path) -> list[str]:
        return [
            self.executable,
            "-m",
            self.model_path,
            "--no-gpu",
            "-f",
            str(wav_path),
            "-l",
            self.language,
            "-t",
            str(self.threads),
            "-nt",
            "-np",
        ]

    def transcribe(self, pcm: bytes, audio_format: VoiceAudioFormat = VoiceAudioFormat()) -> str:
        if not Path(self.model_path).is_file():
            raise VoiceAudioError("whisper_model_missing")
        executable = shutil.which(self.executable) or (self.executable if Path(self.executable).is_file() else "")
        if not executable:
            raise VoiceAudioError("whisper_executable_missing")
        with tempfile.TemporaryDirectory(prefix="qq-call-asr-", dir=self.temp_dir) as directory:
            wav_path = Path(directory) / "utterance.wav"
            write_pcm16_wav(wav_path, pcm, audio_format)
            command = self.build_command(wav_path)
            command[0] = executable
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env={**os.environ, "LC_ALL": "C.UTF-8"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VoiceAudioError(f"whisper_inference_failed:{type(exc).__name__}") from exc
        if result.returncode != 0:
            raise VoiceAudioError(f"whisper_inference_failed:{result.returncode}")
        text = (result.stdout or "").strip()
        if not text:
            raise VoiceAudioError("whisper_empty_transcript")
        return text


class PiperSynthesizer:
    def __init__(
        self,
        *,
        command_prefix: Sequence[str] = ("python3", "-m", "piper"),
        model: str = "zh_CN-huayan-medium",
        data_dir: str | None = None,
        timeout_seconds: int = 30,
        temp_dir: str | None = None,
    ) -> None:
        if not command_prefix:
            raise ValueError("piper_command_required")
        self.command_prefix = tuple(command_prefix)
        self.model = model
        self.data_dir = data_dir
        self.timeout_seconds = max(5, min(int(timeout_seconds), 120))
        self.temp_dir = temp_dir

    def build_command(self, output_path: Path, text: str) -> list[str]:
        command = [*self.command_prefix, "-m", self.model, "-f", str(output_path)]
        if self.data_dir:
            command.extend(["--data-dir", self.data_dir])
        command.extend(["--", text])
        return command

    def synthesize(self, text: str) -> bytes:
        spoken = " ".join(str(text).split())
        if not spoken:
            raise ValueError("voice_tts_text_required")
        executable = shutil.which(self.command_prefix[0])
        if not executable:
            raise VoiceAudioError("piper_runtime_missing")
        with tempfile.TemporaryDirectory(prefix="qq-call-tts-", dir=self.temp_dir) as directory:
            output_path = Path(directory) / "reply.wav"
            command = self.build_command(output_path, spoken)
            command[0] = executable
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env={**os.environ, "LC_ALL": "C.UTF-8"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VoiceAudioError(f"piper_synthesis_failed:{type(exc).__name__}") from exc
            if result.returncode != 0 or not output_path.is_file():
                raise VoiceAudioError(f"piper_synthesis_failed:{result.returncode}")
            audio = output_path.read_bytes()
        if len(audio) < 44:
            raise VoiceAudioError("piper_invalid_wav")
        return audio


def local_audio_runtime_status(
    *,
    whisper_executable: str,
    whisper_model: str,
    piper_command: str = "python3",
) -> dict[str, object]:
    checks = {
        "whisper_executable": bool(shutil.which(whisper_executable) or Path(whisper_executable).is_file()),
        "whisper_model": Path(whisper_model).is_file(),
        "piper_runtime": bool(shutil.which(piper_command)),
    }
    return {
        "ok": all(checks.values()),
        "mode": "codex_subscription_hybrid",
        "native_speech_to_speech": False,
        "external_audio_upload": False,
        "checks": checks,
    }


__all__ = [
    "EnergyVadConfig",
    "EnergyVadSegmenter",
    "PiperSynthesizer",
    "VoiceAudioError",
    "VoiceAudioFormat",
    "WhisperCppTranscriber",
    "local_audio_runtime_status",
    "pcm16_rms",
    "write_pcm16_wav",
]
