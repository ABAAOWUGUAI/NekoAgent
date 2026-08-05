"""Bounded group media preparation and visual-context projection."""

from __future__ import annotations

import bridge_visual_context as visual
from bridge_group_context_frame import group_model_history


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
    visual_context = visual.prepare(
        payload,
        "qq_group",
        group_id,
        payload.get("_external_message_id") or payload.get("trace_id"),
        message,
        fallback_settings,
        allow_model=bool(
            is_mention or payload.get("reply_to_assistant")
            or str(observation or "").strip().lower() == "observe"
        ),
    )
    conversation_frame["media_preflight_state"] = str(
        payload.get("media_preflight_state") or conversation_frame.get("media_preflight_state") or ""
    )
    conversation_frame["visual_context_state"] = str(
        payload.get("visual_context_status") or conversation_frame.get("visual_context_state") or ""
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


__all__ = ["prepare_group_visual_context", "project_group_visual_context"]
