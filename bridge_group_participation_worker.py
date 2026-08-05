"""Durable natural-group participation worker.

The worker owns scheduling only. Conversation participation, model selection,
and delivery remain injected from the existing Bridge services so there is one
decision and one Outbox path for private and group conversations.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from bridge_conversation_reply_runtime import call_openai_with_empty_retry
from bridge_conversation_participation_contract import GroupParticipationMode, group_mode_from_legacy
from bridge_group_participation_queue import (
    claim_due_group_candidates,
    finish_group_candidate,
    group_candidate_is_current,
    reschedule_group_candidate,
)
from bridge_group_participation_policy import (
    apply_natural_participation_floor,
    natural_group_participation_enabled,
    natural_group_preflight,
)
from bridge_group_context_frame import (
    DEFAULT_GROUP_CONTEXT_LIMIT,
    build_group_conversation_frame,
    group_model_history,
)
from bridge_visual_context import append_visual_history, visual_context_lines, visual_scope, with_visual_group_current
from bridge_social_opportunity import (
    add_topic_candidate,
    create_opportunity,
    decide_opportunity,
    social_opportunity_enabled,
)
from bridge_social_engine import STRUCTURED_SOCIAL_DECISION_MAX_TOKENS


BRIDGE_SERVICE_NAMES = {
    "db_connect": "_assistant_db_connect",
    "group_access": "qq_group_access",
    "get_group_policy": "get_group_policy",
    "group_context": "group_context",
    "assistant_settings": "_assistant_settings",
    "settings_for_model_role": "_settings_for_model_role",
    "build_group_decision_messages": "build_group_decision_messages",
    "call_openai": "_call_openai_compatible_chat",
    "run_codex": "_run_codex_assistant_chat",
    "default_cwd": "_default_cwd",
    "record_model_call": "_record_model_call",
    "parse_group_decision": "parse_group_decision",
    "apply_group_turn_policy": "apply_group_turn_policy",
    "participation_confidence_floor": "group_participation_confidence_floor",
    "mark_group_decision": "mark_group_decision",
    "transition_participation": "transition_group_participation",
    "assistant_chat": "_assistant_chat",
    "agent_policy": "_agent_policy",
    "dispatch_response": "dispatch_qq_response",
    "outbox": "_phase2_outbox",
    "continuity_kernel": "CONTINUITY_KERNEL",
    "complete_group_dispatch": "complete_group_dispatch",
}


def _store_group_engagement_metadata(
    conn: Any,
    *,
    decision_id: str,
    decision: dict,
    threshold: float,
    social_decision: dict | None = None,
) -> None:
    """Persist bounded server diagnostics without retaining model prose."""

    if not decision_id:
        return
    stored = conn.execute(
        "SELECT decision_json FROM engagement_decisions WHERE id=?", (decision_id,),
    ).fetchone()
    if not stored:
        return
    try:
        payload = json.loads(str(stored[0] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if social_decision is not None:
        payload["social_opportunity"] = social_decision
    payload["group_engagement"] = {
        "reason_code": str(decision.get("reason") or ""),
        "confidence": float(decision.get("confidence") or 0),
        "classifier_provider": str(decision.get("classifier_provider") or ""),
        "classifier_ok": bool(decision.get("classifier_ok")),
        "threshold": float(threshold),
        "turn_policy": decision.get("turn_policy") if isinstance(decision.get("turn_policy"), dict) else {},
        "social_action": str(decision.get("social_action") or "silent"),
        "participation_floor_applied": bool(decision.get("participation_floor_applied")),
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if social_decision is None:
        conn.execute(
            "UPDATE engagement_decisions SET decision_json=? WHERE id=?",
            (serialized, decision_id),
        )
        return
    final_action = "contextual_participation" if decision.get("should_reply") else "silent"
    conn.execute(
        """UPDATE engagement_decisions
           SET action=?,reason_code=?,confidence=?,decision_json=? WHERE id=?""",
        (
            final_action,
            str(decision.get("reason") or "")[:120],
            float(decision.get("confidence") or 0),
            serialized,
            decision_id,
        ),
    )


def process_group_participation_queue(services: dict[str, Any]) -> None:
    """Evaluate quiet-gap candidates and enqueue one durable QQ reply."""

    services = {
        name: services[source_name]
        for name, source_name in BRIDGE_SERVICE_NAMES.items()
    }
    db_connect: Callable[[], Any] = services["db_connect"]
    with db_connect() as conn:
        candidates = claim_due_group_candidates(conn, limit=3)
    for candidate in candidates:
        group_id = str(candidate.get("group_id") or "")
        latest_message_id = int(candidate.get("latest_message_id") or 0)
        candidate_revision = int(candidate.get("candidate_revision") or 0)
        anchor_message_id = int(candidate.get("anchor_message_id") or 0)
        decision_id = ""

        def is_current(conn: Any) -> bool:
            return group_candidate_is_current(
                conn, group_id, latest_message_id=latest_message_id,
                candidate_revision=candidate_revision,
            )

        def supersede(conn: Any) -> None:
            services["transition_participation"](
                conn, decision_id=decision_id, stage="superseded", action="silent",
                reason_code="candidate_superseded",
            )

        def fence(conn: Any) -> bool:
            if is_current(conn):
                return True
            supersede(conn)
            return False

        try:
            access = services["group_access"](
                db_connect,
                str(candidate.get("latest_sender_id") or ""),
                group_id,
            )
            if not access.get("allowed"):
                with db_connect() as conn:
                    if fence(conn):
                        finish_group_candidate(
                            conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                continue
            with db_connect() as conn:
                policy = services["get_group_policy"](conn, group_id) or {}
                if (
                    not natural_group_participation_enabled(conn)
                    or group_mode_from_legacy(policy) is not GroupParticipationMode.NATURAL_PARTICIPATION
                ):
                    if fence(conn):
                        finish_group_candidate(
                            conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                    continue
                latest = conn.execute(
                    "SELECT * FROM group_messages WHERE id=? AND group_id=?",
                    (int(candidate.get("latest_message_id") or 0), group_id),
                ).fetchone()
                anchor = conn.execute(
                    "SELECT * FROM group_messages WHERE id=? AND group_id=?",
                    (anchor_message_id, group_id),
                ).fetchone() if anchor_message_id else None
                context_items = services["group_context"](
                    conn, group_id, int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
                )
            if not latest:
                with db_connect() as conn:
                    if fence(conn):
                        finish_group_candidate(
                            conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                continue
            latest = dict(latest)
            decision_id = str(latest.get("engagement_decision_id") or "")
            if not anchor:
                with db_connect() as conn:
                    if fence(conn):
                        services["mark_group_decision"](
                            conn, message_id=int(latest["id"]), group_id=group_id,
                            decision={"should_reply": False, "reason": "no_concrete_anchor"}, replied=False,
                        )
                        supersede_reason = "no_concrete_anchor"
                        services["transition_participation"](
                            conn, decision_id=decision_id, stage="preflight_blocked", action="silent",
                            reason_code=supersede_reason,
                        )
                        finish_group_candidate(
                            conn, group_id, latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                continue
            anchor = dict(anchor)
            # The latest candidate must remain available to this one worker
            # decision even when its transient retention window has elapsed
            # before the quiet-gap claim (for example after delayed delivery
            # or clock skew).  This is an in-memory supplement only; durable
            # context reads keep their retention and redaction rules.
            anchor_id = int(anchor.get("id") or 0)
            if anchor_id and not any(
                int(item.get("id") or 0) == anchor_id for item in context_items
            ):
                context_items.append(anchor)
            model_context_items = [
                item for item in context_items if int(item.get("id") or 0) < anchor_id
            ] + [anchor]
            turn_history = model_context_items
            conversation_frame = build_group_conversation_frame(
                context_items, anchor,
                context_limit=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
                continuation_window_seconds=int(policy.get("continuation_window_seconds") or 120),
            )
            with db_connect() as conn:
                guard = natural_group_preflight(
                    conn,
                    policy=policy,
                    group_id=group_id,
                    current=anchor,
                    conversation_frame=conversation_frame,
                    candidate_kind=(
                        "continuation" if conversation_frame.get("active_continuation") else "ambient"
                    ),
                )
                if guard:
                    if fence(conn):
                        services["mark_group_decision"](
                            conn, message_id=int(anchor["id"]), group_id=group_id,
                            decision=guard, replied=False,
                        )
                        services["transition_participation"](
                            conn, decision_id=decision_id, stage="preflight_blocked", action="silent",
                            reason_code=str(guard.get("reason") or "preflight_blocked"),
                        )
                        finish_group_candidate(
                            conn, group_id, latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                    continue
            social_context = None
            with db_connect() as conn:
                if social_opportunity_enabled(conn):
                    assistant_row = conn.execute(
                        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
                    ).fetchone()
                    if not assistant_row:
                        raise ValueError("active_assistant_missing")
                    relationship_row = conn.execute(
                        """SELECT version FROM relationship_states
                           WHERE assistant_id=? AND user_id=? AND scope_type='qq_group' AND scope_id=?
                           ORDER BY updated_at DESC LIMIT 1""",
                        (str(assistant_row[0]), str(anchor.get("sender_id") or ""), group_id),
                    ).fetchone()
                    engagement_id = decision_id
                    existing = conn.execute(
                        "SELECT * FROM social_opportunities WHERE id=?",
                        (f"decision-{engagement_id}",),
                    ).fetchone() if engagement_id else None
                    opportunity = dict(existing) if existing else create_opportunity(
                        conn, assistant_id=str(assistant_row[0]), kind="join",
                        subject_type="qq_group", subject_id=group_id,
                        thread_id=f"qq:group:{group_id}", trigger_type="active_group_topic",
                        trigger_ref=str(anchor.get("external_message_id") or anchor.get("id") or ""),
                        policy_snapshot={
                            "participation_mode": policy.get("participation_mode"),
                            "quiet_gap_seconds": policy.get("quiet_gap_seconds"),
                            "reply_probability": policy.get("reply_probability"),
                        },
                        relationship_version=int(relationship_row[0]) if relationship_row else 0,
                        opportunity_id=f"join-{group_id}-{anchor.get('id')}",
                    )
                    topic = add_topic_candidate(conn, opportunity["id"], {
                        "source_type": "conversation",
                        "source_id": str(anchor.get("external_message_id") or anchor.get("id") or ""),
                        "scope_type": "qq_group", "scope_id": group_id,
                        "summary": str(conversation_frame.get("topic_summary") or "")[:800] or "当前群聊消息",
                        "freshness": str(latest.get("created_at") or ""),
                        "why_relevant": (
                            "助手正在参与且同一成员自然续接"
                            if conversation_frame.get("active_continuation")
                            else "当前准入群正在进行的同一话题"
                        ),
                        "risk": "low",
                    })
                    social_context = {"opportunity": opportunity, "topic": topic}
            fallback_settings = services["assistant_settings"](include_secrets=True)
            visual_event_id = str(
                anchor.get("external_message_id")
                or candidate.get("latest_external_message_id")
                or anchor.get("id")
                or ""
            )
            transient_visual_context = visual_context_lines(
                visual_scope(channel="qq_group", thread_id=group_id),
                visual_event_id,
            )
            anchor = with_visual_group_current(anchor, transient_visual_context)
            classifier_settings = services["settings_for_model_role"](
                "conversation_engagement", fallback_settings,
            )
            decision_messages = services["build_group_decision_messages"](
                policy, model_context_items, anchor, conversation_frame,
            )
            if social_context:
                social_packet = (
                    "SocialOpportunity 已启用。若选择非 silent，只能选择下面的 candidate id，"
                    "并返回 why_now、approach(light_join|continue|share|ask|inform) 与 "
                    "meme_intent(none|optional|strong)；缺失或虚构即静默。\n"
                    + json.dumps(social_context["topic"], ensure_ascii=False)
                )
                # Keep dynamic opportunity data in the final user turn. A late
                # system message broke role semantics and cache prefix reuse.
                decision_messages[-1] = {
                    **decision_messages[-1],
                    "content": str(decision_messages[-1].get("content") or "")
                    + "\n\n" + social_packet,
                }
            provider = str(classifier_settings.get("chat_provider") or "codex")
            if provider == "openai-compatible":
                classifier_settings = dict(classifier_settings)
                # JSON remains schema-checked server-side.  A small non-zero
                # value avoids deterministic over-selection of silence while
                # keeping the engagement decision reproducible enough to audit.
                classifier_settings["chat_temperature"] = "0.2"
                classifier_settings["chat_max_tokens"] = str(
                    STRUCTURED_SOCIAL_DECISION_MAX_TOKENS,
                )
                classifier_result = call_openai_with_empty_retry(
                    classifier_settings,
                    decision_messages,
                    timeout=60,
                    user_id=f"group:{group_id}",
                    call_model=services["call_openai"],
                    record_model=services["record_model_call"],
                    empty_source="group_engagement_empty_initial",
                    retry_instruction="输出协议：必须输出非空 JSON 决策；不要只生成思考过程。",
                )
            else:
                classifier_result = services["run_codex"](
                    "\n\n".join(item["content"] for item in decision_messages),
                    cwd=services["default_cwd"](), timeout=90,
                    settings_override=classifier_settings,
                )
            services["record_model_call"](
                classifier_settings, classifier_result,
                source="group_engagement", user_id=f"group:{group_id}",
            )
            if not classifier_result.get("ok"):
                raise RuntimeError(
                    str(classifier_result.get("error") or "group_classifier_failed"),
                )
            with db_connect() as conn:
                if not fence(conn):
                    continue
            decision = services["parse_group_decision"](
                classifier_result.get("reply") or classifier_result.get("output") or "",
                is_mention=False,
                expected_anchor_message_id=int(anchor["id"]),
            )
            decision = services["apply_group_turn_policy"](
                policy, turn_history, anchor, decision, conversation_frame,
            )
            decision["classifier_ok"] = True
            decision["classifier_provider"] = classifier_result.get("provider") or provider
            threshold = services["participation_confidence_floor"](policy)
            if decision.get("should_reply") and float(decision.get("confidence") or 0) < threshold:
                decision.update({"should_reply": False, "reason": "engagement_below_threshold"})
            with db_connect() as conn:
                if not fence(conn):
                    continue
                decision = apply_natural_participation_floor(
                    conn,
                    policy=policy,
                    group_id=group_id,
                    anchor=anchor,
                    decision=decision,
                    conversation_frame=conversation_frame,
                    current_decision_id=decision_id,
                )
            social_decision = None
            with db_connect() as conn:
                if not fence(conn):
                    continue
                if social_context:
                    try:
                        social_decision = decide_opportunity(conn, social_context["opportunity"]["id"], {
                            "action": "reply" if decision.get("should_reply") else "silent",
                            "reason_code": decision.get("reason"), "why_now": decision.get("why_now"),
                            "topic_candidate_id": decision.get("topic_candidate_id"),
                            "approach": decision.get("approach"), "confidence": decision.get("confidence"),
                            "meme_intent": decision.get("meme_intent"),
                        })
                    except ValueError:
                        decision.update({"should_reply": False, "reason": "invalid_model_social_contract"})
                        social_decision = decide_opportunity(
                            conn, social_context["opportunity"]["id"],
                            {"action": "silent", "reason_code": "invalid_model_social_contract"},
                        )
                    decision["social_opportunity"] = social_decision
                _store_group_engagement_metadata(
                    conn,
                    decision_id=decision_id,
                    decision=decision,
                    threshold=threshold,
                    social_decision=social_decision,
                )
            if not decision.get("should_reply"):
                with db_connect() as conn:
                    if not fence(conn):
                        continue
                    services["mark_group_decision"](
                        conn, message_id=int(anchor["id"]), group_id=group_id,
                        decision=decision, replied=False,
                    )
                    services["transition_participation"](
                        conn,
                        decision_id=decision_id,
                        stage="model_declined",
                        action="silent",
                        reason_code=str(decision.get("reason") or "model_engagement_declined"),
                        model_role="conversation_engagement",
                        model_id=str(classifier_settings.get("chat_model") or ""),
                        confidence=float(decision.get("confidence") or 0),
                    )
                    finish_group_candidate(
                        conn, group_id, latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                continue
            with db_connect() as conn:
                if not fence(conn):
                    continue
            message = str(anchor.get("content") or "").strip()
            history = group_model_history(
                turn_history[:-1],
                limit=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
            )
            history = append_visual_history(history, transient_visual_context)
            chat_settings = dict(fallback_settings)
            if not int(policy.get("meme_enabled") or 0):
                chat_settings["meme_daily_enabled"] = "0"
            transport = {
                "group_id": group_id,
                "sender_id": candidate.get("latest_sender_id") or "",
                "session": candidate.get("latest_session") or "",
                "is_mention": False,
                "_external_message_id": candidate.get("latest_external_message_id") or f"natural:{anchor['id']}",
                "trace_id": f"natural-group:{group_id}:{anchor['id']}",
                "engagement_decision_id": decision_id,
            }

            def _execute_natural_turn(_turn_id: str) -> dict:
                result = services["assistant_chat"](
                    user_id=f"group:{group_id}", message=message, timeout=90,
                    decision_context={
                        "history": history,
                        "settings": chat_settings,
                        "policy": services["agent_policy"](chat_settings),
                        "mode_decision": decision,
                        "source": "qq_group_natural",
                        "raw_message": message,
                        "display_message": f"{candidate.get('latest_sender_name') or '成员'}: {message}",
                        "group": {
                            "group_id": group_id,
                            "group_name": policy.get("group_name") or "",
                            "sender_id": candidate.get("latest_sender_id") or "",
                            "sender_name": candidate.get("latest_sender_name") or "",
                            "topic_anchor": {
                                "id": int(anchor.get("id") or 0),
                                "content": message,
                            },
                            "allow_group_feedback": False,
                        },
                        "conversation_frame": conversation_frame,
                    },
                )
                result = {
                    **result,
                    "dispatch": str(result.get("dispatch") or "chat"),
                    "should_reply": bool(result.get("ok") and result.get("reply")),
                    "group_decision": decision,
                    "engagement_decision_id": transport["engagement_decision_id"],
                }
                with db_connect() as conn:
                    if not fence(conn):
                        raise RuntimeError("candidate_superseded")
                queued = services["dispatch_response"](
                    services["outbox"](), lambda: result, transport,
                    scope="group", enabled=True,
                )
                if not queued.get("delivery_queued"):
                    raise RuntimeError(
                        str(queued.get("error") or "natural_group_delivery_not_queued"),
                    )
                return queued

            queued = services["continuity_kernel"].execute_turn(
                {
                    "user_id": f"group:{group_id}",
                    "message": message,
                    "trace_id": transport["trace_id"],
                    "inbound_context": {
                        "group_id": group_id,
                        "sender_id": transport["sender_id"],
                        "_external_message_id": transport["_external_message_id"],
                    },
                },
                _execute_natural_turn,
            )
            services["continuity_kernel"].bind_delivery(queued)
            if not queued.get("delivery_queued"):
                raise RuntimeError(
                    str(queued.get("error") or "natural_group_delivery_not_queued"),
                )
            with db_connect() as conn:
                if not fence(conn):
                    continue
                services["transition_participation"](
                    conn,
                    decision_id=decision_id,
                    stage="delivery_queued",
                    action="contextual_participation",
                    reason_code="model_engagement_approved",
                    model_role="conversation_engagement",
                    model_id=str(classifier_settings.get("chat_model") or ""),
                    confidence=float(decision.get("confidence") or 0),
                )
                services["complete_group_dispatch"](
                    conn, event=None, deterministic_decision=None,
                    decision=decision, group_id=group_id, payload=transport,
                    classifier_settings=classifier_settings, current=anchor,
                    result=queued,
                    assistant_name=str(fallback_settings.get("display_name") or "助手"),
                    conversation_frame=conversation_frame,
                )
                finish_group_candidate(
                    conn, group_id, latest_message_id=latest_message_id,
                    candidate_revision=candidate_revision,
                )
        except Exception as exc:
            error_text = str(exc)
            if error_text == "candidate_superseded":
                continue
            if (
                isinstance(exc, RuntimeError)
                and error_text != "natural_group_delivery_not_queued"
            ):
                failure_reason = "group_classifier_failed"
            elif error_text == "natural_group_delivery_not_queued":
                failure_reason = "group_delivery_not_queued"
            else:
                failure_reason = "group_participation_worker_failed"
            with db_connect() as conn:
                if not fence(conn):
                    continue
                if int(candidate.get("attempt") or 0) < 3:
                    reschedule_group_candidate(
                        conn,
                        group_id,
                        seconds=15,
                        latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                else:
                    finish_group_candidate(
                        conn, group_id, state="failed", latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                    if latest_message_id:
                        services["mark_group_decision"](
                            conn,
                            message_id=latest_message_id,
                            group_id=group_id,
                            decision={"should_reply": False, "reason": failure_reason},
                            replied=False,
                        )
                        services["transition_participation"](
                            conn,
                            decision_id=decision_id,
                            stage="delivery_failed",
                            action="silent",
                            reason_code=failure_reason,
                        )
            print(
                "natural_group_candidate_failed "
                f"error={type(exc).__name__} reason={failure_reason} "
                f"attempt={int(candidate.get('attempt') or 0)}",
                flush=True,
            )


__all__ = ["process_group_participation_queue"]
