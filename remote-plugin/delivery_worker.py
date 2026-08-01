"""Single-owner QQ delivery worker for AC-3 durable Outbox records."""

from __future__ import annotations

import asyncio
import urllib.parse

from astrbot.api.event import MessageChain
from .voice_media import temporary_voice_file


def _ambiguous_send_timeout(error: object) -> bool:
    text = str(error or "").lower()
    return "retcode=1200" in text and "sendmsg" in text and "timeout" in text


def _exception_text(exc: BaseException) -> str:
    error_type = type(exc).__name__ or "Exception"
    detail = str(exc).strip()
    return f"{error_type}: {detail}" if detail else error_type


def _platform_message_id(send_result: object) -> str:
    if isinstance(send_result, dict):
        for key in ("message_id", "id"):
            value = str(send_result.get(key) or "").strip()
            if value:
                return value[:180]
    for key in ("message_id", "id"):
        value = str(getattr(send_result, key, "") or "").strip()
        if value:
            return value[:180]
    return ""


def _send_parts(delivery: dict, *, format_task, compact_output) -> tuple[str, str, dict | None, dict | None]:
    payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else None
    if task is None and isinstance(delivery.get("task"), dict):
        task = delivery["task"]
    if task is None and payload.get("status") and (payload.get("id") or payload.get("task_id")):
        task = payload
    session = str(
        payload.get("send_session")
        or payload.get("session")
        or (task or {}).get("send_session")
        or delivery.get("send_session")
        or delivery.get("destination")
        or ""
    ).strip()
    if task is not None:
        text = "任务结果推送：\n" + format_task(task, include_output=True)
    else:
        text = compact_output(
            str(
                payload.get("text")
                or payload.get("message")
                or payload.get("content")
                or delivery.get("text")
                or delivery.get("message")
                or ""
            ).strip()
        )
    voice_media = payload.get("voice_media") if isinstance(payload.get("voice_media"), dict) else None
    if not session or (not text and not voice_media) or text == "(empty)":
        raise ValueError("outbox_delivery_session_or_text_missing")
    meme = payload.get("meme") if isinstance(payload.get("meme"), dict) else None
    return session, text, meme, voice_media


async def deliver_claimed_record(
    delivery: dict,
    *,
    call_bridge,
    context,
    logger,
    format_task,
    compact_output,
    message_components,
    fetch_voice_media,
) -> None:
    """Execute claim -> send-start -> QQ send -> ack/retry/ambiguous."""

    delivery_id = str(delivery.get("id") or delivery.get("delivery_id") or "").strip()
    lease = delivery.get("lease") if isinstance(delivery.get("lease"), dict) else {}
    lease_token = str(delivery.get("lease_token") or lease.get("token") or "").strip()
    if not delivery_id or not lease_token:
        raise ValueError("outbox_delivery_lease_identity_missing")
    delivery_path = "/deliveries/" + urllib.parse.quote(delivery_id, safe="")
    started = await call_bridge("POST", delivery_path + "/send-start", {"lease_token": lease_token})
    if not started.get("ok"):
        logger.warning("outbox send-start failed delivery=%s error=%s", delivery_id, started.get("error"))
        return
    try:
        session, text, meme, voice_media = _send_parts(
            delivery,
            format_task=format_task,
            compact_output=compact_output,
        )
        if voice_media:
            audio, expected_hash = await fetch_voice_media(delivery, lease_token)
            with temporary_voice_file(audio, expected_hash) as voice_path:
                sent = await context.send_message(
                    session,
                    MessageChain(message_components(text, meme, voice_path)),
                )
        else:
            sent = await context.send_message(
                session,
                MessageChain(message_components(text, meme)),
            )
        if sent is False:
            raise RuntimeError("qq_delivery_platform_unavailable")
    except Exception as exc:
        error = _exception_text(exc)
        ambiguous = _ambiguous_send_timeout(error)
        if ambiguous:
            error = "qq_send_timeout_uncertain: at-least-once retry may duplicate delivery; " + error
        try:
            attempt = max(1, int(delivery.get("attempt") or 1))
        except (TypeError, ValueError):
            attempt = 1
        endpoint = "ambiguous" if ambiguous else "retry"
        result = await call_bridge(
            "POST",
            delivery_path + "/" + endpoint,
            {
                "lease_token": lease_token,
                "error": error,
                "known_not_sent": not ambiguous,
                "delay_seconds": min(300, 5 * (2 ** min(attempt - 1, 6))),
            },
        )
        if not result.get("ok"):
            logger.warning("outbox delivery retry failed delivery=%s error=%s", delivery_id, result.get("error"))
        logger.warning("outbox QQ delivery failed delivery=%s error=%s", delivery_id, error)
        return
    platform_message_id = _platform_message_id(sent)
    for attempt_no in range(3):
        result = await call_bridge(
            "POST",
            delivery_path + "/ack",
            {"lease_token": lease_token, "platform_message_id": platform_message_id},
        )
        if result.get("ok"):
            return
        logger.warning(
            "outbox delivery ack failed delivery=%s retry=%s error=%s",
            delivery_id,
            attempt_no + 1,
            result.get("error"),
        )
        await asyncio.sleep(0.2 * (attempt_no + 1))


__all__ = ["deliver_claimed_record"]
