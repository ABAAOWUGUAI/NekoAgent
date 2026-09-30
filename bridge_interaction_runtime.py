#!/usr/bin/env python3
"""Runtime orchestration for Gate 5 Interaction Plans.

The main Bridge keeps HTTP and channel orchestration.  This module owns the
classifier compatibility bridge plus transactional plan/message persistence so
the large process file does not absorb another domain service.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from bridge_agent_modes import (
    build_mode_decision_messages,
    build_mode_decision_prompt,
    enforce_fresh_data_route,
    fallback_mode_decision,
    finalize_mode_decision,
    intent_label,
    next_session_state,
    parse_mode_decision,
)
from bridge_conversation_memory import record_conversation
from bridge_continuous_private_conversation import bind_interaction_plan_sources
from bridge_conversation_reply_runtime import remaining_timeout
from bridge_interaction_contract import (
    build_interaction_plan_messages,
    build_interaction_plan_prompt,
    fallback_interaction_plan,
    mode_decision_from_interaction_plan,
    parse_interaction_plan,
    reconcile_plan_with_mode,
)
from bridge_interaction_repository import (
    bind_plan_to_message,
    create_interaction_plan,
    interaction_plan_feature_enabled,
)
from bridge_inbound_context import (
    current_inbound_exchange_context,
    inbound_exchange_context,
)


class InteractionPersistenceRuntime:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect

    def enabled(self) -> bool:
        try:
            with self._connect() as conn:
                return interaction_plan_feature_enabled(conn)
        except sqlite3.Error:
            return False

    def ensure_plan(self, message: str, mode_decision: dict) -> dict:
        if isinstance(mode_decision.get("interaction_plan"), dict):
            return mode_decision
        result = dict(mode_decision)
        result["interaction_plan"] = fallback_interaction_plan(message, result)
        result["interaction_plan_feature_enabled"] = self.enabled()
        return result

    def persist(self, user_id: str, mode_decision: dict, *, source: str = "") -> dict | None:
        existing = mode_decision.get("interaction_plan_record")
        if isinstance(existing, dict) and existing.get("id"):
            return existing
        plan = mode_decision.get("interaction_plan")
        if not isinstance(plan, dict):
            return None
        enabled = self.enabled()
        try:
            with self._connect() as conn:
                saved = create_interaction_plan(
                    conn,
                    user_id,
                    plan,
                    request_source=source,
                    classifier_source=str(mode_decision.get("source") or "fallback"),
                )
            mode_decision["interaction_plan_record"] = saved
            return saved
        except (sqlite3.Error, ValueError):
            if enabled:
                raise
            return None

    def record_exchange(
        self,
        user_id: str,
        message: str,
        reply: str,
        mode_decision: dict,
        *,
        source: str = "",
        inbound_context: dict | None = None,
        exchange_metadata: dict | None = None,
    ) -> None:
        """Atomically record one exchange and bind its exact inbound message."""

        plan_record = mode_decision.get("interaction_plan_record") or {}
        inbound = current_inbound_exchange_context()
        inbound.update(dict(inbound_context or {}))
        metadata = {
            key: inbound.get(key)
            for key in ("reply_text_sha256", "reply_text_length")
            if inbound.get(key) not in {None, ""}
        }
        metadata.update(dict(exchange_metadata or {}))
        assistant_metadata = {
            key: value for key, value in dict(exchange_metadata or {}).items()
            if not str(key).startswith("provider_cache_")
        }
        with self._connect() as conn:
            message_id = record_conversation(
                conn, user_id, "user", message, source=source,
                external_message_id=str(inbound.get("_external_message_id") or ""),
                reply_to_external_message_id=str(inbound.get("reply_to_external_message_id") or ""),
                directed_to_assistant=bool(inbound.get("reply_to_assistant")),
                message_kind=str(
                    inbound.get("message_kind")
                    or ("attachment" if inbound.get("attachments") else "text")
                ),
                source_type_override=str(inbound.get("source_type_override") or ""),
                metadata=metadata,
                actor_ref=str(
                    inbound.get("sender_id")
                    or inbound.get("_qq_actor_id")
                    or inbound.get("user_id")
                    or ""
                ),
            )
            record_conversation(
                conn, user_id, "assistant", reply, source=source,
                metadata=assistant_metadata,
            )
            if message_id and plan_record.get("id"):
                response_cycle_id = str(inbound.get("_response_cycle_id") or "").strip()
                external_ids = [
                    str(item or "").strip()
                    for item in list(inbound.get("source_message_ids") or [])
                    if str(item or "").strip()
                ]
                primary_external_id = str(inbound.get("_external_message_id") or "").strip()
                if not external_ids and primary_external_id:
                    external_ids = [primary_external_id]
                source_message_ids: list[str] = []
                for external_id in external_ids:
                    row = conn.execute(
                        """
                        SELECT id FROM conversation_messages
                        WHERE thread_id=(SELECT thread_id FROM interaction_plans WHERE id=?)
                          AND role='user' AND external_message_id=?
                          AND inbound_sequence IS NOT NULL
                        ORDER BY inbound_sequence LIMIT 1
                        """,
                        (str(plan_record["id"]), external_id),
                    ).fetchone()
                    if row:
                        source_message_ids.append(str(row[0]))
                if response_cycle_id and source_message_ids:
                    bind_interaction_plan_sources(
                        conn,
                        plan_id=str(plan_record["id"]),
                        cycle_id=response_cycle_id,
                        source_message_ids=source_message_ids,
                        trigger_message_id=source_message_ids[-1],
                    )
                else:
                    bind_plan_to_message(
                        conn,
                        str(plan_record["id"]),
                        str(message_id),
                        status="dispatched",
                    )


class InteractionPlannerRuntime:
    def __init__(
        self,
        *,
        store: InteractionPersistenceRuntime,
        get_session: Callable[[str], dict | None],
        get_model_settings: Callable[[str, dict], dict],
        call_openai: Callable[..., dict],
        call_codex: Callable[..., dict],
        default_cwd: Callable[[], object],
        record_model_call: Callable[..., None],
        save_session: Callable[[dict], dict | None],
    ):
        self._store = store
        self._get_session = get_session
        self._get_model_settings = get_model_settings
        self._call_openai = call_openai
        self._call_codex = call_codex
        self._default_cwd = default_cwd
        self._record_model_call = record_model_call
        self._save_session = save_session

    def decide(
        self,
        *,
        user_id: str,
        message: str,
        settings: dict,
        policy: dict,
        history: list[dict],
        timeout: int,
        allow_classifier: bool = True,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[dict, dict | None]:
        previous_session = self._get_session(user_id)
        fallback = fallback_mode_decision(message, previous_session, policy)
        fallback["message"] = message
        decision = dict(fallback)
        daily_bypass_intent = ""
        interaction_enabled = self._store.enabled()
        plan = fallback_interaction_plan(message, fallback)
        if allow_classifier is False:
            fallback_intent = str(fallback.get("intent") or "")
            if (
                fallback_intent in {"chat", "analysis"}
                and fallback.get("need_tools") is False
            ):
                daily_bypass_intent = fallback_intent
                decision.update({
                    "mode": "daily",
                    "intent": fallback_intent,
                    "confidence": max(float(fallback.get("confidence") or 0), 0.95),
                    "reason": "已通过私聊多模态无效果边界，使用日常对话路径。",
                    "work_lifecycle": "none",
                    "end_work": False,
                    "need_tools": False,
                    "execution_lane": "respond",
                    "response_style": "casual",
                    "source": "daily_private_multimodal_rule",
                })
            decision["classifier_bypassed"] = True
            decision["classifier_bypass_reason"] = "daily_private_multimodal"
        elif policy.get("mode_autodetect"):
            classifier_settings = self._get_model_settings("interaction_classifier", settings)
            provider = str(classifier_settings.get("chat_provider") or "codex")
            requested_timeout = (
                max(10, min(int(timeout or 30), 60))
                if provider == "openai-compatible"
                else max(20, min(int(timeout or 60), 120))
            )
            attempt_timeout = remaining_timeout(
                requested_timeout,
                deadline_monotonic,
                clock,
            )
            if deadline_monotonic is not None and attempt_timeout <= 0:
                decision = dict(fallback)
                decision["source"] = "fallback"
                decision["classifier_provider"] = provider
                decision["classifier_ok"] = False
                decision["classifier_error"] = "classifier_deadline_exhausted"
            else:
                try:
                    if provider == "openai-compatible":
                        classifier_settings = dict(classifier_settings)
                        classifier_settings["chat_temperature"] = "0"
                        # Reasoning-capable providers may spend part of this budget
                        # before emitting the strict JSON object.  900 produced an
                        # empty final response in production; 2048 is still bounded
                        # while leaving room for the validated plan contract.
                        classifier_settings["chat_max_tokens"] = "2048" if interaction_enabled else "512"
                        messages = (
                            build_interaction_plan_messages(
                                classifier_settings,
                                user_id,
                                message,
                                history,
                                previous_session,
                                policy,
                            )
                            if interaction_enabled
                            else build_mode_decision_messages(
                                classifier_settings,
                                user_id,
                                message,
                                history,
                                previous_session,
                                policy,
                            )
                        )
                        model_result = self._call_openai(
                            classifier_settings,
                            messages,
                            timeout=attempt_timeout,
                        )
                    else:
                        prompt = (
                            build_interaction_plan_prompt(
                                classifier_settings,
                                user_id,
                                message,
                                history,
                                previous_session,
                                policy,
                            )
                            if interaction_enabled
                            else build_mode_decision_prompt(
                                classifier_settings,
                                user_id,
                                message,
                                history,
                                previous_session,
                                policy,
                            )
                        )
                        model_result = self._call_codex(
                            prompt,
                            cwd=self._default_cwd(),
                            timeout=attempt_timeout,
                            settings_override=classifier_settings,
                        )
                    self._record_model_call(
                        classifier_settings,
                        model_result,
                        source="mode_classifier",
                        user_id=user_id,
                    )
                    raw = (model_result.get("reply") or model_result.get("output") or "").strip()
                    if interaction_enabled:
                        plan, plan_error = parse_interaction_plan(raw, plan)
                        decision = mode_decision_from_interaction_plan(plan, fallback)
                        if plan_error:
                            decision["classifier_error"] = plan_error
                            decision["source"] = "fallback"
                    else:
                        decision = parse_mode_decision(raw, fallback)
                    decision["classifier_provider"] = str(model_result.get("provider") or provider)
                    decision["classifier_ok"] = bool(model_result.get("ok"))
                    if model_result.get("ok") is False:
                        decision["source"] = "fallback"
                        decision["classifier_error"] = (
                            model_result.get("error")
                            or model_result.get("error_kind")
                            or "classifier_failed"
                        )
                except Exception as exc:
                    decision = dict(fallback)
                    decision["source"] = "fallback"
                    decision["classifier_error"] = str(exc)
        decision["message"] = message
        decision = enforce_fresh_data_route(decision, message, history)
        decision["message"] = message
        decision = finalize_mode_decision(decision, previous_session, policy)
        if daily_bypass_intent and not decision.get("fresh_data_required"):
            decision.update({
                "mode": "daily",
                "intent": daily_bypass_intent,
                "intent_label": intent_label(daily_bypass_intent),
                "work_lifecycle": "none",
                "end_work": False,
                "need_tools": False,
                "execution_lane": "respond",
                "response_style": "casual",
            })
        plan = (
            reconcile_plan_with_mode(plan, decision)
            if interaction_enabled
            else fallback_interaction_plan(message, decision)
        )
        decision["interaction_plan"] = plan
        decision["interaction_plan_feature_enabled"] = interaction_enabled
        session = next_session_state(user_id, decision, previous_session, policy)
        saved_session = self._save_session(session)
        decision["session"] = saved_session or session
        decision["previous_session"] = previous_session
        return decision, saved_session


__all__ = [
    "InteractionPersistenceRuntime",
    "InteractionPlannerRuntime",
    "current_inbound_exchange_context",
    "inbound_exchange_context",
]
