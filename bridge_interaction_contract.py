#!/usr/bin/env python3
"""Validated multi-intent plan and immutable response-block contracts.

The classifier may suggest styleable acknowledgement text and typed actions,
but it may not create executable command parameters.  Response assembly keeps
facts byte-for-byte inside immutable blocks and never calls a second model.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping

from bridge_action_registry import action_definition, action_types, planner_action_types


PLAN_SCHEMA_VERSION = 2
PLAN_TOP_LEVEL_FIELDS = {
    "schema_version",
    "summary_mode",
    "primary_intent",
    "confidence",
    "reason",
    "intents",
    "reply_parts",
    "actions",
    "approval_requests",
    "memory_candidates",
}
INTENT_TYPES = {
    "chat",
    "emotional_support",
    "ops",
    "code",
    "research",
    "analysis",
    "memory",
    "automation",
    "meta",
}
WORK_INTENTS = {"ops", "code", "research", "analysis", "memory", "automation"}
TOOL_DEFAULT_INTENTS = {"ops", "code", "research", "automation"}
REPLY_PART_TYPES = {"social_ack", "transition", "progress", "risk", "next_step"}
ACTION_TYPES = set(action_types())
RISK_LEVELS = {"none", "low", "medium", "high"}
FACT_BLOCK_TYPES = {
    "fact",
    "code",
    "command",
    "log",
    "citation",
    "artifact",
    "approval",
    "status",
}


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_confidence(value: object, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(number, 1.0))


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _intent_candidates(message: str, primary: str, mode: str, emotion: str) -> list[str]:
    text = str(message or "").lower()
    result: list[str] = []
    social = emotion not in {"", "neutral"} or _contains_any(
        text,
        (
            "累", "困", "烦", "难受", "委屈", "生气", "气死", "崩溃",
            "开心", "高兴", "陪我", "聊聊", "哈哈", "笑死",
        ),
    )
    if mode == "mixed" or (social and primary in WORK_INTENTS):
        result.append("emotional_support")

    rules = (
        ("memory", ("记住", "以后记得", "偏好", "习惯")),
        ("automation", ("定时任务", "定时计划", "每天", "每日", "每周", "每隔")),
        ("ops", ("服务器", "docker", "日志", "端口", "nginx", "部署", "重启", "systemctl", "排查")),
        ("code", ("代码", "开发", "修复", "bug", "重构", "测试", "git", "仓库", "项目", "网站")),
        ("research", ("查一下", "查询", "搜索", "资料", "新闻", "最新", "天气", "价格", "github")),
        ("analysis", ("分析", "方案", "建议", "评估", "为什么", "怎么", "如何")),
    )
    for intent, hints in rules:
        if intent == primary or _contains_any(text, hints):
            result.append(intent)
    if primary in INTENT_TYPES:
        result.append(primary)
    if not result:
        result.append("chat")
    if mode == "daily" and not any(item in WORK_INTENTS for item in result):
        result.append("chat")

    unique: list[str] = []
    for item in result:
        if item not in unique:
            unique.append(item)
    return unique[:8]


def _social_ack(emotion: str) -> str:
    return {
        "tired": "听起来你已经很累了，这件事我先接过来。",
        "annoyed": "确实够烦的，我先把这件事接过来。",
        "comfort": "听起来这一下挺难受的，我先帮你处理眼前这件事。",
        "sad": "我听到了，先别一个人扛着；眼前这件事我来处理。",
        "happy": "好，进展不错，我接着把这件事办完。",
        "playful": "行，我接到这个梗了，也顺手把正事办掉。",
    }.get(emotion, "我先接住你刚才的感受，再处理这件事。")


def fallback_interaction_plan(message: str, mode_decision: Mapping[str, object]) -> dict:
    """Build a bounded plan when structured classification is unavailable."""

    mode = _clip(mode_decision.get("mode") or "daily", 16).lower()
    if mode not in {"daily", "work", "mixed"}:
        mode = "daily"
    primary = _clip(mode_decision.get("intent") or "chat", 40).lower()
    if primary not in INTENT_TYPES:
        primary = "analysis" if mode != "daily" else "chat"
    emotion = _clip(mode_decision.get("emotion") or "neutral", 24).lower()
    intent_names = _intent_candidates(message, primary, mode, emotion)
    intents = []
    actions = []
    for index, intent in enumerate(intent_names, start=1):
        requires_tools = bool(mode_decision.get("need_tools")) if intent == primary else intent in TOOL_DEFAULT_INTENTS
        intents.append(
            {
                "id": f"intent-{index}",
                "type": intent,
                "confidence": _safe_confidence(mode_decision.get("confidence"), 0.68),
                "objective": "回应当前消息" if intent in {"chat", "emotional_support"} else "完成本轮用户目标",
                "requires_tools": requires_tools,
                "risk_level": "low" if requires_tools else "none",
            },
        )
        lane = str(mode_decision.get("execution_lane") or "").strip()
        if lane in ACTION_TYPES and intent == primary:
            action_type = lane
        elif intent == "memory":
            action_type = "propose_memory"
        elif intent in WORK_INTENTS and requires_tools:
            action_type = "continue_task" if mode_decision.get("work_lifecycle") == "continue" else "start_task"
        elif mode_decision.get("end_work"):
            action_type = "finish_work"
        else:
            action_type = "respond"
        actions.append(
            {
                "id": f"action-{index}",
                "type": action_type,
                "intent_id": f"intent-{index}",
                "objective": intents[-1]["objective"],
                "requires_tools": action_definition(action_type).requires_tools or requires_tools,
                "risk_level": action_definition(action_type).risk_level,
                "depends_on": [],
            },
        )

    reply_parts = []
    if "emotional_support" in intent_names and any(item in WORK_INTENTS for item in intent_names):
        reply_parts.append(
            {
                "id": "reply-1",
                "type": "social_ack",
                "text": _social_ack(emotion),
                "styleable": True,
            },
        )
    memory_candidates = []
    if "memory" in intent_names:
        memory_candidates.append(
            {
                "kind": "preference_or_fact",
                "scope_hint": "source_thread",
                "requires_consent": True,
            },
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "summary_mode": mode,
        "primary_intent": primary,
        "confidence": _safe_confidence(mode_decision.get("confidence"), 0.68),
        "reason": _clip(mode_decision.get("reason") or "compatibility fallback", 500),
        "intents": intents,
        "reply_parts": reply_parts,
        "actions": actions[:12],
        "approval_requests": [],
        "memory_candidates": memory_candidates,
    }


def build_interaction_plan_messages(
    settings: Mapping[str, object],
    user_id: str,
    message: str,
    history: list[dict],
    previous_session: Mapping[str, object] | None,
    policy: Mapping[str, object],
) -> list[dict[str, str]]:
    """Build the strict planner prompt used after the feature cutover."""

    recent = []
    for item in history[-8:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = _clip(item.get("content"), 300)
        if content:
            recent.append(f"{role}: {content}")
    return [
        {
            "role": "system",
            "content": (
                "你是私人助手的交互规划器，只输出一个 JSON 对象。"
                "一条消息可以同时包含多个意图，禁止把聊天与办事强制二选一。\n"
                "顶层字段必须且只能是 schema_version, summary_mode, primary_intent, confidence, reason, "
                "intents, reply_parts, actions, approval_requests, memory_candidates。schema_version 固定为 2。\n"
                "summary_mode 只能是 daily/work/mixed；primary_intent 必须等于某个 intents.type。\n"
                "intents 最多 8 项；type 只能是 chat/emotional_support/ops/code/research/analysis/memory/automation/meta。"
                "每项字段为 id,type,confidence,objective,requires_tools,risk_level。\n"
                "reply_parts 最多 4 项，只用于简短的情绪承接或过渡，不回答技术事实；"
                "字段为 id,type,text,styleable，type 只能是 social_ack/transition/progress/risk/next_step。\n"
                "actions 最多 12 项，只描述动作类别，禁止输出命令、路径、URL、凭据或任意工具参数；"
                "字段为 id,type,intent_id,objective,requires_tools,risk_level,depends_on；depends_on 只能引用更早动作 ID，type 只能是 "
                f"{planner_action_types()}。\n"
                "approval_requests 和 memory_candidates 只给类别元数据，不得复制消息中的敏感内容。"
                "代码、命令、日志、引用、文件、校验值不属于 reply_parts。"
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"用户作用域: {user_id}",
                    f"当前助手: {_clip(settings.get('display_name'), 80)}",
                    f"工作会话摘要: {json.dumps(dict(previous_session or {}), ensure_ascii=False)}",
                    f"允许混合意图: {bool(policy.get('mixed_mode_enabled', True))}",
                    "近期对话:",
                    *(recent or ["(无近期对话)"]),
                    f"本轮消息: {message}",
                ],
            ),
        },
    ]


def build_interaction_plan_prompt(*args, **kwargs) -> str:
    messages = build_interaction_plan_messages(*args, **kwargs)
    return "\n\n".join((messages[0]["content"], messages[1]["content"], "只输出 JSON。"))


def _json_object(raw_text: str) -> dict | None:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        text = match.group(0)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _normalize_intents(items: object) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("interaction_plan_intents_required")
    result = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(items[:8], start=1):
        if not isinstance(raw, dict) or set(raw) - {"id", "type", "confidence", "objective", "requires_tools", "risk_level"}:
            raise ValueError("interaction_plan_intent_invalid")
        intent = _clip(raw.get("type"), 40).lower()
        if intent not in INTENT_TYPES:
            raise ValueError("interaction_plan_intent_type_invalid")
        risk = _clip(raw.get("risk_level") or "none", 16).lower()
        if risk not in RISK_LEVELS:
            raise ValueError("interaction_plan_risk_invalid")
        item_id = _clip(raw.get("id") or f"intent-{index}", 64)
        if not item_id or item_id in seen_ids:
            raise ValueError("interaction_plan_intent_id_invalid")
        seen_ids.add(item_id)
        result.append(
            {
                "id": item_id,
                "type": intent,
                "confidence": _safe_confidence(raw.get("confidence")),
                "objective": _clip(raw.get("objective"), 240),
                "requires_tools": bool(raw.get("requires_tools")),
                "risk_level": risk,
            },
        )
    if not result:
        raise ValueError("interaction_plan_intents_required")
    return result


def _normalize_reply_parts(items: object) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("interaction_plan_reply_parts_required")
    result = []
    for index, raw in enumerate(items[:4], start=1):
        if not isinstance(raw, dict) or set(raw) - {"id", "type", "text", "styleable"}:
            raise ValueError("interaction_plan_reply_part_invalid")
        part_type = _clip(raw.get("type"), 40).lower()
        if part_type not in REPLY_PART_TYPES:
            raise ValueError("interaction_plan_reply_part_type_invalid")
        text = _clip(raw.get("text"), 240)
        if not text:
            continue
        result.append(
            {
                "id": _clip(raw.get("id") or f"reply-{index}", 64),
                "type": part_type,
                "text": text,
                "styleable": True,
            },
        )
    return result


def _normalize_actions(items: object, intent_ids: set[str]) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("interaction_plan_actions_required")
    result = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(items[:12], start=1):
        if not isinstance(raw, dict) or set(raw) - {"id", "type", "intent_id", "objective", "requires_tools", "risk_level", "depends_on"}:
            raise ValueError("interaction_plan_action_invalid")
        action_type = _clip(raw.get("type"), 40).lower()
        intent_id = _clip(raw.get("intent_id"), 64)
        risk = _clip(raw.get("risk_level") or "none", 16).lower()
        if action_type not in ACTION_TYPES or intent_id not in intent_ids or risk not in RISK_LEVELS:
            raise ValueError("interaction_plan_action_reference_invalid")
        item_id = _clip(raw.get("id") or f"action-{index}", 64)
        if not item_id or item_id in seen_ids:
            raise ValueError("interaction_plan_action_id_invalid")
        depends_on = raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else []
        normalized_dependencies = []
        for dependency in depends_on:
            dependency_id = _clip(dependency, 64)
            if not dependency_id or dependency_id not in seen_ids or dependency_id in normalized_dependencies:
                raise ValueError("interaction_plan_action_dependency_invalid")
            normalized_dependencies.append(dependency_id)
        seen_ids.add(item_id)
        result.append(
            {
                "id": item_id,
                "type": action_type,
                "intent_id": intent_id,
                "objective": _clip(raw.get("objective"), 240),
                "requires_tools": bool(raw.get("requires_tools")),
                "risk_level": risk,
                "depends_on": normalized_dependencies,
            },
        )
    return result


def _normalize_metadata_list(items: object, allowed: set[str], limit: int) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("interaction_plan_metadata_list_required")
    result = []
    for raw in items[:limit]:
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError("interaction_plan_metadata_invalid")
        normalized = {}
        for key, value in raw.items():
            if key not in allowed:
                continue
            if key in {"requires_confirmation", "requires_consent"}:
                normalized[key] = bool(value)
            else:
                normalized[key] = _clip(value, 240)
        result.append(normalized)
    return result


def normalize_interaction_plan(data: Mapping[str, object]) -> dict:
    unknown = set(data) - PLAN_TOP_LEVEL_FIELDS
    missing = PLAN_TOP_LEVEL_FIELDS - set(data)
    if unknown or missing or int(data.get("schema_version") or 0) not in {1, PLAN_SCHEMA_VERSION}:
        raise ValueError("interaction_plan_schema_invalid")
    mode = _clip(data.get("summary_mode"), 16).lower()
    primary = _clip(data.get("primary_intent"), 40).lower()
    if mode not in {"daily", "work", "mixed"} or primary not in INTENT_TYPES:
        raise ValueError("interaction_plan_summary_invalid")
    intents = _normalize_intents(data.get("intents"))
    intent_ids = {item["id"] for item in intents}
    if primary not in {item["type"] for item in intents}:
        raise ValueError("interaction_plan_primary_missing")
    actions = _normalize_actions(data.get("actions"), intent_ids)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "summary_mode": mode,
        "primary_intent": primary,
        "confidence": _safe_confidence(data.get("confidence")),
        "reason": _clip(data.get("reason"), 500),
        "intents": intents,
        "reply_parts": _normalize_reply_parts(data.get("reply_parts")),
        "actions": actions,
        "approval_requests": _normalize_metadata_list(
            data.get("approval_requests"),
            {"risk_type", "reason", "requires_confirmation"},
            4,
        ),
        "memory_candidates": _normalize_metadata_list(
            data.get("memory_candidates"),
            {"kind", "scope_hint", "requires_consent"},
            4,
        ),
    }


def parse_interaction_plan(raw_text: str, fallback: dict) -> tuple[dict, str]:
    data = _json_object(raw_text)
    if data is None:
        return dict(fallback), "invalid_json"
    try:
        return normalize_interaction_plan(data), ""
    except (TypeError, ValueError) as exc:
        return dict(fallback), str(exc)


def interaction_plan_hash(plan: Mapping[str, object]) -> str:
    payload = json.dumps(dict(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mode_decision_from_interaction_plan(plan: Mapping[str, object], fallback: Mapping[str, object]) -> dict:
    result = dict(fallback)
    mode = str(plan.get("summary_mode") or result.get("mode") or "daily")
    primary = str(plan.get("primary_intent") or result.get("intent") or "chat")
    actions = list(plan.get("actions") or [])
    result.update(
        {
            "mode": mode,
            "intent": primary,
            "confidence": _safe_confidence(plan.get("confidence"), _safe_confidence(result.get("confidence"))),
            "reason": _clip(plan.get("reason") or result.get("reason"), 500),
            "need_tools": any(bool(item.get("requires_tools")) for item in actions),
            "end_work": any(item.get("type") == "finish_work" for item in actions),
            "source": "interaction_plan",
            "interaction_plan": dict(plan),
        },
    )
    return result


def reconcile_plan_with_mode(plan: Mapping[str, object], decision: Mapping[str, object]) -> dict:
    """Keep legacy freshness/lifecycle overrides reflected in the rich plan."""

    result = dict(plan)
    result["summary_mode"] = str(decision.get("mode") or result.get("summary_mode") or "daily")
    primary = str(decision.get("intent") or result.get("primary_intent") or "chat")
    if primary not in INTENT_TYPES:
        primary = "analysis" if str(decision.get("mode") or "daily") != "daily" else "chat"
    result["primary_intent"] = primary
    intents = [dict(item) for item in result.get("intents") or []]
    if primary not in {str(item.get("type")) for item in intents}:
        removed_intent_id = ""
        if len(intents) >= 8:
            removed_intent_id = str(intents[-1].get("id") or "")
            intents = intents[:-1]
        intent_id = f"intent-{len(intents) + 1}"
        intents.append(
            {
                "id": intent_id,
                "type": primary,
                "confidence": _safe_confidence(decision.get("confidence"), 0.8),
                "objective": "完成本轮用户目标",
                "requires_tools": bool(decision.get("need_tools")),
                "risk_level": "low" if decision.get("need_tools") else "none",
            },
        )
        if removed_intent_id:
            result["actions"] = [
                dict(item)
                for item in result.get("actions") or []
                if str(item.get("intent_id") or "") != removed_intent_id
            ]
    result["intents"] = intents[:8]
    lane = str(decision.get("execution_lane") or "").strip()
    if lane in ACTION_TYPES:
        definition = action_definition(lane)
        for item in result.get("actions") or []:
            if str(item.get("intent_id") or "") == next(
                (str(intent.get("id") or "") for intent in result["intents"] if str(intent.get("type") or "") == primary),
                "",
            ):
                item["type"] = lane
                item["requires_tools"] = definition.requires_tools
                item["risk_level"] = definition.risk_level
                break
    return normalize_interaction_plan(result)


def response_blocks(
    plan: Mapping[str, object],
    factual_text: str,
    *,
    factual_type: str = "status",
    include_styleable_parts: bool = True,
) -> list[dict]:
    """Return ordered content blocks while preserving the factual block."""

    if factual_type not in FACT_BLOCK_TYPES:
        raise ValueError("invalid_fact_block_type")
    blocks: list[dict] = []
    if include_styleable_parts:
        for part in plan.get("reply_parts") or []:
            if str(part.get("type") or "") not in {"social_ack", "transition"}:
                continue
            text = str(part.get("text") or "")
            if text:
                blocks.append(
                    {
                        "id": "block-" + uuid.uuid4().hex,
                        "type": "persona_text",
                        "content": text,
                        "mutable": True,
                        "source": "interaction_plan",
                    },
                )
    blocks.append(
        {
            "id": "block-" + uuid.uuid4().hex,
            "type": factual_type,
            "content": str(factual_text),
            "mutable": False,
            "source": "runtime",
            "content_hash": hashlib.sha256(str(factual_text).encode("utf-8")).hexdigest(),
        },
    )
    return blocks


def persona_response_blocks(text: str, *, source: str = "assistant_model") -> list[dict]:
    """Wrap a conversational model reply without claiming it is a fact block."""

    return [
        {
            "id": "block-" + uuid.uuid4().hex,
            "type": "persona_text",
            "content": str(text),
            "mutable": True,
            "source": str(source or "assistant_model")[:40],
        },
    ]


def assemble_response(
    plan: Mapping[str, object],
    factual_text: str,
    *,
    factual_type: str = "status",
) -> tuple[list[dict], str]:
    blocks = response_blocks(plan, factual_text, factual_type=factual_type)
    return blocks, render_response_blocks(blocks)


def render_response_blocks(blocks: list[Mapping[str, object]]) -> str:
    return "\n".join(str(item.get("content") or "") for item in blocks if item.get("content"))


__all__ = [
    "FACT_BLOCK_TYPES",
    "build_interaction_plan_messages",
    "build_interaction_plan_prompt",
    "fallback_interaction_plan",
    "interaction_plan_hash",
    "mode_decision_from_interaction_plan",
    "normalize_interaction_plan",
    "parse_interaction_plan",
    "persona_response_blocks",
    "reconcile_plan_with_mode",
    "render_response_blocks",
    "response_blocks",
    "assemble_response",
]
