#!/usr/bin/env python3
"""Context assembly helpers for the legacy assistant-chat orchestrator."""

from __future__ import annotations

import sqlite3
from typing import Callable

from bridge_knowledge_service import search_published
from bridge_interaction_contract import persona_response_blocks
from bridge_group_context_frame import group_expression_rhythm
from bridge_migrations import MigrationError
from bridge_relationship_service import get_relationship_state
from bridge_social_engine import build_voice_contract, normalize_social_cues, plan_expression
from bridge_social_experience import hydrate_expression_context


def merge_shared_knowledge(
    db_connect: Callable[[], sqlite3.Connection],
    memories: list[dict],
    *,
    message: str,
    group: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Add only published, channel-compatible knowledge to retrieval context."""

    try:
        with db_connect() as conn:
            shared = search_published(
                conn,
                message,
                channel="group" if group else "private",
                limit=5,
            )
    except (sqlite3.Error, ValueError):
        return list(memories), []
    enriched = list(memories) + [
        {"content": f"[共享知识] {item['title']}：{item['content']}", "kind": "shared_knowledge"}
        for item in shared
    ]
    return enriched, shared


def build_social_context(
    db_connect: Callable[[], sqlite3.Connection],
    history: list[dict] | None = None,
    *,
    settings: dict,
    mode_decision: dict,
    message: str,
    user_id: str,
    group: dict | None = None,
) -> tuple[dict, dict]:
    """Build the voice contract, turn expression plan, and scoped learned habits."""

    group_info = dict(group or {})
    if group_info:
        group_info["expression_rhythm"] = group_expression_rhythm(history or [])
        group = group_info
    cues = normalize_social_cues(mode_decision, message)
    contract = build_voice_contract(
        settings,
        mode_decision=mode_decision,
        group_context=group,
    )
    relationship = {
        "applied": False,
        "version": 0,
        "preferred_address": "",
        "interaction_style": "natural",
        "familiarity_context": "new",
        "allowed_topics": [],
        "blocked_topics": [],
    }
    relationship_subject = str(group_info.get("sender_id") or user_id or "").strip()
    relationship_scope = "qq_group" if group_info else "private_user"
    relationship_scope_id = str(group_info.get("group_id") or "").strip() if group_info else ""
    try:
        with db_connect() as conn:
            loaded = get_relationship_state(
                conn,
                user_id=relationship_subject,
                scope_type=relationship_scope,
                scope_id=relationship_scope_id,
            )
        relationship.update({
            "applied": bool(loaded.get("id")) and bool(contract.get("optional_persona_applied", True)),
            "version": int(loaded.get("version") or 0),
            "preferred_address": str(loaded.get("preferred_address") or "")[:80],
            "interaction_style": str(loaded.get("interaction_style") or "natural")[:30],
            "familiarity_context": str(loaded.get("familiarity_context") or "new")[:30],
            "allowed_topics": list(loaded.get("allowed_topics") or [])[:20],
            "blocked_topics": list(loaded.get("blocked_topics") or [])[:20],
        })
    except (sqlite3.Error, MigrationError, ValueError):
        # Relationship is optional context. Schema drift or a transient DB
        # failure must not turn a factual chat response into a 500.
        relationship["degraded"] = True
    context = {
        "cues": cues,
        "group": group,
        "voice_contract": contract,
        "expression_plan": plan_expression(
            message,
            social_cues=cues,
            mode_decision=mode_decision,
            group_context=group,
            voice_contract=contract,
        ),
        "relationship": relationship,
        "context_blocks": {
            "assistant_identity": {
                "source_type": "persona_version",
                "source_id": str(contract.get("persona_version") or ""),
                "version": str(contract.get("persona_version") or ""),
                "budget": 2400,
                "applied": bool(contract.get("optional_persona_applied", True)),
            },
            "relationship": {
                "source_type": "relationship_state",
                "source_id": relationship_scope_id or relationship_subject,
                "version": relationship.get("version", 0),
                "budget": 800,
                "applied": bool(relationship.get("applied")),
                "value": relationship,
            },
        },
    }
    if contract.get("optional_persona_applied", True):
        context.update(hydrate_expression_context(
            db_connect,
            message=message,
            social_cues=cues,
            user_id=user_id,
            group=group,
            allow_group_feedback=bool(group_info.get("allow_group_feedback")),
        ))
    else:
        context.update({"learned_feedback": None, "habits": []})
    context["context_blocks"]["expression"] = {
        "source_type": "expression_habits",
        "source_id": relationship_scope_id or relationship_subject,
        "version": "",
        "budget": 1200,
        "applied": bool(context.get("habits")),
    }
    return cues, context


def social_result(
    social_cues: dict,
    social_context: dict,
    *,
    runtime_role: str,
    group: dict | None = None,
) -> dict:
    """Expose safe expression metadata without prompts or private habit text."""

    return {
        **social_cues,
        "runtime_role": runtime_role,
        "expression_habits": [item.get("id") for item in social_context.get("habits") or []],
        "expression_plan": social_context.get("expression_plan"),
        "learned_feedback": bool(social_context.get("learned_feedback")),
        "persona_level": social_context.get("voice_contract", {}).get("persona_level"),
        "persona_version": social_context.get("voice_contract", {}).get("persona_version") or "",
        "relationship_applied": bool(social_context.get("relationship", {}).get("applied")),
        "relationship_version": int(social_context.get("relationship", {}).get("version") or 0),
        "group": bool(group),
    }


def attach_chat_result(
    result: dict,
    reply: str,
    meme: dict | None,
    attachment: dict,
    intent: str,
    intent_label: str,
    mode_decision: dict,
    mode_session: dict | None,
    social: dict,
    criteria: list,
    quality: dict,
    quality_event: dict | None,
    memories: list,
    saved_memories: list,
    memory_candidates: list,
    settings: dict,
    project: dict | None,
) -> None:
    """Attach the stable public metadata contract to a chat provider result."""

    result.update({
        "reply": reply,
        "content_blocks": persona_response_blocks(reply),
        "meme": meme,
        "attachment": attachment,
        "intent": intent,
        "intent_label": intent_label,
        "mode": mode_decision.get("mode"),
        "mode_label": mode_decision.get("mode_label"),
        "mode_decision": mode_decision,
        "mode_session": mode_session,
        "social": social,
        "acceptance_criteria": criteria,
        "quality": quality,
        "quality_event": quality_event,
        "memories": memories,
        "saved_memories": saved_memories,
        "memory_candidates": memory_candidates,
        "settings": settings,
        "project": project,
    })


def summarize_prompt(prompt: str, limit: int = 80) -> str:
    text = " ".join(str(prompt or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
