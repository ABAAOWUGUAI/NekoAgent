"""Strict, ephemeral projection for one ordered private multimodal turn."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping


ORDERED_TURN_SCHEMA_VERSION = 1
MAX_PLAIN_TEXT_CHARS = 4000
MAX_REPLY_TEXT_CHARS = 1000
MAX_PROJECTED_TURN_CHARS = 12000

_VISUAL_TYPES = frozenset({
    "image",
    "photo",
    "picture",
    "gif",
    "video",
    "mface",
    "marketface",
    "market_face",
    "dynamicface",
    "dynamic_face",
})
_FORBIDDEN_RAW_FIELDS = frozenset({"url", "file", "path", "data_base64", "digest"})
_PLAIN_FIELDS = frozenset({"type", "position", "text"})
_REPLY_FIELDS = frozenset({"type", "position"})
_VISUAL_FIELDS = frozenset({
    "type",
    "position",
    "visual_index",
    "media_kind",
    "source_component",
})
_COMPLETE_VIEW_FIELDS = frozenset({
    "schema_version",
    "order_contract",
    "components",
    "reply_to",
    "logical_turn_has_text",
})
_OBSERVATION_FIELDS = frozenset({
    "schema_version",
    "status",
    "media_kind",
    "visible_elements",
    "visible_text",
    "depicted_expression_or_gesture",
    "uncertain_elements",
})
_OBSERVATION_STATUSES = frozenset({"ready", "unavailable"})
_MEDIA_KINDS = frozenset({
    "photo",
    "screenshot",
    "document",
    "meme_or_sticker",
    "illustration",
    "unknown",
})
_SYSTEM_PREFIX = (
    "PRIVATE_MULTIMODAL_TURN_V1\n"
    "按组件顺序联合理解本轮；视觉观察不是用户事实。\n"
    "不要把画面中角色的表情等同于用户真实情绪。\n"
    "除非用户明确要求，不要机械复述图片；只生成一条自然回复。"
)
_EXPLICIT_VISUAL_EVIDENCE_PATTERNS = (
    re.compile(
        r"(?:读|识别|辨认|提取|查看|检查|核对|确认|分析)"
        r".{0,12}(?:图|图片|截图|照片|画面)"
        r".{0,12}(?:字|文字|内容|报错|错误|证据|信息|是什么|有什么)"
    ),
    re.compile(
        r"(?:图|图片|截图|照片|画面)(?:里|中|上)(?:的)?"
        r".{0,8}(?:字|文字|内容|报错|错误|证据|信息|是什么|有什么)"
    ),
    re.compile(r"OCR.{0,12}(?:图|图片|截图|照片|画面)", re.IGNORECASE),
)
_CONCRETE_FIRST_PERSON_VISUAL_CLAIM = re.compile(
    r"我(?:刚才|已经|仔细)?(?:看到|看见|看了|看过|查看了|检查了)"
    r"(?:这|那)?(?:一)?(?:张)?(?:图|图片|截图|照片|画面)"
    r"(?:里|中|上|[，,:：])"
)


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _incomplete(reason: object) -> dict:
    return {
        "schema_version": ORDERED_TURN_SCHEMA_VERSION,
        "order_contract": "incomplete",
        "reason": str(reason or "order_contract_invalid")[:80],
        "components": [],
    }


def _contains_forbidden_raw_field(value: object) -> bool:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if str(raw_key or "").strip().lower() in _FORBIDDEN_RAW_FIELDS:
                return True
            if _contains_forbidden_raw_field(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_raw_field(item) for item in value)
    return False


def _reply_projection(inbound_context: Mapping[str, object]) -> tuple[dict | None, str]:
    raw = inbound_context.get("reply_context")
    if raw is None:
        return {"present": False}, ""
    if not isinstance(raw, Mapping) or _contains_forbidden_raw_field(raw):
        return None, "reply_context_invalid"
    if set(raw) != {"external_message_id", "role", "text"}:
        return None, "reply_context_invalid"
    external_message_id = raw.get("external_message_id")
    role = raw.get("role")
    text = raw.get("text")
    if not isinstance(external_message_id, str) or not external_message_id.strip():
        return None, "reply_context_invalid"
    if role not in {"assistant", "user_or_unknown"} or not isinstance(text, str):
        return None, "reply_context_invalid"
    return {
        "present": True,
        "external_message_id": external_message_id.strip()[:160],
        "role": role,
        "text": text[:MAX_REPLY_TEXT_CHARS],
    }, ""


def _fit_projected_turn(view: dict) -> dict:
    if len(_stable_json(view)) <= MAX_PROJECTED_TURN_CHARS:
        return view

    slots: list[tuple[dict, str, int]] = []
    if view["reply_to"].get("present"):
        slots.append((view["reply_to"], "text", 0))
    slots.extend(
        (component, "text", 1)
        for component in reversed(view["components"])
        if component.get("kind") == "text"
    )
    for container, key, minimum in slots:
        while len(_stable_json(view)) > MAX_PROJECTED_TURN_CHARS:
            value = container[key]
            removable = len(value) - minimum
            if removable <= 0:
                break
            excess = len(_stable_json(view)) - MAX_PROJECTED_TURN_CHARS
            remove_count = min(removable, max(1, excess))
            container[key] = value[: len(value) - remove_count]
    return view if len(_stable_json(view)) <= MAX_PROJECTED_TURN_CHARS else _incomplete(
        "current_turn_too_large"
    )


def build_ordered_turn_view(message: str, inbound_context: dict) -> dict:
    """Build one fail-closed view without reconstructing components from ``message``."""

    del message  # Aggregate text is not an ordering source.
    if not isinstance(inbound_context, Mapping):
        return _incomplete("inbound_context_invalid")
    logical_turn_has_text = inbound_context.get("logical_turn_has_text")
    if type(logical_turn_has_text) is not bool:
        return _incomplete("logical_turn_has_text_invalid")
    raw_components = inbound_context.get("message_components")
    if not isinstance(raw_components, list) or not raw_components:
        return _incomplete("message_components_missing")

    projected: list[dict] = []
    seen_positions: set[int] = set()
    seen_visual_indices: set[int] = set()
    next_visual_index = 0
    for expected_position, raw_component in enumerate(raw_components):
        if not isinstance(raw_component, Mapping):
            return _incomplete("component_invalid")
        if _contains_forbidden_raw_field(raw_component):
            return _incomplete("component_fields_forbidden")
        if "position" not in raw_component:
            return _incomplete("component_position_missing")
        position = raw_component.get("position")
        if type(position) is not int or position < 0:
            return _incomplete("component_position_invalid")
        if position in seen_positions:
            return _incomplete("component_position_duplicate")
        if position != expected_position:
            return _incomplete("component_order_invalid")
        seen_positions.add(position)

        component_type = raw_component.get("type")
        if not isinstance(component_type, str) or component_type != component_type.strip().lower():
            return _incomplete("component_type_invalid")
        if component_type == "plain":
            if set(raw_component) - _PLAIN_FIELDS:
                return _incomplete("component_fields_forbidden")
            if "text" not in raw_component:
                return _incomplete("plain_text_missing")
            text = raw_component.get("text")
            if not isinstance(text, str) or not text:
                return _incomplete("plain_text_invalid")
            projected.append({
                "kind": "text",
                "position": position,
                "text": text[:MAX_PLAIN_TEXT_CHARS],
            })
            continue

        if component_type == "reply":
            if set(raw_component) != _REPLY_FIELDS:
                return _incomplete("component_fields_forbidden")
            projected.append({"kind": "reply", "position": position})
            continue

        if component_type not in _VISUAL_TYPES:
            return _incomplete("component_type_unsupported")
        if set(raw_component) - _VISUAL_FIELDS:
            return _incomplete("component_fields_forbidden")
        if "visual_index" not in raw_component:
            return _incomplete("visual_index_missing")
        visual_index = raw_component.get("visual_index")
        if type(visual_index) is not int or visual_index < 0:
            return _incomplete("visual_index_invalid")
        if visual_index in seen_visual_indices or visual_index != next_visual_index:
            return _incomplete("visual_index_order_invalid")
        for optional_field in ("media_kind", "source_component"):
            if optional_field in raw_component:
                optional_value = raw_component.get(optional_field)
                if not isinstance(optional_value, str) or len(optional_value) > 40:
                    return _incomplete("component_fields_forbidden")
        seen_visual_indices.add(visual_index)
        next_visual_index += 1
        projected.append({
            "kind": "visual",
            "position": position,
            "visual_index": visual_index,
        })

    if logical_turn_has_text != any(item.get("kind") == "text" for item in projected):
        return _incomplete("logical_turn_text_mismatch")
    reply_to, reply_error = _reply_projection(inbound_context)
    if reply_to is None:
        return _incomplete(reply_error)
    if any(item.get("kind") == "reply" for item in projected) and not reply_to.get("present"):
        return _incomplete("reply_context_missing")
    return _fit_projected_turn({
        "schema_version": ORDERED_TURN_SCHEMA_VERSION,
        "order_contract": "complete",
        "components": projected,
        "reply_to": reply_to,
        "logical_turn_has_text": logical_turn_has_text,
    })


def has_complete_order_contract(view: object) -> bool:
    """Return the sole eligibility decision for the ordered-turn fast path."""

    if not isinstance(view, Mapping) or set(view) != _COMPLETE_VIEW_FIELDS:
        return False
    if type(view.get("schema_version")) is not int or view.get("schema_version") != 1:
        return False
    if view.get("order_contract") != "complete" or type(view.get("logical_turn_has_text")) is not bool:
        return False
    components = view.get("components")
    if not isinstance(components, list) or not components:
        return False
    visual_index = 0
    has_text = False
    has_reply = False
    for position, component in enumerate(components):
        if (
            not isinstance(component, Mapping)
            or type(component.get("position")) is not int
            or component.get("position") != position
        ):
            return False
        if component.get("kind") == "text":
            if set(component) != {"kind", "position", "text"}:
                return False
            text = component.get("text")
            if not isinstance(text, str) or not text or len(text) > MAX_PLAIN_TEXT_CHARS:
                return False
            has_text = True
        elif component.get("kind") == "visual":
            if set(component) != {"kind", "position", "visual_index"}:
                return False
            if (
                type(component.get("visual_index")) is not int
                or component.get("visual_index") != visual_index
            ):
                return False
            visual_index += 1
        elif component.get("kind") == "reply":
            if set(component) != {"kind", "position"}:
                return False
            has_reply = True
        else:
            return False
    reply_to = view.get("reply_to")
    if not isinstance(reply_to, Mapping) or type(reply_to.get("present")) is not bool:
        return False
    if reply_to.get("present"):
        if set(reply_to) != {"present", "external_message_id", "role", "text"}:
            return False
        if not isinstance(reply_to.get("external_message_id"), str) or not reply_to.get("external_message_id"):
            return False
        if reply_to.get("role") not in {"assistant", "user_or_unknown"}:
            return False
        if not isinstance(reply_to.get("text"), str) or len(reply_to.get("text")) > MAX_REPLY_TEXT_CHARS:
            return False
    elif set(reply_to) != {"present"}:
        return False
    return (
        has_text == view.get("logical_turn_has_text")
        and (not has_reply or bool(reply_to.get("present")))
        and len(_stable_json(view)) <= MAX_PROJECTED_TURN_CHARS
    )


def _daily_private_fast_path_base_allowed(
    *,
    source: object,
    force: object,
    detected_intent: object,
    effectful_work_requested: object,
    daily_conversation_proven: object,
    inbound_context: object,
) -> bool:
    if type(source) is not str or source != "qq":
        return False
    if type(force) is not str or force not in {"auto", "chat"}:
        return False
    if (
        type(detected_intent) is not str
        or detected_intent not in {"chat", "analysis"}
    ):
        return False
    if type(effectful_work_requested) is not bool or effectful_work_requested:
        return False
    if type(daily_conversation_proven) is not bool or not daily_conversation_proven:
        return False
    if not isinstance(inbound_context, Mapping):
        return False
    session = inbound_context.get("session")
    prefix = "qq:private:"
    if (
        type(session) is not str
        or not session.startswith(prefix)
        or not session[len(prefix):].strip()
    ):
        return False
    return True


def daily_multimodal_fast_path_allowed(
    *,
    source: object,
    force: object,
    detected_intent: object,
    effectful_work_requested: object,
    daily_conversation_proven: object,
    inbound_context: object,
    ordered_turn: object,
) -> bool:
    """Return structural eligibility after upstream formal gates have passed."""

    if not _daily_private_fast_path_base_allowed(
        source=source,
        force=force,
        detected_intent=detected_intent,
        effectful_work_requested=effectful_work_requested,
        daily_conversation_proven=daily_conversation_proven,
        inbound_context=inbound_context,
    ):
        return False
    if not has_complete_order_contract(ordered_turn):
        return False
    return any(
        type(component.get("kind")) is str and component.get("kind") == "visual"
        for component in ordered_turn.get("components", [])
    )


def daily_private_situation_fast_path_allowed(
    *,
    source: object,
    force: object,
    detected_intent: object,
    effectful_work_requested: object,
    daily_conversation_proven: object,
    inbound_context: object,
) -> bool:
    """Admit a text successor only when validated prior Situation facts exist."""

    if not _daily_private_fast_path_base_allowed(
        source=source,
        force=force,
        detected_intent=detected_intent,
        effectful_work_requested=effectful_work_requested,
        daily_conversation_proven=daily_conversation_proven,
        inbound_context=inbound_context,
    ):
        return False
    cycle_ids = inbound_context.get("_situation_context_cycle_ids")
    message_ids = inbound_context.get("_situation_context_message_ids")
    return bool(
        inbound_context.get("_situation_context_status") == "ready"
        and isinstance(cycle_ids, list)
        and cycle_ids
        and all(type(item) is str and item for item in cycle_ids)
        and isinstance(message_ids, list)
        and message_ids
        and all(type(item) is str and item for item in message_ids)
    )


def explicit_visual_evidence_requested(message: object) -> bool:
    """Return only whether the user explicitly asks to inspect visual evidence."""

    if not isinstance(message, str):
        return False
    normalized = " ".join(message.split())[:4000]
    if not normalized:
        return False
    return any(pattern.search(normalized) is not None for pattern in _EXPLICIT_VISUAL_EVIDENCE_PATTERNS)


def private_visual_claim_without_observation(reply: object, observation: object) -> bool:
    """Flag a narrow concrete first-person visual claim without ready evidence."""

    if not isinstance(reply, str) or not isinstance(observation, Mapping):
        return False
    if set(observation) != _OBSERVATION_FIELDS or _contains_forbidden_raw_field(observation):
        return False
    if (
        type(observation.get("schema_version")) is not int
        or observation.get("schema_version") != 1
        or observation.get("status") != "unavailable"
        or type(observation.get("media_kind")) is not str
        or observation.get("media_kind") not in _MEDIA_KINDS
        or _bounded_string_list(observation.get("visible_elements")) is None
        or _bounded_string_list(observation.get("uncertain_elements")) is None
        or not isinstance(observation.get("visible_text"), str)
        or not isinstance(observation.get("depicted_expression_or_gesture"), str)
    ):
        return False
    return _CONCRETE_FIRST_PERSON_VISUAL_CLAIM.search(reply[:12000]) is not None


def _unavailable_observation() -> dict:
    return {
        "schema_version": 1,
        "status": "unavailable",
        "media_kind": "unknown",
        "visible_elements": [],
        "visible_text": "",
        "depicted_expression_or_gesture": "",
        "uncertain_elements": [],
    }


def _bounded_string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return [item[:1000] for item in value[:3]]


def _observation_projection(observation: object) -> dict:
    if not isinstance(observation, Mapping):
        return _unavailable_observation()
    if set(observation) != _OBSERVATION_FIELDS or _contains_forbidden_raw_field(observation):
        return _unavailable_observation()
    if type(observation.get("schema_version")) is not int or observation.get("schema_version") != 1:
        return _unavailable_observation()
    status = observation.get("status")
    media_kind = observation.get("media_kind")
    if type(status) is not str or status not in _OBSERVATION_STATUSES:
        return _unavailable_observation()
    if type(media_kind) is not str or media_kind not in _MEDIA_KINDS:
        return _unavailable_observation()
    visible_elements = _bounded_string_list(observation.get("visible_elements"))
    uncertain_elements = _bounded_string_list(observation.get("uncertain_elements"))
    visible_text = observation.get("visible_text")
    gesture = observation.get("depicted_expression_or_gesture")
    if visible_elements is None or uncertain_elements is None:
        return _unavailable_observation()
    if not isinstance(visible_text, str) or not isinstance(gesture, str):
        return _unavailable_observation()
    if status == "unavailable":
        return _unavailable_observation()
    return {
        "schema_version": 1,
        "status": status,
        "media_kind": media_kind,
        "visible_elements": visible_elements,
        "visible_text": visible_text[:4000],
        "depicted_expression_or_gesture": gesture[:1000],
        "uncertain_elements": uncertain_elements,
    }


def merge_visual_observations(observations: object) -> dict:
    """Merge strict per-member observations without inventing conflicting facts."""

    if not isinstance(observations, (list, tuple)) or not observations:
        return _unavailable_observation()
    projected = [_observation_projection(observation) for observation in observations]
    if any(observation.get("status") != "ready" for observation in projected):
        return _unavailable_observation()

    def stable_values(field: str, limit: int = 3) -> list[str]:
        values: list[str] = []
        for observation in projected:
            for value in observation[field]:
                if value not in values:
                    values.append(value)
                    if len(values) >= limit:
                        return values
        return values

    def one_nonempty_value(field: str) -> str:
        values: list[str] = []
        for observation in projected:
            value = observation[field]
            if value and value not in values:
                values.append(value)
        return values[0] if len(values) == 1 else ""

    media_kinds = {observation["media_kind"] for observation in projected}
    return {
        "schema_version": 1,
        "status": "ready",
        "media_kind": next(iter(media_kinds)) if len(media_kinds) == 1 else "unknown",
        "visible_elements": stable_values("visible_elements"),
        "visible_text": one_nonempty_value("visible_text")[:4000],
        "depicted_expression_or_gesture": one_nonempty_value(
            "depicted_expression_or_gesture"
        )[:1000],
        "uncertain_elements": stable_values("uncertain_elements"),
    }


def append_multimodal_turn_history(
    history: list[dict],
    view: dict,
    observation: dict,
) -> list[dict]:
    """Append one bounded system-only projection after eligible history."""

    result = list(history) if isinstance(history, list) else []
    if not has_complete_order_contract(view):
        return result
    payload = {
        "observation_semantics": "MODEL_OBSERVATION_NOT_USER_FACT",
        "ordered_turn": dict(view),
        "visual_observation": _observation_projection(observation),
    }
    result.append({
        "role": "system",
        "content": _SYSTEM_PREFIX + "\n" + _stable_json(payload),
    })
    return result


__all__ = [
    "append_multimodal_turn_history",
    "build_ordered_turn_view",
    "daily_multimodal_fast_path_allowed",
    "daily_private_situation_fast_path_allowed",
    "explicit_visual_evidence_requested",
    "has_complete_order_contract",
    "merge_visual_observations",
    "private_visual_claim_without_observation",
]
