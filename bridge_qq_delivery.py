#!/usr/bin/env python3
"""Create durable QQ replies without giving the request handler send ownership."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import math
import logging
from datetime import datetime, timezone
from typing import Callable, Mapping

from bridge_delivery_continuity import logical_response_id
from bridge_conversation_participation import build_media_delivery_trace, media_trace_categories
from bridge_group_topic_delivery import normalize_topic_delivery_context
from bridge_response_modality import reconcile_voice_capability_claims


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

PREPARED_QQ_DELIVERY_VERSION = 1
MAX_PREPARED_QQ_DELIVERY_BYTES = 65536


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(result: dict) -> str:
    meme = result.get("meme")
    if (
        result.get("delivery_form") == "sticker_only"
        and isinstance(meme, dict)
        and str(meme.get("selection_id") or "").strip()
        and str(meme.get("public_url") or "").startswith(("https://", "http://", "/memes/assets/"))
        and str(result.get("reply") or "").strip()
    ):
        return ""
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


def reserve_qq_response(
    outbox,
    transport: dict,
    *,
    scope: str,
    reservation_suffix: str = "",
) -> dict:
    prepared = dict(transport)
    if scope == "group":
        # Capture before operation/model execution, not when its reply is
        # eventually enqueued. Worker-provided earlier start times may only
        # tighten this boundary; a future transport value cannot bypass it.
        started = datetime.now(timezone.utc).timestamp()
        prior = prepared.get("_group_generation_started_at")
        if type(prior) in (int, float) and math.isfinite(prior) and prior >= 0:
            started = min(started, prior)
        prepared["_group_generation_started_at"] = started
    reservation_key = str(
        prepared.get("_external_message_id") or prepared.get("trace_id") or ""
    )
    if reservation_suffix:
        reservation_key = f"{reservation_key}:{reservation_suffix}"
        prepared["_response_identity_suffix"] = str(reservation_suffix)
    prepared["_response_sequence"] = outbox.reserve_response_sequence(
        "qq",
        _thread_ref(prepared, scope),
        reservation_key=reservation_key,
    )
    return prepared


def dispatch_qq_response(
    outbox,
    operation,
    transport: dict,
    *,
    scope: str,
    enabled: bool,
    voice_output=None,
    response_identity: Mapping[str, object] | None = None,
    prepare_callback: Callable[[dict, str], Mapping[str, object]] | None = None,
    delivery_observer: Callable[[dict], None] | None = None,
) -> dict:
    if not enabled:
        return operation()
    prepared = reserve_qq_response(outbox, transport, scope=scope)
    result = operation()
    resolved_identity = response_identity
    if (
        resolved_identity is None
        and scope == "group"
        and isinstance(transport.get("_group_response_identity"), Mapping)
    ):
        resolved_identity = transport["_group_response_identity"]
    def observe(value: dict) -> None:
        if not callable(delivery_observer):
            return
        try:
            delivery_observer(value)
        except Exception as exc:
            # The unique Outbox already owns any enqueued response. An observer
            # failure must never repeat generation or delivery.
            value['quality_delivery_projection_error'] = type(exc).__name__
            logging.getLogger(__name__).error('quality_delivery_projection_failed kind=%s queued=%s',
                                              type(exc).__name__, bool(value.get('delivery_queued')))
    try:
        queued = enqueue_qq_response(outbox, result, prepared, scope=scope,
            voice_output=voice_output, response_identity=resolved_identity,
            prepare_callback=prepare_callback)
    except Exception as exc:
        observe({**result, 'delivery_queued': False, '_delivery_enqueue_error': type(exc).__name__})
        raise
    observe(queued)
    return queued


def build_qq_response_delivery(
    result: dict,
    transport: dict,
    *,
    scope: str,
    voice_output=None,
    response_identity: Mapping[str, object] | None = None,
) -> dict:
    """Build one bounded Outbox intent without persisting route authority."""

    directed = False
    if scope == "group":
        directed = bool(
            transport.get("is_mention")
            or transport.get("reply_to_assistant")
            or str((result.get("group_decision") or {}).get("participation_action") or "")
            in {"direct_reply", "continuation_reply", "deterministic_control_action"}
        )
        if result.get("dispatch") == "silent":
            return {"enqueue": False, "result": result}
        if result.get("ok") and not result.get("should_reply"):
            return {"enqueue": False, "result": result}
        if not result.get("ok") and not directed:
            return {"enqueue": False, "result": result}
    session = str(transport.get("session") or "").strip()
    actor_id = str(transport.get("sender_id") or transport.get("user_id") or "").strip()
    group_id = str(transport.get("group_id") or "").strip()
    thread_ref = _thread_ref(transport, scope)
    source_message_id = str(transport.get("_external_message_id") or "").strip()
    trace_id = str(transport.get("trace_id") or "").strip()
    if not session:
        return {"enqueue": False, "result": {
            **result, "ok": False, "error": "qq_delivery_session_required",
            "delivery_queued": False,
        }}

    dispatch = str(result.get("dispatch") or ("error" if not result.get("ok") else "chat"))
    # Structural group-chat boundary (not a string blacklist): a social
    # ``chat`` Delivery requires a genuine model reply.  If the turn failed or
    # produced no reply text, ``_text`` would otherwise fill an internal
    # diagnostic (provider/connection/proxy/console wording) into a group as a
    # normal social reply — the C24941 / C16636 bypass.  Block that here so the
    # internal detail never reaches the group, regardless of engagement id.
    if scope == "group" and dispatch == "chat":
        genuine_reply = str(result.get("reply") or result.get("output") or "").strip()
        failed_turn = not result.get("ok") or (not genuine_reply and bool(result.get("error_kind") or result.get("error")))
        if failed_turn:
            return {"enqueue": False, "result": {
                **result,
                "ok": False,
                "dispatch": "blocked",
                "delivery_queued": False,
                "group_error_blocked": True,
                "group_error_reason": "group_chat_without_genuine_reply",
            }}
    identity_suffix = str(transport.get("_response_identity_suffix") or "").strip()
    identity_kind = f"{dispatch}:{identity_suffix}" if identity_suffix else dispatch
    identity = dict(response_identity or {})
    if identity:
        if scope == "private":
            required = {
                "cycle_id", "source_set_hash", "lease_token",
                "logical_response_id", "outbox_dedupe_key",
            }
            if any(not str(identity.get(key) or "").strip() for key in required):
                raise ValueError("qq_response_cycle_identity_invalid")
        elif scope == "group":
            required = {
                "commitment_id", "event_id", "assistant_id", "group_id",
                "source_message_id", "source_set_hash", "situation_revision",
                "owner_kind", "logical_response_id", "outbox_dedupe_key",
            }
            if any(not str(identity.get(key) or "").strip() for key in required):
                raise ValueError("qq_group_response_identity_invalid")
            identity_group = str(identity.get("group_id") or "").strip()
            identity_source = str(identity.get("source_message_id") or "").strip()
            identity_event = str(identity.get("event_id") or "").strip()
            identity_owner = str(identity.get("owner_kind") or "").strip()
            identity_source_hash = str(identity.get("source_set_hash") or "").strip()
            identity_revision = str(identity.get("situation_revision") or "").strip()
            expected_commitment_id = (
                "grc_" + hashlib.sha256(identity_event.encode("utf-8")).hexdigest()[:32]
            )
            expected_response_id = logical_response_id(
                channel="qq",
                thread_ref=f"qq:group:{identity_group}",
                source_message_id=identity_source,
                response_kind="group_response_commitment",
            )
            if (
                identity_group != group_id
                or identity_source != source_message_id
                or identity_owner not in {"direct", "ambient"}
                or str(identity.get("commitment_id") or "") != expected_commitment_id
                or str(identity.get("logical_response_id") or "") != expected_response_id
                or len(identity_source_hash) != 64
                or len(identity_revision) != 64
                or any(character not in "0123456789abcdef" for character in identity_source_hash.lower())
                or any(character not in "0123456789abcdef" for character in identity_revision.lower())
            ):
                raise ValueError("qq_group_response_identity_invalid")
        else:
            raise ValueError("qq_response_identity_scope_invalid")
        response_id = str(identity["logical_response_id"]).strip()[:120]
        dedupe_key = str(identity["outbox_dedupe_key"]).strip()[:300]
        if dedupe_key != f"qq:response:{response_id}":
            raise ValueError(
                "qq_response_cycle_identity_invalid"
                if scope == "private" else "qq_group_response_identity_invalid"
            )
    else:
        response_id = logical_response_id(
            channel="qq",
            thread_ref=thread_ref,
            source_message_id=source_message_id,
            response_kind=identity_kind,
            trace_id=trace_id,
        )
        dedupe_key = f"qq:response:{response_id}"
    decision = result.get("group_decision") if isinstance(result.get("group_decision"), dict) else {}
    topic_delivery = (
        normalize_topic_delivery_context(transport.get("_topic_delivery"))
        if scope == "group" and not directed
        else None
    )
    ambient_participation = bool(scope == "group" and not directed)
    ambient_freshness_ref = (
        normalize_topic_delivery_context(transport.get("_ambient_freshness_ref"))
        if ambient_participation
        else None
    )
    try:
        ambient_policy_version = int(transport.get("_ambient_policy_version") or 0)
    except (TypeError, ValueError):
        ambient_policy_version = 0
    engagement_decision_id = str(
        result.get("engagement_decision_id")
        or decision.get("decision_id")
        or transport.get("engagement_decision_id")
        or ""
    ).strip()
    content = _text(result)
    if ambient_participation and (
        not ambient_freshness_ref
        or ambient_policy_version < 1
        or not str(result.get("_quality_receipt_id") or "").strip()
    ):
        return {"enqueue": False, "result": {
            **result,
            "ok": False,
            "dispatch": "blocked",
            "delivery_queued": False,
            "group_ambient_metadata_blocked": True,
            "group_ambient_metadata_reason": "ambient_delivery_metadata_invalid",
            "error": "ambient_delivery_metadata_invalid",
        }}
    voice_media = None
    voice_error = ""
    if callable(voice_output):
        try:
            voice_media = voice_output(result, transport, scope=scope)
            if voice_media:
                content = str(voice_media.pop("delivery_text", "") or content).strip()
        except Exception as exc:
            voice_error = str(exc).split(":", 1)[0][:120] or "voice_output_failed"
            content, _ = reconcile_voice_capability_claims(content, prepared=False)
            content = content.rstrip() + "\n\n（语音生成未完成，本次先保留文字回复。）"
    meme = result.get("meme") if isinstance(result.get("meme"), dict) else None
    automation_job = result.get("automation_job") if isinstance(result.get("automation_job"), dict) else {}
    automation_job_id = str(result.get("automation_job_id") or automation_job.get("id") or "").strip()
    delivery_class = (
        "operational"
        if dispatch in {
            "task", "task_append", "approval_required", "control", "error",
            "receipt_presence", "execution_presence_ack", "execution_presence_progress",
        }
        or dispatch.startswith("automation_")
        or not result.get("ok")
        else "directed_social"
        if scope == "group" and directed
        else "topic_social"
        if topic_delivery and str(decision.get("social_action") or "") in {
            "echo_reaction", "meme_reaction", "ack_add", "follow_up", "reply", "bridge_topic", "repair",
        }
        else "social"
    )
    media_categories = media_trace_categories({
        **transport,
        **result,
        **decision,
        "media_observation_decision": (
            result.get("media_observation_decision")
            or decision.get("media_observation_decision")
            or transport.get("media_observation_decision")
        ),
    })
    payload = {
        "kind": "assistant_voice_reply" if voice_media else "assistant_reply",
        "logical_response_id": response_id,
        "source_message_id": source_message_id,
        "thread_ref": thread_ref,
        "content": content,
        "meme": meme,
        "delivery_form": "sticker_only" if not content and meme else "text_sticker" if meme else "text",
        "voice_media": voice_media,
        "selection_id": str((meme or {}).get("selection_id") or ""),
        "response_kind": dispatch,
        "task_id": str(
            result.get("task_id")
            or (
                result.get("task", {}).get("id")
                if isinstance(result.get("task"), dict)
                else ""
            )
            or ""
        ),
        "assistant_name": str(result.get("assistant_name") or ""),
        "automation_job_id": automation_job_id,
        "automation_action_plan_id": str(result.get("automation_action_plan_id") or ""),
        "social_action": str((decision or {}).get("social_action") or ""),
        "topic_delivery": topic_delivery or {},
        "media_trace": media_categories,
        "quality_receipt_id": str(result.get("_quality_receipt_id") or ""),
        "ambient_participation": ambient_participation,
        "freshness_ref": ambient_freshness_ref or {},
        "policy_version": ambient_policy_version if ambient_participation else 0,
        "finalized_at": datetime.now(timezone.utc).isoformat(timespec="seconds") if ambient_participation else "",
        "uninvited_group_action": bool(scope == "group" and not directed),
    }
    if scope == "group" and "_group_generation_started_at" in transport:
        payload["group_generation_started_at"] = transport["_group_generation_started_at"]
    prepared = {
        "version": PREPARED_QQ_DELIVERY_VERSION,
        "enqueue": True,
        "scope": scope,
        "cycle_id": str(identity.get("cycle_id") or ""),
        "source_set_hash": str(identity.get("source_set_hash") or ""),
        "logical_response_id": response_id,
        "outbox_dedupe_key": dedupe_key,
        "channel": "qq",
        "thread_ref": thread_ref,
        "source_message_id": source_message_id,
        "engagement_decision_id": engagement_decision_id,
        "delivery_class": delivery_class,
        "max_attempts": 5,
        "supersede_pending_social": False,
        "response_sequence": int(transport.get("_response_sequence") or 0),
        "payload": payload,
        "media_categories": media_categories,
        "voice_error": voice_error,
    }
    if len(_canonical(prepared).encode("utf-8")) > MAX_PREPARED_QQ_DELIVERY_BYTES:
        raise ValueError("qq_prepared_delivery_too_large")
    return prepared


def qq_route_binding_sha256(prepared: Mapping[str, object], destination: str) -> str:
    """Bind one prepared response to its immutable Assistant thread route."""

    return _sha256({
        "assistant_id": str(prepared.get("assistant_id") or ""),
        "thread_id": str(prepared.get("thread_id") or ""),
        "channel_type": str(prepared.get("channel_type") or ""),
        "external_thread_ref": str(prepared.get("external_thread_ref") or ""),
        "destination": str(destination or "").strip(),
    })


def _prepared_payload(prepared: Mapping[str, object], destination: str) -> dict:
    payload = prepared.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("qq_prepared_delivery_invalid")
    scope = str(prepared.get("scope") or "")
    thread_ref = str(prepared.get("thread_ref") or "")
    if scope == "private" and thread_ref.startswith("qq:private:"):
        actor_id = thread_ref.removeprefix("qq:private:")
        group_id = ""
        user_id = actor_id
    elif scope == "group" and thread_ref.startswith("qq:group:"):
        group_id = thread_ref.removeprefix("qq:group:")
        user_id = f"group:{group_id}"
    else:
        raise ValueError("qq_prepared_delivery_route_invalid")
    return {
        **dict(payload),
        "user_id": user_id,
        "group_id": group_id,
        "send_session": str(destination or "").strip(),
    }


def prepared_qq_delivery_matches(
    prepared: Mapping[str, object], delivery: Mapping[str, object], destination: str,
) -> bool:
    expected_payload = _prepared_payload(prepared, destination)
    return all((
        str(delivery.get("dedupe_key") or "") == str(prepared.get("outbox_dedupe_key") or ""),
        str(delivery.get("logical_response_id") or "") == str(prepared.get("logical_response_id") or ""),
        str(delivery.get("channel") or "") == "qq",
        str(delivery.get("destination") or "") == str(destination or "").strip(),
        str(delivery.get("thread_ref") or "") == str(prepared.get("thread_ref") or ""),
        str(delivery.get("source_message_id") or "") == str(prepared.get("source_message_id") or ""),
        _sha256(delivery.get("payload")) == _sha256(expected_payload),
    ))


def enqueue_prepared_qq_response(outbox, prepared: Mapping[str, object], *, destination: str) -> dict:
    """Idempotently enqueue one exact prepared response or reject identity drift."""

    item = dict(prepared)
    if item.get("version") != PREPARED_QQ_DELIVERY_VERSION or item.get("enqueue") is not True:
        raise ValueError("qq_prepared_delivery_invalid")
    destination = str(destination or "").strip()
    if not destination:
        raise ValueError("qq_prepared_delivery_route_invalid")
    route_hash = str(item.get("route_binding_sha256") or "")
    if route_hash and route_hash != qq_route_binding_sha256(item, destination):
        raise ValueError("qq_prepared_delivery_route_drift")
    logical_id = str(item.get("logical_response_id") or "").strip()
    dedupe_key = str(item.get("outbox_dedupe_key") or "").strip()
    if not logical_id or dedupe_key != f"qq:response:{logical_id}":
        raise ValueError("qq_prepared_delivery_identity_invalid")
    supports_lookup = all(callable(getattr(outbox, name, None)) for name in (
        "get_delivery_by_logical_response_id", "get_delivery_by_dedupe_key",
    ))
    by_logical = (
        outbox.get_delivery_by_logical_response_id(logical_id)
        if supports_lookup
        else None
    )
    by_dedupe = (
        outbox.get_delivery_by_dedupe_key(dedupe_key)
        if supports_lookup
        else None
    )
    existing = by_logical or by_dedupe
    if by_logical and by_dedupe and str(by_logical.get("id")) != str(by_dedupe.get("id")):
        raise ValueError("qq_prepared_delivery_conflict")
    if existing is None:
        existing = outbox.enqueue(
            dedupe_key=dedupe_key,
            channel="qq",
            destination=destination,
            payload=_prepared_payload(item, destination),
            max_attempts=int(item.get("max_attempts") or 5),
            logical_response_id=logical_id,
            source_message_id=str(item.get("source_message_id") or ""),
            engagement_decision_id=str(item.get("engagement_decision_id") or ""),
            thread_ref=str(item.get("thread_ref") or ""),
            delivery_class=str(item.get("delivery_class") or "operational"),
            supersede_pending_social=bool(item.get("supersede_pending_social")),
            response_sequence=int(item.get("response_sequence") or 0),
        )
    if supports_lookup and not prepared_qq_delivery_matches(item, existing, destination):
        raise ValueError("qq_prepared_delivery_conflict")
    return existing


def enqueue_qq_response(
    outbox,
    result: dict,
    transport: dict,
    *,
    scope: str,
    voice_output=None,
    response_identity: Mapping[str, object] | None = None,
    prepare_callback: Callable[[dict, str], Mapping[str, object]] | None = None,
) -> dict:
    """Attach exactly one logical response to the existing Delivery Outbox."""

    prepared = build_qq_response_delivery(
        result, transport, scope=scope, voice_output=voice_output,
        response_identity=response_identity,
    )
    if prepared.get("enqueue") is not True:
        return dict(prepared.get("result") or result)
    destination = str(transport.get("session") or "").strip()
    if prepare_callback is not None:
        prepared = dict(prepare_callback(dict(prepared), destination))
    delivery = enqueue_prepared_qq_response(outbox, prepared, destination=destination)
    response_id = str(prepared["logical_response_id"])
    engagement_decision_id = str(prepared.get("engagement_decision_id") or "")
    media_categories = dict(prepared.get("media_categories") or {})
    prepared_payload = prepared.get("payload") if isinstance(prepared.get("payload"), Mapping) else {}
    voice_media = (
        prepared_payload.get("voice_media")
        if isinstance(prepared_payload.get("voice_media"), Mapping)
        else None
    )
    voice_error = str(prepared.get("voice_error") or "")
    delivery_trace = build_media_delivery_trace(
        engagement_decision_id=engagement_decision_id,
        delivery_id=str(delivery["id"]),
        **media_categories,
        delivery_state=str(delivery.get("state") or "pending"),
        ack_state="pending",
    )
    response = {
        **result,
        "delivery_queued": True,
        "logical_response_id": response_id,
        **media_categories,
        "media_delivery_trace": delivery_trace,
        "delivery": {
            "id": delivery["id"],
            "delivery_id": delivery["id"],
            "state": delivery["state"],
            "certainty": delivery.get("delivery_certainty") or "pending",
            "ack_state": "pending",
            "engagement_decision_id": engagement_decision_id,
            "sequence": delivery.get("response_sequence") or 0,
        },
    }
    if voice_media:
        response["voice_output"] = {
            "requested": True,
            "prepared": True,
            "artifact_id": voice_media["artifact_id"],
            "artifact_version_id": voice_media["artifact_version_id"],
            "sha256": voice_media["sha256"],
            "duration_ms": voice_media["duration_ms"],
        }
    elif voice_error:
        response["voice_output"] = {
            "requested": True,
            "prepared": False,
            "error_kind": voice_error,
        }
    return response


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


def load_qq_delivery_sessions(connect) -> dict[str, str]:
    """Read only the delivery routing projection, never conversation content."""

    try:
        with connect() as conn:
            rows = conn.execute("SELECT user_id, session FROM qq_sessions").fetchall()
        return {str(row[0]): str(row[1] or "") for row in rows}
    except sqlite3.Error:
        return {}


__all__ = [
    "bind_qq_response_decision", "build_qq_response_delivery",
    "dispatch_qq_response", "enqueue_prepared_qq_response", "enqueue_qq_response",
    "load_qq_delivery_sessions",
    "prepared_qq_delivery_matches", "qq_route_binding_sha256", "reserve_qq_response",
]
