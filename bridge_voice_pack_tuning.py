#!/usr/bin/env python3
"""Validated, engine-specific VoicePack synthesis tuning."""

from __future__ import annotations

from collections.abc import Mapping


PIPER_TUNING_PRESETS = {
    "balanced_v1": {
        "length_scale": 1.0,
        "noise_scale": 0.667,
        "noise_w_scale": 0.8,
        "sentence_silence": 0.15,
        "volume": 1.0,
    },
    "warm_natural_v1": {
        "length_scale": 1.04,
        "noise_scale": 0.72,
        "noise_w_scale": 0.85,
        "sentence_silence": 0.16,
        "volume": 1.0,
    },
    "lively_v1": {
        "length_scale": 0.96,
        "noise_scale": 0.76,
        "noise_w_scale": 0.9,
        "sentence_silence": 0.12,
        "volume": 1.0,
    },
}
DEFAULT_PIPER_PRESET = "warm_natural_v1"
_BOUNDS = {
    "length_scale": (0.75, 1.5),
    "noise_scale": (0.0, 1.5),
    "noise_w_scale": (0.0, 1.5),
    "sentence_silence": (0.0, 1.0),
    "volume": (0.2, 2.0),
}


class VoicePackTuningError(ValueError):
    """A bounded VoicePack tuning validation failure."""


def piper_tuning_presets() -> list[dict]:
    labels = {
        "balanced_v1": "均衡",
        "warm_natural_v1": "温暖自然",
        "lively_v1": "轻快活泼",
    }
    return [
        {"id": preset_id, "label": labels[preset_id], **values}
        for preset_id, values in PIPER_TUNING_PRESETS.items()
    ]


def normalize_piper_synthesis(value: object) -> dict:
    source = value if isinstance(value, Mapping) else {}
    preset = str(source.get("preset") or DEFAULT_PIPER_PRESET).strip()
    if preset not in PIPER_TUNING_PRESETS and preset != "custom":
        raise VoicePackTuningError("voice_pack_tuning_preset_invalid")
    base = dict(PIPER_TUNING_PRESETS.get(preset, PIPER_TUNING_PRESETS[DEFAULT_PIPER_PRESET]))
    for key, (minimum, maximum) in _BOUNDS.items():
        if key not in source:
            continue
        try:
            number = float(source[key])
        except (TypeError, ValueError) as exc:
            raise VoicePackTuningError(f"voice_pack_tuning_{key}_invalid") from exc
        if number < minimum or number > maximum:
            raise VoicePackTuningError(f"voice_pack_tuning_{key}_invalid")
        base[key] = round(number, 4)
    return {
        "preset": preset,
        **base,
        "emotion_variation": bool(source.get("emotion_variation", True)),
    }


def resolve_piper_synthesis(value: object, affect_kind: object = "neutral") -> dict:
    config = normalize_piper_synthesis(value)
    if not config["emotion_variation"]:
        return config
    affect = str(affect_kind or "neutral").strip().lower()
    adjustments = {
        "happy": (0.96, 0.04, 0.04, 0.8),
        "playful": (0.94, 0.05, 0.05, 0.75),
        "comfort": (1.06, -0.03, -0.02, 1.15),
        "sad": (1.08, -0.04, -0.03, 1.2),
        "tired": (1.1, -0.04, -0.03, 1.2),
        "annoyed": (0.98, 0.02, 0.02, 0.9),
    }.get(affect)
    if not adjustments:
        return config
    length, noise, width, silence = adjustments
    config["length_scale"] = round(config["length_scale"] * length, 4)
    config["noise_scale"] = round(max(0.0, config["noise_scale"] + noise), 4)
    config["noise_w_scale"] = round(max(0.0, config["noise_w_scale"] + width), 4)
    config["sentence_silence"] = round(config["sentence_silence"] * silence, 4)
    return config


__all__ = [
    "DEFAULT_PIPER_PRESET",
    "PIPER_TUNING_PRESETS",
    "VoicePackTuningError",
    "normalize_piper_synthesis",
    "piper_tuning_presets",
    "resolve_piper_synthesis",
]
