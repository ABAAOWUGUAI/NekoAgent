"""VM-1B orchestration kept outside the AstrBot plugin entrypoint."""

from __future__ import annotations

from .voice_message_source import (
    VoiceMessageSourceError,
    build_owner_private_record_transport_probe,
)


async def handle_owner_private_voice_transport_probe(
    event,
    *,
    external_message_id: str,
    call_bridge,
    logger,
) -> tuple[bool, str]:
    """Return ``(handled, safe_reply)`` without downloading media."""

    try:
        payload = build_owner_private_record_transport_probe(
            event,
            external_message_id=external_message_id,
        )
    except VoiceMessageSourceError as exc:
        logger.warning("voice transport probe rejected reason=%s", str(exc))
        return True, "语音传输探针未通过来源校验；本次没有下载或转写音频。"
    if payload is None:
        return False, ""
    result = await call_bridge("POST", "/qq/voice/transport-probe", payload)
    probe = result.get("probe") if isinstance(result, dict) else None
    logger.info(
        "voice transport probe completed gate_status=%s",
        str((probe or {}).get("gate_status") or "failed"),
    )
    if result.get("ok"):
        return True, "已记录语音传输元数据探针；本次没有下载或转写音频。"
    return True, "语音传输探针记录失败；本次没有下载或转写音频。"


__all__ = ["handle_owner_private_voice_transport_probe"]
