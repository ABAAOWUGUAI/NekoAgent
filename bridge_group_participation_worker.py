"""Durable natural-group participation worker.

The worker owns scheduling only. Conversation participation, model selection,
and delivery remain injected from the existing Bridge services so there is one
decision and one Outbox path for private and group conversations.
"""

from __future__ import annotations
from bridge_group_state import group_gate

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable

from bridge_action_truth import enforce_action_truth
from bridge_agent_modes import build_agent_policy
from bridge_conversation_reply_runtime import remaining_timeout
from bridge_conversation_participation_contract import GroupParticipationMode, group_mode_from_legacy
from bridge_group_participation_queue import (
    claim_due_group_candidates,
    finish_group_candidate,
    group_candidate_is_current,
    renew_group_candidate_lease,
    requeue_stale_claimed_candidate,
    reschedule_group_candidate,
)
from bridge_group_participation_policy import (
    apply_natural_participation_floor,
    group_active_topic_window_seconds,
    natural_group_participation_enabled,
    natural_group_preflight,
)
from bridge_group_context_frame import (
    DEFAULT_GROUP_CONTEXT_LIMIT,
    _message_kind,
    _stable_hash,
    build_group_conversation_frame,
    group_model_history,
)
from bridge_group_media_context import finish_group_visual_context
from bridge_group_response_commitment import (
    advance_group_response_situation,
    begin_group_response_phase,
    claim_group_response_commitment,
    group_response_identity,
    load_group_response_commitment_for_event,
    settle_group_response_commitment,
)
from bridge_group_single_plan import (
    GROUP_AMBIENT_ACTIVE_DEADLINE_SECONDS,
    GROUP_AMBIENT_VISUAL_WAIT_SECONDS,
    parse_group_single_plan,
    run_group_single_plan,
    repair_group_risky_reply,
)
from bridge_group_participation_windows import ambient_participation_window_decision
from bridge_group_topic_delivery import (
    build_topic_delivery_context,
    evaluate_topic_context,
    topic_fingerprint,
    topic_delivery_feature_enabled,
)
from bridge_visual_context import append_visual_history, visual_context_lines, visual_scope, with_visual_group_current
from bridge_social_opportunity import (
    add_topic_candidate,
    create_opportunity,
    decide_opportunity,
    social_opportunity_enabled,
)
from bridge_social_reply import group_reply_style_issues_for_delivery
from bridge_meme_attachment import prepare_group_meme_attachment, record_meme_funnel, manual_meme_request
from bridge_qq_quality_receipt import project_group_dispatch_delivery
from bridge_meme_selection import select_and_reserve_meme
from bridge_meme_expression import choose_meme_expression


def _policy_flag(policy: dict, name: str, default: bool = True) -> bool:
    """Read one integer/boolean participation flag from a group policy row."""

    try:
        return int(policy.get(name) or (1 if default else 0)) != 0
    except (TypeError, ValueError):
        return default


def _repeated_anchor_reply(reply: str, history: list[dict], anchor_id: int) -> bool:
    """Detect an exact self-repeat when only attachments followed an old anchor.

    Reuse the bounded confirmed message projection, not a new dedup store.
    A readable new member turn (including an intentional echo) or missing
    text evidence leaves the normal participation/quality decision intact.
    """
    later = [item for item in history if int(item.get("id") or 0) > anchor_id]
    if not later or _message_kind(later[-1]) != "attachment":
        return False
    if any(
        str(item.get("sender_id") or "") != "bot"
        and _message_kind(item) != "attachment"
        for item in later
    ):
        return False
    previous = next((
        item for item in reversed(later)
        if str(item.get("sender_id") or "") == "bot" and item.get("replied") == 1
    ), None)
    return bool(previous and str(reply or "").strip()
                and str(previous.get("content") or "").strip() == str(reply).strip())


BRIDGE_SERVICE_NAMES = {
    "db_connect": "_assistant_db_connect",
    "group_access": "qq_group_access",
    "get_group_policy": "get_group_policy",
    "group_context": "group_context",
    "assistant_settings": "_assistant_settings",
    "settings_for_model_role": "_settings_for_model_role",
    "build_group_decision_messages": "build_group_decision_messages",
    "prepare_group_single_plan": "_prepare_group_single_plan_messages",
    "call_openai": "_call_openai_compatible_chat",
    "run_codex": "_run_codex_assistant_chat",
    "default_cwd": "_default_cwd",
    "record_model_call": "_record_model_call",
    "parse_group_decision": "parse_group_decision",
    "apply_group_turn_policy": "apply_group_turn_policy",
    "participation_confidence_floor": "group_participation_confidence_floor",
    "mark_group_decision": "mark_group_decision",
    "transition_participation": "transition_group_participation",
    "dispatch_response": "dispatch_qq_response",
    "outbox": "_phase2_outbox",
    "continuity_kernel": "CONTINUITY_KERNEL",
    "complete_group_dispatch": "complete_group_dispatch",
    "maybe_group_research": "maybe_group_research",
    "capture_group_single_plan_memory_candidates": "capture_group_single_plan_memory_candidates",
}


# This is deliberately a candidate-state budget, not a general SQLite retry
# policy.  Each retry begins a fresh initial claim transaction before any model
# invocation, Outbox enqueue, dispatch, commit-finalization, or ACK boundary.
GROUP_CANDIDATE_CLAIM_LOCK_RETRY_DELAYS = (0.05, 0.10, 0.20)


def _is_sqlite_lock_error(exc: sqlite3.OperationalError) -> bool:
    detail = str(exc or "").casefold()
    return "database is locked" in detail or "database table is locked" in detail


def _claim_due_group_candidates_with_lock_budget(
    claim_connect: Callable[[], Any],
    *,
    delays: tuple[float, ...] | None = None,
) -> list[dict]:
    """Claim due candidates with a small, fresh-connection lock budget.

    `claim_due_group_candidates` is the sole retryable operation: it owns the
    queue's existing revision/state guard and executes before the worker has
    caused any external effect.  A lock exhaustion intentionally returns no
    work, leaving a pending candidate for a later automation loop instead of
    replaying a model, an Outbox mutation, or a QQ delivery.
    """

    configured_delays = GROUP_CANDIDATE_CLAIM_LOCK_RETRY_DELAYS if delays is None else delays
    retry_delays = tuple(max(0.0, float(delay)) for delay in configured_delays)
    started = time.monotonic()
    for delay in (*retry_delays, None):
        try:
            # A new connection causes the queue helper to re-read canonical
            # state before its guarded UPDATE; do not replay an old cursor.
            with claim_connect() as conn:
                claimed = claim_due_group_candidates(conn, limit=3)
            print(f"group_voice_stage stage=candidate_claim group_id=all elapsed_ms={int((time.monotonic()-started)*1000)} status=ok count={len(claimed)}", flush=True)
            return claimed
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_lock_error(exc):
                raise
            if delay is None:
                print(f"group_voice_stage stage=candidate_claim group_id=all elapsed_ms={int((time.monotonic()-started)*1000)} status=sqlite_busy", flush=True)
                print(
                    "natural_group_candidate_claim_deferred "
                    "error=sqlite_locked",
                    flush=True,
                )
                return []
            time.sleep(delay)
    return []


def _transient_anchor_expired(anchor: dict) -> bool:
    """Keep expired transient body data out of a delayed model invocation."""

    if str(anchor.get("retention_class") or "") != "transient":
        return False
    try:
        expiry = datetime.fromisoformat(str(anchor.get("expires_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc)


def _group_message_event_id(message: dict) -> str:
    try:
        metadata = json.loads(str(message.get("metadata_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("event_id") or "").strip()


def _advance_ambient_response_commitment(
    conn: sqlite3.Connection,
    *,
    response_commitment: dict,
    event_id: str,
    group_id: str,
    source_message_id: str,
    decision_id: str,
    latest_message_id: int,
    fresh_source_set_hash: str,
    situation_revision: str,
) -> dict:
    """Advance the situation without rebinding its immutable ingress source.

    The bounded group history may gain a just-committed neighbor or lose an
    expired body while a candidate waits.  The event/group/source identity
    remains fixed; only the situation revision may follow the fresh frame.
    """

    if any((
        str(response_commitment.get("event_id") or "") != str(event_id or ""),
        str(response_commitment.get("group_id") or "") != str(group_id or ""),
        str(response_commitment.get("source_message_id") or "") != str(source_message_id or ""),
        str(response_commitment.get("owner_kind") or "") != "ambient",
    )):
        raise ValueError("group_response_commitment_binding_conflict")
    if str(response_commitment.get("state") or "") not in {"owned", "planned"}:
        raise ValueError("group_response_commitment_not_active")
    ingress_hash = str(response_commitment.get("source_set_hash") or "")
    if str(fresh_source_set_hash or "") != ingress_hash:
        # A newer visible neighbor is harmless, but an originally bound body
        # that was redacted or expired must never be brought back into a plan.
        row = conn.execute(
            "SELECT decision_json FROM engagement_decisions WHERE id=?",
            (decision_id,),
        ).fetchone()
        try:
            saved = json.loads(str(row[0] or "{}")) if row else {}
        except (TypeError, ValueError):
            saved = {}
        frame = saved.get("group_conversation_frame") if isinstance(saved, dict) else None
        frame = frame if isinstance(frame, dict) else {}
        source_ids = frame.get("source_message_ids") or []
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or len(source_ids) > 81
            or any(not isinstance(source_id, str) or not source_id for source_id in source_ids)
            or len(set(source_ids)) != len(source_ids)
            or source_ids[-1] != source_message_id
            or str(frame.get("source_set_hash") or "") != ingress_hash
            or _stable_hash(source_ids) != ingress_hash
        ):
            raise ValueError("group_response_ingress_frame_unavailable")
        placeholders = ",".join("?" for _ in source_ids)
        readable = {
            str(item[0])
            for item in conn.execute(
                f"""SELECT external_message_id FROM group_messages
                    WHERE group_id=? AND id<=? AND external_message_id IN ({placeholders})
                      AND content<>'' AND retention_class<>'metadata_only'
                      AND (expires_at='' OR expires_at>?)""",
                (
                    group_id, int(latest_message_id), *source_ids,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        }
        if not set(source_ids).issubset(readable):
            raise ValueError("group_response_original_source_unavailable")
    return advance_group_response_situation(
        conn,
        commitment_id=str(response_commitment["id"]),
        owner_kind="ambient",
        source_set_hash=ingress_hash,
        situation_revision=situation_revision,
    )


def _group_response_commitment_schema_active(conn: Any) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='group_response_commitments'",
        ).fetchone())
    except AttributeError:
        return False


def _store_group_engagement_metadata(
    conn: Any,
    *,
    decision_id: str,
    decision: dict,
    threshold: float,
    social_decision: dict | None = None,
    current_topic_fingerprint: str = "",
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
        "opportunity_diagnostic": decision.get("opportunity_diagnostic") if isinstance(decision.get("opportunity_diagnostic"), dict) else {},
        # A digest scopes short-term model coalescing to the same topic while
        # keeping the engagement record body-free.
        "topic_fingerprint": str(current_topic_fingerprint or "")[:64],
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


def _record_candidate_stage(candidate, stage, started, previous, *, clock=time.monotonic):
    """Body-free, source-bound timing on the existing group-stage log stream."""
    current = clock()
    wall = datetime.now(timezone.utc)

    def age_ms(value):
        try:
            stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
            return int((wall - stamp).total_seconds() * 1000)
        except (TypeError, ValueError):
            return -1

    print(
        f"group_voice_stage stage={stage} group_id={candidate.get('group_id')} "
        f"source_id={int(candidate.get('latest_message_id') or 0)} "
        f"anchor_id={int(candidate.get('anchor_message_id') or 0)} "
        f"revision={int(candidate.get('candidate_revision') or 0)} "
        f"elapsed_ms={max(0, int((current-previous)*1000))} "
        f"total_ms={max(0, int((current-started)*1000))} "
        f"source_age_ms={age_ms(candidate.get('last_message_at'))} "
        f"due_lag_ms={age_ms(candidate.get('due_at'))} status=ok",
        flush=True,
    )
    return current


def process_group_participation_queue(services: dict[str, Any], *, now: object | None = None) -> None:
    """Evaluate quiet-gap candidates and enqueue one durable QQ reply."""

    runtime_services = services
    services = {
        name: runtime_services[source_name]
        for name, source_name in BRIDGE_SERVICE_NAMES.items()
    }
    db_connect: Callable[[], Any] = services["db_connect"]
    claim_connect = runtime_services.get("_assistant_group_candidate_claim_connect")
    if not callable(claim_connect):
        claim_connect = db_connect
    candidates = _claim_due_group_candidates_with_lock_budget(claim_connect)
    for candidate in candidates:
        stage_started = stage_previous = time.monotonic()
        stage_candidate = dict(candidate)

        def record_stage(stage):
            nonlocal stage_previous
            stage_previous = _record_candidate_stage(
                stage_candidate, stage, stage_started, stage_previous,
            )

        group_generation_started_at = time.time()
        active_deadline_monotonic = (
            time.monotonic() + GROUP_AMBIENT_ACTIVE_DEADLINE_SECONDS
        )
        group_id = str(candidate.get("group_id") or "")
        record_stage("candidate_selected")
        latest_message_id = int(candidate.get("latest_message_id") or 0)
        candidate_revision = int(candidate.get("candidate_revision") or 0)
        anchor_message_id = int(candidate.get("anchor_message_id") or 0)
        decision_id = ""
        candidate_topic_context: dict | None = None
        ambient_freshness_context: dict | None = None
        candidate_kind = "ambient"
        policy: dict = {}
        response_commitment: dict | None = None
        response_identity: dict | None = None

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
            availability = group_gate(conn, group_id, created_at=candidate.get("last_message_at"))
            if not availability["allowed"]:
                return False
            if is_current(conn):
                return True
            supersede(conn)
            return False

        def dispatch_fence(
            conn: Any,
            topic_context: dict | None,
            *,
            ambient_context: bool = False,
        ) -> bool:
            """Keep a selected contribution if the *topic*, not just the ID, lives on.

            Queue revisions remain the right protection before a decision is
            selected.  After a natural contribution is selected, a related
            inbound turn must not silently discard it just because that turn
            changed the queue revision.  The same opaque context is checked
            again by Outbox immediately before an external send.
            """

            if not topic_context:
                return fence(conn)
            outcome = evaluate_topic_context(
                conn,
                group_id=group_id,
                context=topic_context,
                require_feature=not ambient_context,
            )
            if outcome.get("action") == "allow":
                return True
            services["transition_participation"](
                conn,
                decision_id=decision_id,
                stage="superseded",
                action="silent",
                reason_code=str(outcome.get("reason") or "topic_stale")[:120],
            )
            return False

        def candidate_fence(conn: Any) -> bool:
            """Use topic continuity once an opaque anchor context exists."""

            # Topic continuity only permits a still-owned candidate to survive
            # harmless follow-up traffic.  It must never revive work whose
            # lease was reclaimed by another worker: that worker owns a newer
            # optimistic revision even when the topic itself is unchanged.
            if not fence(conn):
                return False

            allowed = dispatch_fence(
                conn,
                candidate_topic_context or ambient_freshness_context,
                ambient_context=not bool(candidate_topic_context),
            )
            if allowed:
                return True
            # A newer inbound can make the selected anchor stale while the
            # existing lease is active.  Normal inbound coalescing correctly
            # preserves that lease, but without this handoff the worker would
            # reclaim the same obsolete row every 120 seconds forever.  Rebase
            # only when the original optimistic tokens still own the claim;
            # otherwise another inbound/worker has already taken over.
            replacement = conn.execute(
                """SELECT id,sender_id,sender_name,external_message_id,created_at
                   FROM group_messages
                   WHERE group_id=? AND sender_id<>'bot' AND id>?
                     AND length(trim(COALESCE(content,'')))>0
                     AND (expires_at='' OR expires_at>?)
                   ORDER BY id DESC LIMIT 1""",
                (group_id, latest_message_id, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            if replacement:
                moved = requeue_stale_claimed_candidate(
                    conn,
                    group_id=group_id,
                    expected_latest_message_id=latest_message_id,
                    expected_candidate_revision=candidate_revision,
                    replacement=dict(replacement),
                    session=str(candidate.get("latest_session") or ""),
                    quiet_gap_seconds=int(policy.get("quiet_gap_seconds") or 0),
                    active_topic_window_seconds=group_active_topic_window_seconds(policy),
                )
                if not moved:
                    finish_group_candidate(
                        conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                print(f"group_voice_handoff group_id={group_id} outcome={'requeued' if moved else 'lost'}", flush=True)
            else:
                finish_group_candidate(
                    conn,
                    group_id,
                    state="cancelled",
                    latest_message_id=latest_message_id,
                    candidate_revision=candidate_revision,
                )
            return False

        def approved_fence(
            conn: Any,
            topic_context: dict | None,
            *,
            ambient_context: bool = False,
        ) -> bool:
            """Freshness fence for an already model-approved contribution.

            Once the engagement model approved a reply to the anchor topic,
            follow-up traffic must not cancel it merely because it advanced the
            queue's latest_message_id.  In high-traffic groups the reply
            pipeline (reply generation + Outbox) can outlive the claim lease,
            and strict exact fencing there silently dropped approved replies
            before they reached the Outbox (candidate_superseded).

            Ownership is preserved by atomically renewing the claim lease; a
            failed renewal means another worker or an inbound UPSERT owns a
            newer revision, and this worker must never deliver for it (that
            would revive work already handed over and could double-send).
            Topic continuity is still enforced through the opaque context when
            one exists, and a stale topic is handed over to the newer text or
            terminated instead of being left to reclaim-loop (R7-A).  The
            Outbox repeats the topic check immediately before the external
            send.
            """

            availability = group_gate(conn, group_id, created_at=candidate.get("last_message_at"))
            if not availability["allowed"]:
                print(
                    f"group_approved_fence_blocked group_id={group_id} "
                    f"source_id={latest_message_id} revision={candidate_revision} "
                    f"reason={availability.get('reason') or 'group_unavailable'}",
                    flush=True,
                )
                return False
            if not renew_group_candidate_lease(
                conn,
                group_id,
                expected_candidate_revision=candidate_revision,
            ):
                services["transition_participation"](
                    conn,
                    decision_id=decision_id,
                    stage="superseded",
                    action="silent",
                    reason_code="candidate_superseded",
                )
                print(
                    f"group_approved_fence_blocked group_id={group_id} "
                    f"source_id={latest_message_id} revision={candidate_revision} "
                    "reason=candidate_superseded",
                    flush=True,
                )
                return False
            if not topic_context:
                return True
            outcome = evaluate_topic_context(
                conn,
                group_id=group_id,
                context=topic_context,
                require_feature=not ambient_context,
            )
            if outcome.get("action") == "allow":
                return True
            replacement = conn.execute(
                """SELECT id,sender_id,sender_name,external_message_id,created_at
                   FROM group_messages
                   WHERE group_id=? AND sender_id<>'bot' AND id>?
                     AND length(trim(COALESCE(content,'')))>0
                     AND (expires_at='' OR expires_at>?)
                   ORDER BY id DESC LIMIT 1""",
                (group_id, latest_message_id, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            if replacement:
                moved = requeue_stale_claimed_candidate(
                    conn,
                    group_id=group_id,
                    expected_latest_message_id=latest_message_id,
                    expected_candidate_revision=candidate_revision,
                    replacement=dict(replacement),
                    session=str(candidate.get("latest_session") or ""),
                    quiet_gap_seconds=int(policy.get("quiet_gap_seconds") or 0),
                    active_topic_window_seconds=group_active_topic_window_seconds(policy),
                )
                if not moved:
                    finish_group_candidate(
                        conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                print(f"group_voice_handoff group_id={group_id} outcome={'requeued' if moved else 'lost'}", flush=True)
            else:
                finish_group_candidate(
                    conn,
                    group_id,
                    state="cancelled",
                    latest_message_id=latest_message_id,
                    candidate_revision=candidate_revision,
                )
            services["transition_participation"](
                conn,
                decision_id=decision_id,
                stage="superseded",
                action="silent",
                reason_code=str(outcome.get("reason") or "topic_stale")[:120],
            )
            print(
                f"group_approved_fence_blocked group_id={group_id} "
                f"source_id={latest_message_id} revision={candidate_revision} "
                f"reason={outcome.get('reason') or 'topic_stale'}",
                flush=True,
            )
            return False

        try:
            access = services["group_access"](
                db_connect,
                str(candidate.get("latest_sender_id") or ""),
                group_id,
            )
            record_stage("access_checked")
            if not access.get("allowed"):
                with db_connect() as conn:
                    if candidate_fence(conn):
                        finish_group_candidate(
                            conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                continue
            with db_connect() as conn:
                availability = group_gate(conn, group_id, created_at=candidate.get("last_message_at"))
                if not availability["allowed"]:
                    finish_group_candidate(conn, group_id, state="cancelled", latest_message_id=latest_message_id, candidate_revision=candidate_revision)
                    continue
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
                commitment_context_items = services["group_context"](
                    conn, group_id, int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
                    before_message_id=latest_message_id,
                    min_remaining_seconds=600,
                )
                context_items = list(commitment_context_items)
                if latest:
                    context_items.append(dict(latest))
            record_stage("context_loaded")
            if not latest:
                with db_connect() as conn:
                    if fence(conn):
                        finish_group_candidate(
                            conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                continue
            latest = dict(latest)
            if not str(latest.get("content") or "").strip() or _transient_anchor_expired(latest):
                with db_connect() as conn:
                    if fence(conn):
                        finish_group_candidate(
                            conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                continue
            decision_id = str(latest.get("engagement_decision_id") or "")
            if not anchor:
                # An attachment-only candidate has no text anchor of its own,
                # but a live topic in the bounded context is still a valid
                # conversation target when attachment participation is enabled:
                # the participation model may answer the topic or react to the
                # image without ever claiming to have read it (the prompt and
                # Truth Gate enforce that).  Only when there is no readable
                # member text at all do we stay silent.
                topic_anchor = None
                if _policy_flag(policy, "attachment_participation"):
                    topic_anchor = next(
                        (
                            item for item in reversed(context_items)
                            if str(item.get("sender_id") or "") != "bot"
                            and str(item.get("content") or "").strip()
                            and _message_kind(item) != "attachment"
                        ),
                        None,
                    )
                if topic_anchor is None:
                    # No readable member text at all: keep the server-owned
                    # attachment-only silence contract (no_concrete_anchor)
                    # instead of letting the model invent a target.
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
                anchor = dict(topic_anchor)
            anchor = dict(anchor)
            stage_candidate["anchor_message_id"] = int(anchor.get("id") or 0)
            if _transient_anchor_expired(anchor):
                with db_connect() as conn:
                    if fence(conn):
                        services["mark_group_decision"](
                            conn,
                            message_id=int(anchor["id"]),
                            group_id=group_id,
                            decision={"should_reply": False, "reason": "transient_anchor_expired"},
                            replied=False,
                        )
                        services["transition_participation"](
                            conn,
                            decision_id=decision_id,
                            stage="preflight_blocked",
                            action="silent",
                            reason_code="transient_anchor_expired",
                        )
                        finish_group_candidate(
                            conn,
                            group_id,
                            latest_message_id=latest_message_id,
                            candidate_revision=candidate_revision,
                        )
                continue
            try:
                ambient_freshness_context = build_topic_delivery_context(
                    group_id=group_id,
                    anchor=anchor,
                    candidate_revision=candidate_revision,
                    reply_kind="native_contribution",
                    ttl_seconds=min(
                        90,
                        max(15, int(policy.get("continuation_window_seconds") or 60)),
                    ),
                )
            except ValueError:
                # An optional ambient turn without an opaque freshness anchor
                # cannot enter the model/Delivery path safely.
                ambient_freshness_context = None
            with db_connect() as conn:
                if topic_delivery_feature_enabled(conn):
                    candidate_topic_context = ambient_freshness_context
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
            # Anchor is a reply target, not a history cutoff. Preserve the
            # chronological window through the claimed event, including self.
            model_context_items = sorted([
                item for item in context_items
                if int(item.get("id") or 0) < latest_message_id
            ] + [latest], key=lambda item: int(item.get("id") or 0))
            # Existing target-policy callers require current=anchor last.
            # Keep that contract separate from chronological model context.
            turn_history = [
                item for item in context_items if int(item.get("id") or 0) < anchor_id
            ] + [anchor]
            # The queue intentionally preserves the first text anchor during a
            # topic window.  When that old anchor is already stale before a
            # plan starts, requeue the current latest text once instead of
            # cancelling the only pending candidate.  A genuinely newer inbound
            # event still wins; other topic-context failures keep the existing
            # fail-closed behaviour.
            if candidate_topic_context:
                with db_connect() as conn:
                    preplan_topic = evaluate_topic_context(
                        conn,
                        group_id=group_id,
                        context=candidate_topic_context,
                    )
                    if preplan_topic.get("action") != "allow":
                        preplan_reason = str(
                            preplan_topic.get("reason") or "topic_stale"
                        )[:120]
                        replacement = conn.execute(
                            """SELECT id,sender_id,sender_name,external_message_id,created_at
                               FROM group_messages
                               WHERE group_id=? AND sender_id<>'bot' AND id>?
                                 AND length(trim(COALESCE(content,'')))>0
                                 AND (expires_at='' OR expires_at>?)
                               ORDER BY id DESC LIMIT 1""",
                            (group_id, latest_message_id, datetime.now(timezone.utc).isoformat()),
                        ).fetchone()
                        reanchor_current_latest = False
                        if replacement is None:
                            latest_kind = str(_message_kind(latest) or "").strip().lower()
                            latest_is_text = (
                                str(latest.get("sender_id") or "") != "bot"
                                and bool(str(latest.get("content") or "").strip())
                                and latest_kind not in {"attachment", "image", "video", "audio"}
                            )
                            reanchor_current_latest = bool(
                                preplan_reason == "topic_stale"
                                and anchor_id != latest_message_id
                                and int(latest.get("id") or 0) == latest_message_id
                                and latest_is_text
                            )
                            if reanchor_current_latest:
                                replacement = latest
                        if replacement:
                            handed_off = requeue_stale_claimed_candidate(
                                conn,
                                group_id=group_id,
                                expected_latest_message_id=latest_message_id,
                                expected_candidate_revision=candidate_revision,
                                replacement=dict(replacement),
                                session=str(candidate.get("latest_session") or ""),
                                quiet_gap_seconds=int(policy.get("quiet_gap_seconds") or 0),
                                active_topic_window_seconds=group_active_topic_window_seconds(policy),
                                allow_current_latest=reanchor_current_latest,
                            )
                        else:
                            handed_off = finish_group_candidate(
                                conn,
                                group_id,
                                state="cancelled",
                                latest_message_id=latest_message_id,
                                candidate_revision=candidate_revision,
                            )
                        if handed_off:
                            print(f"group_voice_handoff group_id={group_id} outcome=requeued", flush=True)
                            services["transition_participation"](
                                conn,
                                decision_id=decision_id,
                                stage="superseded",
                                action="silent",
                                reason_code=preplan_reason,
                            )
                        else:
                            finish_group_candidate(
                                conn, group_id, state="cancelled", latest_message_id=latest_message_id,
                                candidate_revision=candidate_revision,
                            )
                            print(f"group_voice_handoff group_id={group_id} outcome=lost", flush=True)
                        continue
            record_stage("freshness_ready")
            conversation_frame = build_group_conversation_frame(
                context_items, anchor,
                context_limit=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
                continuation_window_seconds=int(policy.get("continuation_window_seconds") or 120),
                short_turn_participation=_policy_flag(policy, "short_turn_participation"),
            )
            # The inbound commitment is bound to the source ordering observed
            # when the queue's latest event arrived.  A coalesced candidate may
            # intentionally answer an earlier text anchor, whose decision frame
            # has a different current-message order.  Keep that decision frame,
            # but reproduce the ingress frame for commitment binding.
            commitment_frame = build_group_conversation_frame(
                commitment_context_items, latest,
                context_limit=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
                continuation_window_seconds=int(policy.get("continuation_window_seconds") or 120),
                short_turn_participation=_policy_flag(policy, "short_turn_participation"),
            )
            with db_connect() as conn:
                candidate_kind = (
                    "continuation" if conversation_frame.get("active_continuation") else "ambient"
                )
                guard = natural_group_preflight(
                    conn,
                    policy=policy,
                    group_id=group_id,
                    current=anchor,
                    conversation_frame=conversation_frame,
                    candidate_kind=candidate_kind,
                )
                if guard:
                    # This is terminal silence, not an external send. Record
                    # it against the current claim even if the selected anchor
                    # is no longer the latest textual topic; applying the
                    # send-time topic fence here mislabelled burst/cooldown
                    # guards as cancelled queue work.
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
                if candidate_kind in {"ambient", "continuation"}:
                    if not ambient_freshness_context:
                        window_gate = {
                            "action": "cancel",
                            "reason": "ambient_freshness_unavailable",
                            "policy_version": 0,
                        }
                    else:
                        try:
                            window_gate = ambient_participation_window_decision(conn, group_id, now=now)
                        except Exception:
                            window_gate = {
                                "action": "cancel",
                                "reason": "ambient_window_policy_unavailable",
                                "policy_version": 0,
                            }
                    if window_gate.get("action") != "allow":
                        if candidate_fence(conn):
                            services["mark_group_decision"](
                                conn, message_id=int(anchor["id"]), group_id=group_id,
                                decision={"should_reply": False, "reason": window_gate["reason"]},
                                replied=False,
                            )
                            services["transition_participation"](
                                conn,
                                decision_id=decision_id,
                                stage="window_preflight_blocked",
                                action="silent",
                                reason_code=str(window_gate["reason"]),
                            )
                            finish_group_candidate(
                                conn, group_id, latest_message_id=latest_message_id,
                                candidate_revision=candidate_revision,
                            )
                        continue
            record_stage("preflight_ready")
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
            record_stage("social_opportunity_ready")
            fallback_settings = services["assistant_settings"](
                include_secrets=True, integrity_scope="identity",
            )
            record_stage("settings_ready")
            visual_event_id = str(
                anchor.get("external_message_id")
                or candidate.get("latest_external_message_id")
                or anchor.get("id")
                or ""
            )
            anchor_message_kind = str(
                conversation_frame.get("message_kind") or _message_kind(anchor)
            ).strip().lower()
            visual_payload = {
                "attachments": (
                    [{"type": "video" if anchor_message_kind == "video" else "image"}]
                    if anchor_message_kind in {"attachment", "image", "mixed", "video"}
                    else []
                ),
            }
            transient_visual_context = finish_group_visual_context(
                visual_payload,
                group_id=group_id,
                event_id=visual_event_id,
                conversation_frame=conversation_frame,
                wait_timeout_seconds=max(
                    1,
                    remaining_timeout(
                        GROUP_AMBIENT_VISUAL_WAIT_SECONDS,
                        active_deadline_monotonic,
                        time.monotonic,
                    ),
                ),
            )
            anchor = with_visual_group_current(anchor, transient_visual_context)
            record_stage("visual_ready")
            commitment_terminal_reason = ""
            with db_connect() as conn:
                if _group_response_commitment_schema_active(conn):
                    event_id = _group_message_event_id(latest)
                    if not event_id:
                        commitment_terminal_reason = "group_response_event_missing"
                    else:
                        response_commitment = load_group_response_commitment_for_event(
                            conn,
                            event_id,
                        )
                        if response_commitment is None:
                            event_row = conn.execute(
                                "SELECT assistant_id FROM conversation_events WHERE id=?",
                                (event_id,),
                            ).fetchone()
                            if not event_row:
                                commitment_terminal_reason = "group_response_event_missing"
                            else:
                                response_commitment = claim_group_response_commitment(
                                    conn,
                                    event_id=event_id,
                                    assistant_id=str(event_row[0] or ""),
                                    group_id=group_id,
                                    source_message_id=str(
                                        latest.get("external_message_id")
                                        or candidate.get("latest_external_message_id")
                                        or ""
                                    ),
                                    source_set_hash=str(
                                        commitment_frame.get("source_set_hash") or ""
                                    ),
                                    situation_revision=str(
                                        conversation_frame.get("revision") or ""
                                    ),
                                    owner_kind="ambient",
                                )
                        if response_commitment is not None:
                            if str(response_commitment.get("owner_kind") or "") != "ambient":
                                commitment_terminal_reason = "group_response_already_owned"
                            elif int(response_commitment.get("plan_started") or 0):
                                commitment_terminal_reason = "group_response_plan_already_started"
                            else:
                                try:
                                    response_commitment = _advance_ambient_response_commitment(
                                        conn,
                                        response_commitment=response_commitment,
                                        event_id=event_id,
                                        group_id=group_id,
                                        source_message_id=str(
                                            latest.get("external_message_id")
                                            or candidate.get("latest_external_message_id")
                                            or ""
                                        ),
                                        decision_id=decision_id,
                                        latest_message_id=latest_message_id,
                                        fresh_source_set_hash=str(
                                            commitment_frame.get("source_set_hash") or ""
                                        ),
                                        situation_revision=str(
                                            conversation_frame.get("revision") or ""
                                        ),
                                    )
                                except ValueError as exc:
                                    code = str(exc)
                                    commitment_terminal_reason = (
                                        code if code in {
                                            "group_response_commitment_binding_conflict",
                                            "group_response_commitment_not_active",
                                            "group_response_ingress_frame_unavailable",
                                            "group_response_original_source_unavailable",
                                        } else "group_response_situation_conflict"
                                    )
                                if (
                                    not commitment_terminal_reason
                                    and not begin_group_response_phase(
                                        conn,
                                        commitment_id=str(response_commitment["id"]),
                                        owner_kind="ambient",
                                        phase="plan",
                                    )
                                ):
                                    commitment_terminal_reason = "group_response_plan_already_started"
                                if not commitment_terminal_reason:
                                    response_identity = group_response_identity(
                                        response_commitment,
                                    )
            if commitment_terminal_reason:
                if commitment_terminal_reason in {
                    "group_response_original_source_unavailable",
                    "group_response_ingress_frame_unavailable",
                }:
                    print(f"group_voice_stage stage=source_check group_id={group_id} elapsed_ms=0 status=required_source_unavailable", flush=True)
                with db_connect() as conn:
                    if (
                        response_commitment is not None
                        and str(response_commitment.get("owner_kind") or "") == "ambient"
                        and str(response_commitment.get("state") or "") in {"owned", "planned"}
                    ):
                        try:
                            settle_group_response_commitment(
                                conn,
                                commitment_id=str(response_commitment["id"]),
                                owner_kind="ambient",
                                state="held",
                            )
                        except ValueError:
                            pass
                    services["transition_participation"](
                        conn,
                        decision_id=decision_id,
                        stage="preflight_blocked",
                        action="silent",
                        reason_code=commitment_terminal_reason,
                    )
                    finish_group_candidate(
                        conn,
                        group_id,
                        state="cancelled",
                        latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                continue
            record_stage("commitment_ready")
            message = str(anchor.get("content") or "").strip()
            history = group_model_history(
                model_context_items[:-1],
                limit=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
            )
            history = append_visual_history(history, transient_visual_context)
            chat_settings = dict(services["settings_for_model_role"](
                "conversation_reply", fallback_settings,
            ))
            record_stage("role_settings_ready")
            chat_settings["model_session_scope"] = f"group:{group_id}"
            if not int(policy.get("meme_enabled") or 0):
                chat_settings["meme_daily_enabled"] = "0"
            classifier_settings = chat_settings
            try:
                research_context = services["maybe_group_research"](
                    group_id=group_id,
                    anchor=anchor,
                    message=message,
                )
            except Exception:
                # Research is additive. A failure must not leak diagnostics or
                # manufacture evidence in the one visible group response.
                research_context = {"available": False, "sources": []}
            record_stage("research_ready")
            if isinstance(research_context, dict) and research_context.get("fact_unverified"):
                with db_connect() as conn:
                    if not candidate_fence(conn):
                        continue
                    services["transition_participation"](
                        conn,
                        decision_id=decision_id,
                        stage="research_blocked",
                        action="silent",
                        reason_code=str(
                            research_context.get("reason_code") or "research_unverified"
                        )[:120],
                    )
                    finish_group_candidate(
                        conn,
                        group_id,
                        latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                    if response_commitment is not None:
                        settle_group_response_commitment(
                            conn,
                            commitment_id=str(response_commitment["id"]),
                            owner_kind="ambient",
                            state="silent",
                        )
                continue
            prompt_current = anchor if anchor_id == latest_message_id else latest
            prompt_frame = dict(
                conversation_frame if anchor_id == latest_message_id else commitment_frame
            )
            prompt_frame["candidate_anchor_message_id"] = anchor_id
            if anchor_id != latest_message_id:
                prompt_frame["reply_anchor_media"] = conversation_frame.get("media") or {}
            decision_messages = services["build_group_decision_messages"](
                policy, model_context_items, prompt_current, prompt_frame,
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
            group_prompt_context = {
                "group_id": group_id,
                "thread_id": f"qq:group:{group_id}",
                "message_id": str(prompt_current.get("external_message_id") or prompt_current.get("id") or ""),
                "group_name": policy.get("group_name") or "",
                "sender_id": prompt_current.get("sender_id") or "",
                "sender_name": prompt_current.get("sender_name") or "",
                "assistant_id": str((response_commitment or {}).get("assistant_id") or ""),
                "topic_revision": int(anchor.get("id") or 0),
                "observed_user_expression": dict(
                    conversation_frame.get("observed_user_expression") or {}
                ),
                "topic_anchor": {
                    "id": int(anchor.get("id") or 0),
                    "content": message,
                },
                "allow_group_feedback": False,
                "research": research_context,
            }
            record_stage("messages_ready")
            prepared = services["prepare_group_single_plan"](
                settings=chat_settings,
                user_id=f"group:{group_id}",
                message=message,
                history=history,
                group=group_prompt_context,
                decision_messages=decision_messages,
                research_context=research_context,
                current=prompt_current,
            )
            if not isinstance(prepared, dict) or not isinstance(prepared.get("messages"), list):
                raise RuntimeError("group_single_plan_context_failed")
            record_stage("prompt_ready")
            provider = str(chat_settings.get("chat_provider") or "codex")
            model_result = run_group_single_plan(
                chat_settings,
                prepared["messages"],
                deadline_monotonic=active_deadline_monotonic,
                clock=time.monotonic,
                call_openai=services["call_openai"],
                run_codex=services["run_codex"],
                default_cwd=services["default_cwd"](),
                record_model=services["record_model_call"],
                user_id=f"group:{group_id}",
            )
            record_stage("model_returned")
            if not model_result.get("ok"):
                raise RuntimeError(
                    str(model_result.get("error") or "group_single_plan_failed"),
                )
            with db_connect() as conn:
                # Pre-approval ownership stays strict while the one model call
                # is in flight. A newer worker must own the next revision.
                if not candidate_fence(conn):
                    continue
            raw_plan = model_result.get("reply") or model_result.get("output") or ""
            if str(conversation_frame.get("message_kind") or "") in {"text", "mixed"}:
                print(f"group_voice_stage stage=model_text group_id={group_id} elapsed_ms=0 status={'ok' if model_result.get('ok') else 'failed'}", flush=True)
            decision = parse_group_single_plan(
                raw_plan,
                expected_anchor_message_id=int(anchor["id"]),
                parse_decision=services["parse_group_decision"],
            )
            result = {
                **model_result,
                "reply": str(decision.get("reply") or ""),
                "output": str(decision.get("reply") or ""),
                "dispatch": "chat",
                "group_decision": decision,
                "engagement_decision_id": decision_id,
            }
            recent_replies = [
                str(item.get("content") or "")
                for item in history[-14:]
                if str(item.get("role") or "") == "assistant"
                or str(item.get("sender_id") or "") == "bot"
            ]

            def _finalize_single_plan_reply(reply_text: str, candidate_result: dict) -> tuple[str, dict]:
                finalized, guarded = enforce_action_truth(
                    reply_text,
                    candidate_result.get("action_receipts")
                    if isinstance(candidate_result.get("action_receipts"), list)
                    else None,
                )
                return finalized, {"action_truth_guarded": guarded}

            delivery_reply, style_issues, style_metadata = group_reply_style_issues_for_delivery(
                message,
                result["reply"],
                recent_replies=recent_replies,
                uninvited=True,
                expression_plan=(prepared.get("social_context") or {}).get("expression_plan")
                if isinstance(prepared.get("social_context"), dict)
                else None,
                candidate=result,
                finalizer=_finalize_single_plan_reply,
            )
            if _repeated_anchor_reply(delivery_reply, model_context_items, anchor_id):
                style_issues.append("repeated_anchor_reply")
            result.update({
                "reply": delivery_reply,
                "output": delivery_reply,
                **style_metadata,
                "group_style_gate": "passed" if not style_issues else "degraded",
                "group_style_retry_attempted": False,
                "group_style_initial_issues": list(style_issues),
                "group_style_final_issues": list(style_issues),
            })
            if prepared.get("research_truth_context"):
                result["group_research"] = prepared["research_truth_context"]
            decision = services["apply_group_turn_policy"](
                policy, turn_history, anchor, decision, conversation_frame,
            )
            decision["classifier_ok"] = True
            decision["classifier_provider"] = model_result.get("provider") or provider
            decision["single_plan_ok"] = True
            decision["single_plan_provider"] = model_result.get("provider") or provider
            threshold = services["participation_confidence_floor"](policy)
            if decision.get("should_reply") and float(decision.get("confidence") or 0) < threshold:
                decision.update({"should_reply": False, "reason": "engagement_below_threshold"})
            with db_connect() as conn:
                if not approved_fence(
                    conn,
                    candidate_topic_context or ambient_freshness_context,
                    ambient_context=not bool(candidate_topic_context),
                ):
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
            if decision.get("should_reply") and not str(result.get("reply") or "").strip():
                decision.update({
                    "should_reply": False,
                    "social_action": "silent",
                    "reason": "group_single_plan_reply_invalid",
                })
            social_decision = None
            with db_connect() as conn:
                if not approved_fence(
                    conn,
                    candidate_topic_context or ambient_freshness_context,
                    ambient_context=not bool(candidate_topic_context),
                ):
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
                    current_topic_fingerprint=topic_fingerprint(anchor.get("content")),
                )
            if not decision.get("should_reply"):
                with db_connect() as conn:
                    if not candidate_fence(conn):
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
                        model_role="conversation_reply",
                        model_id=str(classifier_settings.get("chat_model") or ""),
                        confidence=float(decision.get("confidence") or 0),
                    )
                    finish_group_candidate(
                        conn, group_id, latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
                    if response_commitment is not None:
                        settle_group_response_commitment(
                            conn,
                            commitment_id=str(response_commitment["id"]),
                            owner_kind="ambient",
                            state="silent",
                        )
                continue
            with db_connect() as conn:
                if not approved_fence(
                    conn,
                    candidate_topic_context or ambient_freshness_context,
                    ambient_context=not bool(candidate_topic_context),
                ):
                    continue
            topic_delivery = None
            if str(decision.get("social_action") or "") in {
                "echo_reaction", "meme_reaction", "ack_add", "follow_up", "reply", "bridge_topic", "repair",
            }:
                topic_delivery = dict(candidate_topic_context or {}) or None
                if topic_delivery and isinstance(research_context, dict) and research_context.get("available"):
                    topic_delivery["reply_kind"] = "research_contribution"
            transport = {
                "_group_generation_started_at": group_generation_started_at,
                "group_id": group_id,
                "sender_id": candidate.get("latest_sender_id") or "",
                "session": candidate.get("latest_session") or "",
                "is_mention": False,
                "_external_message_id": candidate.get("latest_external_message_id") or f"natural:{anchor['id']}",
                "trace_id": f"natural-group:{group_id}:{anchor['id']}",
                "engagement_decision_id": decision_id,
            }
            if response_identity is not None:
                transport["_group_response_identity"] = response_identity
            if candidate_kind in {"ambient", "continuation"}:
                transport["_ambient_freshness_ref"] = ambient_freshness_context
            if topic_delivery:
                transport["_topic_delivery"] = topic_delivery

            def _execute_natural_turn(
                _turn_id: str,
                _single_plan_result: dict = result,
            ) -> dict:
                record_stage("continuity_callback")
                result = {
                    **_single_plan_result,
                    "dispatch": str(_single_plan_result.get("dispatch") or "chat"),
                    "group_decision": decision,
                    "engagement_decision_id": transport["engagement_decision_id"],
                }
                repair_group_risky_reply(
                    result, settings=chat_settings, messages=prepared['messages'],
                    current=anchor, history=model_context_items,
                    deadline_monotonic=active_deadline_monotonic,
                    call_openai=services['call_openai'], run_codex=services['run_codex'],
                    default_cwd=services['default_cwd'](), record_model=services['record_model_call'],
                    user_id=f'group:{group_id}', recent_replies=recent_replies,
                    expression_plan=(prepared.get('social_context') or {}).get('expression_plan'),
                    uninvited=True,
                )
                record_stage("quality_repair_ready")
                with db_connect() as conn:
                    approved = approved_fence(
                        conn,
                        candidate_topic_context or ambient_freshness_context,
                        ambient_context=not bool(candidate_topic_context),
                    )
                    if approved:
                        # Run the same final boundary as direct replies before
                        # the only Outbox call.  A rejected fence must commit
                        # its ownership/topic transition before the turn ends.
                        planned_reply = services["complete_group_dispatch"](
                            conn, event=None, deterministic_decision=None,
                            decision=decision, group_id=group_id, payload=transport,
                            # Receipt/decision correlation belongs to the trigger;
                            # the frame still supplies the semantic reply target.
                            classifier_settings=classifier_settings, current=latest,
                            result=result,
                            assistant_name=str(fallback_settings.get("display_name") or ""),
                            conversation_frame=conversation_frame,
                        )
                record_stage("final_gate_ready")
                if not approved:
                    raise RuntimeError("candidate_topic_stale")
                result["should_reply"] = bool(planned_reply)
                if not planned_reply:
                    return {
                        **result,
                        "delivery_queued": False,
                        "group_final_blocked": bool(
                            result.get("group_truth_blocked")
                            or result.get("group_safety_blocked")
                        ),
                    }
                if candidate_kind in {"ambient", "continuation"}:
                    with db_connect() as conn:
                        try:
                            window_gate = ambient_participation_window_decision(conn, group_id, now=now)
                        except Exception:
                            window_gate = {
                                "action": "cancel",
                                "reason": "ambient_window_policy_unavailable",
                                "policy_version": 0,
                            }
                    if window_gate.get("action") != "allow":
                        return {
                            **result,
                            "delivery_queued": False,
                            "group_window_blocked": True,
                            "group_window_reason": str(window_gate.get("reason") or "ambient_window_policy_unavailable"),
                        }
                    transport["_ambient_policy_version"] = int(window_gate.get("policy_version") or 0)
                social_context = prepared.get("social_context") if isinstance(prepared.get("social_context"), dict) else {}
                voice_contract = social_context.get("voice_contract") if isinstance(social_context, dict) else {}
                if (manual_meme_request(message) and str(decision.get("social_action") or "") == "meme_reaction"
                        and str(decision.get("meme_intent") or "") == "strong"):
                    attachment_context, meme = prepare_group_meme_attachment(
                        db_connect=db_connect, settings=fallback_settings,
                        policy=build_agent_policy(fallback_settings), group_policy=policy,
                        message=message, decision=decision, user_id=f"group:{group_id}",
                        persona_meme_policy=str((voice_contract or {}).get("meme_policy_key") or "contextual"),
                        selection_runtime=(select_and_reserve_meme, None, None, None),
                    )
                    attachment_context["decision_source"] = "model_decision"
                else:
                    attachment_context, meme = choose_meme_expression(
                        db_connect=db_connect,
                        settings={**fallback_settings, **chat_settings},
                        policy=build_agent_policy(fallback_settings), group_policy=policy,
                        scope="group", message=message, reply=str(result.get("reply") or ""),
                        mode=str(decision.get("mode") or "daily"),
                        intent=str(decision.get("intent") or "chat"),
                        user_id=f"group:{group_id}", session=str(transport.get("session") or ""),
                        persona_meme_policy=str((voice_contract or {}).get("meme_policy_key") or "contextual"),
                        visual_ready=(str(anchor.get("message_kind") or "text") not in
                                      {"attachment", "image", "mixed", "video", "audio"}
                                      or str(anchor.get("visual_context_status") or "") == "ready"),
                        deadline_monotonic=active_deadline_monotonic,
                        call_openai=services["call_openai"],
                        run_codex=services["run_codex"],
                        default_cwd=services["default_cwd"](),
                        record_model=services["record_model_call"],
                        approved=bool(result.get("ok") and result.get("group_style_gate") == "passed"
                                      and not result.get("group_truth_blocked")
                                      and not result.get("group_safety_blocked")),
                    )
                    if meme:
                        result["delivery_form"] = attachment_context["delivery_form"]
                result["meme_attachment"] = attachment_context
                record_meme_funnel({**attachment_context, "decision_id": decision_id}, scope="group")
                record_stage("expression_ready")
                if meme:
                    result["meme"] = meme
                try:
                    queued = services["dispatch_response"](
                        services["outbox"](), lambda: result, transport,
                        scope="group", enabled=True,
                        delivery_observer=lambda value: project_group_dispatch_delivery(db_connect, value),
                    )
                    record_stage("outbox_returned")
                except Exception as exc:
                    # Selection precedes reservation on this worker path. Also
                    # settle failures that occur before dispatch's observer exists.
                    try:
                        project_group_dispatch_delivery(db_connect, {
                            **result, 'delivery_queued': False,
                            '_delivery_enqueue_error': type(exc).__name__,
                        })
                    except Exception:
                        pass  # Outer failure handling remains authoritative.
                    raise
                if not queued.get("delivery_queued"):
                    raise RuntimeError(
                        str(queued.get("error") or "natural_group_delivery_not_queued"),
                    )
                continuity_candidates = services["capture_group_single_plan_memory_candidates"](
                    db_connect,
                    explicit_memories=[],
                    legacy_user_id=f"group:{group_id}",
                    message=message,
                    interaction_plan=decision,
                    source="qq_group",
                    group={
                        "group_id": group_id,
                        "sender_id": str(anchor.get("sender_id") or ""),
                        "source_external_message_id": str(
                            anchor.get("external_message_id") or ""
                        ),
                    },
                )
                if continuity_candidates:
                    queued["continuity_memory_candidates"] = continuity_candidates
                record_stage("memory_capture_ready")
                return queued

            record_stage("continuity_enter")
            queued = services["continuity_kernel"].execute_turn(
                {
                    "user_id": f"group:{group_id}",
                    "message": message,
                    "source": "qq_group",
                    "trace_id": transport["trace_id"],
                    "inbound_context": {
                        "group_id": group_id,
                        "sender_id": transport["sender_id"],
                        "_external_message_id": transport["_external_message_id"],
                    },
                },
                _execute_natural_turn,
            )
            record_stage("continuity_returned")
            services["continuity_kernel"].bind_delivery(queued)
            record_stage("delivery_bound")
            if not queued.get("delivery_queued"):
                if queued.get("group_window_blocked"):
                    with db_connect() as conn:
                        if not approved_fence(
                            conn,
                            candidate_topic_context or ambient_freshness_context,
                            ambient_context=not bool(candidate_topic_context),
                        ):
                            continue
                        services["transition_participation"](
                            conn,
                            decision_id=decision_id,
                            stage="window_final_blocked",
                            action="silent",
                            reason_code=str(queued.get("group_window_reason") or "ambient_window_policy_unavailable"),
                            model_role="conversation_reply",
                            model_id=str(classifier_settings.get("chat_model") or ""),
                            confidence=float(decision.get("confidence") or 0),
                        )
                        if is_current(conn):
                            finish_group_candidate(
                                conn, group_id, latest_message_id=latest_message_id,
                                candidate_revision=candidate_revision,
                            )
                        if response_commitment is not None:
                            settle_group_response_commitment(
                                conn,
                                commitment_id=str(response_commitment["id"]),
                                owner_kind="ambient",
                                state="silent",
                            )
                    continue
                if queued.get("group_final_blocked"):
                    with db_connect() as conn:
                        if not approved_fence(
                            conn,
                            candidate_topic_context or ambient_freshness_context,
                            ambient_context=not bool(candidate_topic_context),
                        ):
                            continue
                        services["transition_participation"](
                            conn,
                            decision_id=decision_id,
                            stage="truth_blocked",
                            action="silent",
                            reason_code="group_final_truth_blocked",
                            model_role="conversation_reply",
                            model_id=str(classifier_settings.get("chat_model") or ""),
                            confidence=float(decision.get("confidence") or 0),
                        )
                        if is_current(conn):
                            finish_group_candidate(
                                conn, group_id, latest_message_id=latest_message_id,
                                candidate_revision=candidate_revision,
                            )
                        if response_commitment is not None:
                            settle_group_response_commitment(
                                conn,
                                commitment_id=str(response_commitment["id"]),
                                owner_kind="ambient",
                                state="silent",
                            )
                    continue
                raise RuntimeError(
                    str(queued.get("error") or "natural_group_delivery_not_queued"),
                )
            with db_connect() as conn:
                if not approved_fence(
                    conn,
                    candidate_topic_context or ambient_freshness_context,
                    ambient_context=not bool(candidate_topic_context),
                ):
                    continue
                services["transition_participation"](
                    conn,
                    decision_id=decision_id,
                    stage="delivery_queued",
                    action="contextual_participation",
                    reason_code="model_engagement_approved",
                    model_role="conversation_reply",
                    model_id=str(classifier_settings.get("chat_model") or ""),
                    confidence=float(decision.get("confidence") or 0),
                )
                if response_commitment is not None:
                    delivery = (
                        queued.get("delivery")
                        if isinstance(queued.get("delivery"), dict)
                        else {}
                    )
                    settle_group_response_commitment(
                        conn,
                        commitment_id=str(response_commitment["id"]),
                        owner_kind="ambient",
                        state="committed",
                        delivery_id=str(
                            delivery.get("id") or delivery.get("delivery_id") or ""
                        ),
                    )
                if is_current(conn):
                    finish_group_candidate(
                        conn, group_id, latest_message_id=latest_message_id,
                        candidate_revision=candidate_revision,
                    )
        except Exception as exc:
            record_stage("worker_exception")
            error_text = str(exc)
            if error_text in {"candidate_superseded", "candidate_topic_stale"}:
                continue
            if (
                isinstance(exc, RuntimeError)
                and error_text != "natural_group_delivery_not_queued"
            ):
                failure_reason = "group_single_plan_failed"
            elif error_text == "natural_group_delivery_not_queued":
                failure_reason = "group_delivery_not_queued"
            else:
                failure_reason = "group_participation_worker_failed"
            with db_connect() as conn:
                if not fence(conn):
                    continue
                if response_commitment is not None:
                    try:
                        settle_group_response_commitment(
                            conn,
                            commitment_id=str(response_commitment["id"]),
                            owner_kind="ambient",
                            state="held",
                        )
                    except ValueError:
                        pass
                    finish_group_candidate(
                        conn, group_id, state="failed",
                        latest_message_id=latest_message_id,
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
                elif int(candidate.get("attempt") or 0) < 3:
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
                f"detail={str(exc)[:300]!r} "
                f"attempt={int(candidate.get('attempt') or 0)}",
                flush=True,
            )


__all__ = ["process_group_participation_queue"]
