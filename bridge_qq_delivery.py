#!/usr/bin/env python3
"""Create durable QQ replies without giving the request handler send ownership."""

from __future__ import annotations

from bridge_delivery_continuity import logical_response_id


_ERROR_TEXT = {
    "provider_config": "当前对话场景没有可用模型。请在 Web 控制台检查对应场景的模型绑定、连接状态和运行时应用版本。",
    "invalid_model": "当前场景绑定的模型名称不受该连接支持，请检查接口模型名并重新验证。",
    "auth": "当前模型连接的凭据无效或已过期，请在 Web 控制台更新后重新验证。",
    "quota": "当前模型连接可能额度不足或受到频率限制，请检查提供商状态后重试。",
    "rate_limit": "模型提供商当前请求过于频繁，请稍后重试。",
    "timeout": "模型在限定时间内没有返回，请稍后重试或检查连接响应速度。",
    "network": "模型连接或网络代理异常，请检查连接地址、代理和运行时状态。",
    "empty": "模型没有返回可发送的内容，请检查当前场景绑定并重试。",
}


def _text(result: dict) -> str:
    reply = str(result.get("reply") or result.get("output") or "").strip()
    if reply:
        return reply
    kind = str(result.get("error_kind") or "").strip()
    if kind in _ERROR_TEXT:
        return _ERROR_TEXT[kind]
    error = str(result.get("error") or "").strip()
    if error == "qq_project_required":
        return "当前群聊还没有绑定可执行项目；请先在 Web 控制台为该群选择项目。"
    return "本次请求没有成功完成。请稍后重试，并在 Web 控制台查看对应场景的模型与送达诊断。"


def _thread_ref(transport: dict, scope: str) -> str:
    actor_id = str(transport.get("sender_id") or transport.get("user_id") or "").strip()
    group_id = str(transport.get("group_id") or "").strip()
    return f"qq:group:{group_id}" if scope == "group" else f"qq:private:{actor_id}"


def reserve_qq_response(outbox, transport: dict, *, scope: str) -> dict:
    prepared = dict(transport)
    prepared["_response_sequence"] = outbox.reserve_response_sequence(
        "qq",
        _thread_ref(prepared, scope),
        reservation_key=str(
            prepared.get("_external_message_id") or prepared.get("trace_id") or ""
        ),
    )
    return prepared


def dispatch_qq_response(outbox, operation, transport: dict, *, scope: str, enabled: bool) -> dict:
    if not enabled:
        return operation()
    prepared = reserve_qq_response(outbox, transport, scope=scope)
    return enqueue_qq_response(outbox, operation(), prepared, scope=scope)


def enqueue_qq_response(outbox, result: dict, transport: dict, *, scope: str) -> dict:
    """Attach exactly one logical response to the existing Delivery Outbox."""

    if scope == "group":
        directed = bool(
            transport.get("is_mention")
            or transport.get("reply_to_assistant")
            or str((result.get("group_decision") or {}).get("participation_action") or "")
            in {"direct_reply", "continuation_reply", "deterministic_control_action"}
        )
        if result.get("dispatch") == "silent":
            return result
        if result.get("ok") and not result.get("should_reply"):
            return result
        if not result.get("ok") and not directed:
            return result
    session = str(transport.get("session") or "").strip()
    actor_id = str(transport.get("sender_id") or transport.get("user_id") or "").strip()
    group_id = str(transport.get("group_id") or "").strip()
    thread_ref = _thread_ref(transport, scope)
    source_message_id = str(transport.get("_external_message_id") or "").strip()
    trace_id = str(transport.get("trace_id") or "").strip()
    if not session:
        return {
            **result,
            "ok": False,
            "error": "qq_delivery_session_required",
            "delivery_queued": False,
        }

    dispatch = str(result.get("dispatch") or ("error" if not result.get("ok") else "chat"))
    response_id = logical_response_id(
        channel="qq",
        thread_ref=thread_ref,
        source_message_id=source_message_id,
        response_kind=dispatch,
        trace_id=trace_id,
    )
    decision = result.get("group_decision") if isinstance(result.get("group_decision"), dict) else {}
    engagement_decision_id = str(
        result.get("engagement_decision_id")
        or decision.get("decision_id")
        or transport.get("engagement_decision_id")
        or ""
    ).strip()
    content = _text(result)
    meme = result.get("meme") if isinstance(result.get("meme"), dict) else None
    delivery_class = (
        "operational"
        if dispatch in {"task", "task_append", "approval_required", "control", "error"}
        or not result.get("ok")
        else "social"
    )
    delivery = outbox.enqueue(
        dedupe_key=f"qq:response:{response_id}",
        channel="qq",
        destination=session,
        payload={
            "kind": "assistant_reply",
            "logical_response_id": response_id,
            "source_message_id": source_message_id,
            "thread_ref": thread_ref,
            "user_id": f"group:{group_id}" if scope == "group" else actor_id,
            "group_id": group_id if scope == "group" else "",
            "send_session": session,
            "content": content,
            "meme": meme,
            "selection_id": str((meme or {}).get("selection_id") or ""),
            "response_kind": dispatch,
            "assistant_name": str(result.get("assistant_name") or ""),
            "social_action": str((decision or {}).get("social_action") or ""),
            # Direct @ / quote turns are genuine replies but not unsolicited
            # participation. Settlement keeps the ambient budget truthful.
            "uninvited_group_action": bool(scope == "group" and not directed),
        },
        max_attempts=5,
        logical_response_id=response_id,
        source_message_id=source_message_id,
        engagement_decision_id=engagement_decision_id,
        thread_ref=thread_ref,
        delivery_class=delivery_class,
        supersede_pending_social=delivery_class == "social",
        response_sequence=int(transport.get("_response_sequence") or 0),
    )
    return {
        **result,
        "delivery_queued": True,
        "logical_response_id": response_id,
        "delivery": {
            "id": delivery["id"],
            "state": delivery["state"],
            "certainty": delivery.get("delivery_certainty") or "pending",
            "sequence": delivery.get("response_sequence") or 0,
        },
    }


def bind_qq_response_decision(outbox, result: dict, observation: dict | None) -> dict | None:
    """Project a post-dispatch participation decision onto its queued reply."""

    if not observation or not result.get("delivery_queued"):
        return None
    delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
    delivery_id = str(delivery.get("id") or "").strip()
    decision_id = str(observation.get("engagement_decision_id") or "").strip()
    if not delivery_id or not decision_id:
        return None
    return outbox.bind_engagement_decision(
        delivery_id,
        decision_id,
        source_message_id=str(observation.get("source_message_id") or "").strip(),
    )


__all__ = [
    "bind_qq_response_decision", "dispatch_qq_response", "enqueue_qq_response",
    "reserve_qq_response",
]
