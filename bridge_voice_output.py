#!/usr/bin/env python3
"""Owner-private explicit TTS rendering into an immutable WAV Artifact."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import wave
from pathlib import Path

from bridge_artifact_service import ArtifactService
from bridge_qq_access_runtime import super_admin_ids
from bridge_response_modality import reconcile_voice_capability_claims
from bridge_voice_tts import PiperSynthesizer, VoiceTtsError
from bridge_voice_output_schema import (
    VOICE_DELIVERY_FEATURE_FLAG,
    VOICE_OUTPUT_FEATURE_FLAG,
)
from bridge_voice_response_policy import (
    decide_and_reserve_voice_response,
    explicit_voice_request,
    release_voice_response_reservation,
)
from bridge_voice_pack_tuning import normalize_piper_synthesis, resolve_piper_synthesis


MAX_SPOKEN_CHARS = 600
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 120.0
VOICE_ARTIFACT_KIND = "file"

_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


class VoiceOutputError(RuntimeError):
    """A bounded, user-safe voice-output gate failure."""


def spoken_text(text: object, *, limit: int = MAX_SPOKEN_CHARS) -> str:
    value = str(text or "").strip()
    value = re.sub(r"```[\s\S]*?```", " 代码内容请看文字消息。 ", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"https?://\S+", "链接请看文字消息", value)
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value)
    value = re.sub(r"(?m)^\s*[-*+]\s+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        raise VoiceOutputError("voice_output_text_empty")
    limit = max(80, min(int(limit), MAX_SPOKEN_CHARS))
    if len(value) <= limit:
        return value
    candidate = value[:limit]
    boundary = max(candidate.rfind(mark) for mark in "。！？；")
    if boundary >= limit // 2:
        candidate = candidate[: boundary + 1]
    return candidate.rstrip("，、：； ") + "。详细内容请看文字消息。"


def _validate_wav(payload: bytes) -> dict:
    if len(payload) < 44 or len(payload) > MAX_AUDIO_BYTES:
        raise VoiceOutputError("voice_output_wav_size_invalid")
    try:
        with wave.open(io.BytesIO(payload), "rb") as stream:
            channels = int(stream.getnchannels())
            width = int(stream.getsampwidth())
            rate = int(stream.getframerate())
            frames = int(stream.getnframes())
    except (wave.Error, EOFError) as exc:
        raise VoiceOutputError("voice_output_wav_invalid") from exc
    if channels != 1 or width != 2 or rate < 8_000 or rate > 48_000 or frames <= 0:
        raise VoiceOutputError("voice_output_wav_format_invalid")
    duration = frames / rate
    if duration > MAX_AUDIO_SECONDS:
        raise VoiceOutputError("voice_output_wav_duration_invalid")
    return {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "duration_ms": int(duration * 1000),
        "sample_rate": rate,
    }


class VoiceOutputRuntime:
    def __init__(self, connect, artifact_service: ArtifactService) -> None:
        self.connect = connect
        self.artifact_service = artifact_service
        self.python = os.environ.get(
            "VOICE_TTS_PYTHON",
            "/opt/agent-stack/voice-runtime/piper-venv/bin/python3",
        )
        self.model_root = Path(
            os.environ.get("VOICE_TTS_MODEL_ROOT", "/var/lib/agent-voice/models"),
        ).resolve()
        self.temp_root = Path(
            os.environ.get("VOICE_TTS_TEMP_ROOT", "/var/lib/agent-voice/tmp"),
        ).resolve()
        self.timeout_seconds = max(
            10,
            min(int(os.environ.get("VOICE_TTS_TIMEOUT_SECONDS", "60")), 120),
        )
        self._asset_hash_cache: dict[tuple[str, int, int, str], bool] = {}

    def _verify_asset_hash(self, path: Path, expected: object, *, kind: str) -> None:
        expected_hash = str(expected or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise VoiceOutputError(f"voice_pack_{kind}_hash_invalid")
        try:
            stat = path.stat()
        except OSError as exc:
            raise VoiceOutputError(f"voice_pack_{kind}_missing") from exc
        cache_key = (str(path), stat.st_size, stat.st_mtime_ns, expected_hash)
        if cache_key in self._asset_hash_cache:
            return
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise VoiceOutputError(f"voice_pack_{kind}_hash_mismatch")
        self._asset_hash_cache.clear()
        self._asset_hash_cache[cache_key] = True

    @staticmethod
    def _flags(conn) -> dict[str, bool]:
        names = (VOICE_OUTPUT_FEATURE_FLAG, VOICE_DELIVERY_FEATURE_FLAG)
        rows = dict(
            conn.execute(
                "SELECT name,enabled FROM assistant_feature_flags WHERE name IN (?,?)",
                names,
            ),
        )
        return {name: bool(rows.get(name)) for name in names}

    def _active_voice(self) -> dict:
        with self.connect() as conn:
            flags = self._flags(conn)
            if not flags[VOICE_OUTPUT_FEATURE_FLAG]:
                raise VoiceOutputError("voice_output_disabled")
            if not flags[VOICE_DELIVERY_FEATURE_FLAG]:
                raise VoiceOutputError("voice_delivery_disabled")
            row = conn.execute(
                """
                SELECT a.id,a.owner_actor_id,v.id,v.name,v.status,v.config_json
                FROM assistant_instances a
                LEFT JOIN voice_packs v ON v.id=a.active_voice_pack_id
                WHERE a.status='active' LIMIT 1
                """,
            ).fetchone()
        if not row or not row[2] or str(row[4]) != "active":
            raise VoiceOutputError("voice_pack_not_bound")
        try:
            config = json.loads(str(row[5] or "{}"))
        except json.JSONDecodeError as exc:
            raise VoiceOutputError("voice_pack_config_invalid") from exc
        if not isinstance(config, dict) or config.get("engine") != "piper":
            raise VoiceOutputError("voice_pack_engine_invalid")
        model = str(config.get("model") or "")
        if not _MODEL_NAME.fullmatch(model):
            raise VoiceOutputError("voice_pack_model_invalid")
        if str(config.get("language") or "") != "zh-CN":
            raise VoiceOutputError("voice_pack_language_invalid")
        license_name = str(config.get("license") or "").strip().lower()
        if not license_name or license_name == "unknown":
            raise VoiceOutputError("voice_pack_license_invalid")
        source_url = str(config.get("source_url") or "").strip()
        if not source_url.startswith("https://huggingface.co/rhasspy/piper-voices/"):
            raise VoiceOutputError("voice_pack_source_invalid")
        model_path = (self.model_root / f"{model}.onnx").resolve()
        config_path = (self.model_root / f"{model}.onnx.json").resolve()
        try:
            model_path.relative_to(self.model_root)
            config_path.relative_to(self.model_root)
        except ValueError as exc:
            raise VoiceOutputError("voice_pack_model_path_invalid") from exc
        if not model_path.is_file() or not config_path.is_file():
            raise VoiceOutputError("voice_pack_model_missing")
        self._verify_asset_hash(model_path, config.get("model_sha256"), kind="model")
        self._verify_asset_hash(config_path, config.get("config_sha256"), kind="config")
        phonemizer_path = (self.model_root / "g2pW" / "g2pw.onnx").resolve()
        if not phonemizer_path.is_file():
            raise VoiceOutputError("voice_pack_phonemizer_missing")
        self._verify_asset_hash(
            phonemizer_path,
            config.get("phonemizer_model_sha256"),
            kind="phonemizer_model",
        )
        self._verify_asset_hash(
            self.model_root / "g2pW" / "config.py",
            config.get("phonemizer_config_sha256"),
            kind="phonemizer_config",
        )
        self._verify_asset_hash(
            self.model_root / "g2pW" / "bert-base-chinese" / "vocab.txt",
            config.get("tokenizer_vocab_sha256"),
            kind="tokenizer_vocab",
        )
        return {
            "assistant_id": str(row[0]),
            "owner_id": str(row[1]),
            "voice_pack_id": str(row[2]),
            "model": model,
            "model_path": model_path,
            "license": license_name,
            "max_chars": int(config.get("max_chars") or MAX_SPOKEN_CHARS),
            "synthesis": normalize_piper_synthesis(config.get("synthesis")),
        }

    def prepare(self, result: dict, transport: dict, *, scope: str) -> dict | None:
        actor = str(
            transport.get("_qq_actor_id")
            or transport.get("sender_id")
            or transport.get("user_id")
            or ""
        ).strip()
        with self.connect() as conn:
            response_decision = decide_and_reserve_voice_response(
                conn,
                result,
                transport,
                scope=scope,
                owner_authorized=bool(actor and actor in super_admin_ids(self.connect)),
            )
        if response_decision is None:
            return None
        try:
            voice = self._active_voice()
            delivery_text, capability_truth_guarded = reconcile_voice_capability_claims(
                result.get("reply") or result.get("output") or "",
                prepared=True,
            )
            text = spoken_text(
                delivery_text,
                limit=voice["max_chars"],
            )
            synthesis = resolve_piper_synthesis(
                voice.get("synthesis"),
                response_decision["affect"]["kind"],
            )
            synthesizer = PiperSynthesizer(
                command_prefix=(self.python, "-m", "piper"),
                model=str(voice["model_path"]),
                data_dir=str(self.model_root),
                timeout_seconds=self.timeout_seconds,
                temp_dir=str(self.temp_root),
                synthesis=synthesis,
            )
            self.temp_root.mkdir(parents=True, exist_ok=True)
            try:
                audio = synthesizer.synthesize(text)
            except (VoiceTtsError, OSError, ValueError) as exc:
                kind = str(exc).split(":", 1)[0]
                raise VoiceOutputError(kind or "voice_output_synthesis_failed") from exc
            metadata = _validate_wav(audio)
            task = result.get("task") if isinstance(result.get("task"), dict) else {}
            with tempfile.TemporaryDirectory(prefix="qq-voice-artifact-", dir=self.temp_root) as directory:
                source = Path(directory)
                (source / "reply.wav").write_bytes(audio)
                imported = self.artifact_service.import_from_directory(
                    source_root=source,
                    owner_id=voice["owner_id"],
                    origin_assistant_id=voice["assistant_id"],
                    source_goal_id=str(result.get("goal_id") or task.get("goal_id") or ""),
                    source_run_id=str(result.get("run_id") or task.get("run_id") or ""),
                    title="QQ 语音回复",
                    kind=VOICE_ARTIFACT_KIND,
                    summary="Owner 私聊回复媒介策略生成的受控语音回复。",
                    file_names=("reply.wav",),
                    retention_days=1,
                )
        except Exception as exc:
            try:
                with self.connect() as conn:
                    release_voice_response_reservation(conn, response_decision)
            except Exception as release_exc:
                raise VoiceOutputError(
                    "voice_output_failed_and_reservation_release_failed",
                ) from release_exc
            raise
        version = imported["version"]
        return {
            "kind": "tts_wav",
            "artifact_id": imported["artifact"]["id"],
            "artifact_version_id": version["id"],
            "artifact_owner_id": voice["owner_id"],
            "relative_path": "reply.wav",
            "media_type": "audio/wav",
            "size_bytes": metadata["size_bytes"],
            "sha256": metadata["sha256"],
            "duration_ms": metadata["duration_ms"],
            "voice_pack_id": voice["voice_pack_id"],
            "delivery_text": delivery_text,
            "capability_truth_guarded": capability_truth_guarded,
            "synthesis": {
                "preset": synthesis["preset"],
                "emotion_variation": synthesis["emotion_variation"],
            },
            "response_policy": {
                "mode": response_decision["policy"]["mode"],
                "version": response_decision["policy"]["version"],
                "trigger": response_decision["trigger"],
                "affect_kind": response_decision["affect"]["kind"],
                "affect_confidence": round(
                    float(response_decision["affect"]["confidence"]),
                    4,
                ),
            },
        }


__all__ = [
    "MAX_AUDIO_BYTES",
    "VOICE_ARTIFACT_KIND",
    "VoiceOutputError",
    "VoiceOutputRuntime",
    "explicit_voice_request",
    "spoken_text",
]
