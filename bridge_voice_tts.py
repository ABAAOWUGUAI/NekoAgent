#!/usr/bin/env python3
"""Channel-neutral local Piper TTS adapter for controlled voice delivery."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence


class VoiceTtsError(RuntimeError):
    """A bounded local TTS runtime failure."""


class PiperSynthesizer:
    def __init__(
        self,
        *,
        command_prefix: Sequence[str],
        model: str,
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
            raise VoiceTtsError("piper_runtime_missing")
        with tempfile.TemporaryDirectory(prefix="voice-tts-", dir=self.temp_dir) as directory:
            output_path = Path(directory) / "reply.wav"
            command = self.build_command(output_path, spoken)
            command[0] = executable
            try:
                cache_root = Path(self.temp_dir or directory) / "transformers-cache"
                cache_root.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    command,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    cwd=self.data_dir or None,
                    env={
                        **os.environ,
                        "LC_ALL": "C.UTF-8",
                        "HF_HOME": str(cache_root),
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                    },
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VoiceTtsError(f"piper_synthesis_failed:{type(exc).__name__}") from exc
            if result.returncode != 0 or not output_path.is_file():
                raise VoiceTtsError(f"piper_synthesis_failed:{result.returncode}")
            audio = output_path.read_bytes()
        if len(audio) < 44:
            raise VoiceTtsError("piper_invalid_wav")
        return audio


__all__ = ["PiperSynthesizer", "VoiceTtsError"]
