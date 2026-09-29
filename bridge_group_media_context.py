"""Bounded group media preparation and visual-context projection."""

from __future__ import annotations

from collections import OrderedDict
import re
from threading import RLock

import bridge_visual_context as visual
from bridge_group_context_frame import group_model_history, refresh_group_situation_revision


MAX_PENDING_GROUP_VISUAL_CONTEXTS = 96
_PENDING_GROUP_VISUAL_CONTEXTS: OrderedDict[tuple[str, str], object] = OrderedDict()
_PENDING_GROUP_VISUAL_LOCK = RLock()


def _visual_key(group_id: object, event_id: object) -> tuple[str, str]:
    return (
        str(group_id or "").strip()[:180],
        str(event_id or "").strip()[:320],
    )


def _clear_detached_replay_media(payload: dict) -> None:
    media = payload.pop("visual_media", None)
    if not isinstance(media, list):
        return
    for item in media:
        if isinstance(item, dict):
            item.clear()
    media.clear()


def clear_pending_group_visual_contexts() -> None:
    """Clear no-raw handles; visual workers remain responsible for raw cleanup."""

    with _PENDING_GROUP_VISUAL_LOCK:
        _PENDING_GROUP_VISUAL_CONTEXTS.clear()


def pending_group_visual_context_count() -> int:
    with _PENDING_GROUP_VISUAL_LOCK:
        return len(_PENDING_GROUP_VISUAL_CONTEXTS)


def _remember_visual_handle(key: tuple[str, str], handle: object) -> None:
    if not all(key):
        return
    with _PENDING_GROUP_VISUAL_LOCK:
        _PENDING_GROUP_VISUAL_CONTEXTS[key] = handle
        _PENDING_GROUP_VISUAL_CONTEXTS.move_to_end(key)
        while len(_PENDING_GROUP_VISUAL_CONTEXTS) > MAX_PENDING_GROUP_VISUAL_CONTEXTS:
            _PENDING_GROUP_VISUAL_CONTEXTS.popitem(last=False)


def _take_visual_handle(key: tuple[str, str]) -> object | None:
    with _PENDING_GROUP_VISUAL_LOCK:
        return _PENDING_GROUP_VISUAL_CONTEXTS.pop(key, None)


def begin_group_visual_context(
    payload: dict,
    *,
    group_id: str,
    event_id: str,
    message: str,
    fallback_settings: dict,
    allow_model: bool,
    retain_for_queue: bool = False,
) -> object:
    """Start the existing bounded visual worker and optionally hand it to the queue."""

    key = _visual_key(group_id, event_id)
    if retain_for_queue:
        with _PENDING_GROUP_VISUAL_LOCK:
            existing = _PENDING_GROUP_VISUAL_CONTEXTS.get(key)
        if existing is not None:
            _clear_detached_replay_media(payload)
            return existing
    handle = visual.begin_qq_visual_turn(
        payload,
        "qq_group",
        group_id,
        event_id,
        message,
        fallback_settings,
        allow_model=bool(allow_model),
    )
    if retain_for_queue:
        _remember_visual_handle(key, handle)
    return handle


def observe_group_visual_without_reply(
    payload: dict,
    *,
    group_id: str,
    event_id: str,
    message: str,
    fallback_settings: dict,
    allow_model: bool,
) -> None:
    """Consume raw media now, then finish an admitted worker without a reply."""

    handle = begin_group_visual_context(
        payload, group_id=group_id, event_id=event_id, message=message,
        fallback_settings=fallback_settings, allow_model=allow_model,
    )
    if not allow_model:
        return

    def completed(_future: object) -> None:
        # The callback retains only the no-raw handle and identifiers.  The
        # worker writes its observation into the existing ten-minute cache.
        projected: dict = {}
        try:
            finish_group_visual_context(
                projected, group_id=group_id, event_id=event_id,
                conversation_frame={}, wait_timeout_seconds=1, handle=handle,
            )
            status = str(projected.get("visual_context_status") or "unavailable")
            reason = str(projected.get("visual_context_reason") or "")[:80]
        except Exception:
            status, reason = "unavailable", "visual_completion_failed"
        print(
            f"group_visual_observation stage=completed group_id={group_id} "
            f"status={status} reason={reason}", flush=True,
        )

    future = getattr(handle, "future", None)
    if future is not None and callable(getattr(future, "add_done_callback", None)):
        future.add_done_callback(completed)
    else:
        completed(None)


def prior_group_visual_context(
    *,
    group_id: str,
    message: str,
    reply_to_external_message_id: str,
    conversation_frame: dict,
) -> list[str]:
    """Recall only an exact quoted image or one unambiguous recent image."""

    scope = visual.visual_scope(channel="qq_group", thread_id=group_id)
    recent = visual.recent_visual_evidence(scope)
    quoted = str(reply_to_external_message_id or "").strip()
    if quoted:
        eligible = [item for item in recent if str(item.get("event_id") or "") == quoted
                    or str(item.get("event_id") or "").endswith(":" + quoted)]
    elif re.search(
        r"这[张幅个]图|那[张幅个]图|刚才.{0,4}图|图(?:片|上|里|中)|照片|表情包|image|picture",
        str(message or ""), re.I,
    ):
        eligible = recent if len(recent) == 1 else []
    else:
        eligible = []
    if len(eligible) != 1:
        return []
    summary = str(eligible[0].get("text") or "").strip()[:600]
    if not summary:
        return []
    envelope = conversation_frame.get("grounding_envelope")
    if isinstance(envelope, dict) and isinstance(envelope.get("media"), dict):
        envelope["media"]["observation"] = "observed"
    _project_group_media_situation(
        {}, conversation_frame,
        {"status": "ready", "media_kind": "image", "observation": {}},
        [summary],
    )
    return [f"同群近期所指图片的临时理解：{summary}"]


def _project_group_media_situation(
    payload: dict,
    conversation_frame: dict,
    result: dict,
    objective_facts: list[str],
) -> None:
    status = str(result.get("status") or payload.get("visual_context_status") or "none")
    kind = str(
        result.get("media_kind")
        or payload.get("visual_media_kind")
        or (conversation_frame.get("grounding_envelope") or {}).get("media", {}).get("kind")
        or "unknown"
    )
    observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
    uncertainties = [
        str(item).strip()[:500]
        for item in list(observation.get("uncertain_elements") or [])[:3]
        if str(item).strip()
    ]
    conversation_frame["media"] = {
        "status": status,
        "kind": kind,
        "objective_facts": [str(item).strip()[:600] for item in objective_facts[:3]],
        "uncertainties": uncertainties,
        "social_interpretation_policy": "do_not_infer_sender_intent",
    }
    conversation_frame["media_preflight_state"] = str(
        payload.get("media_preflight_state")
        or conversation_frame.get("media_preflight_state")
        or ""
    )
    conversation_frame["visual_context_state"] = status
    envelope = conversation_frame.get("grounding_envelope")
    if isinstance(envelope, dict) and isinstance(envelope.get("media"), dict):
        envelope["media"].update({
            "kind": kind,
            "visual_context": status,
        })
    refresh_group_situation_revision(conversation_frame)


def finish_group_visual_context(
    payload: dict,
    *,
    group_id: str,
    event_id: str,
    conversation_frame: dict,
    wait_timeout_seconds: int,
    handle: object | None = None,
) -> list[str]:
    """Finish one direct or queued handle and project objective facts once."""

    key = _visual_key(group_id, event_id)
    if handle is None:
        handle = _take_visual_handle(key)
    else:
        pending = _take_visual_handle(key)
        if pending is not None and pending is not handle:
            _remember_visual_handle(key, pending)
    if handle is None:
        has_visual = any(
            isinstance(item, dict)
            and str(item.get("type") or "").strip().lower() in {"image", "video"}
            for item in list(payload.get("attachments") or [])
        )
        status = "unavailable" if has_visual else "none"
        payload["visual_context_status"] = status
        payload["visual_context_reason"] = "group_visual_handle_missing"
        for item in list(payload.get("attachments") or []):
            if isinstance(item, dict) and str(item.get("type") or "").strip().lower() in {"image", "video"}:
                item["visual_context_ready"] = False
                item["visual_context_state"] = status
        result = {
            "status": status,
            "reason": "group_visual_handle_missing",
            "media_kind": "unknown",
            "observation": {},
        }
        _project_group_media_situation(payload, conversation_frame, result, [])
        return []
    result = visual.finish_qq_visual_turn(
        handle,
        payload,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    lines = visual.visual_context_lines(
        visual.visual_scope(channel="qq_group", thread_id=group_id),
        event_id,
    )
    _project_group_media_situation(payload, conversation_frame, result, lines)
    return lines


def prepare_group_visual_context(
    payload: dict,
    *,
    group_id: str,
    message: str,
    fallback_settings: dict,
    is_mention: bool,
    conversation_frame: dict,
) -> list[str]:
    """Prepare at most one bounded visual context for a group turn."""

    # Always cross the visual boundary.  The adapter may leave a malformed or
    # stale ``visual_media`` value even when no structural image/video marker
    # is present; ``visual.prepare`` consumes it and projects typed ``none``
    # while stripping stale visual-context markers.  It remains bounded and
    # will not call the vision model when no media is present.
    observation = conversation_frame.get("media_observation_decision") or conversation_frame.get("media_observation")
    if isinstance(observation, dict):
        observation = observation.get("decision")
    event_id = str(payload.get("_external_message_id") or payload.get("trace_id") or "")
    handle = begin_group_visual_context(
        payload,
        group_id=group_id,
        event_id=event_id,
        message=message,
        fallback_settings=fallback_settings,
        allow_model=bool(
            is_mention or payload.get("reply_to_assistant")
            or str(observation or "").strip().lower() == "observe"
        ),
    )
    visual_context = finish_group_visual_context(
        payload,
        group_id=group_id,
        event_id=event_id,
        conversation_frame=conversation_frame,
        wait_timeout_seconds=45,
        handle=handle,
    )
    return visual_context


def project_group_visual_context(
    current: dict,
    context_items: list[dict],
    visual_context: list[str],
    *,
    max_context: int,
) -> tuple[dict, list[dict]]:
    """Project typed visual evidence into current turn and model history."""

    current = visual.current(current, visual_context)
    history = group_model_history(context_items[:-1], limit=max_context)
    return current, history


__all__ = [
    "begin_group_visual_context",
    "clear_pending_group_visual_contexts",
    "finish_group_visual_context",
    "observe_group_visual_without_reply",
    "pending_group_visual_context_count",
    "prepare_group_visual_context",
    "prior_group_visual_context",
    "project_group_visual_context",
]
