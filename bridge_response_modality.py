#!/usr/bin/env python3
"""Server-owned response-modality facts and truthful text reconciliation."""

from __future__ import annotations

import re
import sqlite3


_CLAUSE_BOUNDARY = re.compile(r"([，,；;。！？!?])")
_VOICE_DENIAL = re.compile(
    r"(?:"
    r"(?:我|当前助手|助手).{0,10}(?:没法|不能|无法|不会).{0,8}"
    r"(?:开口|说话|发语音|用语音|语音回复)"
    r"|(?:语音|声音).{0,10}(?:发不出去|不能发|无法发|没法发|做不到)"
    r"|只能.{0,8}(?:打字|文字回复|用文字)"
    r")",
)


def voice_modality_prompt_lines(context: dict | None) -> list[str]:
    """Describe server-owned modality facts without promising delivery success."""

    item = context or {}
    if not item.get("available"):
        return []
    lines = [
        "本轮回复媒介事实：",
        "- QQ 发送层具备受控语音消息能力；VoicePack、TTS、Artifact 与 Delivery Gate 在你输出文字之后执行。",
        "- 不得声称自己不能说话、发不了语音、只能打字，或把当前能力描述成永久缺失。",
        "- 你只负责写要表达的内容；不得提前声称语音已经生成、发送或送达。",
    ]
    if item.get("requested"):
        lines.append("- 用户本轮明确要求语音；发送层会尝试语音 Gate，失败时由服务器给出真实文字回退说明。")
    elif item.get("policy_may_select"):
        lines.append("- 当前策略可能按情绪选择语音；是否选择由服务器策略决定，不由自由文本承诺。")
    return lines


def attach_voice_response_prompt_context(
    target: dict,
    connect,
    message: object,
    *,
    scope: str,
    owner_authorized: bool,
) -> None:
    """Attach a prompt-safe voice fact to the existing output-media context."""

    from bridge_voice_response_policy import voice_response_prompt_context

    try:
        with connect() as conn:
            target["response_modality"] = voice_response_prompt_context(
                conn, message, scope=scope, owner_authorized=owner_authorized,
            )
    except sqlite3.Error:
        target["response_modality"] = {"available": False}


def reconcile_voice_capability_claims(
    text: object,
    *,
    prepared: bool,
) -> tuple[str, bool]:
    """Remove only disproven self-capability denials from a voice-request turn.

    The model may still express uncertainty or discuss voice functionality in
    general.  This guard targets clauses that claim the assistant itself can
    never speak or can only type.  It runs only after the server has selected a
    voice response, so ordinary text turns are untouched.
    """

    value = str(text or "").strip()
    if not value or not _VOICE_DENIAL.search(value):
        return value, False
    parts = _CLAUSE_BOUNDARY.split(value)
    kept: list[str] = []
    guarded = False
    index = 0
    while index < len(parts):
        clause = parts[index]
        punctuation = parts[index + 1] if index + 1 < len(parts) else ""
        if _VOICE_DENIAL.search(clause):
            guarded = True
        else:
            kept.extend((clause, punctuation))
        index += 2
    cleaned = "".join(kept).strip()
    cleaned = re.sub(r"^[，,；;。！？!?\s]+", "", cleaned)
    cleaned = re.sub(r"^[啊呀哎嗯哦唔欸诶]+[，,、\s]*", "", cleaned).strip()
    cleaned = re.sub(r"([，,；;]){2,}", r"\1", cleaned)
    if not cleaned:
        cleaned = "这次我用语音回复你。" if prepared else "这次语音没有生成成功。"
    return cleaned, guarded


__all__ = [
    "attach_voice_response_prompt_context",
    "reconcile_voice_capability_claims",
    "voice_modality_prompt_lines",
]
