#!/usr/bin/env python3
"""VM-1C Owner-private controlled fetch orchestration."""

from .voice_message_source import VoiceMessageSourceError, extract_owner_private_record_source


async def handle_owner_private_voice_fetch(event, *, external_message_id, call_bridge, logger):
    try:
        source = extract_owner_private_record_source(event, external_message_id=external_message_id)
    except VoiceMessageSourceError as exc:
        logger.warning("voice fetch source rejected reason=%s", str(exc))
        return True, "语音来源未通过安全校验；本次没有下载或转写音频。"
    if source is None:
        return False, ""
    result = await call_bridge("POST", "/qq/voice/fetch", source)
    receipt = result.get("receipt") if isinstance(result, dict) else None
    if result.get("ok") and (receipt or {}).get("fetch_status") == "fetched":
        logger.info("voice controlled fetch completed media_type=%s", (receipt or {}).get("detected_media_type"))
        return True, "语音媒体已通过受控获取验证；临时文件已删除，本次尚未转写。"
    logger.warning("voice controlled fetch failed status=%s", result.get("status") if isinstance(result, dict) else 0)
    return True, "语音媒体获取未通过安全验证；本次没有转写音频。"


async def handle_owner_private_voice_input(
    event,
    *,
    external_message_id,
    session,
    call_bridge,
    logger,
):
    """Forward one Owner-private Record into Bridge-owned ASR and Delivery."""

    try:
        source = extract_owner_private_record_source(
            event,
            external_message_id=external_message_id,
        )
    except VoiceMessageSourceError as exc:
        logger.warning("voice input source rejected reason=%s", str(exc))
        return True, "语音来源未通过安全校验；本次没有下载、转写或发送内容。"
    if source is None:
        return False, ""
    delivery_session = str(session or "").strip()
    if not delivery_session:
        logger.warning("voice input rejected reason=delivery_session_missing")
        return True, "这条语音缺少可确认的回复会话，本次没有转写或发送内容。"
    source["session"] = delivery_session
    result = await call_bridge("POST", "/qq/voice/input", source)
    if result.get("ok") and (
        result.get("delivery_queued") or result.get("processing")
    ):
        logger.info(
            "voice input accepted delivery_queued=%s deduplicated=%s",
            bool(result.get("delivery_queued")),
            bool(result.get("deduplicated")),
        )
        return True, ""
    logger.warning(
        "voice input failed status=%s error=%s",
        result.get("status") if isinstance(result, dict) else 0,
        str(result.get("error") or "voice_input_failed")[:80]
        if isinstance(result, dict)
        else "voice_input_failed",
    )
    return True, "这条语音没有处理完成，请稍后重新发送一次。"


async def handle_owner_private_voice(
    event,
    *,
    input_enabled,
    external_message_id,
    session,
    call_bridge,
    logger,
):
    handler = (
        handle_owner_private_voice_input
        if input_enabled
        else handle_owner_private_voice_fetch
    )
    arguments = {
        "external_message_id": external_message_id,
        "call_bridge": call_bridge,
        "logger": logger,
    }
    if input_enabled:
        arguments["session"] = session
    return await handler(event, **arguments)


__all__ = [
    "handle_owner_private_voice_fetch",
    "handle_owner_private_voice_input",
    "handle_owner_private_voice",
]
