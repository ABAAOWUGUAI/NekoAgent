#!/usr/bin/env python3
import base64
import copy
import http.server
import hmac
import html
import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse
try:
    import yaml
except Exception:
    yaml = None
try:
    from admin_console import ADMIN_ASSET_VERSION, ADMIN_HTML as ADMIN_CONSOLE_HTML, admin_asset
except ImportError:
    ADMIN_ASSET_VERSION = ""
    ADMIN_CONSOLE_HTML = ""
    admin_asset = None
try:
    from bridge_system_audit import system_audit as _run_system_audit
except Exception:
    _run_system_audit = None
from bridge_provider_presets import (
    PROVIDER_PRESETS,
    apply_provider_preset,
    provider_label,
    provider_presets_public,
    provider_preset,
)
from bridge_product_framework import build_system_framework
from bridge_proxy_probe import target_probe as proxy_target_probe
from bridge_proxy_probe import targets_probe as proxy_targets_probe
from bridge_proxy_instances import safe_subscription_rollback_error
from bridge_proxy_service import (
    concurrent_node_delays,
    read_only_diagnostics,
)
from bridge_migrations import MigrationError
from bridge_meme_social import (
    choose_meme,
    due_proactive_plans,
    ensure_social_tables,
    list_proactive_plans,
    mark_meme_delivery,
    mark_proactive_plan,
    public_asset,
    seed_default_memes,
    update_qq_session,
    upsert_proactive_plan,
)
from bridge_meme_discovery import ensure_meme_discovery_tables
from bridge_meme_selection import select_and_reserve_meme
from bridge_meme_expression import choose_meme_expression
from bridge_meme_http import MemeHttpApi
from bridge_meme_attachment import (
    manual_meme_request,
    align_reply_with_attachment,
    mark_failed_attachment,
    prepare_group_meme_attachment,
    prepare_meme_attachment,
    record_meme_funnel,
)
from bridge_http_routes import BRIDGE_POST_ROUTES
from bridge_project_http import ProjectHttpApi
from bridge_project_service import ProjectService
from bridge_http_body import read_json_object
from bridge_assistant_home_invalidation import (
    ASSISTANT_HOME_ASSISTANT_TABLES,
    ASSISTANT_HOME_TASK_TABLES,
    connect_home_database,
)
from bridge_proxy_environment import apply_proxy_environment, command_environment, direct_command_environment
from bridge_aiclient_proxy_consumer import (
    AICLIENT_MODEL_ID,
    AICLIENT_PROVIDER_ID,
    runtime_proxy_consumer_status,
)
from bridge_ops_broker_contract import action_hash
from bridge_auth import PrincipalKind, read_secret, resolve_principal, route_allowed, secrets_distinct
from bridge_admin_token import (
    TOKEN_MAX_LENGTH,
    TOKEN_MIN_LENGTH,
    admin_token_value_valid,
    fixed_token_status,
    validate_admin_token,
)
from bridge_http_responses import (
    binary_response as _binary_response,
    html_response as _html_response,
    json_response as _json_response,
    json_response_with_cookie as _json_response_with_cookie,
    redirect_response as _redirect_response,
)
from bridge_artifact_runtime import ArtifactRuntime
from bridge_voice_delivery import VoiceDeliveryRuntime
from bridge_voice_output import VoiceOutputRuntime
from bridge_business_health import BusinessHealthService
from bridge_executor_health import probe_executor
from bridge_server_status import build_server_status
from bridge_worker_health import WorkerHealthRegistry
from bridge_runtime_text_utils import (
    codex_failure_diagnosis as _codex_failure_diagnosis_impl,
    extract_codex_last_message as _extract_codex_last_message_impl,
    human_bytes as _human_bytes_impl,
    last_index as _last_index_impl,
    read_meminfo as _read_meminfo_impl,
    recent_matching_lines as _recent_matching_lines_impl,
    safe_log_text as _safe_log_text_impl,
    sanitize_log_text as _sanitize_log_text_impl,
    strip_ansi as _strip_ansi_impl,
    trim_output as _trim_output_impl,
)
from bridge_runtime_data_mappers import (
    clip_text as _clip_text_impl,
    compact_projection as _compact_projection_impl,
    memory_from_row as _memory_from_row_impl,
    mode_session_from_row as _mode_session_from_row_impl,
    qq_event_from_row as _qq_event_from_row_impl,
    quality_event_from_row as _quality_event_from_row_impl,
    slugify as _slugify_impl,
)
from bridge_gate8_http import Gate8HttpApi
from bridge_social_virtual_http import SocialVirtualHttpApi
from bridge_qq_access_http import QqAccessHttpApi
from bridge_qq_admin_actions import dispatch_qq_admin_action
from bridge_qq_diagnostics import collect_qq_diagnostics
from bridge_qq_llbot_diagnostics import collect_llbot_diagnostics
from bridge_qq_qrcode import qrcode_freshness, refresh_napcat_qrcode, restart_napcat
from bridge_qq_runtime_http import QqRuntimeHttpApi
from bridge_group_state import group_gate
from bridge_qq_object_runtime import QqObjectRuntime
from bridge_qq_access_runtime import (
    channel_runtime_enabled as qq_channel_runtime_enabled,
    diagnostic_access_snapshot,
    group_access as qq_group_access,
    private_access_http_error as qq_private_access_http_error,
    private_owner_access as qq_private_owner_access,
    super_admin_ids as qq_super_admin_ids,
)
import bridge_assistant_identity as assistant_identity
from bridge_assistant_identity_http import AssistantIdentityHttpApi, AssistantIdentityPatchMixin
from bridge_persona_runtime_http import PersonaRuntimeHttpApi
from bridge_assistant_home import AssistantHomeService
from bridge_assistant_home_http import AssistantHomeHttpApi
from bridge_b2_product_http import B2ProductHttpRuntime
from bridge_continuity_kernel import ContinuityKernel
from bridge_action_commitment import ActionCommitmentRepository
from bridge_action_followup import dispatch_action_followup_context
from bridge_route_dispatch import dispatch_deterministic_route
from bridge_pet_http import PetHttpApi
from bridge_pet_service import ensure_pet_tables
from bridge_conversation_memory import (
    build_group_memory_retrieval_context,
    add_memory as scoped_add_memory,
    conversation_history as scoped_conversation_history,
    delete_memory as scoped_delete_memory,
    list_memories as scoped_list_memories,
    record_conversation as scoped_record_conversation,
)
from bridge_qq_participation_shadow import complete_group_dispatch,finalize_group_shadow,observe_group_access_denied,observe_private_participation,prepare_group_dispatch,transition_group_participation,with_qq_transport_metadata
from bridge_inbound_media import inbound_media_notice, inbound_media_retry_notice
from bridge_conversation_model_runtime import run_conversation_model_reply
from bridge_conversation_reply_runtime import remaining_timeout
from bridge_private_multimodal_context import (
    append_multimodal_turn_history,
    build_ordered_turn_view,
    daily_multimodal_fast_path_allowed,
    daily_private_situation_fast_path_allowed,
    explicit_visual_evidence_requested,
    has_complete_order_contract,
    merge_visual_observations,
    private_visual_claim_without_observation,
)
from bridge_private_situation_context import (
    prior_visual_context_lines,
    select_prior_visual_contexts,
)
from bridge_conversation_visual_store import (
    ConversationVisualObservationError,
    load_cycle_visual_observation,
    record_cycle_visual_observation,
)
import bridge_visual_context as visual
from bridge_group_media_context import (
    begin_group_visual_context,
    finish_group_visual_context,
    observe_group_visual_without_reply,
    prepare_group_visual_context,
    prior_group_visual_context,
    project_group_visual_context,
)
from bridge_group_response_commitment import (
    advance_group_response_situation,
    begin_group_response_phase,
    claim_group_response_commitment,
    group_response_identity,
    settle_group_response_commitment,
)
from bridge_group_direct_dispatch import apply_group_work_boundary, dispatch_group_control_action, group_single_plan_requires_work, group_work_allowed, prepare_direct_group_turn, run_admitted_group_turn
from bridge_group_context_frame import DEFAULT_GROUP_CONTEXT_LIMIT
from bridge_task_dispatch_policy import (
    active_qq_task,
    dispatch_sandbox as _dispatch_sandbox,
    dispatch_timeout as _dispatch_timeout,
    has_explicit_delegation_contract,
    message_is_proven_daily_conversation,
    message_is_pure_assistant_address,
    message_requests_effectful_work,
    new_task_requested as _new_task_requested,
    pending_messages as _pending_messages,
    should_dispatch_as_task as _should_dispatch_as_task,
)
from bridge_group_participation_policy import natural_group_cutover_plan
from bridge_group_participation_http import GroupParticipationHttpApi
from bridge_continuity_service import (
    capture_group_single_plan_memory_candidates,
    capture_plan_candidate_metadata,
    expire_stale_memories,
)
from bridge_assistant_chat_context import (
    attach_chat_result,
    build_social_context,
    merge_shared_knowledge,
    social_result,
    summarize_prompt as _task_summary,
)
from bridge_knowledge_http import KnowledgeHttpApi
from bridge_knowledge_service import search_published
from bridge_interaction_contract import (
    assemble_response,
    delivery_mode_from_message,
    research_goal_from_message,
)
from bridge_interaction_repository import (
    load_task_delivery_projection,
    store_task_delivery_projection,
)
from bridge_interaction_http import InteractionPlanHttpApi
from bridge_interaction_action_gate import gate_actions
from bridge_interaction_runtime import InteractionPersistenceRuntime, InteractionPlannerRuntime
from bridge_approval_runtime import (
    consume_legacy_pending,
    create_legacy_pending,
    create_paused_task_approval,
    decide_formal_message,
    formal_expiry_worker,
    formal_feature_enabled,
    has_explicit_authorization as _has_explicit_authorization,
    requires_risky_confirmation as _requires_risky_confirmation,
    sync_runtime_task,
)
from bridge_formal_approval_http import FormalApprovalHttpApi
from bridge_goal_continuity import create_goal_revision, ensure_task_revision_binding, record_goal_feedback
from bridge_goal_continuity_http import GoalContinuityHttpApi
from bridge_learning_service import capture_owner_group_expression_candidate
from bridge_learning_http import LearningHttpApi
from bridge_behavior_growth_http import BehaviorEvolutionHttpApi
from bridge_behavior_evidence_collection_http import BehaviorEvidenceCollectionHttpApi
from bridge_behavior_optimizer_http import BehaviorPolicyOptimizerHttpApi
from bridge_behavior_owner_authorization_http import BehaviorOwnerAuthorizationHttpApi
from bridge_behavior_paired_shadow_http import BehaviorPairedShadowHttpApi
from bridge_behavior_private_evaluator_http import BehaviorPrivateEvaluatorHttpApi
from bridge_behavior_paired_shadow_cutover import paired_shadow_runtime_config
from bridge_network_policy import (
    apply_network_policy_command,
    get_network_policy,
    network_capability_allowed,
    parse_network_policy_command,
    task_web_search_allowed,
)
from bridge_network_policy_http import NetworkPolicyHttpApi
from bridge_goal_followup import (
    classify_goal_followup,
    followup_prompt_context,
    load_durable_failure_projection,
)
from bridge_goal_followup_runtime import (
    followup_history,
    followup_scope,
    load_goal_followup,
    resolved_failure_followup_result,
    unresolved_followup_result,
)
from bridge_model_profiles import codex_model_args
from bridge_social_engine import (
    STRUCTURED_SOCIAL_DECISION_MAX_TOKENS,
    apply_group_turn_policy,
    attachment_capability_lines,
    build_daily_system_prompt,
    build_voice_contract,
    build_group_decision_messages,
    ensure_social_experience_tables,
    get_group_policy,
    group_context,
    group_recent_turn_metadata,
    list_expression_habits,
    list_group_policies,
    mark_group_decision,
    normalize_group_inbound,
    normalize_social_reply,
    plan_expression,
    parse_group_decision,
    relationship_context_lines,
    seed_expression_habits,
    upsert_expression_habit,
    upsert_group_policy,
    voice_contract_lines,
    expression_plan_lines,
)
from bridge_prompt_cache_contract import build_conversation_messages, build_work_cache_layers, with_conversation_cache_contract, with_role_cache_contract
from bridge_group_participation_policy import group_participation_confidence_floor
from bridge_group_research_runtime import (
    build_group_research_prompt,
    parse_group_research_output,
)
from bridge_group_research_service import (
    auto_publish_low_public_knowledge,
    classify_research_topic,
    create_review_required_knowledge_draft,
    group_research_execution_allowed,
    public_research_context,
    research_unknown_topic,
)
from bridge_group_research_http import GroupResearchHttpApi
from bridge_capability_registry import (
    build_skill_context,
    discover_local_skills,
    discover_skill_plan,
    ensure_capability_tables,
    list_plugins as list_capability_plugins,
    list_skills,
    reload_plugins as reload_capability_plugins,
    seed_builtin_skills,
    set_plugin_enabled as set_capability_plugin_enabled,
    set_skill_enabled,
    upsert_skill,
    validate_skill_contract,
)
from bridge_capabilities import CapabilityCatalog, get_fixed_capability, list_fixed_capabilities
from bridge_plugin_marketplace import (
    ensure_plugin_market_tables,
    get_marketplace,
    list_market_operations,
    operate_market_plugin,
)
from bridge_delivery_outbox import DeliveryOutbox, LeaseLostError
from bridge_delivery_operations import (
    delivery_task_id as _delivery_task_id,
    is_terminal_task_delivery as _is_terminal_task_delivery,
    requeue_delivery,
)
from bridge_delivery_continuity import logical_response_id, unified_delivery_enabled
from bridge_delivery_claim import claim_deliveries
from bridge_delivery_settlement import settle_ack, settle_ambiguous, settle_retry
from bridge_proactive_decision_contract import (
    parse_proactive_json as _parse_proactive_json,
    proactive_system_prompt,
    sanitize_proactive_decision as _sanitize_proactive_decision,
)
from bridge_outbound_policy import DeliveryPolicyBlockedError, begin_delivery_with_policy, filter_claimed_deliveries, social_proactive_globally_enabled
from bridge_qq_delivery import (
    bind_qq_response_decision,
    dispatch_qq_response,
    enqueue_qq_response,
    load_qq_delivery_sessions,
    reserve_qq_response,
)
from bridge_group_participation_worker import process_group_participation_queue
from bridge_group_single_plan import (
    GROUP_SINGLE_PLAN_OUTPUT_PROTOCOL,
    build_group_single_plan_messages,
    parse_group_single_plan,
    run_group_single_plan,
    repair_group_risky_reply,
)
from bridge_qq_quality_receipt import project_group_dispatch_delivery
from bridge_social_reply import group_reply_style_issues_for_delivery
from bridge_proactive_runtime import process_proactive_policies
from bridge_task_delivery import enqueue_task_result
from bridge_task_expression import (
    ARTIFACT_READY,
    FALLBACK_HEARTBEAT,
    MEANINGFUL_PROGRESS,
    REVISION_STARTED,
    REVISION_SUCCEEDED,
    TASK_ACCEPTED,
    TASK_FAILED,
    TASK_SUCCEEDED,
    persona_frame,
    render_task_character_expression,
    task_accepted_text,
    task_append_text,
    task_approval_text,
    task_blocked_error,
    task_expression_enabled,
    task_failure_projection_text,
    task_failure_fact_slots,
    task_lifecycle_fact,
    task_lifecycle_blocks,
    task_status_blocks,
    task_terminal_text,
    valid_retained_research_evidence,
)
from bridge_relationship_accumulation import (
    accumulation_mode_from_settings as _relationship_accumulation_mode,
    apply_accumulation as _relationship_apply_accumulation,
    ensure_relationship_accumulation_tables as _ensure_relationship_accumulation_tables,
)
from bridge_work_context import resolve_work_cwd, work_context_routing_enabled
from bridge_automation import (
    attach_proactive_delivery,
    claim_due_jobs,
    claim_due_proactive_policies,
    ensure_automation_tables,
    finish_automation_run,
    list_automation_jobs,
    list_automation_runs,
    list_automation_seen_items,
    list_proactive_events,
    list_proactive_policies,
    note_user_activity,
    record_proactive_decision,
    record_proactive_failure,
    reconcile_group_proactive_policies,
    reconcile_owner_proactive_policy,
    reserve_automation_items,
    seconds_until_next_event,
    transition_automation_job_archive,
    upsert_automation_job,
    upsert_proactive_policy,
)
from bridge_automation_actions import dispatch_automation_action
from bridge_automation_execution_contract import (
    audit_execution_contract_repair,
    derive_execution_contract,
    normalize_execution_contract,
)
from bridge_automation_capability_runtime import execute_automation_capability
from bridge_automation_reference_runtime import (
    github_purpose_summaries,
    prepare_github_delivery_payload,
    resolve_automation_target,
)
from bridge_automation_execution import (
    automation_thread_ref as _automation_thread_ref,
    build_skill_execution_contract as _build_skill_execution_contract,
    classify_automation_failure as _classify_automation_failure,
    is_permanent_error as _automation_error_is_permanent,
    notify_failure as _notify_automation_failure_impl,
    preflight as _automation_preflight,
)
from bridge_reliability_runtime import drain_action_outbox
from bridge_automation_reliability import reconcile_automation_tasks
from bridge_automation_business_gate import (
    automation_leak_gate as _automation_leak_gate,
    evaluate_automation_business_verdict as _automation_business_verdict,
)

# Public alias surface used by tests and the reconciler.
automation_leak_gate = _automation_leak_gate
evaluate_automation_business_verdict = _automation_business_verdict
from bridge_automation_worker import run_automation_worker
from bridge_inbound_idempotency import (
    InboundConflictError, InboundIdempotencyUnavailableError, InboundOutcomeUnknownError,
    InboundProcessingError, InboundRequestValidationError, execute_once as execute_inbound_once,
    web_dispatch_receipt_context,
)
from bridge_inbound_context import current_inbound_exchange_context, inbound_exchange_context
from bridge_continuous_private_conversation import (
    ContinuousPrivateConversationDisabledError,
    ResponseCycleBusyError,
    ResponseCycleEffectBlockedError,
    ResponseCycleLeaseError,
    ResponseCycleRecoveryError,
    ResponseCycleSupersededError,
    accept_private_ingress,
    acquire_response_cycle,
    assert_response_cycle_effects_allowed,
    bind_prepared_response_delivery,
    complete_response_cycle_outbox,
    continuous_private_conversation_enabled,
    mark_response_cycle_failure,
    mark_response_cycle_generation_started,
    prepare_or_supersede_response_cycle_delivery,
    prepare_response_cycle_delivery,
    project_delivery_state,
    set_continuous_private_conversation_feature,
)
from bridge_response_coordinator import BridgeResponseCoordinator
from bridge_private_turn_idempotency import (
    register_private_turn_member_aliases,
    replay_private_turn_member,
)
from bridge_reliability_http import ReliabilityHttpApi
from bridge_task_followup import consume_running_supplements
from bridge_task_persistence import task_db_payload
from bridge_task_retry import retry_task
from bridge_task_query import (
    get_task as query_task,
    list_tasks as query_tasks,
    load_active_and_recent,
)
from bridge_light_executor import LightExecutor
from bridge_capability_routing import (
    resolve_capability_requirements,
    resolve_execution_context,
    select_execution_lane,
)
from bridge_private_research import RESEARCH_CAPABILITY_ID, execute_private_research, filter_summary_points, render_research_reply
from bridge_platform_repository import PlatformRepository, ensure_platform_schema
from bridge_model_registry import (
    activate_work_executor,
    bind_model_role,
    ensure_model_registry_tables,
    list_model_registry,
    list_role_change_log,
    provider_test_settings,
    reconcile_active_work_executor_runtime,
    recover_interrupted_work_executor_activation,
    record_provider_test,
    seed_model_registry,
    upsert_model,
    upsert_provider,
)
from bridge_model_discovery import discover_provider_models, discovered_model_validation_settings
from bridge_model_role_runtime import runtime_settings_for_role_safe
from bridge_model_probe_log import list_proxy_probe_log, record_proxy_probe
from bridge_executor_runtime import (
    codex_exec_args as _codex_exec_args,
    codex_exec_env,
    validate_executor_sandbox_and_cwd as _validate_executor_sandbox_and_cwd,
)
from bridge_executor_apply import (
    apply_profiles_for_dependency,
    executor_runtime_identity_matches,
    executor_runtime_shared_lock,
    model_runtime_dependency_fingerprint,
    provider_runtime_dependency_fingerprint,
)
from bridge_ops_broker_client import OpsBrokerClient, OpsBrokerClientError
from bridge_ops_actions import admin_token_client_error, broker_write
from bridge_ops_command_router import capture_command as _capture_command_via_broker
from bridge_service_status import collect_service_status
from bridge_connectivity_probe import probe_bridge
from bridge_container_status import collect_containers
from bridge_provider_secrets import prune_unreferenced_provider_secrets
from bridge_model_control import (
    LEGACY_MODEL_KEYS,
    contract_catalog,
    validate_legacy_model_write,
)
from bridge_model_runtime_inventory import runtime_inventories
from bridge_model_instances import (
    connection_templates,
    delete_model,
    delete_provider,
    dependency_error_payload,
)
from bridge_model_observability import (
    ensure_model_usage_tables,
    provider_reported_model,
    recent_model_observations,
    record_model_usage,
    usage_report,
)
from bridge_model_adapters import openai_response_facts, prepare_model_request, parse_model_response
from bridge_conversation_reply_runtime import (
    call_openai_conversation_reply,
    call_openai_with_empty_retry,
)
from bridge_provider_errors import (
    is_hard_quota_response,
    provider_http_error_facts,
    provider_transport_error_kind,
)
from bridge_action_truth import enforce_action_truth
from bridge_model_playground import run_model_playground
from bridge_executor_profiles import (
    executor_workspace_root,
    get_executor_profile,
    profile_sha256,
    read_executor_credential,
)
from bridge_executor_verification import verify_executor_work_mode
from bridge_codex_operations import codex_operations_status
from bridge_proxy_status import proxy_status, proxy_full_probe, proxy_executor_test
import bridge_assistant_migrations as am
from bridge_agent_modes import (
    AGENT_MODE_BOOLEAN_KEYS,
    AGENT_MODE_CHOICES,
    AGENT_MODE_DEFAULTS,
    AGENT_MODE_SETTING_KEYS,
    acceptance_criteria as build_acceptance_criteria,
    agent_policy_lines as build_agent_policy_lines,
    build_agent_policy,
    detect_agent_intent,
    intent_label,
    mode_policy_lines,
    normalize_agent_policy_setting as normalize_agent_mode_setting,
    quality_check_response as check_agent_response_quality,
    requires_fresh_external_data,
    truthy_setting as bridge_truthy_setting,
)
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "18777"))
OPS_BROKER_SOCKET = os.environ.get("OPS_BROKER_SOCKET", "/run/agent-bridge/ops.sock")
OPS_BROKER_REQUIRED = os.environ.get("OPS_BROKER_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}
OPS_BROKER_SHADOW = os.environ.get("OPS_BROKER_SHADOW", "0").strip().lower() in {"1", "true", "yes", "on"}
TOKEN_PATH = Path(os.environ.get("TOKEN_PATH", "/etc/agent-bridge/secrets/admin-token"))
CHANNEL_TOKEN_PATH = Path(os.environ.get("CHANNEL_TOKEN_PATH", "/etc/agent-bridge/secrets/qq-channel-token"))
ADMIN_GATEWAY_TOKEN_PATH = Path(
    os.environ.get("ADMIN_GATEWAY_TOKEN_PATH", "/etc/agent-bridge/secrets/admin-gateway-token"),
)
WORKSPACE_BASE = Path(os.environ.get("WORKSPACE_BASE", "/opt/agent-workspace")).resolve()
DEFAULT_CWD = Path(os.environ.get("DEFAULT_CWD", str(WORKSPACE_BASE))).resolve()
MIHOMO_CONTROLLER_URL = os.environ.get("MIHOMO_CONTROLLER_URL", "http://127.0.0.1:9090").rstrip("/")
MIHOMO_CONTROLLER_SECRET = os.environ.get("MIHOMO_CONTROLLER_SECRET", "")
MIHOMO_PROXY_URL = os.environ.get("MIHOMO_PROXY_URL", "http://127.0.0.1:7890")
MIHOMO_SOCKS_PROXY_URL = os.environ.get("MIHOMO_SOCKS_PROXY_URL", "")
MIHOMO_CONFIG_PATH = Path(os.environ.get("MIHOMO_CONFIG_PATH", "/etc/mihomo/config.yaml"))
MIHOMO_CONFIG_DIR = Path(os.environ.get("MIHOMO_CONFIG_DIR", "/etc/mihomo"))
MIHOMO_SUBSCRIPTION_STATE_PATH = Path(
    os.environ.get("MIHOMO_SUBSCRIPTION_STATE_PATH", "/etc/mihomo/codex-subscriptions.json"),
)
ASTRBOT_CONTAINER = os.environ.get("ASTRBOT_CONTAINER", "astrbot")
NAPCAT_CONTAINER = os.environ.get("NAPCAT_CONTAINER", "maim-bot-napcat")
QQ_ADAPTER = os.environ.get("QQ_ADAPTER", "napcat").strip().lower()
LLBOT_SERVICE = os.environ.get("LLBOT_SERVICE", "llbot").strip() or "llbot"
MIHOMO_CONTAINER = os.environ.get("MIHOMO_CONTAINER", "mihomo")
MAIM_BOT_CORE_CONTAINER = os.environ.get("MAIM_BOT_CORE_CONTAINER", "maim-bot-core")
NAPCAT_QRCODE_PATH = os.environ.get("NAPCAT_QRCODE_PATH", "/app/napcat/cache/qrcode.png")
NAPCAT_QRCODE_MAX_AGE_SECONDS = max(
    30,
    min(int(os.environ.get("NAPCAT_QRCODE_MAX_AGE_SECONDS", "300")), 900),
)
LLBOT_QRCODE_MAX_AGE_SECONDS = max(
    30,
    min(int(os.environ.get("LLBOT_QRCODE_MAX_AGE_SECONDS", "180")), 600),
)
NAPCAT_QRCODE_CANDIDATES = tuple(
    item.strip()
    for item in os.environ.get(
        "NAPCAT_QRCODE_CANDIDATES",
        ",".join(
            (
                NAPCAT_QRCODE_PATH,
                "/app/.config/QQ/NapCat/cache/qrcode.png",
                "/root/.config/QQ/NapCat/cache/qrcode.png",
                "/tmp/qrcode.png",
            )
        ),
    ).split(",")
    if item.strip()
)
def _ops_broker_request(action: str, target: str, args: dict | None = None) -> dict:
    if not (OPS_BROKER_REQUIRED or OPS_BROKER_SHADOW):
        raise OpsBrokerClientError("broker_disabled")
    return OpsBrokerClient(OPS_BROKER_SOCKET).request({
        "action": action,
        "target": target,
        "args": args or {},
    })


def _ops_broker_write_request(action: str, target: str, args: dict | None = None, *, request_id: str = "") -> dict:
    if not OPS_BROKER_REQUIRED:
        raise OpsBrokerClientError("broker_required_for_proxy_write")
    operation = {"action": action, "target": target, "args": args or {}}
    operation["approval"] = {
        "action_hash": action_hash(operation),
        "version": 1,
        "idempotency_key": request_id or f"{action}:{uuid.uuid4().hex}",
        "expires_at": datetime.fromtimestamp(
            time.time() + (150 if action == "aiclient_proxy_test" else 120),
            timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
    }
    timeout = 135.0 if action == "aiclient_proxy_test" else 30.0
    return OpsBrokerClient(OPS_BROKER_SOCKET, timeout=timeout).request(operation)


def _safe_request_id(handler) -> str:
    value = str(handler.headers.get("X-Request-ID") or "").strip()
    return value if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}", value) else ""


def _with_request_id(payload: dict, request_id: str) -> dict:
    result = dict(payload)
    if request_id:
        result["request_id"] = request_id
    return result
PROXY_TEST_TARGETS = (
    {"name": "chatgpt", "label": "ChatGPT", "url": "https://chatgpt.com/cdn-cgi/trace", "required": True},
    {"name": "chatgpt_backend", "label": "ChatGPT backend", "url": "https://chatgpt.com/backend-api/models", "required": True},
    {"name": "openai", "label": "OpenAI API", "url": "https://api.openai.com/v1/models", "required": True},
    {"name": "github", "label": "GitHub", "url": "https://github.com", "required": False},
)
PROXY_OK_HTTP_CODES = {"200", "204", "301", "302", "401", "403"}
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "12000"))
MAX_OUTPUT_CHARS = int(os.environ.get("MAX_OUTPUT_CHARS", "12000"))
MAX_TASKS = int(os.environ.get("MAX_TASKS", "100"))
TASK_HISTORY_PATH = Path(
    os.environ.get("TASK_HISTORY_PATH", "/opt/agent-stack/codex-qq-bridge/tasks.jsonl"),
)
TASK_DB_PATH = Path(
    os.environ.get("TASK_DB_PATH", "/opt/agent-stack/codex-qq-bridge/tasks.sqlite3"),
)
ASSISTANT_DB_PATH = Path(
    os.environ.get("ASSISTANT_DB_PATH", "/opt/agent-stack/codex-qq-bridge/assistant.sqlite3"),
)
SAMPLE_BACKGROUND_ASSET_PATH = Path(
    os.environ.get(
        "SAMPLE_BACKGROUND_ASSET_PATH",
        "/opt/agent-stack/codex-qq-bridge/assets/sample-background.jpg",
    ),
)
TRENDING_CACHE_PATH = Path(
    os.environ.get(
        "TRENDING_CACHE_PATH",
        "/opt/agent-stack/codex-qq-bridge/github_trending_cache.json",
    ),
)
ALLOWED_CLIENTS = os.environ.get(
    "ALLOWED_CLIENTS",
    "127.0.0.0/8,172.16.0.0/12",
)
ALLOW_PUBLIC_TOKEN_AUTH = os.environ.get("ALLOW_PUBLIC_TOKEN_AUTH", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ADMIN_SESSION_COOKIE = os.environ.get("ADMIN_SESSION_COOKIE", "codex_admin_session")
ADMIN_SESSION_TTL = int(os.environ.get("ADMIN_SESSION_TTL", "86400"))
ADMIN_COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "1").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ADMIN_LOGIN_MAX_FAILURES = int(os.environ.get("ADMIN_LOGIN_MAX_FAILURES", "8"))
ADMIN_LOGIN_WINDOW = int(os.environ.get("ADMIN_LOGIN_WINDOW", "300"))
CODEGRAPH_AUTO_ENABLED = os.environ.get("CODEGRAPH_AUTO_ENABLED", "1").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CODEGRAPH_COMMAND = os.environ.get("CODEGRAPH_COMMAND", "codegraph")
CODEGRAPH_AUTO_TIMEOUT = int(os.environ.get("CODEGRAPH_AUTO_TIMEOUT", "45"))
CODEGRAPH_AUTO_MIN_INTERVAL = int(os.environ.get("CODEGRAPH_AUTO_MIN_INTERVAL", "15"))
ASSISTANT_CHAT_TIMEOUT = int(os.environ.get("ASSISTANT_CHAT_TIMEOUT", "180"))
ASSISTANT_HISTORY_LIMIT = int(os.environ.get("ASSISTANT_HISTORY_LIMIT", "10"))
ASSISTANT_MEMORY_LIMIT = int(os.environ.get("ASSISTANT_MEMORY_LIMIT", "8"))
EXTRA_CWD_ROOTS = tuple(
    Path(item.strip()).resolve()
    for item in os.environ.get("EXTRA_CWD_ROOTS", "").split(",")
    if item.strip()
)
RUN_LOCK = threading.Lock()
TASK_LOCK = threading.RLock()
CODEGRAPH_LOCK = threading.RLock()
ASSISTANT_LOCK = threading.RLock()
ADMIN_SESSION_LOCK = threading.RLock()
LOGIN_FAILURE_LOCK = threading.RLock()
TASK_QUEUE: deque[str] = deque()
TASKS: dict[str, dict] = {}
ADMIN_SESSIONS: dict[str, float] = {}
LOGIN_FAILURES: dict[str, list[float]] = {}
CODEGRAPH_LAST_RUN: dict[str, float] = {}
TASK_EVENT = threading.Event()
AUTOMATION_EVENT = threading.Event()
PHASE2_OUTBOX_LOCK = threading.Lock()
PHASE2_OUTBOXES: dict[str, DeliveryOutbox] = {}
PHASE2_PRESENCE_OUTBOXES: dict[str, DeliveryOutbox] = {}
_TASK_EXECUTION_PROGRESS_LOCK = threading.RLock()
_TASK_EXECUTION_PROGRESS: dict[str, object] = {}
_PRIVATE_RESPONSE_OWNERSHIP_LOCK = threading.RLock()
_PRIVATE_RESPONSE_OWNERSHIPS: dict[str, dict] = {}
PHASE2_CAPABILITY_CATALOG = CapabilityCatalog()
FINAL_STATUSES = {"done", "failed", "timeout", "cancelled"}
TASK_STATUSES = FINAL_STATUSES | {"queued", "running", "waiting_approval"}
RETRYABLE_STATUSES = {"failed", "timeout", "cancelled"}
QQ_TASK_SOURCE = "qq"
TASK_DELIVERY_NONE = "none"
TASK_DELIVERY_PENDING = "pending"
WORK_TASK_TIMEOUT = int(os.environ.get("WORK_TASK_TIMEOUT", "600"))
DISPATCH_CHAT_TIMEOUT = int(os.environ.get("DISPATCH_CHAT_TIMEOUT", "180"))
_PRIVATE_MULTIMODAL_CLOCK = time.monotonic
_PRIVATE_MULTIMODAL_DAILY_DEADLINE_SECONDS = 45
_PRIVATE_MULTIMODAL_EVIDENCE_DEADLINE_SECONDS = 60
_PRIVATE_MULTIMODAL_FINAL_REPLY_RESERVE_SECONDS = 15
_PRIVATE_MULTIMODAL_DAILY_VISUAL_SECONDS = 8
_PRIVATE_MULTIMODAL_EVIDENCE_VISUAL_SECONDS = 15
PROJECT_MARKERS = (
    ".git",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "docker-compose.yml",
)
SOURCE_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".php",
    ".rb",
    ".swift",
    ".vue",
    ".svelte",
)
DEFAULT_ASSISTANT_SETTINGS = {
    "display_name": "Assistant",
    "relationship": "朋友",
    "persona": "你是长期陪伴同一位用户的私人 AI 助手：熟悉、可靠、有自己的判断。日常愿意接住情绪和玩笑，工作时会认真把事情做完；不谄媚、不装真人，也不靠固定口癖表演人格。",
    "style": "使用自然中文和有呼吸感的短句。先回应对方真正说的重点，再补必要内容；能一句说清就不写报告。可以温和、俏皮或直接，但不客服化、不机械复述、不强行追问。技术结论与执行状态必须准确。",
    "chat_provider": "codex",
    "chat_provider_preset": "codex",
    "chat_base_url": "https://api.openai.com/v1",
    "chat_model": "",
    "chat_temperature": "0.7",
    "chat_max_tokens": "900",
    "chat_api_key": "",
    "codex_model_profile": "",
    "codex_model": "",
    "meme_enabled": "1",
    "meme_daily_enabled": "1",
    "meme_work_enabled": "0",
    "proactive_enabled": "0",
    "task_expression_v1": "0",
    "work_context_routing_v1": "0",
    "relationship_accumulation_v1": "0",
    "relationship_accumulation_mode": "off",
    "agent_language": "zh-CN",
    "agent_detail_level": "standard",
    "agent_persona_level": "full",
    "agent_technical_mode": "professional",
    "agent_summarize_tools": "1",
    "agent_disclose_fallback": "1",
    "agent_self_check": "1",
    "agent_clarify_when_uncertain": "1",
    "agent_confirm_risky_ops": "1",
    "agent_quality_log_enabled": "1",
} | AGENT_MODE_DEFAULTS
_ASSISTANT_SETTINGS_CACHE_LOCK = threading.RLock()
_ASSISTANT_SETTINGS_CACHE: dict | None = None
AGENT_POLICY_SETTING_KEYS = {
    "agent_language",
    "agent_detail_level",
    "agent_persona_level",
    "agent_technical_mode",
    "agent_summarize_tools",
    "agent_disclose_fallback",
    "agent_self_check",
    "agent_clarify_when_uncertain",
    "agent_confirm_risky_ops",
    "agent_quality_log_enabled",
} | AGENT_MODE_SETTING_KEYS
AGENT_POLICY_BOOLEAN_KEYS = {
    "agent_summarize_tools",
    "agent_disclose_fallback",
    "agent_self_check",
    "agent_clarify_when_uncertain",
    "agent_confirm_risky_ops",
    "agent_quality_log_enabled",
} | AGENT_MODE_BOOLEAN_KEYS
AGENT_POLICY_CHOICES = {
    "agent_language": {"zh-CN", "auto"},
    "agent_detail_level": {"brief", "standard", "detailed"},
    "agent_persona_level": {"off", "light", "full"},
    "agent_technical_mode": {"professional", "balanced", "friendly"},
} | AGENT_MODE_CHOICES
ASSISTANT_PUBLIC_SETTING_KEYS = {
    "display_name",
    "relationship",
    "persona",
    "style",
    "chat_provider",
    "chat_provider_preset",
    "chat_base_url",
    "chat_model",
    "chat_temperature",
    "chat_max_tokens",
    "codex_model_profile",
    "codex_model",
    "meme_enabled",
    "meme_daily_enabled",
    "meme_work_enabled",
    "proactive_enabled",
    "task_expression_v1",
    "work_context_routing_v1",
    "relationship_accumulation_v1",
    "relationship_accumulation_mode",
} | AGENT_POLICY_SETTING_KEYS
ASSISTANT_SECRET_SETTING_KEYS = {"chat_api_key"}
CHAT_PROVIDERS = {"codex", "openai-compatible"}
# E-2: the QQ channel adapter may not write the model/provider control plane.
# LLBot legitimately writes persona/relationship/display_name/style; the
# provider endpoint, provider selection, model identity and credentials are
# admin-owned and feed the executor and chat completion Authorization header.
QQ_CHANNEL_FORBIDDEN_SETTING_KEYS = frozenset(
    {
        "chat_provider",
        "chat_provider_preset",
        "chat_base_url",
        "chat_model",
        "chat_api_key",
        "clear_chat_api_key",
        "codex_model",
        "codex_model_profile",
    },
)
DEFAULT_SAMPLE_BACKGROUND_URL = "/admin/assets/sample-background.jpg"
DEFAULT_ADMIN_APPEARANCE_SETTINGS = {
    "admin_background_enabled": "1",
    "admin_background_url": DEFAULT_SAMPLE_BACKGROUND_URL,
    "admin_background_dim": "0.12",
    "admin_panel_opacity": "0.88",
}
ADMIN_APPEARANCE_KEYS = set(DEFAULT_ADMIN_APPEARANCE_SETTINGS)
MEMORY_TRIGGERS = (
    "记住",
    "请记住",
    "你要记住",
    "以后记得",
    "以后要记得",
)
MEMORY_FACT_HINTS = (
    "我叫",
    "我是",
    "我的",
    "我喜欢",
    "我不喜欢",
    "我讨厌",
    "我希望",
    "我习惯",
    "我正在",
    "我想要",
    "以后",
)
TASK_DB_COLUMNS = (
    "id",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "sandbox",
    "cwd",
    "summary",
    "prompt",
    "timeout",
    "duration",
    "returncode",
    "ok",
    "cancel_requested",
    "error_kind",
    "source_task_id",
    "stdout",
    "stderr",
    "output",
    "error",
    "updated_at",
    "source",
    "user_id",
    "trace_id",
    "origin_message",
    "intent",
    "mode",
    "delivery_status",
    "delivery_error",
    "delivered_at",
    "delivery_attempts",
    "delivery_next_at",
    "pending_messages",
    "delivery_recipient_id",
    "delivery_session",
    "request_idempotency_key",
    "automation_run_id",
    "follow_up_source_task_id",
    "executor_provider_id",
    "executor_model_id",
    "executor_model_name",
    "executor_adapter",
    "executor_config_version",
    "executor_profile_sha256",
    "artifact_revision_id",
    "artifact_revision_base_version_id",
    "network_mode",
)
def _allowed_networks() -> list[ipaddress._BaseNetwork]:
    networks = []
    for item in ALLOWED_CLIENTS.split(","):
        item = item.strip()
        if item:
            networks.append(ipaddress.ip_network(item, strict=False))
    return networks
ALLOWED_NETWORKS = _allowed_networks()
def _read_token() -> str:
    token = read_secret(TOKEN_PATH)
    if not admin_token_value_valid(token):
        return ""
    return token if secrets_distinct(token, read_secret(CHANNEL_TOKEN_PATH)) else ""
def _fixed_token_status() -> dict:
    return fixed_token_status(TOKEN_PATH, configured=bool(_read_token()))
def _validate_fixed_token(value: object, confirmation: object) -> str:
    return validate_admin_token(
        value,
        confirmation,
        current_token=_read_token(),
        channel_token=read_secret(CHANNEL_TOKEN_PATH),
    )
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
def _cookie_header(name: str, value: str, *, max_age: int) -> str:
    parts = [
        f"{name}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if ADMIN_COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)
def _read_cookie(handler: http.server.BaseHTTPRequestHandler, name: str) -> str:
    raw_cookie = handler.headers.get("Cookie", "")
    if not raw_cookie:
        return ""
    try:
        cookie = SimpleCookie(raw_cookie)
    except Exception:
        return ""
    item = cookie.get(name)
    return item.value if item else ""
def _cleanup_admin_sessions(now: float | None = None) -> None:
    now = now or time.time()
    expired = [key for key, expires_at in ADMIN_SESSIONS.items() if expires_at <= now]
    for key in expired:
        ADMIN_SESSIONS.pop(key, None)
def _create_admin_session() -> str:
    now = time.time()
    session_id = secrets.token_urlsafe(32)
    with ADMIN_SESSION_LOCK:
        _cleanup_admin_sessions(now)
        ADMIN_SESSIONS[session_id] = now + ADMIN_SESSION_TTL
    return _cookie_header(ADMIN_SESSION_COOKIE, session_id, max_age=ADMIN_SESSION_TTL)

def _clear_admin_session(handler: http.server.BaseHTTPRequestHandler) -> str:
    session_id = _read_cookie(handler, ADMIN_SESSION_COOKIE)
    if session_id:
        with ADMIN_SESSION_LOCK:
            ADMIN_SESSIONS.pop(session_id, None)
    return _cookie_header(ADMIN_SESSION_COOKIE, "", max_age=0)

def _clear_all_admin_sessions() -> None:
    with ADMIN_SESSION_LOCK:
        ADMIN_SESSIONS.clear()

def _has_admin_session(handler: http.server.BaseHTTPRequestHandler) -> bool:
    session_id = _read_cookie(handler, ADMIN_SESSION_COOKIE)
    if not session_id:
        return False
    now = time.time()
    with ADMIN_SESSION_LOCK:
        expires_at = ADMIN_SESSIONS.get(session_id)
        if not expires_at or expires_at <= now:
            ADMIN_SESSIONS.pop(session_id, None)
            return False
        ADMIN_SESSIONS[session_id] = now + ADMIN_SESSION_TTL
        return True


def _web_dispatch_receipt_context(handler: http.server.BaseHTTPRequestHandler, principal: PrincipalKind) -> dict[str, str]:
    """Build a durable Web receipt scope without persisting a raw session/token."""
    if principal is PrincipalKind.ADMIN_SESSION:
        session_identity = _read_cookie(handler, ADMIN_SESSION_COOKIE)
    elif principal is PrincipalKind.ADMIN_TOKEN:
        # Admin Token is one server-side control-plane principal.  Keeping its
        # receipt namespace stable across rotation prevents a client retry from
        # becoming a new business execution merely because a secret changed.
        # Neither the raw token nor its hash is persisted.
        session_identity = "admin-token"
    elif principal is PrincipalKind.ADMIN_GATEWAY:
        # The Gateway is one stable, independently authorized Web ingress
        # principal.  Never persist or derive this scope from its raw token.
        session_identity = "admin-gateway"
    else:
        raise InboundRequestValidationError("web_dispatch_admin_required")
    request_id = str(handler.headers.get("X-Request-ID", "") or "").strip()
    legacy_request_id = str(handler.headers.get("X-QQ-Message-ID", "") or "").strip()
    if request_id and legacy_request_id and request_id != legacy_request_id:
        raise InboundRequestValidationError("web_dispatch_request_id_conflict")
    return web_dispatch_receipt_context(
        principal.value,
        session_identity,
        request_id or legacy_request_id,
    )

def _recent_login_failures(client_ip: str, now: float | None = None) -> list[float]:
    now = now or time.time()
    cutoff = now - ADMIN_LOGIN_WINDOW
    with LOGIN_FAILURE_LOCK:
        recent = [item for item in LOGIN_FAILURES.get(client_ip, []) if item >= cutoff]
        if recent:
            LOGIN_FAILURES[client_ip] = recent
        else:
            LOGIN_FAILURES.pop(client_ip, None)
        return recent


def _login_rate_limited(client_ip: str) -> bool:
    return len(_recent_login_failures(client_ip)) >= ADMIN_LOGIN_MAX_FAILURES


def _record_login_failure(client_ip: str) -> None:
    now = time.time()
    with LOGIN_FAILURE_LOCK:
        recent = _recent_login_failures(client_ip, now)
        recent.append(now)
        LOGIN_FAILURES[client_ip] = recent


def _clear_login_failures(client_ip: str) -> None:
    with LOGIN_FAILURE_LOCK:
        LOGIN_FAILURES.pop(client_ip, None)


ADMIN_HTML = "<!doctype html><title>Admin unavailable</title><h1>Admin console module failed to load.</h1>"


def _invalidate_assistant_settings_cache() -> None:
    global _ASSISTANT_SETTINGS_CACHE
    with _ASSISTANT_SETTINGS_CACHE_LOCK:
        _ASSISTANT_SETTINGS_CACHE = None


def _invalidate_assistant_home_projection_cache() -> None:
    service = globals().get("ASSISTANT_HOME_SERVICE")
    invalidate = getattr(service, "invalidate", None)
    if callable(invalidate):
        invalidate()


def _invalidate_assistant_home_cache() -> None:
    _invalidate_assistant_settings_cache()
    _invalidate_assistant_home_projection_cache()


def _db_connect() -> sqlite3.Connection:
    return connect_home_database(
        TASK_DB_PATH,
        ASSISTANT_HOME_TASK_TABLES,
        _invalidate_assistant_home_projection_cache,
    )


def _phase2_outbox() -> DeliveryOutbox:
    key = os.path.normcase(str(TASK_DB_PATH.resolve()))
    with PHASE2_OUTBOX_LOCK:
        outbox = PHASE2_OUTBOXES.get(key)
        if outbox is None:
            outbox = DeliveryOutbox(
                TASK_DB_PATH,
                mutation_callback=_invalidate_assistant_home_projection_cache,
            )
            PHASE2_OUTBOXES[key] = outbox
    return outbox


def _phase2_presence_outbox() -> DeliveryOutbox:
    """Presence writes fail fast under SQLite contention."""

    key = os.path.normcase(str(TASK_DB_PATH.resolve()))
    with PHASE2_OUTBOX_LOCK:
        outbox = PHASE2_PRESENCE_OUTBOXES.get(key)
        if outbox is None:
            outbox = DeliveryOutbox(
                TASK_DB_PATH,
                busy_timeout_seconds=0.5,
                mutation_callback=_invalidate_assistant_home_projection_cache,
            )
            PHASE2_PRESENCE_OUTBOXES[key] = outbox
    return outbox


_EXECUTION_PRESENCE_PROGRESS_SECONDS = (30, 90)
_EXECUTION_PRESENCE_TIMER_FACTORY = threading.Timer
_EXECUTION_PRESENCE_CLOCK = time.monotonic
_PRIVATE_RESPONSE_OWNERSHIP_CLOCK = time.monotonic
_PRIVATE_RESPONSE_OWNERSHIP_TTL_SECONDS = 600.0
_PRIVATE_RESPONSE_OWNERSHIP_MAX = 64


def _execution_presence_result(
    settings: dict,
    *,
    stage: str,
    task_id: str,
    interaction_plan: Mapping[str, object] | None = None,
    heartbeat_slot: int = 0,
    milestone: str = "",
    channel_shows_identity: bool = False,
) -> dict:
    if stage == "ack":
        event, action = TASK_ACCEPTED, "accepted"
    elif stage == "meaningful_progress":
        event, action = MEANINGFUL_PROGRESS, "meaningful_progress"
    elif stage == "progress":
        event, action = FALLBACK_HEARTBEAT, "fallback_heartbeat"
    else:
        raise ValueError("unsupported_execution_presence_stage")
    if event == TASK_ACCEPTED:
        fact_slots = {"execution_established": True, "accepted_scope": "当前请求"}
        factual_text = "已接受范围：当前请求。执行链路已经建立。"
    elif event == MEANINGFUL_PROGRESS:
        fact_slots = {"milestone": str(milestone or "")}
        factual_text = task_lifecycle_fact(event, milestone=milestone)
    else:
        fact_slots = {"running": True}
        factual_text = task_lifecycle_fact(event, milestone=milestone)
    blocks, reply = _assemble_task_status_response(
        settings,
        dict(interaction_plan or {}) if stage == "ack" else {},
        factual_text,
        factual_type="status",
        action=action,
        variant=max(0, int(heartbeat_slot) - 1),
        event=event,
        fact_slots=fact_slots,
        channel_shows_identity=channel_shows_identity,
    )
    return {
        "ok": True,
        "dispatch": (
            "execution_presence_ack"
            if stage == "ack"
            else "execution_presence_milestone"
            if stage == "meaningful_progress"
            else "execution_presence_progress"
        ),
        "reply": reply,
        "content_blocks": blocks,
        "execution_presence": True,
        "task_id": str(task_id),
    }


def _enqueue_execution_presence(
    outbox: DeliveryOutbox,
    transport: dict,
    *,
    scope: str,
    settings: dict,
    stage: str,
    suffix: str,
    task_id: str,
    interaction_plan: Mapping[str, object] | None = None,
    heartbeat_slot: int = 0,
    milestone: str = "",
    response_identity: Mapping[str, object] | None = None,
    prepare_callback=None,
) -> dict:
    prepared = reserve_qq_response(
        outbox,
        transport,
        scope=scope,
        reservation_suffix=suffix,
    )
    return enqueue_qq_response(
        outbox,
        _execution_presence_result(
            settings,
            stage=stage,
            task_id=task_id,
            interaction_plan=interaction_plan,
            heartbeat_slot=heartbeat_slot,
            milestone=milestone,
            channel_shows_identity=True,
        ),
        prepared,
        scope=scope,
        voice_output=VOICE_OUTPUT_RUNTIME.prepare,
        response_identity=response_identity,
        prepare_callback=prepare_callback,
    )


def _execution_presence_ack_response_id(transport: Mapping[str, object]) -> str:
    actor_id = str(
        transport.get("sender_id") or transport.get("user_id") or ""
    ).strip()
    return logical_response_id(
        channel="qq",
        thread_ref=f"qq:private:{actor_id}",
        source_message_id=str(
            transport.get("_external_message_id")
            or transport.get("external_message_id")
            or ""
        ).strip(),
        response_kind="execution_presence_ack:execution-presence-ack",
        trace_id=str(transport.get("trace_id") or "").strip(),
    )


class _ExecutionPresence:
    """Request-owned response arbitration that starts Task-owned progress."""

    def __init__(self, outbox: DeliveryOutbox, transport: dict, *, scope: str, settings: dict):
        self._outbox = outbox
        self._transport = dict(transport)
        self._scope = scope
        self._settings = dict(settings)
        self._lock = threading.Lock()
        self._finished = False
        self._started = False
        self._task_id = ""
        self._response_started = False
        self.ack: dict | None = None
        self.ack_latency_seconds: float | None = None
        self._direct_handoff_completed = False
        self._direct_handoff_queued = False
        self._response_identity: dict | None = None
        self._prepare_callback = None

    def configure_response_cycle(self, response_identity: Mapping[str, object], prepare_callback) -> None:
        """Bind the request owner to the cycle's sole prepared response."""

        identity = dict(response_identity)
        if not identity or not callable(prepare_callback):
            raise ValueError("response_cycle_presence_contract_invalid")
        with self._lock:
            if self._response_started:
                raise RuntimeError("response_cycle_presence_already_started")
            if self._response_identity is not None and self._response_identity != identity:
                raise RuntimeError("response_cycle_presence_identity_drift")
            self._response_identity = identity
            self._prepare_callback = prepare_callback

    def enqueue_response(self, operation) -> dict:
        """Persist one normal visible response under the response-owner lock."""

        with self._lock:
            result = operation()
            if result.get("delivery_queued"):
                self._response_started = True
            return result

    def start(
        self,
        task_id: str,
        interaction_plan: Mapping[str, object] | None = None,
    ) -> bool:
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        started_at = _EXECUTION_PRESENCE_CLOCK()
        with self._lock:
            if self._finished:
                return False
            if self._started:
                return bool(self.ack and self.ack.get("delivery_queued"))
            self._started = True
            self._task_id = task_id
            try:
                self.ack = _enqueue_execution_presence(
                    self._outbox,
                    self._transport,
                    scope=self._scope,
                    settings=self._settings,
                    stage="ack",
                    suffix="execution-presence-ack",
                    task_id=task_id,
                    interaction_plan=interaction_plan,
                    response_identity=self._response_identity,
                    prepare_callback=self._prepare_callback,
                )
            except (sqlite3.Error, ValueError):
                self.ack = None
                self.ack_latency_seconds = max(
                    0.0, _EXECUTION_PRESENCE_CLOCK() - started_at,
                )
                return False
            self.ack_latency_seconds = max(
                0.0, _EXECUTION_PRESENCE_CLOCK() - started_at,
            )
            if not self.ack.get("delivery_queued"):
                return False
            self._response_started = True
        _start_task_execution_progress(
            self._outbox,
            self._transport,
            scope=self._scope,
            settings=self._settings,
            task_id=task_id,
        )
        return True

    def replay_delivery(
        self,
        task_id: str,
        *,
        task_status: str = "",
        error_kind: str = "",
    ) -> dict | None:
        """Read the existing Task opening or terminal without creating either."""

        task_id = str(task_id or "").strip()
        if not task_id:
            return None

        def reusable(delivery: dict | None) -> dict | None:
            if not isinstance(delivery, dict):
                return None
            certainty = str(delivery.get("delivery_certainty") or "")
            if delivery.get("superseded_by"):
                return None
            if delivery.get("dead_letter") and certainty != "ambiguous":
                return None
            return delivery

        try:
            terminal = reusable(self._outbox.get_delivery_by_dedupe_key(
                f"qq:task:{task_id}:final:v1",
            ))
            if terminal is not None:
                delivery = terminal
            elif (
                str(task_status or "") in FINAL_STATUSES
                or str(error_kind or "") == "execution_activation_failed"
            ):
                return None
            else:
                response_id = (
                    str((self._response_identity or {}).get("logical_response_id") or "")
                    or _execution_presence_ack_response_id(self._transport)
                )
                delivery = reusable(self._outbox.get_delivery_by_logical_response_id(response_id))
        except (sqlite3.Error, ValueError):
            return None
        payload = (
            delivery.get("payload")
            if isinstance(delivery, dict) and isinstance(delivery.get("payload"), dict)
            else {}
        )
        if str(payload.get("task_id") or "") != task_id:
            return None
        response_kind = str(payload.get("response_kind") or "")
        if response_kind not in {"", "execution_presence_ack"}:
            return None
        if not response_kind and str(payload.get("kind") or "") != "run_result":
            return None
        with self._lock:
            self._response_started = True
        return delivery

    def milestone(self, milestone: str) -> bool:
        """Delegate one allowlisted milestone to the durable Task-owned lifecycle."""

        milestone = str(milestone or "").strip().lower()
        # Validate before touching the delivery path. Unknown stage names must
        # never become plausible lifecycle claims.
        task_lifecycle_fact(MEANINGFUL_PROGRESS, milestone=milestone)
        with self._lock:
            if not self._started or not self._task_id:
                return False
            task_id = self._task_id
        return _record_task_execution_milestone(task_id, milestone)

    def finish(self) -> None:
        with self._lock:
            self._finished = True

    def response_started(self) -> bool:
        """Claim a direct plugin reply when no durable reply has started."""

        with self._lock:
            allowed = not self._response_started
            if allowed:
                self._response_started = True
            self._finished = True
            return allowed

    def handoff_direct_response(self, operation) -> dict:
        """Choose direct send or ordered Outbox handoff exactly once."""

        with self._lock:
            if self._direct_handoff_completed:
                return {
                    "direct_reply_allowed": False,
                    "direct_reply_queued": self._direct_handoff_queued,
                    "idempotent_replay": True,
                }
            if self._response_started:
                self._finished = True
                self._direct_handoff_completed = True
                return {
                    "direct_reply_allowed": False,
                    "direct_reply_queued": False,
                    "idempotent_replay": True,
                }
            queued = operation()
            if not queued.get("delivery_queued"):
                raise RuntimeError("private_direct_handoff_not_queued")
            self._response_started = True
            self._finished = True
            self._direct_handoff_completed = True
            self._direct_handoff_queued = True
            return {
                "direct_reply_allowed": False,
                "direct_reply_queued": True,
                "idempotent_replay": False,
                "delivery_queued": True,
                "delivery": queued.get("delivery"),
            }


def _private_response_ownership_identity(transport: dict) -> str:
    if str(transport.get("group_id") or "").strip():
        raise ValueError("private_response_ownership_scope_invalid")
    actor_id = str(
        transport.get("_qq_actor_id")
        or transport.get("sender_id")
        or transport.get("user_id")
        or ""
    ).strip()
    source_message_id = str(
        transport.get("_external_message_id")
        or transport.get("external_message_id")
        or ""
    ).strip()
    session = str(transport.get("session") or "").strip()
    if not actor_id:
        raise ValueError("private_response_ownership_actor_required")
    if not source_message_id:
        raise ValueError("private_response_ownership_source_required")
    if not session:
        raise ValueError("private_response_ownership_session_required")
    return logical_response_id(
        channel="qq",
        thread_ref=f"qq:private:{actor_id}",
        source_message_id=source_message_id,
        response_kind="private_response_ownership",
    )


def _prune_private_response_ownerships_locked(now: float) -> None:
    stale = [
        key for key, entry in _PRIVATE_RESPONSE_OWNERSHIPS.items()
        if now - float(entry.get("last_seen") or entry.get("created_at") or now)
        >= _PRIVATE_RESPONSE_OWNERSHIP_TTL_SECONDS
    ]
    for key in stale:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.pop(key, None)
        if isinstance(entry, dict):
            _clear_private_turn_join_items_locked(entry)
        presence = (entry or {}).get("presence")
        if isinstance(presence, _ExecutionPresence):
            presence.finish()


def _reserve_private_response_ownership_capacity_locked() -> None:
    """Evict only completed history; never cancel a live owned turn."""

    while len(_PRIVATE_RESPONSE_OWNERSHIPS) >= _PRIVATE_RESPONSE_OWNERSHIP_MAX:
        completed = [
            (key, entry)
            for key, entry in _PRIVATE_RESPONSE_OWNERSHIPS.items()
            if entry.get("state") == "completed"
        ]
        if not completed:
            raise RuntimeError("private_response_ownership_capacity")
        key, entry = min(
            completed,
            key=lambda item: float(item[1].get("last_seen") or 0.0),
        )
        _PRIVATE_RESPONSE_OWNERSHIPS.pop(key, None)
        _clear_private_turn_join_items_locked(entry)
        presence = entry.get("presence")
        if isinstance(presence, _ExecutionPresence):
            presence.finish()


def register_private_response_ownership(transport: dict) -> dict:
    """Idempotently establish one private logical-turn response owner."""

    started = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
    identity = _private_response_ownership_identity(transport)
    # A conversation mutation may have invalidated the normal settings cache.
    # Task ACK rendering must never re-enter the default 10-second SQLite path
    # while holding the ownership registry lock.
    presence_settings = _assistant_settings_for_receipt()
    now = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        _prune_private_response_ownerships_locked(now)
        existing = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if existing:
            existing["last_seen"] = now
            return {
                "ok": True,
                "ownership_established": True,
                "idempotent_replay": True,
                "ownership_id": identity,
                "ownership_latency_ms": round(
                    max(0.0, _PRIVATE_RESPONSE_OWNERSHIP_CLOCK() - started) * 1000,
                    3,
                ),
            }
        _reserve_private_response_ownership_capacity_locked()
        presence = _ExecutionPresence(
            _phase2_presence_outbox(),
            transport,
            scope="private",
            settings=presence_settings,
        )
        _PRIVATE_RESPONSE_OWNERSHIPS[identity] = {
            "presence": presence,
            "created_at": now,
            "last_seen": now,
            "state": "owned",
        }
    return {
        "ok": True,
        "ownership_established": True,
        "idempotent_replay": False,
        "ownership_id": identity,
        "ownership_latency_ms": round(
            max(0.0, _PRIVATE_RESPONSE_OWNERSHIP_CLOCK() - started) * 1000,
            3,
        ),
    }


def _clear_private_turn_join_media(value: object) -> None:
    if isinstance(value, dict):
        for item in list(value.values()):
            _clear_private_turn_join_media(item)
        value.clear()
    elif isinstance(value, list):
        for item in list(value):
            _clear_private_turn_join_media(item)
        value.clear()
    elif isinstance(value, bytearray):
        value.clear()


def _clear_private_turn_join_items_locked(entry: dict) -> None:
    items = entry.pop("joined_items", [])
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                item.clear()


def _record_active_private_turn_dispatch(
    transport: dict,
    *,
    dispatch_started: object,
    initial_source_count: object,
    initial_component_count: object,
    initial_visual_count: object,
    initial_explicit_visual_evidence: object,
) -> bool:
    """Record only the safe authority scalars needed by a possible late upgrade."""

    if (
        type(dispatch_started) not in {int, float}
        or isinstance(dispatch_started, bool)
        or float(dispatch_started) < 0
        or type(initial_source_count) is not int
        or not 1 <= initial_source_count <= 16
        or type(initial_component_count) is not int
        or not 0 <= initial_component_count <= 512
        or type(initial_visual_count) is not int
        or not 0 <= initial_visual_count <= 48
        or type(initial_explicit_visual_evidence) is not bool
    ):
        return False
    try:
        identity = _private_response_ownership_identity(transport)
    except ValueError:
        return False
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if not entry or str(entry.get("state") or "") == "completed":
            return False
        entry.update({
            "dispatch_started": float(dispatch_started),
            "initial_source_count": initial_source_count,
            "initial_component_count": initial_component_count,
            "initial_visual_count": initial_visual_count,
            "initial_explicit_visual_evidence": initial_explicit_visual_evidence,
            "join_admission_ready": False,
            "vision_call_reserved": False,
        })
        for item in list(entry.get("joined_items") or []):
            if not isinstance(item, dict) or item.get("protocol_version") != 0:
                continue
            item_explicit = initial_explicit_visual_evidence or (
                item.get("member_has_text") is True
                and explicit_visual_evidence_requested(item.get("text"))
            )
            item["provisional_explicit_visual_evidence"] = item_explicit
            item["visual_wait_seconds"] = (
                _PRIVATE_MULTIMODAL_EVIDENCE_VISUAL_SECONDS
                if item_explicit
                else _PRIVATE_MULTIMODAL_DAILY_VISUAL_SECONDS
            )
            item["provisional_deadline"] = float(dispatch_started) + (
                _PRIVATE_MULTIMODAL_EVIDENCE_DEADLINE_SECONDS
                if item_explicit
                else _PRIVATE_MULTIMODAL_DAILY_DEADLINE_SECONDS
            )
        entry["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
        return True


def _reserve_active_private_turn_visual_call(transport: dict) -> bool:
    """Atomically claim the logical turn's sole model-enabled visual attempt."""

    try:
        identity = _private_response_ownership_identity(transport)
    except ValueError:
        return False
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if (
            not entry
            or str(entry.get("state") or "") == "completed"
            or bool(entry.get("vision_call_reserved"))
        ):
            return False
        entry["vision_call_reserved"] = True
        entry["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
        return True


def _mark_active_private_turn_join_ready(transport: dict) -> bool:
    """Open late visual admission only after the local B1 terminal gates pass."""

    identity = _private_response_ownership_identity(transport)
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if (
            not entry
            or str(entry.get("state") or "") == "completed"
            or bool(entry.get("input_closed"))
            or "dispatch_started" not in entry
        ):
            return False
        entry["join_admission_ready"] = True
        entry["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
        return True


_PRIVATE_TURN_JOIN_FORBIDDEN_FIELDS = frozenset({
    "url", "file", "path", "data_base64", "digest", "settings", "secret",
    "credential", "credentials", "prompt", "observation", "raw_text", "visual_media",
})
_PRIVATE_TURN_JOIN_UNSET = object()


def _private_turn_join_contains_forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key or "").strip().lower() in _PRIVATE_TURN_JOIN_FORBIDDEN_FIELDS
            or _private_turn_join_contains_forbidden(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_private_turn_join_contains_forbidden(item) for item in value)
    return False


def _private_turn_join_structural_list(
    value: object,
    *,
    limit: int,
    error: str,
) -> list[dict]:
    if (
        not isinstance(value, list)
        or len(value) > limit
        or not all(isinstance(item, dict) for item in value)
        or _private_turn_join_contains_forbidden(value)
    ):
        raise InboundRequestValidationError(error)
    return [dict(item) for item in value]


def _private_turn_join_contract(item: dict) -> dict:
    return {
        key: copy.deepcopy(item.get(key))
        for key in (
            "protocol_version", "order_contract", "source_message_id", "source_message_ids",
            "member_index", "component_start", "visual_start", "member_has_text", "text",
            "attachments", "message_components",
        )
    }


def register_active_private_turn_join(
    transport: dict,
    *,
    primary_message_id: object,
    source_message_ids: object,
    raw_text: object,
    text: object,
    attachments: object,
    message_components: object,
    visual_media: object,
    private_turn_protocol_version: object = _PRIVATE_TURN_JOIN_UNSET,
    logical_turn_member_index: object = _PRIVATE_TURN_JOIN_UNSET,
    logical_turn_component_start: object = _PRIVATE_TURN_JOIN_UNSET,
    logical_turn_visual_start: object = _PRIVATE_TURN_JOIN_UNSET,
    logical_turn_member_has_text: object = _PRIVATE_TURN_JOIN_UNSET,
) -> dict:
    """Bind one late natural member to an owned primary before its final reply.

    This reuses the existing owner registry and durable member aliases.  It is
    intentionally not a second response owner and keeps no media beyond the
    in-flight primary request.
    """

    try:
        primary = str(primary_message_id or "").strip()
        source_message_id = str(
            transport.get("_external_message_id") or transport.get("external_message_id") or ""
        ).strip()
        actor_id = str(transport.get("_qq_actor_id") or transport.get("user_id") or "").strip()
        conversation_ref = str(transport.get("session") or "").strip()
        if not primary or not source_message_id or not actor_id or not conversation_ref:
            raise InboundRequestValidationError("qq_private_turn_join_identity_required")
        if primary == source_message_id:
            raise InboundRequestValidationError("qq_private_turn_join_primary_mismatch")
        if (
            not isinstance(source_message_ids, list)
            or not source_message_ids
            or len(source_message_ids) > 16
            or any(type(item) is not str or not item.strip() or item != item.strip() for item in source_message_ids)
            or len(set(source_message_ids)) != len(source_message_ids)
        ):
            raise InboundRequestValidationError("qq_private_turn_join_sources_invalid")
        if source_message_ids[0] != primary or source_message_id not in source_message_ids:
            raise InboundRequestValidationError("qq_private_turn_join_sources_invalid")
        if (
            not isinstance(visual_media, list)
            or len(visual_media) > 3
            or not all(isinstance(item, dict) for item in visual_media)
        ):
            raise InboundRequestValidationError("qq_private_turn_join_visual_media_invalid")
        safe_attachments = _private_turn_join_structural_list(
            attachments,
            limit=16,
            error="qq_private_turn_join_attachments_invalid",
        )
        safe_components = _private_turn_join_structural_list(
            message_components,
            limit=32,
            error="qq_private_turn_join_components_invalid",
        )
        if type(text) is not str or len(text) > 12000:
            raise InboundRequestValidationError("qq_private_turn_join_text_invalid")
        normalized_text = text

        protocol_values = (
            private_turn_protocol_version,
            logical_turn_member_index,
            logical_turn_component_start,
            logical_turn_visual_start,
            logical_turn_member_has_text,
        )
        protocol_fields_present = tuple(
            value is not _PRIVATE_TURN_JOIN_UNSET for value in protocol_values
        )
        if any(protocol_fields_present) and not all(protocol_fields_present):
            raise InboundRequestValidationError("qq_private_turn_join_contract_invalid")
        strict_v1 = all(protocol_fields_present)
        if strict_v1:
            if type(private_turn_protocol_version) is not int or private_turn_protocol_version != 1:
                raise InboundRequestValidationError("qq_private_turn_join_protocol_invalid")
            if str(raw_text or "").strip():
                raise InboundRequestValidationError("qq_private_turn_join_raw_text_forbidden")
            if (
                type(logical_turn_member_index) is not int
                or not 1 <= logical_turn_member_index < 16
                or type(logical_turn_component_start) is not int
                or not 0 <= logical_turn_component_start <= 512
                or type(logical_turn_visual_start) is not int
                or not 0 <= logical_turn_visual_start <= 48
                or type(logical_turn_member_has_text) is not bool
            ):
                raise InboundRequestValidationError("qq_private_turn_join_contract_invalid")
            if (
                len(source_message_ids) != logical_turn_member_index + 1
                or source_message_ids[-1] != source_message_id
            ):
                raise InboundRequestValidationError("qq_private_turn_join_sources_invalid")
            visual_index = logical_turn_visual_start
            for offset, component in enumerate(safe_components):
                if type(component.get("position")) is not int or component.get("position") != (
                    logical_turn_component_start + offset
                ):
                    raise InboundRequestValidationError("qq_private_turn_join_component_start_invalid")
                if "visual_index" in component:
                    if type(component.get("visual_index")) is not int or component.get("visual_index") != visual_index:
                        raise InboundRequestValidationError("qq_private_turn_join_visual_start_invalid")
                    visual_index += 1
            has_plain = any(component.get("type") == "plain" for component in safe_components)
            if logical_turn_member_has_text != has_plain:
                raise InboundRequestValidationError("qq_private_turn_join_text_contract_invalid")
            if logical_turn_member_has_text and not normalized_text.strip():
                raise InboundRequestValidationError("qq_private_turn_join_text_contract_invalid")
            member_index = logical_turn_member_index
            component_start = logical_turn_component_start
            visual_start = logical_turn_visual_start
            member_has_text = logical_turn_member_has_text
            order_contract = "strict_v1"
            protocol_version = 1
        else:
            member_index = None
            component_start = None
            visual_start = None
            member_has_text = bool(str(raw_text or "").strip())
            order_contract = "legacy"
            protocol_version = 0

        primary_transport = dict(transport)
        primary_transport["_external_message_id"] = primary
        identity = _private_response_ownership_identity(primary_transport)
        with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
            entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
            if not entry:
                return {"ok": True, "joined": False, "error": "qq_private_turn_not_active"}
            if bool(entry.get("input_closed")) or str(entry.get("state") or "") == "completed":
                return {"ok": True, "joined": False, "error": "qq_private_turn_input_closed"}
            admission_ready = bool(entry.get("join_admission_ready"))
            if strict_v1 and not admission_ready:
                return {"ok": True, "joined": False, "error": "qq_private_turn_runtime_pending"}

        settings = _assistant_settings(include_secrets=True) if admission_ready else {}
        safe_item = {
            "protocol_version": protocol_version,
            "order_contract": order_contract,
            "source_message_id": source_message_id,
            "source_message_ids": list(source_message_ids),
            "member_index": member_index,
            "component_start": component_start,
            "visual_start": visual_start,
            "member_has_text": member_has_text,
            "text": normalized_text,
            "attachments": safe_attachments,
            "message_components": safe_components,
        }
        with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
            entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
            if not entry or bool(entry.get("input_closed")) or str(entry.get("state") or "") == "completed":
                return {"ok": True, "joined": False, "error": "qq_private_turn_input_closed"}
            runtime_ready = bool(entry.get("join_admission_ready")) and admission_ready
            if strict_v1 and not runtime_ready:
                return {"ok": True, "joined": False, "error": "qq_private_turn_runtime_pending"}
            joined_items = entry.setdefault("joined_items", [])
            for existing in joined_items:
                if not isinstance(existing, dict):
                    continue
                same_source = existing.get("source_message_id") == source_message_id
                same_index = strict_v1 and existing.get("member_index") == member_index
                if same_source or same_index:
                    if _private_turn_join_contract(existing) != _private_turn_join_contract(safe_item):
                        raise InboundRequestValidationError("qq_private_turn_join_conflict")
                    return {"ok": True, "joined": True, "idempotent_replay": True}
                if strict_v1 and existing.get("protocol_version") == 1:
                    existing_prefix = existing.get("source_message_ids") or []
                    common = min(len(existing_prefix), len(source_message_ids))
                    if list(existing_prefix[:common]) != list(source_message_ids[:common]):
                        raise InboundRequestValidationError("qq_private_turn_join_prefix_conflict")

            initial_source_count = entry.get("initial_source_count")
            initial_component_count = entry.get("initial_component_count")
            initial_visual_count = entry.get("initial_visual_count")
            if strict_v1 and (
                member_index < initial_source_count
                or component_start < initial_component_count
                or visual_start < initial_visual_count
            ):
                raise InboundRequestValidationError("qq_private_turn_join_start_invalid")
            register_private_turn_member_aliases(
                _assistant_db_connect,
                primary_message_id=primary,
                source_message_ids=[primary, source_message_id],
                actor_id=actor_id,
                conversation_ref=actor_id,
            )
            explicit_evidence = bool(entry.get("initial_explicit_visual_evidence")) or (
                member_has_text and explicit_visual_evidence_requested(normalized_text)
            )
            visual_limit = (
                _PRIVATE_MULTIMODAL_EVIDENCE_VISUAL_SECONDS
                if explicit_evidence
                else _PRIVATE_MULTIMODAL_DAILY_VISUAL_SECONDS
            )
            deadline = (
                float(entry["dispatch_started"])
                + (
                    _PRIVATE_MULTIMODAL_EVIDENCE_DEADLINE_SECONDS
                    if explicit_evidence
                    else _PRIVATE_MULTIMODAL_DAILY_DEADLINE_SECONDS
                )
                if runtime_ready
                else None
            )
            visual_timeout = (
                remaining_timeout(
                    visual_limit,
                    deadline,
                    _PRIVATE_MULTIMODAL_CLOCK,
                )
                if runtime_ready
                else 0
            )
            visual_model_authority = bool(visual_media) and runtime_ready and visual_timeout > 0
            if visual_model_authority:
                if bool(entry.get("vision_call_reserved")):
                    visual_model_authority = False
                else:
                    entry["vision_call_reserved"] = True
            worker_visual_media = list(visual_media)
            visual_media.clear()
            visual_payload = {
                "attachments": [dict(item) for item in safe_attachments],
                "visual_media": worker_visual_media,
            }
            try:
                handle = visual.begin_qq_visual_turn(
                    visual_payload,
                    "qq_private",
                    actor_id,
                    source_message_id,
                    "",
                    settings,
                    allow_model=visual_model_authority,
                    timeout_seconds=visual_timeout if visual_timeout > 0 else visual_limit,
                )
            except Exception:
                _clear_private_turn_join_media(worker_visual_media)
                raise
            safe_item["visual_handle"] = handle
            safe_item["provisional_explicit_visual_evidence"] = explicit_evidence
            safe_item["visual_wait_seconds"] = visual_limit
            if deadline is not None:
                safe_item["provisional_deadline"] = deadline
            joined_items.append(safe_item)
            entry["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
        return {"ok": True, "joined": True, "idempotent_replay": False}
    finally:
        _clear_private_turn_join_media(visual_media)


def consume_active_private_turn_joins(transport: dict) -> dict:
    """Close the active turn's input immediately before its one final reply."""

    identity = _private_response_ownership_identity(transport)
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if not entry:
            return {"order_contract": "none", "reason": "", "items": []}
        entry["input_closed"] = True
        entry["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
        joined_items = entry.pop("joined_items", [])
        items = [item for item in joined_items if isinstance(item, dict)]
        runtime = {
            "dispatch_started": entry.get("dispatch_started"),
            "initial_source_count": entry.get("initial_source_count"),
            "initial_component_count": entry.get("initial_component_count"),
            "initial_visual_count": entry.get("initial_visual_count"),
            "initial_explicit_visual_evidence": entry.get(
                "initial_explicit_visual_evidence"
            ),
        }
    if not items:
        return {
            "order_contract": "none", "reason": "", "items": [], "runtime": runtime,
        }
    if any(item.get("protocol_version") != 1 for item in items):
        return {
            "order_contract": "incomplete", "reason": "legacy_join",
            "items": items, "runtime": runtime,
        }

    initial_sources = [
        item for item in list(transport.get("source_message_ids") or [])
        if isinstance(item, str) and item
    ]
    if not initial_sources:
        primary = str(transport.get("_external_message_id") or transport.get("external_message_id") or "").strip()
        initial_sources = [primary] if primary else []
    ordered = sorted(items, key=lambda item: item.get("member_index"))
    expected_source_count = runtime["initial_source_count"]
    expected_component = runtime["initial_component_count"]
    expected_visual = runtime["initial_visual_count"]
    reason = ""
    if (
        type(expected_source_count) is not int
        or type(expected_component) is not int
        or type(expected_visual) is not int
        or len(initial_sources) != expected_source_count
    ):
        reason = "initial_contract_invalid"
    else:
        expected_prefix = list(initial_sources)
        seen_positions: set[int] = set()
        seen_visuals: set[int] = set()
        for offset, item in enumerate(ordered):
            expected_index = expected_source_count + offset
            if item.get("member_index") != expected_index:
                reason = "member_index_gap"
                break
            expected_prefix.append(item.get("source_message_id"))
            if item.get("source_message_ids") != expected_prefix:
                reason = "source_prefix_invalid"
                break
            if item.get("component_start") != expected_component:
                reason = "component_start_gap"
                break
            if item.get("visual_start") != expected_visual:
                reason = "visual_start_gap"
                break
            for component in item.get("message_components") or []:
                position = component.get("position")
                if position in seen_positions:
                    reason = "component_position_duplicate"
                    break
                seen_positions.add(position)
                if "visual_index" in component:
                    visual_index = component.get("visual_index")
                    if visual_index in seen_visuals:
                        reason = "visual_index_duplicate"
                        break
                    seen_visuals.add(visual_index)
                    expected_visual += 1
            if reason:
                break
            expected_component += len(item.get("message_components") or [])
    if reason:
        return {
            "order_contract": "incomplete", "reason": reason,
            "items": items, "runtime": runtime,
        }
    return {
        "order_contract": "complete", "reason": "", "items": ordered,
        "runtime": runtime,
    }


def _active_private_turn_replacement_failure_terminal() -> dict:
    reply = "这次没能完整处理你刚补充的内容，请重新发送一次。"
    return {
        "ok": False,
        "dispatch": "chat",
        "reply": reply,
        "output": reply,
        "error": "late_private_turn_replacement_failed",
        "error_kind": "late_private_turn_replacement_failed",
        "retryable": False,
        "reply_attempt_count": 0,
        "retry_suppressed": True,
        "late_private_turn_replacement": True,
    }


def _active_private_turn_retry_input(
    *,
    transport: dict,
    user_id: str,
    message: str,
    history: list[dict],
    settings: dict,
    source: str,
    force: str = "auto",
    initial_observation: object = None,
) -> dict | None:
    """Build one replacement chat input from late members before final delivery.

    The original model request is never mutated.  A joined item that arrives
    before that request returns causes one fresh, not-yet-visible chat attempt
    with the complete logical-turn input.
    """

    consumed = consume_active_private_turn_joins(transport)
    joined_items = consumed.get("items") or []
    if not joined_items:
        return None

    def deadline_terminal() -> dict:
        reply = "这次没能在本轮时限内完整处理你刚补充的内容，请重新发送一次。"
        return {
            "ok": False,
            "dispatch": "chat",
            "reply": reply,
            "output": reply,
            "error": "reply_deadline_exhausted",
            "error_kind": "deadline_exhausted",
            "retryable": False,
            "reply_attempt_count": 0,
            "retry_suppressed": True,
            "deadline_outcome": "expired_before_attempt",
            "late_private_turn_replacement": True,
        }

    try:
        safe_context_keys = (
            "session", "user_id", "_qq_actor_id", "_external_message_id",
            "external_message_id", "logical_turn_id", "source_message_ids",
            "attachments", "message_components", "logical_turn_has_text", "reply_context",
            "_response_cycle_id", "_response_cycle_source_set_hash",
            "_response_cycle_lease_token", "_response_cycle_logical_response_id",
            "_response_cycle_outbox_dedupe_key",
        )
        inbound_context = {
            key: copy.deepcopy(transport[key])
            for key in safe_context_keys
            if key in transport
        }
        source_message_ids = [
            item.strip()
            for item in list(inbound_context.get("source_message_ids") or [])
            if isinstance(item, str) and item.strip()
        ]
        attachments = [
            dict(item)
            for item in list(inbound_context.get("attachments") or [])
            if isinstance(item, dict)
        ]
        components = [
            dict(item)
            for item in list(inbound_context.get("message_components") or [])
            if isinstance(item, dict)
        ]
        initial_message = str(message or "").strip()
        message_parts = (
            [initial_message]
            if transport.get("logical_turn_has_text") is True and initial_message
            else []
        )
        observations: list[object] = []
        runtime = (
            consumed.get("runtime")
            if isinstance(consumed.get("runtime"), dict)
            else {}
        )
        if type(runtime.get("initial_visual_count")) is int and runtime.get(
            "initial_visual_count"
        ) > 0:
            observations.append(
                initial_observation
                if isinstance(initial_observation, Mapping)
                else None
            )

        for item in joined_items:
            source_message_id = str(item.get("source_message_id") or "").strip()
            if source_message_id and source_message_id not in source_message_ids:
                source_message_ids.append(source_message_id)
            if item.get("member_has_text") is True:
                joined_text = str(item.get("text") or "").strip()
                if joined_text:
                    message_parts.append(joined_text)

            handle = item.get("visual_handle")
            provisional_deadline = item.get("provisional_deadline")
            wait_limit = item.get("visual_wait_seconds")
            wait_timeout = remaining_timeout(
                wait_limit if type(wait_limit) is int else 0,
                provisional_deadline,
                _PRIVATE_MULTIMODAL_CLOCK,
            )
            if wait_timeout > 0:
                finish_payload = {
                    "attachments": [
                        dict(value)
                        for value in list(item.get("attachments") or [])
                        if isinstance(value, dict)
                    ],
                    "message_components": [
                        dict(value)
                        for value in list(item.get("message_components") or [])
                        if isinstance(value, dict)
                    ],
                }
                visual_result = visual.finish_qq_visual_turn(
                    handle,
                    finish_payload,
                    wait_timeout_seconds=wait_timeout,
                )
                observations.append(
                    visual_result.get("observation")
                    if isinstance(visual_result, Mapping)
                    else None
                )
            else:
                observations.append(None)

        combined_semantic_message = " ".join(
            part for part in message_parts if part
        )[:12000]
        combined_message = (
            combined_semantic_message
            or initial_message
            or "（发送了一项媒体内容）"
        )
        strict_order = consumed.get("order_contract") == "complete"
        if strict_order:
            for item in joined_items:
                attachments.extend(
                    dict(value)
                    for value in list(item.get("attachments") or [])
                    if isinstance(value, dict)
                )
                components.extend(
                    dict(value)
                    for value in list(item.get("message_components") or [])
                    if isinstance(value, dict)
                )
        inbound_context.update({
            "source_message_ids": source_message_ids,
            "attachments": attachments,
            "message_components": components,
            "logical_turn_has_text": bool(combined_semantic_message),
        })
        ordered_turn = build_ordered_turn_view(combined_message, inbound_context)
        strict_order = strict_order and has_complete_order_contract(ordered_turn)
        merged_observation = merge_visual_observations(
            observations if strict_order else []
        )

        dispatch_started = runtime.get("dispatch_started")
        if (
            type(dispatch_started) not in {int, float}
            or isinstance(dispatch_started, bool)
            or float(dispatch_started) < 0
        ):
            return {"terminal_result": deadline_terminal()}
        explicit_visual_evidence = explicit_visual_evidence_requested(
            combined_semantic_message
        )
        deadline_monotonic = float(dispatch_started) + (
            _PRIVATE_MULTIMODAL_EVIDENCE_DEADLINE_SECONDS
            if explicit_visual_evidence
            else _PRIVATE_MULTIMODAL_DAILY_DEADLINE_SECONDS
        )
        if remaining_timeout(1, deadline_monotonic, _PRIVATE_MULTIMODAL_CLOCK) <= 0:
            return {"terminal_result": deadline_terminal()}

        detected_intent = _detect_agent_intent(combined_message)
        active_multimodal_task = active_qq_task(
            TASKS, TASK_LOCK, QQ_TASK_SOURCE, user_id,
        )
        artifact_revision_request = _looks_like_delivered_artifact_revision(
            combined_message
        )
        fast_path = bool(
            strict_order
            and not explicit_visual_evidence
            and active_multimodal_task is None
            and not artifact_revision_request
            and daily_multimodal_fast_path_allowed(
                source=source,
                force=force,
                detected_intent=detected_intent,
                effectful_work_requested=message_requests_effectful_work(
                    combined_message,
                ),
                daily_conversation_proven=message_is_proven_daily_conversation(
                    combined_message,
                    assistant_display_name=(
                        settings.get("display_name")
                        if isinstance(settings, Mapping)
                        else None
                    ),
                ),
                inbound_context=inbound_context,
                ordered_turn=ordered_turn,
            )
        )
        retry_history = (
            append_multimodal_turn_history(
                history,
                ordered_turn,
                merged_observation,
            )
            if strict_order
            else list(history)
        )
        return {
            "message": combined_message,
            "decision_context": {
                "history": retry_history,
                "settings": settings,
                "source": source,
                "inbound_context": inbound_context,
                "active_private_turn_retry_used": True,
                "allow_classifier": not fast_path,
                "deadline_monotonic": deadline_monotonic,
                "classifier_deadline_monotonic": (
                    deadline_monotonic
                    if fast_path
                    else deadline_monotonic
                    - _PRIVATE_MULTIMODAL_FINAL_REPLY_RESERVE_SECONDS
                ),
                "clock": _PRIVATE_MULTIMODAL_CLOCK,
                "private_multimodal_truth_guard": True,
                "visual_observation": merged_observation,
            },
        }
    except (RuntimeError, ValueError, sqlite3.Error):
        return {
            "terminal_result": _active_private_turn_replacement_failure_terminal(),
        }
    finally:
        for item in joined_items:
            if isinstance(item, dict):
                item.clear()


def _private_response_ownership_presence(transport: dict) -> _ExecutionPresence | None:
    try:
        identity = _private_response_ownership_identity(transport)
    except ValueError:
        return None
    now = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        _prune_private_response_ownerships_locked(now)
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if not entry:
            return None
        entry["last_seen"] = now
        entry["state"] = "dispatching"
        presence = entry.get("presence")
        return presence if isinstance(presence, _ExecutionPresence) else None


def complete_private_response_ownership(transport: dict) -> dict:
    identity = _private_response_ownership_identity(transport)
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if not entry:
            return {
                "ok": True,
                "ownership_established": False,
                "idempotent_replay": True,
                "direct_reply_allowed": True,
            }
        presence = entry.get("presence")
        direct_reply_allowed = (
            presence.response_started()
            if isinstance(presence, _ExecutionPresence)
            else True
        )
        entry["state"] = "completed"
        entry["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
        _clear_private_turn_join_items_locked(entry)
    return {
        "ok": True,
        "ownership_established": True,
        "idempotent_replay": False,
        "direct_reply_allowed": bool(direct_reply_allowed),
    }


def handoff_private_owned_direct_response(
    transport: dict,
    reply: str,
    *,
    ownership_required: bool = True,
) -> dict:
    """Persist one owned direct result before allowing any plugin reply."""

    text = str(reply or "").strip()
    if not text:
        raise ValueError("private_direct_reply_required")
    if len(text) > 12000:
        raise ValueError("private_direct_reply_too_long")
    identity = _private_response_ownership_identity(transport)
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        presence = (entry or {}).get("presence")
    if not isinstance(presence, _ExecutionPresence):
        if ownership_required:
            raise RuntimeError("private_response_ownership_missing")
        return {
            "ok": True,
            "ownership_established": False,
            "direct_reply_allowed": True,
            "direct_reply_queued": False,
            "idempotent_replay": True,
        }

    result = presence.handoff_direct_response(lambda: enqueue_qq_response(
        _phase2_outbox(),
        {"ok": True, "dispatch": "direct_handoff", "reply": text},
        reserve_qq_response(_phase2_outbox(), transport, scope="private"),
        scope="private",
        voice_output=VOICE_OUTPUT_RUNTIME.prepare,
    ))
    if result.get("delivery_queued"):
        CONTINUITY_KERNEL.bind_delivery(result)
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        current = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if current:
            current["state"] = "completed"
            current["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
            _clear_private_turn_join_items_locked(current)
    return {
        "ok": True,
        "ownership_established": True,
        **result,
    }


def _mark_private_response_ownership_completed(transport: dict) -> None:
    try:
        identity = _private_response_ownership_identity(transport)
    except ValueError:
        return
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity)
        if entry:
            entry["state"] = "completed"
            entry["last_seen"] = _PRIVATE_RESPONSE_OWNERSHIP_CLOCK()
            _clear_private_turn_join_items_locked(entry)


def private_response_ownership_snapshot(transport: dict) -> dict:
    identity = _private_response_ownership_identity(transport)
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entry = _PRIVATE_RESPONSE_OWNERSHIPS.get(identity) or {}
        return {
            "present": bool(entry),
            "state": str(entry.get("state") or ""),
        }


def _clear_private_response_ownerships_for_tests() -> None:
    with _PRIVATE_RESPONSE_OWNERSHIP_LOCK:
        entries = list(_PRIVATE_RESPONSE_OWNERSHIPS.values())
        for entry in entries:
            _clear_private_turn_join_items_locked(entry)
        _PRIVATE_RESPONSE_OWNERSHIPS.clear()
    for entry in entries:
        presence = entry.get("presence")
        if isinstance(presence, _ExecutionPresence):
            presence.finish()


class _TaskExecutionProgress:
    """Bounded deterministic heartbeat owned by one durable Task lifecycle."""

    def __init__(
        self,
        outbox: DeliveryOutbox,
        transport: dict,
        *,
        scope: str,
        settings: dict,
        task_id: str,
    ):
        self._outbox = outbox
        self._transport = dict(transport)
        self._scope = scope
        self._settings = dict(settings)
        self._task_id = str(task_id)
        self._lock = threading.RLock()
        self._timer: object | None = None
        self._slot = 0
        self._stopped = False
        self._generation = 0
        self._milestones: set[str] = set()

    def start(self) -> None:
        self._schedule(float(_EXECUTION_PRESENCE_PROGRESS_SECONDS[0]))

    def _schedule(self, seconds: float) -> None:
        with self._lock:
            if self._stopped:
                return
            previous = self._timer
            cancel = getattr(previous, "cancel", None)
            if callable(cancel):
                cancel()
            self._generation += 1
            generation = self._generation
            timer = _EXECUTION_PRESENCE_TIMER_FACTORY(
                seconds,
                lambda: self._tick(generation),
            )
            if hasattr(timer, "daemon"):
                timer.daemon = True
            self._timer = timer
        timer.start()

    def _tick(self, generation: int) -> None:
        with TASK_LOCK:
            with self._lock:
                if self._stopped or generation != self._generation:
                    return
                task = TASKS.get(self._task_id)
                if not task or str(task.get("status") or "") not in {"queued", "running"}:
                    active = False
                else:
                    active = True
                    self._slot += 1
                    slot = self._slot
            if active:
                try:
                    _enqueue_execution_presence(
                        self._outbox,
                        self._transport,
                        scope=self._scope,
                        settings=self._settings,
                        stage="progress",
                        suffix=f"execution-presence-progress-{slot}",
                        task_id=self._task_id,
                        interaction_plan={},
                        heartbeat_slot=slot,
                    )
                except (sqlite3.Error, ValueError):
                    # Presence is best effort and cannot change Task outcome.
                    pass
        if not active:
            _stop_task_execution_progress(self._task_id, expected=self)
            return
        next_delay = (
            float(_EXECUTION_PRESENCE_PROGRESS_SECONDS[1])
            - float(_EXECUTION_PRESENCE_PROGRESS_SECONDS[0])
            if slot == 1
            else float(_EXECUTION_PRESENCE_PROGRESS_SECONDS[1])
        )
        self._schedule(next_delay)

    def milestone(self, milestone: str) -> bool:
        """Emit one authoritative Task milestone and reset the silence budget."""

        milestone = str(milestone or "").strip().lower()
        task_lifecycle_fact(MEANINGFUL_PROGRESS, milestone=milestone)
        with TASK_LOCK:
            task = TASKS.get(self._task_id)
            active = bool(
                task and str(task.get("status") or "") in {"queued", "running"}
            )
        if not active:
            return False
        with self._lock:
            if self._stopped or milestone in self._milestones:
                return False
            try:
                result = _enqueue_execution_presence(
                    self._outbox,
                    self._transport,
                    scope=self._scope,
                    settings=self._settings,
                    stage="meaningful_progress",
                    suffix=f"execution-presence-milestone-{milestone}",
                    task_id=self._task_id,
                    interaction_plan={},
                    milestone=milestone,
                )
            except (sqlite3.Error, ValueError):
                return False
            if not result.get("delivery_queued"):
                return False
            self._milestones.add(milestone)
            self._schedule(float(_EXECUTION_PRESENCE_PROGRESS_SECONDS[0]))
        return True

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._generation += 1
            timer = self._timer
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            cancel()


def _start_task_execution_progress(
    outbox: DeliveryOutbox,
    transport: dict,
    *,
    scope: str,
    settings: dict,
    task_id: str,
) -> bool:
    task_id = str(task_id or "").strip()
    if not task_id:
        return False
    with _TASK_EXECUTION_PROGRESS_LOCK:
        if task_id in _TASK_EXECUTION_PROGRESS:
            return False
        heartbeat = _TaskExecutionProgress(
            outbox,
            transport,
            scope=scope,
            settings=settings,
            task_id=task_id,
        )
        _TASK_EXECUTION_PROGRESS[task_id] = heartbeat
    heartbeat.start()
    return True


def _stop_task_execution_progress(
    task_id: str,
    *,
    expected: _TaskExecutionProgress | None = None,
) -> None:
    task_id = str(task_id or "").strip()
    with _TASK_EXECUTION_PROGRESS_LOCK:
        heartbeat = _TASK_EXECUTION_PROGRESS.get(task_id)
        if expected is not None and heartbeat is not expected:
            return
        _TASK_EXECUTION_PROGRESS.pop(task_id, None)
    if heartbeat is not None:
        heartbeat.stop()


def _record_task_execution_milestone(task_id: str, milestone: str) -> bool:
    """Route authoritative progress through the existing Task-owned registry."""

    task_id = str(task_id or "").strip()
    if not task_id:
        return False
    with _TASK_EXECUTION_PROGRESS_LOCK:
        heartbeat = _TASK_EXECUTION_PROGRESS.get(task_id)
    if not isinstance(heartbeat, _TaskExecutionProgress):
        return False
    return heartbeat.milestone(milestone)


def _response_cycle_delivery_contract(
    response_cycle: Mapping[str, object] | None,
    transport: Mapping[str, object] | None = None,
):
    if not response_cycle:
        return None, None
    required = (
        "id", "source_set_hash", "lease_token",
        "logical_response_id", "outbox_dedupe_key",
    )
    if any(not str(response_cycle.get(key) or "").strip() for key in required):
        raise ValueError("response_cycle_delivery_identity_invalid")
    identity = {
        "cycle_id": str(response_cycle["id"]),
        "source_set_hash": str(response_cycle["source_set_hash"]),
        "lease_token": str(response_cycle["lease_token"]),
        "logical_response_id": str(response_cycle["logical_response_id"]),
        "outbox_dedupe_key": str(response_cycle["outbox_dedupe_key"]),
    }

    def prepare(prepared: dict, destination: str) -> dict:
        with _assistant_db_connect() as conn:
            result = prepare_or_supersede_response_cycle_delivery(
                conn,
                cycle_id=identity["cycle_id"],
                lease_token=identity["lease_token"],
                source_set_hash=identity["source_set_hash"],
                delivery=prepared,
                expected_destination=destination,
                interaction_plan_id=str(
                    (transport or {}).get("_response_cycle_plan_id") or ""
                ),
                context_message_ids=list(
                    (transport or {}).get("_situation_context_message_ids") or []
                ),
                assistant_metadata=(
                    (transport or {}).get("_response_cycle_assistant_metadata")
                    if isinstance(
                        (transport or {}).get("_response_cycle_assistant_metadata"),
                        Mapping,
                    )
                    else {}
                ),
            )
        if result.get("status") == "superseded":
            raise ResponseCycleSupersededError("response_cycle_superseded_precommit")
        delivery = result.get("delivery")
        if not isinstance(delivery, dict):
            raise ResponseCycleRecoveryError("response_cycle_prepare_result_invalid")
        return delivery

    return identity, prepare


def _settle_group_response_from_transport(
    transport: Mapping[str, object],
    *,
    state: str,
    delivery_id: str = "",
) -> bool:
    identity = transport.get("_group_response_identity")
    if not isinstance(identity, Mapping):
        return False
    try:
        with _assistant_db_connect() as conn:
            settle_group_response_commitment(
                conn,
                commitment_id=str(identity.get("commitment_id") or ""),
                owner_kind=str(identity.get("owner_kind") or ""),
                state=state,
                delivery_id=delivery_id,
            )
        return True
    except (sqlite3.Error, ValueError) as exc:
        print(
            "group_response_commitment_settle_failed "
            f"state={state} error={type(exc).__name__}",
            flush=True,
        )
        return False


def _settle_group_response_result(
    transport: Mapping[str, object],
    result: Mapping[str, object],
) -> bool:
    delivery = result.get("delivery") if isinstance(result.get("delivery"), Mapping) else {}
    delivery_id = str(delivery.get("id") or delivery.get("delivery_id") or "")
    if result.get("delivery_queued") and delivery_id:
        return _settle_group_response_from_transport(
            transport,
            state="committed",
            delivery_id=delivery_id,
        )
    identity = (
        transport.get("_group_response_identity")
        if isinstance(transport.get("_group_response_identity"), Mapping)
        else {}
    )
    natural_queue = (
        result.get("natural_queue")
        if isinstance(result.get("natural_queue"), Mapping)
        else {}
    )
    queue_latest_id = str(natural_queue.get("latest_message_id") or "").strip()
    queue_revision = str(natural_queue.get("candidate_revision") or "").strip()
    deferred_ambient = bool(
        str(identity.get("owner_kind") or "") == "ambient"
        and str(natural_queue.get("group_id") or "")
        == str(transport.get("group_id") or "")
        and str(natural_queue.get("state") or "") in {"pending", "claimed"}
        and queue_latest_id.isdigit()
        and int(queue_latest_id) > 0
        and queue_revision.isdigit()
        and int(queue_revision) > 0
    )
    if deferred_ambient:
        # This response represents durable queue ownership, not a terminal
        # decision to stay silent.  The ambient worker alone settles it after
        # its queue fence, preflight, and one-plan decision.
        return True
    if result.get("ok") and (
        str(result.get("dispatch") or "") == "silent"
        or result.get("should_reply") is False
    ):
        return _settle_group_response_from_transport(transport, state="silent")
    if not result.get("ok"):
        return _settle_group_response_from_transport(transport, state="held")
    return _settle_group_response_from_transport(transport, state="planned")


def _dispatch_qq_response_if_enabled(
    operation,
    transport: dict,
    *,
    scope: str,
    response_cycle: Mapping[str, object] | None = None,
) -> dict:
    response_identity, prepare_callback = _response_cycle_delivery_contract(
        response_cycle, transport,
    )
    with _assistant_db_connect() as conn:
        enabled = unified_delivery_enabled(conn)
    if not enabled:
        if scope == "private":
            try:
                complete_private_response_ownership(transport)
            except ValueError:
                pass
        try:
            result = dispatch_qq_response(
                _phase2_outbox(), operation, transport, scope=scope, enabled=enabled,
                voice_output=VOICE_OUTPUT_RUNTIME.prepare,
                response_identity=response_identity,
                prepare_callback=prepare_callback,
                delivery_observer=(lambda value: project_group_dispatch_delivery(_assistant_db_connect, value)) if scope == 'group' else None,
            )
        except Exception:
            if scope == "group":
                _settle_group_response_from_transport(transport, state="held")
            raise
        if scope == "group":
            _settle_group_response_result(transport, result)
        CONTINUITY_KERNEL.bind_delivery(result)
        return result

    try:
        presence = None
        if scope == "private":
            settings = _assistant_settings()
            outbox = _phase2_presence_outbox()
            presence = (
                _private_response_ownership_presence(transport)
                or _ExecutionPresence(outbox, transport, scope=scope, settings=settings)
            )
    except (sqlite3.Error, ValueError):
        presence = None
    if presence is None:
        try:
            result = dispatch_qq_response(
                _phase2_outbox(), operation, transport, scope=scope, enabled=enabled,
                voice_output=VOICE_OUTPUT_RUNTIME.prepare,
                response_identity=response_identity,
                prepare_callback=prepare_callback,
                delivery_observer=(lambda value: project_group_dispatch_delivery(_assistant_db_connect, value)) if scope == 'group' else None,
            )
        except Exception:
            if scope == "group":
                _settle_group_response_from_transport(transport, state="held")
            raise
        if scope == "group":
            _settle_group_response_result(transport, result)
        CONTINUITY_KERNEL.bind_delivery(result)
        return result

    if response_identity is not None:
        presence.configure_response_cycle(response_identity, prepare_callback)
    transport["_execution_presence_start"] = presence.start
    transport["_execution_presence_replay"] = presence.replay_delivery
    transport["_execution_presence_milestone"] = presence.milestone
    transport["_execution_presence_finish"] = presence.finish
    try:
        result = operation()
    except Exception:
        presence.finish()
        _mark_private_response_ownership_completed(transport)
        raise
    finally:
        transport.pop("_execution_presence_start", None)
        transport.pop("_execution_presence_replay", None)
        transport.pop("_execution_presence_milestone", None)
        transport.pop("_execution_presence_finish", None)
    try:
        # Research succeeded and created a Work task.  The user already
        # received the one permitted acceptance; the existing task worker owns
        # the later Artifact/result delivery.  Do not enqueue a duplicate ACK.
        reused_delivery = result.pop("_existing_durable_delivery", None)
        if isinstance(reused_delivery, Mapping):
            result = {
                **result,
                "delivery_queued": True,
                "delivery_reused": True,
                "logical_response_id": str(
                    reused_delivery.get("logical_response_id") or ""
                ),
                "delivery": {
                    "id": str(reused_delivery.get("id") or ""),
                    "delivery_id": str(reused_delivery.get("id") or ""),
                    "state": str(reused_delivery.get("state") or "pending"),
                    "certainty": str(
                        reused_delivery.get("delivery_certainty") or "pending"
                    ),
                    "ack_state": (
                        "confirmed" if reused_delivery.get("acked_at") else "pending"
                    ),
                    "sequence": int(reused_delivery.get("response_sequence") or 0),
                },
            }
        elif result.get("execution_presence_enqueue_failed"):
            result = {
                **result,
                "delivery_queued": False,
                "execution_presence_acknowledged": False,
            }
        elif (
            str(result.get("dispatch") or "") == "task"
            and presence.ack
            and presence.ack.get("delivery_queued")
        ):
            ack = presence.ack or {}
            result = {
                **result,
                "delivery_queued": bool(ack.get("delivery_queued")),
                "delivery": ack.get("delivery"),
                "execution_presence_acknowledged": True,
            }
        else:
            result = presence.enqueue_response(lambda: enqueue_qq_response(
                _phase2_outbox(),
                result,
                reserve_qq_response(_phase2_outbox(), transport, scope=scope),
                scope=scope,
                voice_output=VOICE_OUTPUT_RUNTIME.prepare,
                response_identity=response_identity,
                prepare_callback=prepare_callback,
            ))
    finally:
        presence.finish()
        _mark_private_response_ownership_completed(transport)
    CONTINUITY_KERNEL.bind_delivery(result)
    return result
def _init_phase2_state() -> None:
    _init_task_db()
    with _db_connect() as conn:
        ensure_platform_schema(conn)
        from bridge_migrations import ensure_agent_platform_migrations
        ensure_agent_platform_migrations(conn)
    _phase2_outbox()
    _phase2_presence_outbox()
def _phase2_task_lookup() -> dict[str, dict]:
    with TASK_LOCK:
        return {task_id: dict(task) for task_id, task in TASKS.items()}
def _sync_phase2_task(task: dict) -> dict:
    with _db_connect() as conn:
        result = PlatformRepository(conn).sync_task(task, task_lookup=_phase2_task_lookup())
    projection = result.get("projection") or {}
    task["goal_id"] = projection.get("goal_id") or ""
    task["run_id"] = projection.get("run_id") or ""
    return result
def _qq_delivery_sessions() -> dict[str, str]:
    return load_qq_delivery_sessions(_assistant_db_connect)


def _restore_task_delivery_projection(task: dict) -> dict:
    goal_id = str(task.get("goal_id") or "")
    task_id = str(task.get("id") or "")
    if not goal_id or not task_id:
        return {}
    candidate = (
        task.get("_delivery_projection")
        if isinstance(task.get("_delivery_projection"), dict)
        else {}
    )
    with _db_connect() as conn:
        stored = load_task_delivery_projection(conn, goal_id, task_id)
        if not stored and candidate:
            stored = store_task_delivery_projection(conn, goal_id, task_id, candidate)
    if stored:
        task["_delivery_projection"] = stored
    return stored


def _enqueue_phase2_delivery(task: dict, projection: dict | None = None) -> dict | None:
    settings = _assistant_settings()
    expression_enabled = task_expression_enabled(settings)

    def terminal_presentation_factory(item):
        status = str(item.get("status") or "")
        has_delivery_access = bool(item.get("_has_valid_delivery_access"))
        if (
            not expression_enabled
            and status not in {"failed", "timeout", "cancelled"}
            and not (status == "done" and has_delivery_access)
        ):
            return None
        is_revision = bool(str(item.get("artifact_revision_id") or "").strip())
        canonical = (
            item.get("_artifact_delivery_projection")
            if isinstance(item.get("_artifact_delivery_projection"), dict)
            else {}
        )
        artifact_record = (
            canonical.get("artifact_record")
            if isinstance(canonical.get("artifact_record"), dict)
            else {}
        )
        version_record = (
            canonical.get("version_record")
            if isinstance(canonical.get("version_record"), dict)
            else {}
        )
        title = " ".join(str(artifact_record.get("title") or "完整成果").split())[:120]
        version_number = version_record.get("version_number")
        version_label = (
            f"v{version_number}"
            if isinstance(version_number, int) and version_number > 0
            else "新版本" if is_revision else "当前版本"
        )
        result_url = str(canonical.get("redemption_url") or "").strip()
        event = ""
        fact_slots = None
        artifact_delivery_rendered = False
        if status == "done" and is_revision and has_delivery_access:
            action = "revision_succeeded"
            event = REVISION_SUCCEEDED
            fact_slots = {
                "source_version": "已交付的原版本",
                "preserved_scope": "原版本保持不变",
                "new_version": f"{title}（{version_label}）",
                "result_ready": True,
                "result_url": result_url,
            }
            terminal_fact = (
                "已交付的原版本仍保持不变。\n"
                f"{title}（{version_label}）已经生成并可交付。\n"
                f"在线查看：{result_url}"
            )
            artifact_delivery_rendered = True
        elif status == "done" and has_delivery_access:
            action = "artifact_ready"
            event = ARTIFACT_READY
            fact_slots = {
                "completed_scope": "这项任务",
                "artifact_title": title,
                "artifact_version": version_label,
                "result_ready": True,
                "result_url": result_url,
            }
            terminal_fact = (
                "这项任务已经完成。\n"
                f"{title}（{version_label}）已经生成并可交付。\n"
                f"在线查看：{result_url}"
            )
            artifact_delivery_rendered = True
        elif status == "done":
            action = "completed"
            event = TASK_SUCCEEDED
            detail_source = ""
            for key in ("stdout", "output"):
                candidate = str(item.get(key) or "").strip()
                if candidate:
                    detail_source = candidate
                    break
            detail = _trim_output(detail_source) or "完成状态已经确认。"
            fact_slots = {
                "completed_scope": "这项任务",
                "result_detail": detail,
            }
            terminal_fact = "这件事办完了。\n结果内容：" + detail
        elif status in {"failed", "timeout", "cancelled"} and isinstance(
            item.get("_failure_projection"), dict,
        ):
            action = status
            event = TASK_FAILED
            failure_projection = item["_failure_projection"]
            fact_slots = task_failure_fact_slots(failure_projection)
            terminal_fact = task_failure_projection_text(failure_projection)
        else:
            action = "failed"
            terminal_fact = task_lifecycle_fact(TASK_FAILED) if status == "failed" else task_terminal_text(status)
        if not expression_enabled:
            return {
                "content": terminal_fact,
                "artifact_delivery_rendered": artifact_delivery_rendered,
            }
        if event and fact_slots is not None:
            rendered = render_task_character_expression(
                settings,
                event=event,
                fact_slots=fact_slots,
                neutral_text=terminal_fact,
                channel_shows_identity=True,
            )
            blocks = rendered["content_blocks"]
            completion = rendered["content"]
        else:
            blocks, completion = task_status_blocks(
                {},
                settings,
                terminal_fact,
                factual_type="status",
                action=action,
                channel_shows_identity=True,
            )
        return {
            "content": completion,
            "content_blocks": blocks,
            "artifact_delivery_rendered": artifact_delivery_rendered,
        }
    return enqueue_task_result(
        _phase2_outbox(), task, projection,
        sessions=_qq_delivery_sessions(), public_task=_public_task, trim_output=_trim_output,
        terminal_presentation_factory=terminal_presentation_factory,
    )
def _sync_and_enqueue_phase2_task(task: dict) -> dict:
    projection = _sync_phase2_task(task)
    with _db_connect() as conn:
        from bridge_migrations import ensure_agent_platform_migrations
        ensure_agent_platform_migrations(conn)
        projection["revision_binding"] = ensure_task_revision_binding(conn, task)
    _restore_task_delivery_projection(task)
    delivery = _enqueue_phase2_delivery(task, projection)
    CONTINUITY_KERNEL.observe_task(task, projection, delivery)
    return projection
def _backfill_phase2_state() -> dict:
    """Project only missing legacy tasks so richer Run data is never overwritten."""

    lookup = _phase2_task_lookup()
    with _db_connect() as conn:
        repo = PlatformRepository(conn)
        existing = {str(item.get("legacy_task_id") or "") for item in repo.list_runs(limit=200)}
    synced = 0
    deliveries = 0
    for task_id, task in lookup.items():
        if task_id not in existing:
            projection = _sync_phase2_task(task)
            synced += 1
        else:
            projection = {}
            if str(task.get("status") or "") == "failed":
                with _db_connect() as conn:
                    durable_failure = load_durable_failure_projection(conn, task_id)
                if durable_failure:
                    task["_failure_projection"] = durable_failure
        if _enqueue_phase2_delivery(task, projection):
            deliveries += 1
    return {"ok": True, "tasks_projected": synced, "deliveries_seen": deliveries}


def _compact_projection(item: dict, fields: tuple[str, ...]) -> dict:
    return _compact_projection_impl(item, fields)


def _execution_snapshot(limit: int = 20, *, detailed: bool = False) -> dict:
    limit = max(1, min(int(limit or 20), 100))
    with _db_connect() as conn:
        repo = PlatformRepository(conn)
        overview = repo.overview()
        goals = repo.list_goals(limit=limit)
        runs = repo.list_runs(limit=limit)
        evidence: list[dict] = []
        for run in runs:
            if len(evidence) >= limit:
                break
            evidence.extend(repo.list_evidence(str(run.get("id") or ""), limit=limit - len(evidence)))
        reconciliation = repo.reconcile_tasks(_phase2_task_lookup().values())
    delivery_items = _phase2_outbox().list_deliveries(limit=limit)
    delivery_counts = {
        "total": len(delivery_items),
        "pending": sum(1 for item in delivery_items if item.get("state") in {"available", "scheduled", "leased"}),
        "delivered": sum(1 for item in delivery_items if item.get("state") == "delivered"),
        "dead_letter": sum(1 for item in delivery_items if item.get("state") == "dead_letter"),
        "ambiguous": sum(1 for item in delivery_items if item.get("state") == "ambiguous"),
    }
    if detailed:
        delivery_counts["items"] = delivery_items
    else:
        goals = [
            _compact_projection(item, (
                "id", "title", "status", "completion_policy", "legacy_root_task_id",
                "current_run_id", "created_at", "updated_at", "completed_at",
            ))
            for item in goals
        ]
        runs = [
            _compact_projection(item, (
                "id", "goal_id", "legacy_task_id", "status", "strategy", "capability_id",
                "summary", "created_at", "updated_at", "started_at", "finished_at",
            ))
            for item in runs
        ]
        evidence = [
            _compact_projection(item, (
                "id", "run_id", "source_name", "source_uri", "source_url", "excerpt",
                "retrieved_at", "created_at", "expires_at", "valid_until",
            ))
            for item in evidence
        ]
    return {
        "ok": True,
        "overview": overview,
        "goals": goals,
        "runs": runs,
        "evidence": evidence,
        "deliveries": delivery_counts,
        "reconciliation": reconciliation,
    }


def _claim_phase2_deliveries(
    lease_owner: str,
    *,
    wait_seconds: float = 20,
    lease_seconds: float = 30,
    limit: int = 5,
    channel: str = "qq",
) -> list[dict]:
    return claim_deliveries(
        _phase2_outbox(),
        lease_owner,
        wait_seconds=wait_seconds,
        lease_seconds=lease_seconds,
        limit=limit,
        channel=channel,
        sessions=_qq_delivery_sessions() if channel == "qq" else {},
        policy_filter=lambda deliveries: filter_claimed_deliveries(_phase2_outbox(), deliveries, _assistant_db_connect),
    )


def _begin_phase2_delivery(delivery_id: str, lease_token: str) -> dict | None:
    return begin_delivery_with_policy(_phase2_outbox(), delivery_id, lease_token, _assistant_db_connect)


def _ack_phase2_delivery(
    delivery_id: str,
    lease_token: str,
    *,
    platform_message_id: str = "",
) -> dict | None:
    result = settle_ack(
        _phase2_outbox(),
        delivery_id,
        lease_token,
        platform_message_id=platform_message_id,
        assistant_db_connect=_assistant_db_connect,
        set_task_delivery=_set_task_delivery,
        record_conversation=_record_conversation,
    )
    if result:
        with _assistant_db_connect() as conn:
            project_delivery_state(conn, delivery_id, "channel_acked")
    return result


def _retry_phase2_delivery(
    delivery_id: str,
    lease_token: str,
    *,
    error: str = "",
    delay_seconds: float = 10,
    known_not_sent: bool = False,
) -> dict | None:
    result = settle_retry(
        _phase2_outbox(),
        delivery_id,
        lease_token,
        error=error,
        delay_seconds=delay_seconds,
        known_not_sent=known_not_sent,
        assistant_db_connect=_assistant_db_connect,
        set_task_delivery=_set_task_delivery,
        pending_status=TASK_DELIVERY_PENDING,
    )
    if result:
        projected = "failed" if str(result.get("state") or "") == "dead_letter" else "outbox_queued"
        with _assistant_db_connect() as conn:
            project_delivery_state(conn, delivery_id, projected)
    return result


def _mark_phase2_delivery_ambiguous(
    delivery_id: str,
    lease_token: str,
    *,
    error: str = "",
) -> dict | None:
    result = settle_ambiguous(
        _phase2_outbox(),
        delivery_id,
        lease_token,
        error=error,
        assistant_db_connect=_assistant_db_connect,
    )
    if result:
        with _assistant_db_connect() as conn:
            project_delivery_state(conn, delivery_id, "ambiguous")
    return result


def _legacy_phase2_delivery_marker(task_id: str, status: str, error: str = "") -> dict | None:
    """Map the old task delivery callback onto the durable Outbox state."""

    delivery = next(
        (
            item
            for item in _phase2_outbox().list_deliveries(state="all", channel="qq", limit=500)
            if _delivery_task_id(item) == task_id and _is_terminal_task_delivery(item)
        ),
        None,
    )
    if not delivery:
        return None
    token = str(delivery.get("lease_token") or "")
    if status == "pending" and token and delivery.get("state") == "leased":
        return _phase2_outbox().retry(
            str(delivery.get("id") or ""),
            token,
            error=error,
            delay_seconds=10,
        )
    if status in {"sent", "skipped"}:
        if token and delivery.get("state") == "leased":
            return _phase2_outbox().ack(str(delivery.get("id") or ""), token)
        now = _utc_now()
        with _db_connect() as conn:
            conn.execute(
                """
                UPDATE delivery_outbox
                SET acked_at = ?, lease_owner = '', lease_expires_at = '',
                    last_action = 'legacy_ack', last_error = '', updated_at = ?
                WHERE id = ? AND acked_at = '' AND dead_letter = 0
                """,
                (now, now, str(delivery.get("id") or "")),
            )
        return next(
            (
                item
                for item in _phase2_outbox().list_deliveries(state="all", channel="qq", limit=500)
                if str(item.get("id") or "") == str(delivery.get("id") or "")
            ),
            delivery,
        )
    if status == "failed":
        now = _utc_now()
        with _db_connect() as conn:
            conn.execute(
                """
                UPDATE delivery_outbox
                SET dead_letter = 1, dead_lettered_at = ?, lease_owner = '',
                    lease_expires_at = '', last_action = 'legacy_dead_letter',
                    last_error = ?, updated_at = ?
                WHERE id = ? AND acked_at = ''
                """,
                (now, str(error or "legacy_delivery_failed")[:2000], now, str(delivery.get("id") or "")),
            )
    return delivery


def _init_task_db() -> None:
    try:
        with _db_connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    sandbox TEXT,
                    cwd TEXT,
                    summary TEXT,
                    prompt TEXT,
                    timeout INTEGER,
                    duration REAL,
                    returncode INTEGER,
                    ok INTEGER,
                    cancel_requested INTEGER,
                    error_kind TEXT,
                    source_task_id TEXT,
                    stdout TEXT,
                    stderr TEXT,
                    output TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    source TEXT,
                    user_id TEXT,
                    trace_id TEXT,
                    origin_message TEXT,
                    intent TEXT,
                    mode TEXT,
                    delivery_status TEXT,
                    delivery_error TEXT,
                    delivered_at TEXT,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    delivery_next_at TEXT,
                    pending_messages TEXT
                )
                """,
            )
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            migrations = {
                "source": "TEXT",
                "user_id": "TEXT",
                "trace_id": "TEXT",
                "origin_message": "TEXT",
                "intent": "TEXT",
                "mode": "TEXT",
                "delivery_status": "TEXT",
                "delivery_error": "TEXT",
                "delivered_at": "TEXT",
                "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
                "delivery_next_at": "TEXT",
                "pending_messages": "TEXT",
                "delivery_recipient_id": "TEXT",
                "delivery_session": "TEXT",
                "request_idempotency_key": "TEXT",
                "automation_run_id": "TEXT",
                "follow_up_source_task_id": "TEXT",
                "executor_provider_id": "TEXT",
                "executor_model_id": "TEXT",
                "executor_model_name": "TEXT",
                "executor_adapter": "TEXT",
                "executor_config_version": "INTEGER",
                "executor_profile_sha256": "TEXT",
                "artifact_revision_id": "TEXT NOT NULL DEFAULT ''",
                "artifact_revision_base_version_id": "TEXT NOT NULL DEFAULT ''",
                "network_mode": "TEXT NOT NULL DEFAULT 'controlled'",
            }
            for column, column_type in migrations.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source_user ON tasks(source, user_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_request_idempotency ON tasks(request_idempotency_key) WHERE request_idempotency_key<>''")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_automation_run ON tasks(automation_run_id) WHERE automation_run_id<>''")
            conn.execute("PRAGMA user_version = 1")
        os.chmod(TASK_DB_PATH, 0o600)
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("task_database_initialization_failed") from exc


def _assistant_db_connect() -> sqlite3.Connection:
    return connect_home_database(
        ASSISTANT_DB_PATH,
        ASSISTANT_HOME_ASSISTANT_TABLES,
        _invalidate_assistant_home_cache,
    )


def _assistant_group_candidate_claim_connect() -> sqlite3.Connection:
    """Open a short-lived connection for the safe, pre-effect queue claim only."""

    return connect_home_database(
        ASSISTANT_DB_PATH,
        ASSISTANT_HOME_ASSISTANT_TABLES,
        _invalidate_assistant_home_cache,
        timeout_seconds=0.25,
    )


def _assistant_ingress_connect() -> sqlite3.Connection:
    return connect_home_database(
        ASSISTANT_DB_PATH,
        ASSISTANT_HOME_ASSISTANT_TABLES,
        _invalidate_assistant_home_cache,
        timeout_seconds=0.5,
    )


def _assistant_fast_read_connect() -> sqlite3.Connection:
    return connect_home_database(
        ASSISTANT_DB_PATH,
        ASSISTANT_HOME_ASSISTANT_TABLES,
        _invalidate_assistant_home_cache,
        timeout_seconds=0.5,
        query_only=True,
    )


def _slugify(value: str, fallback: str = "project") -> str:
    return _slugify_impl(value, fallback)


def _init_assistant_db() -> None:
    try:
        with _assistant_db_connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            # ``group_policies`` is an existing legacy fact source.  Bootstrap
            # its additive columns before validating a registered database so
            # an older installation can pass the fail-closed schema audit.
            # The full social bootstrap remains below with the other legacy
            # tables and is idempotent.
            ensure_social_experience_tables(conn)
            am.validate_registered_assistant_core(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    score INTEGER NOT NULL DEFAULT 5,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quality_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT '',
                    intent TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    request TEXT NOT NULL DEFAULT '',
                    response TEXT NOT NULL DEFAULT '',
                    checks TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT '',
                    issues TEXT NOT NULL DEFAULT '[]',
                    tool TEXT NOT NULL DEFAULT '',
                    fallback INTEGER NOT NULL DEFAULT 0,
                    duration REAL,
                    created_at TEXT NOT NULL
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mode_sessions (
                    user_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL DEFAULT 'daily',
                    intent TEXT NOT NULL DEFAULT 'chat',
                    confidence REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    work_lifecycle TEXT NOT NULL DEFAULT 'none',
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    work_turns INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL DEFAULT '',
                    ended_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            ensure_social_tables(conn)
            ensure_meme_discovery_tables(conn)
            ensure_automation_tables(conn)
            ensure_capability_tables(conn)
            ensure_plugin_market_tables(conn)
            ensure_model_registry_tables(conn)
            ensure_model_usage_tables(conn)
            ensure_pet_tables(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, deleted)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qq_events_user ON qq_events(user_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qq_events_trace ON qq_events(trace_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_events_user ON quality_events(user_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_events_status ON quality_events(status, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mode_sessions_updated ON mode_sessions(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_approvals_user ON pending_approvals(user_id, status, created_at)")

            now = _utc_now()
            for key, value in DEFAULT_ASSISTANT_SETTINGS.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO settings(key, value, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (key, value, now),
                )
            seed_default_memes(conn)
            seed_expression_habits(conn)
            seed_builtin_skills(conn)
            discover_local_skills(conn)
            current_settings = dict(DEFAULT_ASSISTANT_SETTINGS)
            for row in conn.execute("SELECT key, value FROM settings").fetchall():
                if row["key"] in current_settings:
                    current_settings[row["key"]] = row["value"]
            seed_model_registry(conn, current_settings)

            default_project = DEFAULT_CWD.resolve()
            project_id = _slugify(default_project.name or "agent-stack")
            conn.execute(
                """
                INSERT OR IGNORE INTO projects(id, name, path, description, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    project_id,
                    default_project.name or "agent-stack",
                    str(default_project),
                    "当前服务器 AI Agent 项目",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO settings(key, value, updated_at)
                VALUES ('current_project_id', ?, ?)
                """,
                (project_id, now),
            )
            conn.execute("PRAGMA user_version = 1")
            am.register_after_legacy_bootstrap(conn)
        os.chmod(ASSISTANT_DB_PATH, 0o600)
        # If a process stopped after the singleton proxy changed but before
        # its role binding was durably completed, resolve the non-secret
        # activation journal before serving any request.  Recovery either
        # confirms the exact target identity or restores the previous binding;
        # it never guesses a new executor.
        with _assistant_db_connect() as conn:
            recover_interrupted_work_executor_activation(conn)
            # Older already-active custom executors predate the non-secret
            # proxy identity markers.  Reconcile only that exact verified
            # work binding; Draft profiles remain untouched.
            reconcile_active_work_executor_runtime(conn)
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("assistant_database_initialization_failed") from exc


def _path_in_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _allowed_cwd_roots() -> tuple[Path, ...]:
    roots = [WORKSPACE_BASE, DEFAULT_CWD]
    roots.extend(EXTRA_CWD_ROOTS)
    result = []
    for root in roots:
        if root not in result:
            result.append(root)
    return tuple(result)


def _safe_cwd(raw: str | None) -> Path:
    if not raw:
        resolved = _default_cwd()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = WORKSPACE_BASE / candidate
    resolved = candidate.resolve()
    if not any(_path_in_root(resolved, root) for root in _allowed_cwd_roots()):
        roots = ", ".join(str(root) for root in _allowed_cwd_roots())
        raise ValueError(f"cwd must stay inside allowed roots: {roots}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _setting_get(key: str, default: str = "") -> str:
    try:
        with _assistant_db_connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default
    except sqlite3.Error:
        return default


def _setting_set(key: str, value: str) -> None:
    now = _utc_now()
    with _assistant_db_connect() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
    _invalidate_assistant_settings_cache()


def _project_from_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "path": row["path"],
        "description": row["description"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _list_projects() -> list[dict]:
    try:
        with _assistant_db_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects WHERE active = 1 ORDER BY updated_at DESC, name ASC",
            ).fetchall()
        return [_project_from_row(row) for row in rows if row]
    except sqlite3.Error:
        return []


def _find_project(identifier: str | None) -> dict | None:
    identifier = (identifier or "").strip()
    try:
        with _assistant_db_connect() as conn:
            row = None
            if identifier:
                row = conn.execute(
                    """
                    SELECT * FROM projects
                    WHERE active = 1 AND (id = ? OR name = ? OR path = ?)
                    """,
                    (identifier, identifier, identifier),
                ).fetchone()
            if not row:
                current = conn.execute(
                    "SELECT value FROM settings WHERE key = 'current_project_id'",
                ).fetchone()
                current_id = str(current["value"]) if current else ""
                if current_id:
                    row = conn.execute(
                        "SELECT * FROM projects WHERE active = 1 AND id = ?",
                        (current_id,),
                    ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM projects WHERE active = 1 ORDER BY updated_at DESC LIMIT 1",
                ).fetchone()
        return _project_from_row(row)
    except sqlite3.Error:
        return None


def _current_project() -> dict | None:
    return _find_project(None)


def _default_cwd() -> Path:
    project = _current_project()
    if project:
        try:
            path = Path(project["path"]).resolve()
            if any(_path_in_root(path, root) for root in _allowed_cwd_roots()):
                return path
        except OSError:
            pass
    return DEFAULT_CWD.resolve()


def _create_project(name: str, path: str | None = None, description: str = "", make_current: bool = True) -> dict:
    return PROJECT_SERVICE.create(
        name, path or "", description, make_current=make_current,
        actor_type="admin" if make_current else "qq_channel",
    )


def _set_current_project(identifier: str) -> dict:
    project = _find_project(identifier)
    if not project:
        raise ValueError("project_not_found")
    _setting_set("current_project_id", project["id"])
    project["codegraph"] = _ensure_codegraph(Path(project["path"]), phase="project-switch", force=True)
    return project


def _normalize_chat_setting(key: str, value: str) -> str:
    if key == "chat_temperature":
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.7
        number = max(0.0, min(number, 2.0))
        return f"{number:.2f}".rstrip("0").rstrip(".")
    if key == "chat_max_tokens":
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            number = 900
        return str(max(64, min(number, 8192)))
    return str(value or "").strip()


def _truthy_setting(value: object) -> bool:
    return bridge_truthy_setting(value)


def _normalize_agent_policy_setting(key: str, value: object) -> str:
    if key in AGENT_POLICY_BOOLEAN_KEYS:
        return "1" if _truthy_setting(value) else "0"
    if key in AGENT_MODE_SETTING_KEYS:
        return normalize_agent_mode_setting(key, value, DEFAULT_ASSISTANT_SETTINGS)
    raw = str(value or "").strip()
    choices = AGENT_POLICY_CHOICES.get(key)
    if choices and raw not in choices:
        return DEFAULT_ASSISTANT_SETTINGS[key]
    return raw or DEFAULT_ASSISTANT_SETTINGS.get(key, "")


def _agent_policy(settings: dict | None = None) -> dict:
    current = settings or _assistant_settings()
    return build_agent_policy(current)


def _mask_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}...{value[-4:]}"


def _neutral_assistant_settings(reason: str) -> dict:
    settings = dict(DEFAULT_ASSISTANT_SETTINGS)
    settings.update(
        {
            "display_name": "Assistant",
            "relationship": "用户与 Assistant",
            "persona": "",
            "style": "",
            "agent_persona_level": "off",
            "settings_degraded": True,
            "settings_degraded_reason": reason,
        },
    )
    return settings


def _load_assistant_settings(
    *, include_secrets: bool = False, connect=None, integrity_scope: str = "database",
) -> dict:
    settings = dict(DEFAULT_ASSISTANT_SETTINGS)
    settings["settings_degraded"] = False
    settings["settings_degraded_reason"] = ""
    try:
        with (connect or _assistant_db_connect)() as conn:
            rows = conn.execute(
                f"""
                SELECT key, value FROM settings
                WHERE key IN ({",".join("?" for _ in DEFAULT_ASSISTANT_SETTINGS)})
                """,
                tuple(DEFAULT_ASSISTANT_SETTINGS),
            ).fetchall()
            for row in rows:
                settings[row["key"]] = row["value"]
            settings = assistant_identity.identity_overlay_settings(
                conn, settings, integrity_scope=integrity_scope,
            )
    except sqlite3.Error:
        settings = _neutral_assistant_settings("assistant_settings_unavailable")
    except ValueError as exc:
        known_identity_failures = {
            "identity_shadow_compare_failed": "assistant_identity_shadow_mismatch",
            "active_assistant_missing": "active_assistant_unavailable",
        }
        reason = known_identity_failures.get(str(exc))
        if not reason:
            raise
        settings = _neutral_assistant_settings(reason)
    provider = str(settings.get("chat_provider") or "codex").strip()
    if provider not in CHAT_PROVIDERS:
        settings["chat_provider"] = "codex"
    preset_key = str(settings.get("chat_provider_preset") or "").strip().lower()
    if preset_key not in PROVIDER_PRESETS:
        settings["chat_provider_preset"] = "codex" if settings["chat_provider"] == "codex" else "custom"
    if include_secrets:
        return settings
    api_key = str(settings.get("chat_api_key") or "").strip()
    settings.pop("chat_api_key", None)
    settings["chat_api_key_set"] = bool(api_key)
    settings["chat_api_key_preview"] = _mask_secret(api_key)
    return settings


def _assistant_settings(*, include_secrets: bool = False, integrity_scope: str = "database") -> dict:
    """Return the exact validated settings snapshot, cached between mutations.

    Identity validation includes a full foreign-key audit and is intentionally
    completed before the server becomes ready.  Commit-bound invalidation keeps
    interactive ACKs on the same configured identity/Voice Contract without
    repeating that audit on the user-message critical path.
    """

    global _ASSISTANT_SETTINGS_CACHE
    if include_secrets:
        return _load_assistant_settings(
            include_secrets=True, integrity_scope=integrity_scope,
        )
    with _ASSISTANT_SETTINGS_CACHE_LOCK:
        if _ASSISTANT_SETTINGS_CACHE is not None:
            return copy.deepcopy(_ASSISTANT_SETTINGS_CACHE)
        settings = _load_assistant_settings()
        if not settings.get("settings_degraded"):
            _ASSISTANT_SETTINGS_CACHE = copy.deepcopy(settings)
        return copy.deepcopy(settings)


def _assistant_settings_for_receipt() -> dict:
    """Return fact-safe presence settings through a sub-second read boundary."""

    if not _ASSISTANT_SETTINGS_CACHE_LOCK.acquire(blocking=False):
        return _neutral_assistant_settings("assistant_settings_cache_busy")
    try:
        if _ASSISTANT_SETTINGS_CACHE is not None:
            return copy.deepcopy(_ASSISTANT_SETTINGS_CACHE)
    finally:
        _ASSISTANT_SETTINGS_CACHE_LOCK.release()
    return _load_assistant_settings(connect=_assistant_fast_read_connect)


def _warm_assistant_settings_cache() -> dict:
    """Finish the expensive validated settings read before accepting traffic."""

    return _assistant_settings()


def _settings_for_model_role(role: str, fallback_settings: dict | None = None) -> dict:
    return with_role_cache_contract(runtime_settings_for_role_safe(
        _assistant_db_connect, role, fallback_settings or _assistant_settings(include_secrets=True),
    ), role=role)


def _with_model_session_scope(settings: Mapping[str, object], logical_scope: object) -> dict:
    """Carry one local conversation scope to provider adapters for opaque binding."""

    result = dict(settings)
    scope = str(logical_scope or "").strip()
    if scope:
        result["model_session_scope"] = scope
    return result


def _resolve_executor_snapshot() -> dict:
    settings = _settings_for_model_role("work_executor")
    pid = settings.get("model_registry_provider_id") or ""
    mid = settings.get("model_registry_id") or ""
    if not pid or not mid:
        raise RuntimeError("executor_snapshot_missing")

    transport = str(settings.get("model_transport") or "")
    profile_hash = ""
    if transport == "codex_cli_chatgpt":
        adapter = "codex_login"
        model_name = (settings.get("codex_model") or "").strip()
        config_version = "codex-login-v1"
    elif transport == "codex_cli_custom_provider":
        adapter = "codex_custom_provider"
        model_name = (settings.get("codex_model") or "").strip()
        if not model_name:
            raise RuntimeError("executor_model_missing")
        profile = dict(settings.get("executor_profile") or {})
        if not profile or not int(profile.get("enabled") or 0):
            raise RuntimeError("executor_profile_missing")
        config_version = int(profile.get("config_version") or 0)
        if str(profile.get("last_apply_status") or "").strip() != "applied":
            raise RuntimeError("executor_runtime_not_applied")
        if int(profile.get("applied_version") or 0) != config_version:
            raise RuntimeError("executor_runtime_not_applied")
        profile_hash = profile_sha256(str(profile.get("profile_name") or ""))
        if not profile_hash:
            raise RuntimeError("executor_profile_file_missing")
        if not read_executor_credential(str(profile.get("credential_source") or "")):
            raise RuntimeError("executor_credential_missing")
        if not executor_runtime_identity_matches(pid, config_version, model_name):
            raise RuntimeError("executor_runtime_identity_mismatch")
    else:
        raise RuntimeError(f"unsupported_executor_transport:{transport}")

    return {
        "provider_id": pid, "model_id": mid,
        "model_name": model_name, "adapter": adapter,
        "config_version": config_version, "profile_sha256": profile_hash if transport == "codex_cli_custom_provider" else "",
        "profile_name": str(profile.get("profile_name") or "") if transport == "codex_cli_custom_provider" else "",
        "credential_source": str(profile.get("credential_source") or "") if transport == "codex_cli_custom_provider" else "",
    }


def _codex_exec_env(adapter: str, profile: dict | None = None) -> dict[str, str]:
    return codex_exec_env(adapter, profile, MIHOMO_PROXY_URL, MIHOMO_SOCKS_PROXY_URL)


def _parse_codex_jsonl(stdout: str) -> dict:
    r = {
        "final_status": "unknown", "terminal_event": "",
        "error_type": "", "error_summary": "",
        "hard_quota_proven": False,
        "tool_call_count": 0, "tool_failures": [],
        "file_change_count": 0, "final_output": "",
        "saw_error_event": False, "usage": {},
        "all_outputs": [],
    }
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        et = ev.get("type", "")
        item = ev.get("item") or {}
        it = item.get("type", "")
        ist = item.get("status", "")

        if et == "turn.completed":
            r["terminal_event"] = "turn.completed"
            r["final_status"] = "completed"
            u = ev.get("usage") or {}
            r["usage"] = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)}
        elif et == "turn.failed":
            r["terminal_event"] = "turn.failed"
            r["final_status"] = "failed"
            e = ev.get("error") or {}
            r["error_type"] = "turn_failed"
            error_message = str(e.get("message") or "")[:10000]
            r["error_summary"] = error_message[:500]
            # Preserve the exact structured provider fact before reducing the
            # event to its user-safe message.  The classifier still requires
            # both HTTP 429 and the exact GoUsageLimitError type.
            if isinstance(e, dict):
                r["hard_quota_proven"] = is_hard_quota_response(error_message) or is_hard_quota_response({
                    "status": e.get("status") or e.get("status_code") or e.get("http_status"),
                    "error": e,
                }) or is_hard_quota_response(e)
        elif et == "error":
            r["saw_error_event"] = True
            if not r["terminal_event"]:
                r["final_status"] = "failed"
                r["error_type"] = r["error_type"] or "error"
                r["error_summary"] = str(ev.get("message") or "")[:500]
            r["hard_quota_proven"] = bool(r["hard_quota_proven"]) or is_hard_quota_response(ev)
        elif et == "item.started":
            if it == "command_execution":
                r["tool_call_count"] += 1
            elif it == "file_change":
                r["file_change_count"] += 1
        elif et == "item.completed":
            if it == "command_execution":
                if ist == "failed" or (item.get("exit_code") or 0) != 0:
                    r["tool_failures"].append({
                        "command": str(item.get("command", ""))[:200],
                        "exit_code": item.get("exit_code"),
                    })
                output = item.get("aggregated_output") or ""
                if output:
                    r["all_outputs"].append(output)
                    r["final_output"] = output
            elif it == "agent_message":
                output = str(item.get("text") or "").strip()
                if output:
                    r["all_outputs"].append(output)
                    r["final_output"] = output

    return r


def _finalize_codex_result(proc, parsed, started, timeout_expired=False):
    """综合 JSONL 解析结果和进程退出状态。

    ``turn.completed`` only proves the executor round ended (process terminal),
    never business success; the final body must pass the internal-prose gate.
    """
    if timeout_expired:
        return {"ok": False, "status": "failed", "error_kind": "process_timeout",
                "error": "任务执行超时。", "duration": round(time.monotonic() - started, 2)}

    status = parsed["final_status"]

    if status == "unknown":
        rc = proc.returncode
        if rc is not None and rc < 0:
            status = "failed"
            parsed["error_type"] = "process_terminated"
        elif rc is not None and rc != 0:
            status = "failed"
            parsed["error_type"] = "process_exit_nonzero"
        elif parsed["saw_error_event"]:
            status = "failed"
        else:
            status = "failed"
            parsed["error_type"] = "incomplete_stream"

    error_type = parsed.get("error_type") or ""
    error_summary = parsed.get("error_summary") or ""
    final_output = parsed.get("final_output") or ""
    summary = {
        "tool_calls": parsed["tool_call_count"],
        "tool_failures": len(parsed["tool_failures"]),
        "terminal_event": parsed["terminal_event"],
        "usage": parsed.get("usage", {}),
    }

    if status != "completed":
        failure_detail = error_summary or final_output
        classified_error = (
            "hard_quota"
            if parsed.get("hard_quota_proven") or is_hard_quota_response(failure_detail)
            else error_type or "task_execution_failed"
        )
        return {
            "ok": False,
            "status": "failed",
            "returncode": proc.returncode if proc else 1,
            "duration": round(time.monotonic() - started, 2),
            "output": final_output if final_output else error_summary,
            "error_kind": classified_error,
            "error": error_summary or (final_output or "")[:500],
            "jsonl_summary": summary,
        }

    # turn.completed only establishes the process terminal.  A final body that
    # is internal runtime/tooling/sandbox prose must not be surfaced as
    # success or as a user-visible result.
    leak = _automation_leak_gate(final_output)
    if not leak.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "returncode": proc.returncode if proc else 1,
            "duration": round(time.monotonic() - started, 2),
            "output": "",
            "error_kind": leak.get("error_kind") or "no_business_evidence",
            "error": "执行器回合结束，但没有可验证的业务结果。",
            "jsonl_summary": summary,
        }

    return {
        "ok": True,
        "status": "done",
        "returncode": proc.returncode if proc else 1,
        "duration": round(time.monotonic() - started, 2),
        "output": final_output,
        "error_kind": "",
        "error": "",
        "jsonl_summary": summary,
    }


# === 错误消息映射 ===

_USER_ERROR_MAP = {
    "executor_snapshot_missing": "任务创建时未记录执行器配置，无法执行。",
    "executor_adapter_missing": "任务缺少执行器标识，无法执行。",
    "executor_profile_missing": "执行器 Profile 未配置、已停用或无法读取，任务已安全停止。",
    "executor_runtime_not_applied": "执行器配置尚未成功应用到运行时，请在模型连接页检查上游连接和应用状态。",
    "executor_credential_missing": "执行器受保护凭证缺失，任务已安全停止。",
    "executor_model_missing": "执行器连接未登记接口模型名，请在模型与 Provider 中检查。",
    "executor_sandbox_unavailable": "执行器安全沙箱依赖不可用，请先完成服务器预检。",
    "deepseek_proxy_access_key_missing": "DeepSeek 代理访问密钥缺失，任务已安全停止。",
    "deepseek_proxy_model_missing": "DeepSeek 代理未配置模型名，请在管理后台检查。",
    "work_executor_binding_missing": "未配置工作执行器。请到管理后台设置。",
    "danger_full_access_not_allowed_for_proxy": "DeepSeek 代理不支持此沙箱级别。",
    "cwd_not_allowed_for_proxy": "当前工作目录不允许使用 DeepSeek 代理。",
    "executor_profile_changed": "代理配置在任务排队期间变化，请重建任务。",
    "incomplete_stream": "Codex 输出流不完整，任务无法确认完成。",
    "proxy_unreachable": "DeepSeek 执行代理当前未运行。",
    "proxy_auth_required": "代理鉴权失败，请检查访问密钥。",
    "upstream_auth_failed": "DeepSeek API Key 无效或已失效。",
    "upstream_rate_limited": "DeepSeek 请求受限，请稍后重试。",
    "hard_quota": "当前执行容量已经用尽，任务已停止；请由 Owner 处理额度或手动调整模型配置。",
    "task_network_authorization_expired": (
        "这项任务的网页搜索授权已关闭或到期，未执行联网步骤。"
        "Owner 可在控制台重新限时授权后新建任务。"
    ),
}


def _user_error_message(error_key: str) -> str:
    """返回脱敏后的用户可读错误消息。"""
    return _USER_ERROR_MAP.get(error_key, f"工作执行器不可用（{error_key}）。")


# The Runtime Console must not turn an exception raised by a provider, proxy,
# SQLite, or subprocess into a browser-visible diagnostic.  Keep this list
# deliberately small: any unknown detail becomes one stable public failure
# code and is retained only in the server-side operational path.
_PUBLIC_WORK_EXECUTOR_ACTIVATION_ERRORS = {
    "primary_model_unavailable",
    "model_not_found",
    "model_disabled",
    "provider_disabled",
    "executor_transport_unsupported",
    "provider_not_trusted",
    "tools_capability_missing",
    "executor_profile_missing",
    "executor_bound_profile_not_applied",
    "executor_runtime_unavailable",
    "executor_verification_required",
    "executor_verification_failed",
    "executor_verification_stale",
    "executor_runtime_apply_failed",
    "executor_runtime_rollback_failed",
    "executor_proxy_health_timeout",
    "executor_proxy_health_unavailable",
    "executor_proxy_model_mismatch",
}


def _public_work_executor_activation_error(exc: Exception) -> str:
    code = str(exc or "").strip()
    return code if code in _PUBLIC_WORK_EXECUTOR_ACTIVATION_ERRORS else "work_executor_activation_failed"


_PUBLIC_EXECUTOR_VERIFICATION_ERRORS = {
    "executor_profile_missing",
    "executor_runtime_unavailable",
    "executor_runtime_stage_restore_failed",
    "executor_runtime_stage_requires_active_runtime",
    "executor_verification_hash_unavailable",
    "executor_verification_schema_missing",
}


def _public_executor_verification_error(exc: Exception) -> str:
    code = str(exc or "").strip()
    return code if code in _PUBLIC_EXECUTOR_VERIFICATION_ERRORS else "executor_verification_unavailable"


_PUBLIC_MODEL_REGISTRY_ERRORS = {
    "invalid_provider_kind",
    "invalid_provider_base_url",
    "insecure_remote_base_url",
    "invalid_provider_transport",
    "invalid_provider_billing_scope",
    "provider_billing_transport_mismatch",
    "runtime_owned_provider_read_only",
    "provider_used_by_executor_profile",
    "provider_transport_change_requires_rebind",
    "model_not_found",
    "model_in_use",
    "model_used_by_executor_profile",
    "provider_not_found",
    "provider_has_models",
    "codex_login_instance_already_exists",
    "model_label_required",
    "model_name_required",
    "model_capability_required",
    "invalid_model_capability",
    "invalid_model_role",
    "primary_model_unavailable",
    "fallback_model_unavailable",
    "fallback_model_capability_mismatch",
    "work_executor_fallback_not_supported",
    "work_executor_requires_tool_support",
    "work_executor_activation_required",
    "custom_executor_role_not_supported",
    "executor_profile_missing",
    "executor_profile_not_applied",
    "executor_runtime_unavailable",
    "executor_verification_required",
    "executor_verification_failed",
    "executor_verification_stale",
}


def _public_model_registry_error(exc: Exception) -> str:
    code = str(exc or "").strip()
    return code if code in _PUBLIC_MODEL_REGISTRY_ERRORS else "model_registry_update_failed"


_PUBLIC_MODEL_DISCOVERY_ERRORS = {
    "provider_id_required",
    "provider_not_found",
    "invalid_provider_base_url",
    "provider_disabled",
    "model_discovery_requires_azure_deployment",
    "model_discovery_not_available_for_codex_connection",
    "model_discovery_unsupported_transport",
    "discovered_model_name_invalid",
    "provider_secret_or_url_unavailable",
    "provider_secret_missing",
}


def _public_model_discovery_error(exc: Exception, *, validation: bool = False) -> str:
    """Keep discovery and pre-save validation diagnostics server-side.

    Both operations reach a saved provider and may receive library, network or
    provider exceptions.  Browser responses may contain only stable public
    codes; raw URLs, credentials and response bodies are never diagnostics for
    the Owner surface.
    """

    code = str(exc or "").strip()
    if code in _PUBLIC_MODEL_DISCOVERY_ERRORS:
        return code
    return "model_discovery_validation_failed" if validation else "model_discovery_unavailable"


def _public_model_validation_error(_exc: Exception) -> str:
    """The validation lab has no browser-visible provider exception path."""

    return "model_validation_unavailable"


_PUBLIC_MODEL_PROBE_ERROR_KINDS = {
    "provider_config",
    "auth",
    "rate_limit",
    "quota",
    "upstream",
    "http",
    "waf",
    "network",
    "timeout",
    "invalid_model",
    "parse",
    "empty",
}

_PUBLIC_MODEL_PROBE_ERRORS = {
    "provider_config": "provider_config_invalid",
    "auth": "provider_auth_failed",
    "rate_limit": "provider_rate_limited",
    "quota": "provider_quota_exhausted",
    "upstream": "provider_upstream_unavailable",
    "http": "provider_request_rejected",
    "waf": "provider_request_rejected",
    "network": "provider_request_failed",
    "timeout": "provider_request_failed",
    "invalid_model": "provider_model_unavailable",
    "parse": "provider_response_invalid",
    "empty": "empty_provider_reply",
}

_PUBLIC_MODEL_PROBE_FINISH_REASONS = {
    "stop",
    "length",
    "tool_calls",
    "function_call",
    "content_filter",
}


def _public_model_probe_duration(value: object) -> float | None:
    """Keep only a bounded numeric duration from a provider probe result."""

    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return round(duration, 3) if 0 <= duration <= 3600 else None


def _public_model_probe_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _public_model_probe_finish_reason(value: object) -> str:
    reason = str(value or "").strip()
    return reason if reason in _PUBLIC_MODEL_PROBE_FINISH_REASONS else ""


def _public_model_probe_usage(value: object) -> dict:
    """Project protocol usage facts, never an arbitrary provider response."""

    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            count = int(value.get(key))
        except (TypeError, ValueError):
            continue
        if 0 <= count <= 10_000_000:
            result[key] = count
    return result


def _public_model_probe_result(value: object) -> dict:
    """Turn a probe/validation result into its safe browser DTO.

    Provider and Codex runners may use returned failure dictionaries for rich
    operational diagnostics.  A response body, stdout, stderr, exception text
    or a synthesized reply is not a public error contract.  On success the
    actual reply and finite product facts remain available to the Console;
    on failure only stable classification and bounded protocol facts remain.
    """

    result = value if isinstance(value, dict) else {}
    ok = bool(result.get("ok"))
    duration = _public_model_probe_duration(result.get("duration"))
    common = {
        "ok": ok,
        "duration": duration,
        "retryable": bool(result.get("retryable")),
        "owner_action_required": bool(result.get("owner_action_required")),
    }
    if ok:
        common.update({
            "reply": _public_model_probe_text(result.get("reply") or result.get("output"), 16_000),
            "provider": _public_model_probe_text(result.get("provider"), 80),
            "provider_label": _public_model_probe_text(result.get("provider_label"), 120),
            "model": _public_model_probe_text(result.get("model"), 200),
            "usage": _public_model_probe_usage(result.get("usage")),
            "validated_at": _public_model_probe_text(result.get("validated_at"), 64),
            "finish_reason": _public_model_probe_finish_reason(result.get("finish_reason")),
            "reasoning_only": bool(result.get("reasoning_only")),
            "response_shape": _public_model_probe_text(result.get("response_shape"), 40),
            "error_kind": "",
            "error": "",
        })
        return common

    error_kind = str(result.get("error_kind") or "").strip()
    if error_kind not in _PUBLIC_MODEL_PROBE_ERROR_KINDS:
        error_kind = "model_validation"
    common.update({
        "error_kind": error_kind,
        "error": _PUBLIC_MODEL_PROBE_ERRORS.get(error_kind, "model_validation_failed"),
        "finish_reason": _public_model_probe_finish_reason(result.get("finish_reason")),
        "reasoning_only": bool(result.get("reasoning_only")),
        "response_shape": _public_model_probe_text(result.get("response_shape"), 40),
    })
    return common


def _public_model_registry_conflict(exc: Exception) -> dict:
    """Expose an actionable deletion code without serializing raw exceptions."""

    return {
        "ok": False,
        "error": _public_model_registry_error(exc),
        "dependencies": getattr(exc, "dependencies", []),
    }


def _update_assistant_settings(payload: dict) -> dict:
    payload = apply_provider_preset(payload)
    if "meme_work_enabled" in payload and "agent_work_emoji_enabled" not in payload:
        payload["agent_work_emoji_enabled"] = payload["meme_work_enabled"]
    elif "agent_work_emoji_enabled" in payload and "meme_work_enabled" not in payload:
        payload["meme_work_enabled"] = payload["agent_work_emoji_enabled"]
    now = _utc_now()
    with _assistant_db_connect() as conn:
        validate_legacy_model_write(conn, payload)
        assistant_identity.write_identity_settings(conn, payload)
        for key in ASSISTANT_PUBLIC_SETTING_KEYS:
            if key in payload:
                if key in LEGACY_MODEL_KEYS:
                    continue
                value = str(payload.get(key) or "").strip()
                if key == "chat_provider" and value not in CHAT_PROVIDERS:
                    raise ValueError("unsupported_chat_provider")
                if key == "chat_provider_preset" and value not in PROVIDER_PRESETS:
                    value = "custom"
                if key in {"chat_temperature", "chat_max_tokens"}:
                    value = _normalize_chat_setting(key, value)
                if key in AGENT_POLICY_SETTING_KEYS:
                    value = _normalize_agent_policy_setting(key, payload.get(key))
                if value or key in {"chat_model", "chat_base_url", "codex_model"}:
                    conn.execute(
                        """
                        INSERT INTO settings(key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (key, value, now),
                    )
    _invalidate_assistant_settings_cache()
    return _assistant_settings()


def _normalize_float_setting(value: object, default: float, minimum: float, maximum: float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    number = max(minimum, min(number, maximum))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _normalize_background_url(value: object) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > 1200:
        raise ValueError("background_url_too_long")
    if url == DEFAULT_SAMPLE_BACKGROUND_URL:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid_background_url")
    return url


def _admin_appearance() -> dict:
    settings = dict(DEFAULT_ADMIN_APPEARANCE_SETTINGS)
    try:
        with _assistant_db_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT key, value FROM settings
                WHERE key IN ({",".join("?" for _ in ADMIN_APPEARANCE_KEYS)})
                """,
                tuple(ADMIN_APPEARANCE_KEYS),
            ).fetchall()
        for row in rows:
            settings[row["key"]] = row["value"]
    except sqlite3.Error:
        pass
    settings["admin_background_enabled"] = (
        "1" if str(settings.get("admin_background_enabled") or "0").lower() in {"1", "true", "yes", "on"} else "0"
    )
    settings["admin_background_dim"] = _normalize_float_setting(
        settings.get("admin_background_dim"),
        0.12,
        0.0,
        0.96,
    )
    settings["admin_panel_opacity"] = _normalize_float_setting(
        settings.get("admin_panel_opacity"),
        0.88,
        0.72,
        1.0,
    )
    settings["sample_background_url"] = DEFAULT_SAMPLE_BACKGROUND_URL
    return settings


def _update_admin_appearance(payload: dict) -> dict:
    updates = {
        "admin_background_enabled": "1" if bool(payload.get("admin_background_enabled")) else "0",
        "admin_background_url": _normalize_background_url(payload.get("admin_background_url")),
        "admin_background_dim": _normalize_float_setting(payload.get("admin_background_dim"), 0.12, 0.0, 0.96),
        "admin_panel_opacity": _normalize_float_setting(payload.get("admin_panel_opacity"), 0.88, 0.72, 1.0),
    }
    now = _utc_now()
    with _assistant_db_connect() as conn:
        for key, value in updates.items():
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
    return _admin_appearance()


def _memory_from_row(row: sqlite3.Row) -> dict:
    return _memory_from_row_impl(row)


def _add_memory(
    user_id: str,
    content: str,
    *,
    kind: str = "fact",
    source: str = "manual",
    score: int = 5,
    request_source: str = "",
    scope_type: str = "",
    sensitivity: str = "private",
    project_id: str | None = None,
    subject_actor_ref: str = "",
    source_external_message_id: str = "",
) -> dict:
    with _assistant_db_connect() as conn:
        return scoped_add_memory(
            conn,
            user_id,
            content,
            kind=kind,
            source=source,
            score=score,
            request_source=request_source,
            scope_type=scope_type,
            sensitivity=sensitivity,
            project_id=project_id,
            subject_actor_ref=subject_actor_ref,
            source_external_message_id=source_external_message_id,
        )


def _delete_memory(memory_id: str, user_id: str | None = None) -> bool:
    with _assistant_db_connect() as conn:
        return scoped_delete_memory(conn, memory_id)


def _clip_text(value: object, limit: int = 800) -> str:
    return _clip_text_impl(value, limit)


def _qq_event_from_row(row: sqlite3.Row) -> dict:
    return _qq_event_from_row_impl(row)


def _record_qq_event(payload: dict) -> dict:
    trace_id = _clip_text(payload.get("trace_id") or uuid.uuid4().hex[:12], 80)
    user_id = _clip_text(payload.get("user_id") or "unknown", 80)
    stage = _clip_text(payload.get("stage") or "event", 80)
    action = _clip_text(payload.get("action") or "", 80)
    status = _clip_text(payload.get("status") or "", 80)
    task_id = _clip_text(payload.get("task_id") or "", 80)
    message = _clip_text(payload.get("message") or "", 1000)
    detail = _clip_text(payload.get("detail") or "", 2000)
    session = _clip_text(payload.get("session") or "", 500)
    now = _utc_now()
    with _assistant_db_connect() as conn:
        update_qq_session(conn, user_id, session)
        cur = conn.execute(
            """
            INSERT INTO qq_events(trace_id, user_id, stage, action, status, task_id, message, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (trace_id, user_id, stage, action, status, task_id, message, detail, now),
        )
        row = conn.execute("SELECT * FROM qq_events WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _qq_event_from_row(row)


def _list_qq_events(user_id: str = "", trace_id: str = "", limit: int = 30) -> list[dict]:
    clauses = []
    params: list[object] = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(int(limit or 30), 100)))
    with _assistant_db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM qq_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_qq_event_from_row(row) for row in rows]


def _quality_event_from_row(row: sqlite3.Row) -> dict:
    return _quality_event_from_row_impl(row)


def _record_quality_event(
    *,
    user_id: str,
    intent: str,
    provider: str,
    request: str,
    response: str,
    checks: dict,
    tool: str = "",
    fallback: bool = False,
    duration: float | None = None,
) -> dict | None:
    status = str(checks.get("status") or "unknown")
    issues = checks.get("issues") or []
    now = _utc_now()
    try:
        with _assistant_db_connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO quality_events(
                    user_id, intent, provider, request, response, checks, status,
                    issues, tool, fallback, duration, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _clip_text(user_id or "default", 80),
                    _clip_text(intent, 80),
                    _clip_text(provider, 80),
                    _clip_text(request, 4000),
                    _clip_text(response, 4000),
                    json.dumps(checks, ensure_ascii=False),
                    _clip_text(status, 40),
                    json.dumps(issues, ensure_ascii=False),
                    _clip_text(tool, 80),
                    1 if fallback else 0,
                    duration,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM quality_events WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _quality_event_from_row(row) if row else None
    except sqlite3.Error:
        return None


def _list_quality_events(user_id: str = "", status: str = "", limit: int = 20) -> list[dict]:
    clauses = []
    params: list[object] = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(int(limit or 20), 100)))
    try:
        with _assistant_db_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM quality_events
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_quality_event_from_row(row) for row in rows]


def _mode_session_from_row(row: sqlite3.Row | None) -> dict | None:
    return _mode_session_from_row_impl(row)


def _get_mode_session(user_id: str) -> dict | None:
    try:
        with _assistant_db_connect() as conn:
            row = conn.execute(
                "SELECT * FROM mode_sessions WHERE user_id = ?",
                ((user_id or "default").strip(),),
            ).fetchone()
        return _mode_session_from_row(row)
    except sqlite3.Error:
        return None


def _save_mode_session(session: dict) -> dict | None:
    try:
        with _assistant_db_connect() as conn:
            conn.execute(
                """
                INSERT INTO mode_sessions(
                    user_id, mode, intent, confidence, reason, source, work_lifecycle,
                    turn_count, work_turns, expires_at, ended_reason, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    mode = excluded.mode,
                    intent = excluded.intent,
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    source = excluded.source,
                    work_lifecycle = excluded.work_lifecycle,
                    turn_count = excluded.turn_count,
                    work_turns = excluded.work_turns,
                    expires_at = excluded.expires_at,
                    ended_reason = excluded.ended_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    _clip_text(session.get("user_id") or "default", 80),
                    _clip_text(session.get("mode") or "daily", 20),
                    _clip_text(session.get("intent") or "chat", 80),
                    float(session.get("confidence") or 0),
                    _clip_text(session.get("reason") or "", 800),
                    _clip_text(session.get("source") or "", 40),
                    _clip_text(session.get("work_lifecycle") or "none", 40),
                    int(session.get("turn_count") or 0),
                    int(session.get("work_turns") or 0),
                    _clip_text(session.get("expires_at") or "", 80),
                    _clip_text(session.get("ended_reason") or "", 80),
                    _clip_text(session.get("updated_at") or _utc_now(), 80),
                ),
            )
            row = conn.execute(
                "SELECT * FROM mode_sessions WHERE user_id = ?",
                (_clip_text(session.get("user_id") or "default", 80),),
            ).fetchone()
        return _mode_session_from_row(row)
    except (sqlite3.Error, ValueError):
        return None


def _list_mode_sessions(user_id: str = "", mode: str = "", limit: int = 20) -> list[dict]:
    clauses = []
    params: list[object] = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(int(limit or 20), 100)))
    try:
        with _assistant_db_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM mode_sessions
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_mode_session_from_row(row) for row in rows]
    except sqlite3.Error:
        return []


def _detect_agent_intent(message: str) -> str:
    return detect_agent_intent(message)


def _intent_label(intent: str) -> str:
    return intent_label(intent)


def _acceptance_criteria(
    intent: str,
    message: str,
    policy: dict,
    mode_decision: dict | None = None,
) -> list[str]:
    return build_acceptance_criteria(intent, message, policy, mode_decision)


def _quality_check_response(
    *,
    request: str,
    response: str,
    result: dict,
    intent: str,
    criteria: list[str],
    policy: dict,
    mode_decision: dict | None = None,
) -> dict:
    return check_agent_response_quality(
        request=request,
        response=response,
        result=result,
        intent=intent,
        criteria=criteria,
        policy=policy,
        mode_decision=mode_decision,
        now_text=_utc_now(),
    )


def _keyword_set(text: str) -> set[str]:
    lowered = (text or "").lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    for chunk in chinese:
        words.add(chunk)
        for idx in range(max(0, len(chunk) - 1)):
            words.add(chunk[idx : idx + 2])
    return words


def _search_memories(
    user_id: str,
    query: str = "",
    limit: int = ASSISTANT_MEMORY_LIMIT,
    *,
    request_source: str = "",
    purpose: str = "chat",
    project_id: str | None = None,
    retrieval_context: dict | None = None,
) -> list[dict]:
    owner_bound = str(user_id or "").strip() in qq_super_admin_ids(_assistant_db_connect)
    try:
        with _assistant_db_connect() as conn:
            return scoped_list_memories(
                conn,
                user_id,
                request_source=request_source,
                query=query,
                limit=limit,
                purpose=purpose,
                owner_bound=owner_bound,
                project_id=project_id,
                **({"retrieval_context": retrieval_context} if retrieval_context is not None else {}),
            )
    except sqlite3.Error:
        return []

def _list_memories(
    user_id: str = "default",
    query: str = "",
    limit: int = 20,
    *,
    request_source: str = "",
    purpose: str = "chat",
    owner_management: bool = False,
    project_id: str | None = None,
    fail_on_read_error: bool = False,
) -> list[dict]:
    owner_bound = str(user_id or "").strip() in qq_super_admin_ids(_assistant_db_connect)
    try:
        with _assistant_db_connect() as conn:
            return scoped_list_memories(
                conn,
                user_id,
                request_source=request_source,
                query=query,
                limit=limit,
                purpose=purpose,
                owner_management=owner_management,
                owner_bound=owner_bound,
                project_id=project_id,
            )
    except sqlite3.Error as exc:
        if fail_on_read_error:
            raise RuntimeError("memory_read_failed") from exc
        return []


def _record_conversation(
    user_id: str,
    role: str,
    content: str,
    *,
    source: str = "",
) -> str | None:
    with _assistant_db_connect() as conn:
        return scoped_record_conversation(conn, user_id, role, content, source=source)


INTERACTION_STORE = InteractionPersistenceRuntime(_assistant_db_connect)
ACTION_COMMITMENTS = ActionCommitmentRepository(_assistant_db_connect)
CONTINUITY_KERNEL = ContinuityKernel(_assistant_db_connect)


def _conversation_history(
    user_id: str,
    limit: int = ASSISTANT_HISTORY_LIMIT,
    *,
    source: str = "",
) -> list[dict]:
    try:
        with _assistant_db_connect() as conn:
            return scoped_conversation_history(
                conn,
                user_id,
                source=source,
                limit=limit,
            )
    except sqlite3.Error:
        return []


def _extract_memory_candidates(text: str) -> list[str]:
    text = " ".join((text or "").split())
    if not text:
        return []
    candidates = []
    for trigger in MEMORY_TRIGGERS:
        if text.startswith(trigger):
            fact = text[len(trigger) :].strip(" ：:，,。.")
            if fact:
                candidates.append(fact)
    result = []
    seen = set()
    for item in candidates:
        item = item.strip()
        if 2 <= len(item) <= 180 and item not in seen:
            result.append(item)
            seen.add(item)
    return result[:3]


def _agent_policy_lines(policy: dict) -> list[str]:
    return build_agent_policy_lines(policy)


def _assistant_voice_lines(settings: dict, mode_decision: dict, social_context: dict | None) -> list[str]:
    context = social_context or {}
    contract = dict(context.get("voice_contract") or build_voice_contract(
        settings,
        mode_decision=mode_decision,
        group_context=context.get("group"),
    ))
    turn_plan = dict(context.get("expression_plan") or plan_expression(
        "",
        social_cues=context.get("cues"),
        mode_decision=mode_decision,
        group_context=context.get("group"),
        voice_contract=contract,
    ))
    relationship_lines = (
        relationship_context_lines(context.get("relationship"))
        if contract.get("optional_persona_applied", True)
        else []
    )
    return [
        "稳定 Voice Contract:",
        *voice_contract_lines(contract),
        *( ["", *relationship_lines] if relationship_lines else [] ),
        "",
        "本轮 Expression Plan:",
        *expression_plan_lines(turn_plan),
    ]


def _assistant_identity_prompt_lines(settings: dict, social_context: dict | None) -> list[str]:
    context = social_context or {}
    contract = dict(context.get("voice_contract") or build_voice_contract(settings))
    lines = [f"你的名字: {contract.get('identity') or 'Assistant'}"]
    if contract.get("optional_persona_applied", True):
        lines.extend(
            [
                f"关系模式: {contract.get('relationship') or ''}",
                f"人设: {contract.get('persona') or ''}",
                f"回复风格: {contract.get('style') or ''}",
            ],
        )
    else:
        lines.append("可选人格已关闭；只保留身份事实、安全和动作真实性边界。")
    return lines


def _format_assistant_system_prompt(
    settings: dict,
    memories: list[dict],
    *,
    intent: str = "chat",
    criteria: list[str] | None = None,
    policy: dict | None = None,
    mode_decision: dict | None = None,
    social_context: dict | None = None,
    attachment_context: dict | None = None,
) -> str:
    project = _current_project() or {}
    memory_lines = [f"- {item['content']}" for item in memories] or ["- 暂无相关长期记忆。"]
    policy = policy or _agent_policy(settings)
    mode_decision = mode_decision or {"mode": "work" if intent in {"ops", "code", "research", "analysis", "memory"} else "daily"}
    if str(mode_decision.get("mode") or "daily") != "work":
        context = social_context or {}
        return build_daily_system_prompt(
            settings,
            memories,
            mode_decision=mode_decision,
            habits=list(context.get("habits") or []),
            group_context=context.get("group"),
            attachment_context=attachment_context,
            voice_contract=context.get("voice_contract"),
            expression_plan=context.get("expression_plan"),
            relationship_context=context.get("relationship"),
        )
    criteria = criteria or _acceptance_criteria(intent, "", policy, mode_decision)
    attachment_lines = attachment_capability_lines(attachment_context)
    return "\n".join(
        [
            "你正在通过 QQ 和用户私聊。请只输出要发给用户的一条中文回复，不要输出分析过程。",
            "你不是普通命令行工具；你是一个有记忆的虚拟 AI 助手。",
            *_assistant_identity_prompt_lines(settings, social_context),
            "",
            *_assistant_voice_lines(settings, mode_decision, social_context),
            "",
            "边界:",
            "- 不要声称自己是真人。",
            "- 普通闲聊要自然，像熟悉的人一样回应。",
            "- 日常模式先接住用户情绪，再回答事情；可以短一点、口语一点，不要像工单系统。",
            "- 用户表达疲惫、烦躁、撒娇或开玩笑时，先回应情绪，再给建议。",
            "- 涉及服务器、代码、项目时要准确、克制，不假装已经执行了操作。",
            "- 无 ActionReceipt，不得声称执行过任何操作。",
            "- 如果用户提出明确开发、运维、资料查询或项目目标，自动按工作模式处理，不要求用户说固定口令。",
            "",
            "Agent 工作协议:",
            *_agent_policy_lines(policy),
            "",
            "模式策略:",
            *mode_policy_lines(mode_decision, policy),
            "",
            "本轮识别:",
            f"- 意图: {_intent_label(intent)}",
            "",
            "本轮验收标准:",
            *[f"- {item}" for item in criteria],
            "",
            "当前项目:",
            f"- {project.get('name', '?')}: {project.get('path', '?')}",
            "",
            "长期记忆:",
            *memory_lines,
            *(["", *attachment_lines] if attachment_lines else []),
        ],
    )


def _assistant_chat_messages(settings, user_id, message, memories, history, intent="chat", criteria=None,
                             policy=None, mode_decision=None, social_context=None, attachment_context=None):
    resolved_policy = policy or _agent_policy(settings)
    return build_conversation_messages(
        settings, message, memories, history, mode_decision=mode_decision, social_context=social_context,
        attachment_context=attachment_context, history_limit=ASSISTANT_HISTORY_LIMIT,
        build_work_layers=lambda: build_work_cache_layers(
            _assistant_identity_prompt_lines(settings, social_context), mode_policy_lines(mode_decision or {}, resolved_policy),
            _intent_label(intent), criteria or _acceptance_criteria(intent, "", resolved_policy, mode_decision or {}),
            _current_project() or {}, [f"- {item['content']}" for item in memories] or ["- 暂无相关长期记忆。"], attachment_capability_lines(attachment_context),
        ),
    )


def _prepare_group_single_plan_messages(
    *,
    settings: dict,
    user_id: str,
    message: str,
    history: list[dict],
    group: dict,
    decision_messages: list[dict],
    research_context: dict | None = None,
    current: dict | None = None,
) -> dict:
    """Prepare persona/relationship context for one ambient model call.

    This helper performs local and governed retrieval only. It deliberately
    does not invoke a planner, classifier, reply model or outgoing-meme model.
    """

    with ASSISTANT_LOCK:
        memories = _search_memories(
            user_id,
            message,
            ASSISTANT_MEMORY_LIMIT,
            request_source="qq_group_natural",
            retrieval_context=build_group_memory_retrieval_context(
                current or {"sender_id": group.get("sender_id")}, history, decision_messages,
            ),
        )
    memories, _ = merge_shared_knowledge(
        _assistant_db_connect,
        memories,
        message=message,
        group=group,
    )
    research_truth_context = None
    research = research_context if isinstance(research_context, dict) else {}
    if research.get("available"):
        source_lines = []
        source_urls = []
        for source in list(research.get("sources") or [])[:3]:
            if not isinstance(source, dict):
                continue
            title = str(source.get("title") or "").strip()[:180]
            url = str(source.get("url") or "").strip()[:500]
            excerpt = str(source.get("excerpt") or "").strip()[:600]
            if title and url.startswith("https://") and excerpt:
                source_lines.append(f"- {title}：{excerpt}（{url}）")
                source_urls.append(url)
        if source_lines:
            memories.append({
                "kind": "public_research_evidence",
                "content": (
                    "[本轮公共资料：只能据此回答可确认事实；回复须附至少一个来源链接，"
                    "无法确认就说明不确定，不得把资料外的内容当事实]\n"
                    + "\n".join(source_lines)
                ),
            })
            research_truth_context = {"source_urls": source_urls}
    elif research.get("fact_unverified"):
        memories.append({
            "kind": "public_research_unverified",
            "content": (
                "[本轮外部事实没有取得允许的可引用证据。不要把该问题当作已验证事实；"
                "应明确说明暂不下结论，不能编造来源或补全细节。]"
            ),
        })
        research_truth_context = {"fact_unverified": True}

    mode_seed = {
        "mode": "daily",
        "mode_label": "日常聊天",
        "intent": "chat",
        "emotion": "neutral",
        "reply_length": "short",
        "meme_intent": "none",
        "engagement": "consider",
    }
    _cues, social_context = build_social_context(
        _assistant_db_connect,
        history,
        settings=settings,
        mode_decision=mode_seed,
        message=message,
        user_id=user_id,
        group=group,
    )
    prompt_expression_plan = dict(social_context.get("expression_plan") or {})
    prompt_expression_plan.pop("social_action", None)
    persona_prompt = build_daily_system_prompt(
        settings,
        memories,
        mode_decision=mode_seed,
        habits=list(social_context.get("habits") or []),
        group_context=social_context.get("group"),
        voice_contract=social_context.get("voice_contract"),
        expression_plan=prompt_expression_plan,
        relationship_context=social_context.get("relationship"),
        output_protocol=(
            "你正在 QQ 群聊中回复消息。本轮只输出随后定义的 Group Single Plan JSON，"
            "不输出分析、模式标签或额外文字。"
        ),
    )
    return {
        "messages": build_group_single_plan_messages(persona_prompt, decision_messages),
        "social_context": social_context,
        "research_truth_context": research_truth_context,
        "output_protocol": GROUP_SINGLE_PLAN_OUTPUT_PROTOCOL,
    }


def _chat_completion_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _provider_request_opener(url: str):
    host = (urlparse(url).hostname or "").lower()
    use_proxy = True
    if host in {"localhost", "127.0.0.1", "::1"}:
        use_proxy = False
    else:
        try:
            address = ipaddress.ip_address(host)
            use_proxy = not (address.is_private or address.is_loopback or address.is_link_local)
        except ValueError:
            use_proxy = True
    if use_proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": MIHOMO_PROXY_URL, "https": MIHOMO_PROXY_URL}),
        )
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _assistant_provider_ready(settings: dict) -> tuple[bool, str]:
    if str(settings.get("chat_provider") or "codex") == "codex":
        return True, ""
    if not str(settings.get("chat_base_url") or "").strip():
        return False, "chat_base_url_missing"
    if not str(settings.get("chat_model") or "").strip():
        return False, "chat_model_missing"
    if (
        str(settings.get("model_billing_scope") or "api_key") != "local_proxy"
        and not str(settings.get("chat_api_key") or "").strip()
    ):
        return False, "chat_api_key_missing"
    return True, ""


def _record_model_call(
    settings: dict,
    result: dict,
    *,
    source: str,
    user_id: str = "",
    trace_id: str = "",
) -> None:
    try:
        with _assistant_db_connect() as conn:
            record_model_usage(
                conn,
                settings,
                result,
                source=source,
                user_id=user_id,
                trace_id=trace_id,
            )
    except sqlite3.Error:
        return


def _call_openai_compatible_chat(settings: dict, messages: list[dict], timeout: int) -> dict:
    ready, reason = _assistant_provider_ready(settings)
    if not ready:
        return {
            "ok": False,
            "error_kind": "provider_config",
            "error": reason,
            "provider": "openai-compatible",
            "provider_label": provider_label(settings),
        }
    model = str(settings.get("chat_model") or "").strip()
    try:
        spec = prepare_model_request(settings, messages)
    except (TypeError, ValueError) as exc:
        return {
            "ok": False,
            "error_kind": "provider_config",
            "error": str(exc),
            "provider": "model-provider",
            "provider_label": provider_label(settings),
            "model": model,
        }
    url = spec["url"]
    provider = spec["provider"]
    transport = spec["transport"]
    request = urllib.request.Request(
        url,
        data=json.dumps(spec["payload"], ensure_ascii=False).encode("utf-8"),
        headers=spec["headers"],
        method="POST",
    )
    started = time.monotonic()
    try:
        opener = _provider_request_opener(url)
        with opener.open(request, timeout=max(1, min(int(timeout or 60), 300))) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        http = provider_http_error_facts(exc.code, detail)
        return {
            "ok": False,
            "error_kind": http["kind"],
            "error": http["error"],
            "upstream_error_code": http["upstream_error_code"],
            "retryable": http["retryable"],
            "owner_action_required": http["owner_action_required"],
            "stderr": detail,
            "provider": provider,
            "provider_label": provider_label(settings),
            "model": model,
            "duration": round(time.monotonic() - started, 3),
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "error_kind": provider_transport_error_kind(exc),
            "error": str(exc),
            "provider": provider,
            "provider_label": provider_label(settings),
            "model": model,
            "duration": round(time.monotonic() - started, 3),
        }
    try:
        data = json.loads(raw)
        reply, usage = parse_model_response(transport, data)
    except Exception as exc:
        return {
            "ok": False,
            "error_kind": "parse",
            "error": str(exc),
            "stdout": raw[:2000],
            "provider": provider,
            "provider_label": provider_label(settings),
            "model": model,
            "duration": round(time.monotonic() - started, 3),
        }
    return {
        "ok": bool(reply),
        "reply": reply,
        "output": reply,
        "provider": provider,
        "provider_label": provider_label(settings),
        "model": model,
        "reported_model": provider_reported_model(transport, data),
        "usage": usage,
        **openai_response_facts(data, reply, usage),
        "duration": round(time.monotonic() - started, 3),
        "error": "" if reply else "empty_provider_reply",
        "error_kind": "" if reply else "empty",
    }


def _assistant_provider_test(timeout: int = 45, payload: dict | None = None) -> dict:
    fallback = _assistant_settings(include_secrets=True)
    model_item = None
    request_payload = payload or {}
    with _assistant_db_connect() as conn:
        try:
            settings, model_item = provider_test_settings(conn, request_payload, fallback)
        except ValueError:
            if str(request_payload.get("model_id") or "").strip() or "role" in request_payload:
                raise
            settings = _settings_for_model_role("conversation_reply", fallback)
    if str(settings.get("chat_provider") or "codex") == "codex":
        transport = str(settings.get("model_transport") or "codex_cli_chatgpt")
        validation_cwd = (
            executor_workspace_root()
            if transport == "codex_cli_custom_provider"
            else DEFAULT_CWD
        )
        result = _run_codex_assistant_chat(
            "你是接口连通性测试助手。请只回复 OK",
            cwd=validation_cwd,
            timeout=max(20, min(int(timeout or 45), 120)),
            settings_override=settings,
        )
        result.update({
            "provider": "codex",
            "provider_label": provider_label(settings),
            "model": str(settings.get("codex_model") or ""),
            "message": "Codex 模型调用验证通过。" if result.get("ok") else "Codex 模型调用验证失败。",
        })
        if model_item:
            with _assistant_db_connect() as conn:
                record_provider_test(conn, str(model_item.get("provider_id") or ""), result)
        _record_model_call(settings, result, source="connection_test")
        result["settings"] = _assistant_settings()
        return result
    messages = [
        {"role": "system", "content": "你是接口连通性测试助手。"},
        {"role": "user", "content": "请只回复 OK"},
    ]
    result = _call_openai_compatible_chat(settings, messages, timeout=timeout)
    _record_model_call(settings, result, source="connection_test")
    if model_item:
        with _assistant_db_connect() as conn:
            record_provider_test(conn, str(model_item.get("provider_id") or ""), result)
    result["settings"] = _assistant_settings()
    return result


def _strip_ansi(text: str) -> str:
    return _strip_ansi_impl(text)


def _extract_codex_last_message(text: str) -> str:
    return _extract_codex_last_message_impl(text, strip_ansi_fn=_strip_ansi)


def _run_codex_assistant_chat(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    settings_override: dict | None = None,
) -> dict:
    """Run a conversational Codex request without racing the shared proxy.

    A custom executor profile is backed by the same singleton proxy used for
    work tasks.  Conversation, group participation and decision requests must
    therefore take the same reader lock and verify the active runtime identity
    before spawning Codex; otherwise an Owner activation could switch the
    proxy beneath an already-built request.
    """
    settings = dict(settings_override or _assistant_settings(include_secrets=True))
    if str(settings.get("model_transport") or "") == "codex_cli_custom_provider":
        profile = dict(settings.get("executor_profile") or {})
        provider_id = str(settings.get("model_registry_provider_id") or profile.get("provider_id") or "").strip()
        config_version = profile.get("config_version")
        model_name = str(settings.get("codex_model") or "").strip()
        with executor_runtime_shared_lock():
            if not executor_runtime_identity_matches(provider_id, config_version, model_name):
                return {
                    "ok": False,
                    "returncode": 1,
                    "duration": 0,
                    "output": "",
                    "error": "执行器运行时身份与所选连接不一致，已停止本次请求。",
                    "error_kind": "executor_runtime_identity_mismatch",
                }
            return _run_codex_assistant_chat_locked(
                prompt,
                cwd=cwd,
                timeout=timeout,
                settings_override=settings,
            )
    return _run_codex_assistant_chat_locked(
        prompt,
        cwd=cwd,
        timeout=timeout,
        settings_override=settings,
    )


def _run_codex_assistant_chat_locked(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    settings_override: dict | None = None,
) -> dict:
    fd, output_name = tempfile.mkstemp(prefix="codex-assistant-", suffix=".txt")
    os.close(fd)
    output_path = Path(output_name)
    try:
        settings = dict(settings_override or _assistant_settings(include_secrets=True))
        transport = str(settings.get("model_transport") or "")
        profile = dict(settings.get("executor_profile") or {})
        args = [
            "codex",
            "exec",
            "--skip-git-repo-check",
        ]
        env = _codex_exec_env("codex_login", profile)
        if transport == "codex_cli_custom_provider":
            if not shutil.which("bwrap"):
                return {
                    "ok": False,
                    "returncode": 1,
                    "duration": 0,
                    "output": "Codex executor sandbox prerequisite is unavailable: bubblewrap is not installed.",
                    "error": "Codex executor sandbox prerequisite is unavailable: bubblewrap is not installed.",
                    "error_kind": "executor_sandbox_unavailable",
                }
            _validate_executor_sandbox_and_cwd("read-only", "codex_custom_provider", cwd)
            profile_name = str(profile.get("profile_name") or "").strip()
            if not profile_name:
                raise RuntimeError("executor_profile_missing")
            args.extend(["--profile", profile_name, "--ephemeral"])
            env = _codex_exec_env("codex_custom_provider", profile)
        args.extend([
            *codex_model_args(settings),
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
        ])
        result = _run_command(
            args,
            input_text=prompt,
            cwd=cwd,
            timeout=timeout,
            env=env,
        )
        result["codex_model"] = str(settings.get("codex_model") or "")
        reply = ""
        try:
            if output_path.exists():
                reply = output_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            reply = ""
        if result.get("ok") and not reply:
            diagnostic = str(result.get("stderr") or result.get("stdout") or result.get("output") or "")
            result.update({
                "ok": False,
                "error_kind": "codex_no_last_agent_message",
                "error": "模型进程已结束，但兼容层没有返回可识别的助手正文。",
                "output": diagnostic[-2000:],
            })
        if result.get("ok") and reply:
            result["output"] = reply
            result["reply"] = reply
            result["stdout"] = reply
            result["stderr"] = ""
        return result
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_group_research_adapter(query: str, *, group_id: str) -> dict:
    """Use only Codex Login Web Search for the separately authorized R7 pilot."""

    try:
        with _assistant_db_connect() as conn:
            if not group_research_execution_allowed(conn, group_id):
                return {"ok": False, "sources": []}
        snapshot = _resolve_executor_snapshot()
        if str(snapshot.get("adapter") or "") != "codex_login":
            return {"ok": False, "sources": []}
    except (sqlite3.Error, RuntimeError, ValueError):
        return {"ok": False, "sources": []}

    fd, output_name = tempfile.mkstemp(prefix="codex-group-research-", suffix=".json")
    os.close(fd)
    output_path = Path(output_name)
    try:
        settings = _assistant_settings(include_secrets=True)
        args = [
            "codex", "exec", "--skip-git-repo-check",
            *codex_model_args(settings), "--sandbox", "read-only",
            "--color", "never", "--output-last-message", str(output_path),
        ]
        result = _run_command(
            args,
            input_text=build_group_research_prompt(query),
            cwd=_default_cwd(),
            timeout=75,
            env=_codex_exec_env("codex_login", {}),
        )
        if not result.get("ok"):
            return {"ok": False, "sources": []}
        try:
            output = output_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"ok": False, "sources": []}
        return parse_group_research_output(output)
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_private_research_adapter(query: str) -> dict:
    """Capability-owned Codex --search adapter for private research.web.read.

    This is a Research Capability Backend, not a task-model fallback.
    It is independent of the currently selected work executor (deepseek_proxy vs codex_login).
    The user's selected assistant/work model remains unchanged; only this
    capability's retrieval uses the dedicated Codex Search backend when
    network policy allows.  Failure here does not switch the user's model,
    it falls through to bounded HTTP discovery (GitHub etc).
    """

    try:
        with _assistant_db_connect() as conn:
            if not network_capability_allowed(conn, RESEARCH_CAPABILITY_ID):
                return {"ok": False, "sources": []}
    except (sqlite3.Error, RuntimeError, ValueError):
        return {"ok": False, "sources": []}
    # Intentionally NOT checking _resolve_executor_snapshot().  Research
    # retrieval must not be tied to the current work executor's adapter.
    # We directly attempt Codex with codex_login env; if Codex is not
    # logged in or the binary is unavailable, the run will fail and the
    # caller will fall back to bounded HTTP discovery.

    fd, output_name = tempfile.mkstemp(prefix="codex-private-research-", suffix=".json")
    os.close(fd)
    output_path = Path(output_name)
    try:
        settings = _assistant_settings(include_secrets=True)
        args = [
            "codex", "exec", "--skip-git-repo-check",
            *codex_model_args(settings), "--sandbox", "read-only",
            "--color", "never", "--output-last-message", str(output_path),
        ]
        # Reuse strict group prompt (bounded, JSON-only sources)
        result = _run_command(
            args,
            input_text=build_group_research_prompt(query),
            cwd=_default_cwd(),
            timeout=75,
            env=_codex_exec_env("codex_login", {}),
        )
        if not result.get("ok"):
            return {"ok": False, "sources": []}
        try:
            output = output_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"ok": False, "sources": []}
        return parse_group_research_output(output)
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass


def maybe_group_research(*, group_id: str, anchor: dict, message: str) -> dict:
    """Return public evidence for one current group question, or a safe no-op.

    The worker calls this only for an already selected natural contribution.
    Disabled policy and available local knowledge are intentionally no-op paths
    so ordinary conversation never produces a stream of blocked audit rows.
    """

    try:
        with _assistant_db_connect() as conn:
            if not group_research_execution_allowed(conn, group_id):
                return {"available": False, "sources": []}
            classification = classify_research_topic(message)
            # Casual, private and prohibited conversation is not a research
            # candidate and must not create a durable audit row merely because
            # the pilot happens to be enabled.
            if str(classification.get("reason_code") or "") in {
                "research_empty_topic", "research_not_factual",
                "research_private_context", "research_prohibited_content",
                "research_public_subject_unproven", "research_public_query_unproven",
            }:
                return {"available": False, "sources": []}
            public_query = str(classification.get("query") or "")
            if not public_query:
                return {"available": False, "sources": []}
            known = bool(search_published(
                conn,
                public_query,
                channel="group",
                group_id=group_id,
                limit=1,
                audit_query="public-query:" + hashlib.sha256(public_query.encode("utf-8")).hexdigest()[:20],
            ))
            if known:
                return {"available": False, "sources": []}
            result = research_unknown_topic(
                conn,
                group_id=group_id,
                anchor_message_id=anchor.get("id"),
                message=message,
                knowledge_found=False,
                research_adapter=lambda query: _run_group_research_adapter(query, group_id=group_id),
            )
            if result.get("status") == "succeeded":
                try:
                    auto_publish_low_public_knowledge(conn, result)
                except (sqlite3.Error, ValueError):
                    # Retention is never allowed to erase a valid, cited
                    # one-turn evidence packet.  The run remains auditable and
                    # the reply path retains its own Truth/Delivery gates.
                    pass
            elif str(result.get("reason_code") or "") == "research_review_required":
                try:
                    create_review_required_knowledge_draft(conn, result)
                except (sqlite3.Error, ValueError):
                    pass
            context = public_research_context(result)
            reason = str(result.get("reason_code") or "")
            if reason in {
                "research_review_required", "research_no_approved_sources",
                "research_adapter_unavailable", "research_quota_exhausted",
            }:
                context["fact_unverified"] = True
                context["reason_code"] = reason
            return context
    except (sqlite3.Error, RuntimeError, ValueError):
        return {"available": False, "sources": []}


visual.configure(lambda *a: _settings_for_model_role(*a), lambda *a: _call_openai_compatible_chat(*a), lambda *a, **k: _record_model_call(*a, **k))


def _assistant_model_playground(payload: dict, timeout: int = 90) -> dict:
    """Run an isolated model validation without writing QQ or conversation state."""
    fallback = _assistant_settings(include_secrets=True)
    with _assistant_db_connect() as conn:
        settings, model_item = provider_test_settings(conn, payload, fallback)
    validation_cwd = (
        executor_workspace_root()
        if str(settings.get("model_transport") or "") == "codex_cli_custom_provider"
        else DEFAULT_CWD
    )
    return run_model_playground(
        payload, timeout, settings=settings, model_item=model_item, default_cwd=validation_cwd,
        run_codex=_run_codex_assistant_chat, run_transport=_call_openai_compatible_chat,
        record_usage=_record_model_call,
    )


def _assistant_discovered_model_playground(payload: dict, timeout: int = 90) -> dict:
    """Validate a discovered model before it exists in the persistent catalog."""

    fallback = _assistant_settings(include_secrets=True)
    with _assistant_db_connect() as conn:
        settings, model_item = discovered_model_validation_settings(
            conn, payload.get("provider_id"), payload.get("model"), fallback,
        )
    return run_model_playground(
        payload, timeout, settings=settings, model_item=model_item, default_cwd=DEFAULT_CWD,
        run_codex=_run_codex_assistant_chat, run_transport=_call_openai_compatible_chat,
        record_usage=_record_model_call,
    )


def _format_assistant_prompt(
    user_id: str,
    message: str,
    memories: list[dict],
    history: list[dict],
    *,
    intent: str = "chat",
    criteria: list[str] | None = None,
    policy: dict | None = None,
    mode_decision: dict | None = None,
    social_context: dict | None = None,
    attachment_context: dict | None = None,
) -> str:
    settings = _assistant_settings()
    project = _current_project() or {}
    memory_lines = [f"- {item['content']}" for item in memories] or ["- 暂无相关长期记忆。"]
    history_lines = [
        f"{'用户' if item['role'] == 'user' else '助手'}: {item['content']}"
        for item in history[-ASSISTANT_HISTORY_LIMIT:]
    ] or ["(暂无近期对话)"]
    policy = policy or _agent_policy(settings)
    mode_decision = mode_decision or {"mode": "work" if intent in {"ops", "code", "research", "analysis", "memory"} else "daily"}
    if str(mode_decision.get("mode") or "daily") != "work":
        context = social_context or {}
        system_prompt = build_daily_system_prompt(
            settings,
            memories,
            mode_decision=mode_decision,
            habits=list(context.get("habits") or []),
            group_context=context.get("group"),
            attachment_context=attachment_context,
            voice_contract=context.get("voice_contract"),
            expression_plan=context.get("expression_plan"),
            relationship_context=context.get("relationship"),
        )
        return "\n".join(
            [
                system_prompt,
                "",
                "近期对话:",
                *history_lines,
                "",
                f"用户现在说: {message}",
            ],
        )
    criteria = criteria or _acceptance_criteria(intent, message, policy, mode_decision)
    attachment_lines = attachment_capability_lines(attachment_context)
    return "\n".join(
        [
            "你正在通过 QQ 和用户私聊。请只输出要发给用户的一条中文回复，不要输出分析过程。",
            "你不是普通命令行工具；你是一个有记忆的虚拟 AI 助手。",
            *_assistant_identity_prompt_lines(settings, social_context),
            "",
            *_assistant_voice_lines(settings, mode_decision, social_context),
            "",
            "边界:",
            "- 不要声称自己是真人。",
            "- 普通闲聊要自然，像熟悉的人一样回应。",
            "- 涉及服务器、代码、项目时要准确、克制，不假装已经执行了操作。",
            "- 如果用户提出明确开发、运维、资料查询或项目目标，自动按工作模式处理，不要求用户说固定口令。",
            "",
            "Agent 工作协议:",
            *_agent_policy_lines(policy),
            "",
            "模式策略:",
            *mode_policy_lines(mode_decision, policy),
            "",
            "本轮识别:",
            f"- QQ 用户: {user_id}",
            f"- 意图: {_intent_label(intent)}",
            f"- 模式: {mode_decision.get('mode_label') or mode_decision.get('mode')}",
            "",
            "本轮验收标准:",
            *[f"- {item}" for item in criteria],
            "",
            "当前项目:",
            f"- {project.get('name', '?')}: {project.get('path', '?')}",
            "",
            "长期记忆:",
            *memory_lines,
            *(["", *attachment_lines] if attachment_lines else []),
            "",
            "近期对话:",
            *history_lines,
            "",
            f"用户现在说: {message}",
        ],
    )


INTERACTION_PLANNER = InteractionPlannerRuntime(
    store=INTERACTION_STORE,
    get_session=_get_mode_session,
    get_model_settings=_settings_for_model_role,
    call_openai=lambda *args, **kwargs: _call_openai_compatible_chat(*args, **kwargs),
    call_codex=lambda *args, **kwargs: _run_codex_assistant_chat(*args, **kwargs),
    default_cwd=_default_cwd,
    record_model_call=lambda *args, **kwargs: _record_model_call(*args, **kwargs),
    save_session=_save_mode_session,
)


def _assistant_chat(
    user_id: str,
    message: str,
    timeout: int = ASSISTANT_CHAT_TIMEOUT,
    *,
    decision_context: dict | None = None,
) -> dict:
    user_id = (user_id or "default").strip()
    context = decision_context or {}
    deadline_monotonic = context.get("deadline_monotonic")
    if type(deadline_monotonic) not in {int, float}:
        deadline_monotonic = None
    classifier_deadline_monotonic = context.get("classifier_deadline_monotonic")
    if type(classifier_deadline_monotonic) not in {int, float}:
        classifier_deadline_monotonic = deadline_monotonic
    elif (
        deadline_monotonic is not None
        and float(classifier_deadline_monotonic) > float(deadline_monotonic)
    ):
        classifier_deadline_monotonic = deadline_monotonic
    deadline_clock = context.get("clock")
    if not callable(deadline_clock):
        deadline_clock = time.monotonic
    raw_message = str(context.get("raw_message") or message or "").strip()
    display_message = str(context.get("display_message") or raw_message).strip()
    if not raw_message:
        return {"ok": False, "error": "message is required"}
    project = context.get("project") if isinstance(context.get("project"), dict) else (_current_project() or {})
    project_id = str(context.get("project_id") or project.get("id") or "").strip() or None
    request_source = str(context.get("source") or ("qq_group" if context.get("group") else "")).strip()
    inbound_exchange = (
        context.get("inbound_context")
        if isinstance(context.get("inbound_context"), dict)
        else None
    )
    defer_response_cycle_exchange = bool(
        request_source == QQ_TASK_SOURCE
        and not context.get("group")
        and inbound_exchange is not None
        and str(inbound_exchange.get("_response_cycle_id") or "").strip()
    )

    def stage_response_cycle_exchange(metadata: Mapping[str, object] | None) -> None:
        if not defer_response_cycle_exchange:
            return
        plan_record = mode_decision.get("interaction_plan_record")
        if not isinstance(plan_record, Mapping) or not str(plan_record.get("id") or ""):
            raise ResponseCycleRecoveryError("response_cycle_interaction_plan_missing")
        inbound_exchange["_response_cycle_plan_id"] = str(plan_record["id"])
        inbound_exchange["_response_cycle_assistant_metadata"] = {
            key: value
            for key, value in dict(metadata or {}).items()
            if not str(key).startswith("provider_cache_")
        }
    with ASSISTANT_LOCK:
        memory_candidates = _extract_memory_candidates(raw_message)
        memories = _search_memories(
            user_id,
            raw_message,
            ASSISTANT_MEMORY_LIMIT,
            request_source=request_source,
            project_id=project_id,
        )
        history = list(context.get("history") or _conversation_history(user_id, ASSISTANT_HISTORY_LIMIT))
    memories, _ = merge_shared_knowledge(
        _assistant_db_connect, memories, message=raw_message, group=context.get("group"),
    )
    research_context = context.get("research_context")
    if not isinstance(research_context, dict) and isinstance(context.get("group"), dict):
        research_context = context["group"].get("research")
    research_truth_context = None
    if isinstance(research_context, dict) and research_context.get("available"):
        source_lines = []
        for source in list(research_context.get("sources") or [])[:3]:
            if not isinstance(source, dict):
                continue
            title = str(source.get("title") or "").strip()[:180]
            url = str(source.get("url") or "").strip()[:500]
            excerpt = str(source.get("excerpt") or "").strip()[:600]
            if title and url.startswith("https://") and excerpt:
                source_lines.append(f"- {title}：{excerpt}（{url}）")
        if source_lines:
            research_truth_context = {
                "source_urls": [
                    str(source.get("url") or "").strip()
                    for source in list(research_context.get("sources") or [])[:3]
                    if isinstance(source, dict)
                    and str(source.get("url") or "").strip().startswith("https://")
                ],
            }
            memories.append({
                "kind": "public_research_evidence",
                "content": "[本轮公共资料：只能据此回答可确认事实；回复须附至少一个来源链接，"
                "无法确认就说明不确定，不得把资料外的内容当事实]\n" + "\n".join(source_lines),
            })
    elif isinstance(research_context, dict) and research_context.get("fact_unverified"):
        research_truth_context = {"fact_unverified": True}
        memories.append({
            "kind": "public_research_unverified",
            "content": "[本轮外部事实没有取得允许的可引用证据。不要把该问题当作已验证事实；"
            "应明确说明暂不下结论，不能编造来源或补全细节。]",
        })
    settings = _with_model_session_scope(
        dict(context.get("settings") or _assistant_settings(include_secrets=True)),
        user_id,
    )
    policy = dict(context.get("policy") or _agent_policy(settings))
    mode_decision = context.get("mode_decision")
    mode_session = context.get("mode_session")
    if not mode_decision:
        planner_kwargs = {
            "user_id": user_id,
            "message": raw_message,
            "settings": settings,
            "policy": policy,
            "history": history,
            "timeout": min(int(timeout or ASSISTANT_CHAT_TIMEOUT), 90),
        }
        if classifier_deadline_monotonic is not None:
            planner_kwargs.update({
                "deadline_monotonic": classifier_deadline_monotonic,
                "clock": deadline_clock,
            })
        allow_classifier = context.get("allow_classifier")
        if type(allow_classifier) is bool:
            planner_kwargs["allow_classifier"] = allow_classifier
        mode_decision, mode_session = INTERACTION_PLANNER.decide(
            **planner_kwargs,
        )
    else:
        mode_decision = INTERACTION_STORE.ensure_plan(raw_message, mode_decision)
    INTERACTION_STORE.persist(user_id, mode_decision, source=request_source)
    continuity_candidates = capture_plan_candidate_metadata(
        _assistant_db_connect,
        explicit_memories=memory_candidates,
        legacy_user_id=user_id,
        message=raw_message,
        interaction_plan=mode_decision.get("interaction_plan"),
        source=request_source or ("qq_group" if context.get("group") else ""),
        group=context.get("group"),
        source_external_message_id=(
            str((inbound_exchange or {}).get("_external_message_id") or "")
            if not context.get("group") else ""
        ),
        source_actor_ref=(
            str((inbound_exchange or {}).get("_qq_actor_id")
                or (inbound_exchange or {}).get("sender_id") or user_id)
            if not context.get("group") else ""
        ),
    )
    intent = str(mode_decision.get("intent") or _detect_agent_intent(raw_message))
    criteria = _acceptance_criteria(intent, raw_message, policy, mode_decision)
    social_cues, social_context = build_social_context(
        _assistant_db_connect, history,
        settings=settings,
        mode_decision=mode_decision,
        message=raw_message,
        user_id=user_id,
        group=context.get("group"),
        interaction_context=context.get("inbound_context"),
    )
    runtime_role = "conversation_reply" if str(mode_decision.get("mode") or "daily") != "work" else "work_planner"
    chat_settings = _with_model_session_scope(
        _settings_for_model_role(runtime_role, settings),
        user_id,
    )
    chat_settings = with_conversation_cache_contract(chat_settings, group=bool(context.get("group")), work=runtime_role == "work_planner")
    attachment_settings = dict(settings)
    attachment_policy = dict(policy)
    persona_meme_policy = str(
        social_context.get("voice_contract", {}).get("meme_policy_key") or "contextual"
    )
    if persona_meme_policy == "never":
        attachment_settings.update({
            "meme_enabled": "0",
            "meme_daily_enabled": "0",
            "meme_work_enabled": "0",
        })
    elif persona_meme_policy == "frequent":
        attachment_policy["daily_emoji_mode"] = "auto"
    meme_selection_runtime = (
        select_and_reserve_meme,
        _settings_for_model_role("vision_caption", settings),
        _call_openai_compatible_chat,
        _record_model_call,
    )
    if (
        request_source == QQ_TASK_SOURCE
        and not context.get("group")
        and (
            context.get("private_multimodal_truth_guard") is True
            or deadline_monotonic is not None
        )
    ):
        meme_selection_runtime = (select_and_reserve_meme, None, None, None)
    attachment_context, meme = prepare_meme_attachment(
        db_connect=_assistant_db_connect,
        settings=attachment_settings,
        policy=attachment_policy,
        message=raw_message,
        mode_decision=mode_decision,
        social_cues=social_cues,
        user_id=user_id,
        intent=intent,
        selection_runtime=meme_selection_runtime,
    )
    record_meme_funnel(attachment_context, scope="group" if context.get("group") else "private")
    provider = str(chat_settings.get("chat_provider") or "codex")
    group_reply_finalizer = None
    if context.get("group"):
        def group_reply_finalizer(reply_text, candidate_result):
            finalized_reply, action_truth_guarded = enforce_action_truth(
                reply_text,
                candidate_result.get("action_receipts")
                if isinstance(candidate_result.get("action_receipts"), list) else None,
            )
            if candidate_result.get("ok") and finalized_reply:
                finalized_reply = align_reply_with_attachment(finalized_reply, attachment_context)
            return finalized_reply, {"action_truth_guarded": action_truth_guarded}

    reply_kwargs = {
        "intent": intent,
        "criteria": criteria,
        "policy": policy,
        "mode_decision": mode_decision,
        "social_context": social_context,
        "attachment_context": attachment_context,
        "timeout": timeout,
        "build_messages": _assistant_chat_messages,
        "format_prompt": _format_assistant_prompt,
        "call_model": _call_openai_compatible_chat,
        "record_model": _record_model_call,
        "run_codex": _run_codex_assistant_chat,
        "cwd": _default_cwd(),
        "group_reply_finalizer": group_reply_finalizer,
    }
    if deadline_monotonic is not None:
        reply_kwargs.update({
            "deadline_monotonic": deadline_monotonic,
            "clock": deadline_clock,
        })
    result, cache_replay_metadata = run_conversation_model_reply(
        provider,
        chat_settings,
        user_id,
        display_message,
        memories,
        history,
        **reply_kwargs,
    )
    active_private_turn_retry = context.get("active_private_turn_retry")
    if callable(active_private_turn_retry) and not context.get("active_private_turn_retry_used"):
        try:
            retry = active_private_turn_retry()
        except (RuntimeError, ValueError, sqlite3.Error):
            retry = None
        if isinstance(retry, dict):
            terminal_result = retry.get("terminal_result")
            if isinstance(terminal_result, dict):
                stage_response_cycle_exchange({})
                return dict(terminal_result)
            if str(retry.get("message") or "").strip():
                retry_context = retry.get("decision_context")
                if isinstance(retry_context, dict):
                    retry_result = _assistant_chat(
                        user_id,
                        str(retry["message"]),
                        timeout=timeout,
                        decision_context=retry_context,
                    )
                    retry_inbound = retry_context.get("inbound_context")
                    if isinstance(retry_inbound, Mapping) and inbound_exchange is not None:
                        for key in (
                            "_response_cycle_plan_id",
                            "_response_cycle_assistant_metadata",
                            "_situation_context_message_ids",
                            "_situation_context_cycle_ids",
                            "_situation_context_selection",
                        ):
                            if key in retry_inbound:
                                inbound_exchange[key] = copy.deepcopy(retry_inbound[key])
                    return retry_result
    if (
        context.get("private_multimodal_truth_guard") is True
        and request_source == QQ_TASK_SOURCE
        and not context.get("group")
        and str(mode_decision.get("mode") or "daily") == "daily"
        and private_visual_claim_without_observation(
            result.get("reply") or result.get("output") or "",
            context.get("visual_observation"),
        )
    ):
        result = dict(result)
        result.update({
            "ok": False,
            "reply": "",
            "output": "",
            "error": "private_visual_claim_without_observation",
            "error_kind": "private_visual_claim_without_observation",
            "retryable": False,
            "visual_truth_guarded": True,
        })
    _record_model_call(chat_settings, result, source="assistant_chat", user_id=user_id)
    if research_truth_context:
        # Delivery-time truth checking receives only source URLs or the
        # unverified bit, never the group utterance, search query or excerpts.
        result["group_research"] = research_truth_context
    reply = (result.get("reply") or result.get("output") or "").strip()
    if not context.get("group") and str(mode_decision.get("mode") or "daily") != "work":
        expression_plan = social_context.get("expression_plan") if isinstance(social_context, dict) else None
        reply = normalize_social_reply(
            reply,
            group=False,
            request=raw_message,
        )
    if context.get("group"):
        action_truth_guarded = bool(result.get("action_truth_guarded"))
    else:
        reply, action_truth_guarded = enforce_action_truth(
            reply,
            result.get("action_receipts") if isinstance(result.get("action_receipts"), list) else None,
        )
    result["action_truth_guarded"] = action_truth_guarded
    if not context.get("group") and result.get("ok") and reply:
        reply = align_reply_with_attachment(reply, attachment_context)
    elif meme and (not result.get("ok") or not reply):
        mark_failed_attachment(
            db_connect=_assistant_db_connect,
            mark_delivery=mark_meme_delivery,
            meme=meme,
            error=result.get("error_kind") or result.get("error") or "model_reply_missing",
        )
        meme = None
    result["reply"] = reply
    result["output"] = reply
    quality = _quality_check_response(
        request=raw_message,
        response=reply,
        result=result,
        intent=intent,
        criteria=criteria,
        policy=policy,
        mode_decision=mode_decision,
    )
    if context.get("group"):
        quality.update({
            "group_style_gate": result.get("group_style_gate") or "provider_failed",
            "group_style_retry_attempted": bool(result.get("group_style_retry_attempted")),
            "group_style_initial_issues": list(result.get("group_style_initial_issues") or []),
            "group_style_final_issues": list(result.get("group_style_final_issues") or []),
            "social_action": str((mode_decision or {}).get("social_action") or "silent"),
        })
    elif not meme and request_source == QQ_TASK_SOURCE:
        private_inbound = context.get("inbound_context") or {}
        if str(quality.get("status") or "failed") == "passed" and result.get("ok"):
            expression_context, expression_meme = choose_meme_expression(
                db_connect=_assistant_db_connect,
                settings={**attachment_settings, **chat_settings},
                policy=attachment_policy,
                group_policy=None,
                scope="private", message=raw_message, reply=reply,
                mode=str(mode_decision.get("mode") or "daily"), intent=intent,
                user_id=user_id,
                session=str(private_inbound.get("session") or ""),
                persona_meme_policy=persona_meme_policy,
                visual_ready=(not private_inbound.get("attachments") or
                              str(private_inbound.get("visual_context_status") or "") == "ready"),
                deadline_monotonic=deadline_monotonic,
                call_openai=_call_openai_compatible_chat,
                run_codex=_run_codex_assistant_chat,
                default_cwd=_default_cwd(),
                record_model=_record_model_call,
            )
            expression_context["decision_id"] = str(private_inbound.get("_external_message_id") or "")
            record_meme_funnel(expression_context, scope="private")
            if expression_meme:
                attachment_context, meme = expression_context, expression_meme
                result["delivery_form"] = expression_context["delivery_form"]
    quality_event = None
    saved = []
    if result.get("ok") and reply:
        with ASSISTANT_LOCK:
            group_memory_context = context.get("group") if isinstance(context.get("group"), dict) else {}
            exchange_inbound_context = dict(context.get("inbound_context") or {})
            if group_memory_context:
                source_external_message_id = str(
                    group_memory_context.get("message_id")
                    or exchange_inbound_context.get("_external_message_id")
                    or ""
                ).strip()
                subject_actor_ref = str(group_memory_context.get("sender_id") or "").strip()
                if source_external_message_id:
                    exchange_inbound_context.setdefault(
                        "_external_message_id", source_external_message_id,
                    )
                if subject_actor_ref:
                    exchange_inbound_context.setdefault("sender_id", subject_actor_ref)
                exchange_inbound_context.setdefault(
                    "group_id", str(group_memory_context.get("group_id") or ""),
                )
            else:
                source_external_message_id = ""
                subject_actor_ref = ""
            if not defer_response_cycle_exchange:
                INTERACTION_STORE.record_exchange(
                    user_id,
                    display_message,
                    reply,
                    mode_decision,
                    source=request_source,
                    inbound_context=exchange_inbound_context,
                    exchange_metadata=cache_replay_metadata,
                )
            for fact in memory_candidates:
                saved.append(_add_memory(
                    user_id,
                    fact,
                    kind="fact",
                    source="auto",
                    score=7,
                    request_source=request_source,
                    project_id=project_id,
                    subject_actor_ref=subject_actor_ref,
                    source_external_message_id=source_external_message_id,
                ))
    if policy.get("quality_log_enabled"):
        quality_event = _record_quality_event(
            user_id=user_id,
            intent=intent,
            provider=str(result.get("provider") or provider),
            request=raw_message,
            response=reply or str(result.get("error") or ""),
            checks=quality,
            tool=str(result.get("tool") or ""),
            fallback=bool(quality.get("fallback")),
            duration=result.get("duration"),
        )
    attach_chat_result(
        result, reply, meme, attachment_context, intent, _intent_label(intent),
        mode_decision, mode_session,
        social_result(social_cues, social_context, runtime_role=runtime_role, group=context.get("group")),
        criteria, quality, quality_event, memories, saved, continuity_candidates,
        _assistant_settings(), project,
    )
    stage_response_cycle_exchange(cache_replay_metadata)
    # P1-2 relationship accumulation: deterministic, slow, instance-scoped.
    # Best-effort; never fail the chat result. Only for private chats.
    try:
        if not context.get("group"):
            _settings_for_accum = _assistant_settings()
            _mode = _relationship_accumulation_mode(_settings_for_accum)
            if _mode != "off":
                with _assistant_db_connect() as _acc_conn:
                    _ensure_relationship_accumulation_tables(_acc_conn)
                    _relationship_apply_accumulation(
                        _acc_conn,
                        user_id=user_id,
                        scope_type="private_user",
                        scope_id="",
                        now=None,
                        message_text=raw_message,
                        mode=_mode,
                    )
    except Exception:
        # accumulation must not break chat
        pass
    return result


def _command_env() -> dict[str, str]:
    return command_environment(os.environ, MIHOMO_PROXY_URL, MIHOMO_SOCKS_PROXY_URL)


def _direct_command_env() -> dict[str, str]:
    return direct_command_environment(os.environ)


def _project_marker_found(path: Path) -> bool:
    for marker in PROJECT_MARKERS:
        if (path / marker).exists():
            return True
    try:
        for item in path.iterdir():
            if item.is_file() and item.suffix.lower() in SOURCE_SUFFIXES:
                return True
    except OSError:
        return False
    return False


def _find_codegraph_root(cwd: Path) -> Path | None:
    resolved = cwd.resolve()
    for current in (resolved, *resolved.parents):
        if not any(_path_in_root(current, root) for root in _allowed_cwd_roots()):
            break
        if (current / ".codegraph").is_dir():
            return current
    return None


def _codegraph_candidate_root(cwd: Path) -> Path | None:
    existing = _find_codegraph_root(cwd)
    if existing:
        return existing
    resolved = cwd.resolve()
    if _project_marker_found(resolved):
        return resolved
    return None


def _run_codegraph(args: list[str], timeout: int | None = None) -> tuple[bool, int, str]:
    try:
        completed = subprocess.run(
            [CODEGRAPH_COMMAND, *args],
            text=True,
            cwd=str(DEFAULT_CWD),
            env=_command_env(),
            capture_output=True,
            timeout=timeout or CODEGRAPH_AUTO_TIMEOUT,
        )
        output = _trim_output((completed.stdout or "") + (completed.stderr or ""))
        return completed.returncode == 0, completed.returncode, output
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return False, 124, _trim_output(stdout + stderr + f"\nTimed out after {timeout or CODEGRAPH_AUTO_TIMEOUT}s")
    except Exception as exc:
        return False, 1, _trim_output(str(exc))


def _codegraph_status(cwd: Path | None = None) -> dict:
    started = time.monotonic()
    if not CODEGRAPH_AUTO_ENABLED:
        return {"enabled": False, "ok": True, "status": "disabled"}
    if not shutil.which(CODEGRAPH_COMMAND, path=_command_env().get("PATH", "")):
        return {"enabled": True, "ok": False, "status": "missing", "command": CODEGRAPH_COMMAND}

    cwd = (cwd or DEFAULT_CWD).resolve()
    root = _find_codegraph_root(cwd)
    if not root:
        candidate = _codegraph_candidate_root(cwd)
        return {
            "enabled": True,
            "ok": False,
            "status": "not_initialized" if candidate else "not_a_project",
            "cwd": str(cwd),
            "root": str(candidate or cwd),
        }

    ok, returncode, output = _run_codegraph(["status", "--json", str(root)], timeout=12)
    payload = {
        "enabled": True,
        "ok": ok,
        "status": "ready" if ok else "status_failed",
        "root": str(root),
        "returncode": returncode,
        "duration": round(time.monotonic() - started, 2),
    }
    if ok:
        try:
            data = json.loads(output)
            payload.update(
                {
                    "files": data.get("fileCount"),
                    "nodes": data.get("nodeCount"),
                    "edges": data.get("edgeCount"),
                    "pending": data.get("pendingChanges"),
                    "backend": data.get("backend"),
                },
            )
        except json.JSONDecodeError:
            payload["output"] = output
    else:
        payload["error"] = output
    return payload


def _ensure_codegraph(cwd: Path, *, phase: str, force: bool = False) -> dict:
    started = time.monotonic()
    if not CODEGRAPH_AUTO_ENABLED:
        return {"enabled": False, "ok": True, "status": "disabled", "phase": phase}
    if not shutil.which(CODEGRAPH_COMMAND, path=_command_env().get("PATH", "")):
        return {
            "enabled": True,
            "ok": False,
            "status": "missing",
            "phase": phase,
            "command": CODEGRAPH_COMMAND,
        }

    cwd = cwd.resolve()
    existing_root = _find_codegraph_root(cwd)
    root = existing_root or _codegraph_candidate_root(cwd)
    if not root:
        return {
            "enabled": True,
            "ok": True,
            "status": "skipped",
            "phase": phase,
            "reason": "no project markers",
            "cwd": str(cwd),
        }

    action = "sync" if existing_root else "init"
    cache_key = f"{root}:{action}"
    now = time.monotonic()
    with CODEGRAPH_LOCK:
        last_run = CODEGRAPH_LAST_RUN.get(cache_key, 0)
        if not force and action == "sync" and now - last_run < CODEGRAPH_AUTO_MIN_INTERVAL:
            return {
                "enabled": True,
                "ok": True,
                "status": "cached",
                "phase": phase,
                "action": action,
                "root": str(root),
            }

        ok, returncode, output = _run_codegraph([action, str(root)])
        if ok:
            CODEGRAPH_LAST_RUN[cache_key] = time.monotonic()

    return {
        "enabled": True,
        "ok": ok,
        "status": "ready" if ok else "failed",
        "phase": phase,
        "action": action,
        "root": str(root),
        "returncode": returncode,
        "duration": round(time.monotonic() - started, 2),
        "output": output[-2000:],
    }


def _trim_output(text: str) -> str:
    return _trim_output_impl(text, MAX_OUTPUT_CHARS)


def _codex_failure_diagnosis(returncode: int | None, output: str) -> tuple[str, str]:
    return _codex_failure_diagnosis_impl(returncode, output, trim_output_fn=_trim_output)


def _run_command(
    args: list[str],
    *,
    input_text: str | None,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            input=input_text,
            text=True,
            cwd=str(cwd),
            env=env or _command_env(),
            capture_output=True,
            timeout=timeout,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        output = stdout + stderr
        output = _trim_output(output)
        error_kind, error = _codex_failure_diagnosis(completed.returncode, output)
        if error:
            output = error
        result = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "duration": round(time.monotonic() - started, 2),
            "stdout": stdout[-MAX_OUTPUT_CHARS:],
            "stderr": stderr[-MAX_OUTPUT_CHARS:],
            "output": output,
        }
        if error:
            result["error_kind"] = error_kind
            result["error"] = error
        return result
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        output = stdout + stderr
        output = _trim_output(output)
        output = _trim_output(output + f"\nTimed out after {timeout}s")
        return {
            "ok": False,
            "returncode": 124,
            "duration": round(time.monotonic() - started, 2),
            "stdout": stdout[-MAX_OUTPUT_CHARS:],
            "stderr": stderr[-MAX_OUTPUT_CHARS:],
            "output": output,
            "error": output,
            "error_kind": "network",
        }
    except Exception as exc:
        output = _trim_output(str(exc))
        return {
            "ok": False,
            "returncode": 1,
            "duration": round(time.monotonic() - started, 2),
            "stdout": "",
            "stderr": output,
            "output": output,
            "error": output,
            "error_kind": "codex_failed",
        }


def _run_codex_for_task(task: dict) -> dict:
    """Run custom-proxy tasks under a shared lock for their whole lifetime.

    A Console activation takes the matching exclusive lock, so it cannot swap
    the singleton proxy while a task built from another executor snapshot is
    still running.
    """
    if str(task.get("executor_adapter") or "") in {"codex_custom_provider", "deepseek_proxy"}:
        with executor_runtime_shared_lock():
            return _run_codex_for_task_locked(task)
    return _run_codex_for_task_locked(task)


def _run_codex_for_task_locked(task: dict) -> dict:
    started = time.monotonic()
    cwd = Path(task["cwd"]).resolve()

    if str(task.get("network_mode") or "controlled") == "search":
        is_owner_task = (
            str(task.get("source") or "") == "admin"
            or str(task.get("user_id") or "") in qq_super_admin_ids(
                _assistant_db_connect,
            )
        )
        with _assistant_db_connect() as conn:
            search_still_allowed = (
                is_owner_task and task_web_search_allowed(conn)
            )
        if not search_still_allowed:
            return {
                "ok": False,
                "returncode": 1,
                "duration": round(time.monotonic() - started, 2),
                "status": "failed",
                "error_kind": "task_network_authorization_expired",
                "error": _user_error_message(
                    "task_network_authorization_expired",
                ),
                "codegraph": {},
            }

    codegraph = {"before": _ensure_codegraph(cwd, phase="before")}
    with TASK_LOCK:
        task["codegraph"] = codegraph

    # 从任务快照读取执行器，不再实时查 DB
    adapter = task.get("executor_adapter") or ""
    if not adapter:
        return {
            "ok": False, "returncode": 1, "duration": 0,
            "status": "failed", "error_kind": "executor_adapter_missing",
            "error": _user_error_message("executor_adapter_missing"),
            "codegraph": codegraph,
        }

    executor_profile = None
    if adapter in {"codex_custom_provider", "deepseek_proxy"}:
        if not shutil.which("bwrap"):
            return {
                "ok": False, "returncode": 1, "duration": 0,
                "status": "failed", "error_kind": "executor_sandbox_unavailable",
                "error": _user_error_message("executor_sandbox_unavailable"),
                "codegraph": codegraph,
            }
        with _assistant_db_connect() as conn:
            executor_profile = get_executor_profile(
                conn,
                str(task.get("executor_provider_id") or ""),
            )
        if not executor_profile or not int(executor_profile.get("enabled") or 0):
            return {
                "ok": False, "returncode": 1, "duration": 0,
                "status": "failed", "error_kind": "executor_profile_missing",
                "error": _user_error_message("executor_profile_missing"),
                "codegraph": codegraph,
            }
        expected_version = str(task.get("executor_config_version") or "")
        current_version = str(executor_profile.get("config_version") or "")
        if adapter == "codex_custom_provider" and expected_version != current_version:
            return {
                "ok": False, "returncode": 1, "duration": 0,
                "status": "failed", "error_kind": "executor_profile_changed",
                "error": _user_error_message("executor_profile_changed"),
                "codegraph": codegraph,
            }
        current_hash = profile_sha256(str(executor_profile.get("profile_name") or ""))
        snapshot_hash = str(task.get("executor_profile_sha256") or "")
        if not current_hash or not snapshot_hash:
            return {
                "ok": False, "returncode": 1, "duration": 0,
                "status": "failed", "error_kind": "executor_profile_missing",
                "error": _user_error_message("executor_profile_missing"),
                "codegraph": codegraph,
            }
        if current_hash != snapshot_hash:
            return {
                "ok": False, "returncode": 1, "duration": 0,
                "status": "failed", "error_kind": "executor_profile_changed",
                "error": _user_error_message("executor_profile_changed"),
                "codegraph": codegraph,
            }
        if not executor_runtime_identity_matches(
            str(task.get("executor_provider_id") or ""),
            expected_version,
            str(task.get("executor_model_name") or ""),
        ):
            return {
                "ok": False, "returncode": 1, "duration": 0,
                "status": "failed", "error_kind": "executor_runtime_identity_mismatch",
                "error": _user_error_message("executor_runtime_identity_mismatch"),
                "codegraph": codegraph,
            }
        # cwd 二次检查（防备任务创建后目录被替换）
        try:
            _validate_executor_sandbox_and_cwd(task["sandbox"], adapter, cwd)
        except ValueError as exc:
            return {
                "ok": False, "returncode": 1, "duration": 0,
                "status": "failed", "error_kind": str(exc).split(":")[0],
                "error": str(exc),
                "codegraph": codegraph,
            }

    try:
        env = _codex_exec_env(adapter, executor_profile)
        args = _codex_exec_args(task, executor_profile)
    except RuntimeError as exc:
        error_kind = str(exc).split(":", 1)[0]
        return {
            "ok": False, "returncode": 1, "duration": 0,
            "status": "failed", "error_kind": error_kind,
            "error": _user_error_message(error_kind),
            "codegraph": codegraph,
        }

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=task["cwd"],
        env=env,
        start_new_session=True,
    )
    with TASK_LOCK:
        task["process"] = proc
    try:
        stdout, stderr = proc.communicate(input=task["prompt"], timeout=task["timeout"])
        stdout = stdout or ""
        stderr = stderr or ""

        if adapter in {"codex_custom_provider", "deepseek_proxy"}:
            parsed = _parse_codex_jsonl(stdout)
            result = _finalize_codex_result(proc, parsed, started)
        else:
            combined_output = _trim_output(stdout + stderr)
            clean_stdout = _trim_output(stdout)
            error_kind, error = _codex_failure_diagnosis(proc.returncode, combined_output)
            output = clean_stdout if proc.returncode == 0 and clean_stdout else combined_output
            if error:
                output = error
            result = {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "duration": round(time.monotonic() - started, 2),
                "stdout": stdout[-MAX_OUTPUT_CHARS:],
                "stderr": stderr[-MAX_OUTPUT_CHARS:],
                "output": output,
                "status": "done" if proc.returncode == 0 else "failed",
            }
            if error:
                result["error_kind"] = error_kind
                result["error"] = error

        if task["sandbox"] == "workspace-write":
            codegraph["after"] = _ensure_codegraph(cwd, phase="after", force=True)
        result["codegraph"] = codegraph
        return result
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        stdout, stderr = proc.communicate()
        stdout = stdout or (exc.stdout if isinstance(exc.stdout, str) else "")
        stderr = stderr or (exc.stderr if isinstance(exc.stderr, str) else "")

        if adapter in {"codex_custom_provider", "deepseek_proxy"}:
            result = _finalize_codex_result(None, {"final_status": "unknown"}, started, timeout_expired=True)
        else:
            output = stdout + stderr
            output = _trim_output(output)
            output = _trim_output(output + f"\nTimed out after {task['timeout']}s")
            result = {
                "ok": False, "returncode": 124,
                "duration": round(time.monotonic() - started, 2),
                "stdout": stdout[-MAX_OUTPUT_CHARS:],
                "stderr": stderr[-MAX_OUTPUT_CHARS:],
                "output": output, "error": output,
                "error_kind": "network", "status": "timeout",
            }

        if task["sandbox"] == "workspace-write":
            codegraph["after"] = _ensure_codegraph(cwd, phase="after", force=True)
        result["codegraph"] = codegraph
        return result
    finally:
        with TASK_LOCK:
            task.pop("process", None)


def _public_task(task: dict, include_output: bool = False) -> dict:
    fields = {
        "id",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "sandbox",
        "cwd",
        "summary",
        "duration",
        "returncode",
        "ok",
        "cancel_requested",
        "error_kind",
        "source_task_id",
        "source",
        "user_id",
        "trace_id",
        "origin_message",
        "intent",
        "mode",
        "delivery_status",
        "delivery_error",
        "delivered_at",
        "delivery_attempts",
        "delivery_next_at",
        "pending_messages",
        "delivery_recipient_id",
        "delivery_session",
        "codegraph",
        "goal_id",
        "run_id",
        "capability_id",
        "strategy",
        "executor_provider_id",
        "executor_model_id",
        "executor_model_name",
        "executor_adapter",
        "executor_config_version",
        "executor_profile_sha256",
        "artifact",
        "artifact_revision_id",
        "artifact_revision_base_version_id",
        "network_mode",
    }
    payload = {key: task.get(key) for key in fields if key in task}
    if str(task.get("source") or "") == QQ_TASK_SOURCE:
        artifact = payload.get("artifact")
        if isinstance(artifact, dict) and isinstance(artifact.get("delivery_access"), dict):
            access = artifact["delivery_access"]
            payload["artifact"] = {
                "delivery_available": True,
                "expires_at": str(access.get("expires_at") or ""),
            }
        payload.pop("artifact_revision_id", None)
        payload.pop("artifact_revision_base_version_id", None)
    try:
        pending = json.loads(str(task.get("pending_messages") or "[]"))
        payload["pending_message_count"] = len(pending) if isinstance(pending, list) else 0
    except json.JSONDecodeError:
        payload["pending_message_count"] = 0
    if include_output:
        for key in ("stdout", "stderr", "output", "error", "error_kind"):
            if key in task:
                payload[key] = task.get(key)
    return payload


def _task_db_payload(task: dict) -> dict:
    return task_db_payload(task, TASK_DB_COLUMNS, updated_at=_utc_now())


def _row_to_task(row: sqlite3.Row) -> dict:
    task = {key: row[key] for key in row.keys()}
    for key in ("ok", "cancel_requested"):
        if task.get(key) is not None:
            task[key] = bool(task[key])
    for key in ("timeout", "returncode"):
        if task.get(key) is not None:
            task[key] = int(task[key])
    if task.get("duration") is not None:
        task["duration"] = float(task["duration"])
    task.pop("updated_at", None)
    return {key: value for key, value in task.items() if value is not None}


def _upsert_task_row(conn: sqlite3.Connection, task: dict) -> None:
    payload = _task_db_payload(task)
    columns = list(TASK_DB_COLUMNS)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "id")
    conn.execute(
        f"""
        INSERT INTO tasks ({", ".join(columns)}) VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET {updates}
        """,
        [payload.get(column) for column in columns],
    )


def _save_task_db(task: dict) -> None:
    try:
        _init_task_db()
        with _db_connect() as conn:
            _upsert_task_row(conn, task)
        os.chmod(TASK_DB_PATH, 0o600)
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("task_persistence_failed") from exc
    try:
        _sync_and_enqueue_phase2_task(task)
        if task.get("status") in FINAL_STATUSES:
            consume_running_supplements(
                task, pending_messages=_pending_messages, create_task=_create_task,
                safe_cwd=_safe_cwd, save_task=_save_task_db,
            )
    finally:
        if task.get("status") in FINAL_STATUSES:
            _stop_task_execution_progress(str(task.get("id") or ""))


def _load_tasks_from_db(limit: int = MAX_TASKS) -> list[dict]:
    try:
        _init_task_db()
        return load_active_and_recent(_db_connect, _row_to_task, recent_limit=limit)
    except (OSError, sqlite3.Error):
        return []


def _task_stats() -> dict:
    counts = {status: 0 for status in sorted(TASK_STATUSES)}
    try:
        _init_task_db()
        with _db_connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status",
            ).fetchall()
        for row in rows:
            status = row["status"] or "unknown"
            counts[status] = int(row["count"])
    except (OSError, sqlite3.Error):
        with TASK_LOCK:
            for task in TASKS.values():
                status = task.get("status") or "unknown"
                counts[status] = counts.get(status, 0) + 1
    total = sum(counts.values())
    active = counts.get("queued", 0) + counts.get("running", 0)
    return {"ok": True, "total": total, "active": active, "counts": counts}


def _read_jsonl_history() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    try:
        with TASK_HISTORY_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                task_id = str(item.get("id", "")).strip()
                if not task_id:
                    continue
                latest[task_id] = item
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    return latest


def _append_history(task: dict) -> None:
    _save_task_db(task)
    try:
        TASK_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = _public_task(task, include_output=True)
        for key in ("prompt", "timeout"):
            if key in task:
                payload[key] = task.get(key)
        with TASK_HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        os.chmod(TASK_HISTORY_PATH, 0o600)
    except OSError:
        pass


def _trim_tasks() -> None:
    with TASK_LOCK:
        if len(TASKS) <= MAX_TASKS:
            return
        removable = [
            item
            for item in TASKS.values()
            if item.get("status") in FINAL_STATUSES
        ]
        removable.sort(key=lambda item: item.get("created_at", ""))
        for item in removable[: max(0, len(TASKS) - MAX_TASKS)]:
            TASKS.pop(item["id"], None)


def _load_history() -> None:
    _init_task_db()
    db_items = _load_tasks_from_db(MAX_TASKS)
    if db_items:
        items = db_items
    else:
        latest = _read_jsonl_history()
        if not latest:
            return
        items = sorted(latest.values(), key=lambda item: item.get("created_at", ""))
        for item in items:
            _save_task_db(item)

    recovered_queue = []
    with TASK_LOCK:
        for item in items:
            task = dict(item)
            task.setdefault("status", "done")
            task.setdefault("cancel_requested", False)
            task.setdefault("cwd", str(DEFAULT_CWD))
            if task.get("status") == "running":
                task.update(
                    {
                        "status": "failed",
                        "finished_at": _utc_now(),
                        "ok": False,
                        "returncode": 75,
                        "error_kind": "service_restart",
                        "error": "Bridge restarted while this task was running. Review partial changes before retrying.",
                        "output": "服务重启时任务仍在运行，已停止自动续跑。请检查可能产生的部分修改后再重试。",
                    },
                )
                _append_history(task)
                _save_task_db(task)
            TASKS[str(task["id"])] = task
            if task.get("status") == "queued":
                recovered_queue.append(str(task["id"]))
        _trim_tasks()
        TASK_QUEUE.extend(task_id for task_id in recovered_queue if task_id not in TASK_QUEUE)
        if TASK_QUEUE:
            TASK_EVENT.set()


def _task_worker() -> None:
    while True:
        TASK_EVENT.wait()
        while True:
            with TASK_LOCK:
                if not TASK_QUEUE:
                    TASK_EVENT.clear()
                    break
                task_id = TASK_QUEUE.popleft()
                task = TASKS.get(task_id)
            if not task:
                continue
            if task.get("cancel_requested"):
                with TASK_LOCK:
                    task.update(
                        {
                            "status": "cancelled",
                            "finished_at": _utc_now(),
                            "ok": False,
                            "returncode": 130,
                            "duration": 0,
                            "output": "Task cancelled before start.",
                        },
                    )
                    _append_history(task)
                    _save_task_db(task)
                continue
            with TASK_LOCK:
                task["status"] = "running"
                task.setdefault("started_at", _utc_now())
                _save_task_db(task)
            try:
                _record_task_execution_milestone(task_id, "work_started")
            except (sqlite3.Error, RuntimeError, ValueError):
                # Presence is best effort and cannot change Task execution.
                pass
            try:
                result = _run_codex_for_task(task)
                if result.get("ok"):
                    _restore_task_delivery_projection(task)
                    artifact = ARTIFACT_RUNTIME.capture(task)
                    if artifact:
                        result["artifact"] = artifact
                with TASK_LOCK:
                    task.update(result)
                    task["finished_at"] = _utc_now()
                    if task.get("cancel_requested") and task.get("returncode") not in (0, None):
                        task["status"] = "cancelled"
                        task["output"] = (task.get("output") or "") + "\nTask cancelled."
                    _append_history(task)
                    _trim_tasks()
                    _save_task_db(task)
            except Exception as exc:
                with TASK_LOCK:
                    task.update(
                        {
                            "status": "failed",
                            "finished_at": _utc_now(),
                            "ok": False,
                            "returncode": 1,
                            "error": str(exc),
                            "output": str(exc),
                        },
                    )
                    _append_history(task)
                    _save_task_db(task)


def _build_task(
    prompt: str,
    sandbox: str,
    timeout: int,
    cwd: Path,
    *,
    source: str = "admin",
    user_id: str = "",
    trace_id: str = "",
    origin_message: str = "",
    intent: str = "",
    mode: str = "",
    delivery_status: str | None = None,
    pending_messages: str = "",
    delivery_recipient_id: str = "",
    delivery_session: str = "",
    source_task_id: str = "",
    request_idempotency_key: str = "",
    automation_run_id: str = "",
    follow_up_source_task_id: str = "",
    artifact_revision_id: str = "",
    artifact_revision_base_version_id: str = "",
    network_mode: str = "controlled",
    delivery_projection: dict | None = None,
    status: str = "queued",
) -> dict:
    task_id = uuid.uuid4().hex[:8]
    delivery = delivery_status
    if delivery is None:
        delivery = TASK_DELIVERY_PENDING if source == QQ_TASK_SOURCE and user_id else TASK_DELIVERY_NONE

    # 执行器快照 — 创建任务时一次性确定，Worker 仅读快照
    snapshot = _resolve_executor_snapshot()
    _validate_executor_sandbox_and_cwd(sandbox, snapshot["adapter"], cwd)
    if network_mode not in {"controlled", "search"}:
        raise ValueError("invalid_task_network_mode")
    if network_mode == "search" and snapshot["adapter"] != "codex_login":
        raise RuntimeError("executor_web_search_unsupported")
    summary = _task_summary(origin_message or prompt)
    created_at = _utc_now()
    prompt = ARTIFACT_RUNTIME.decorate_prompt(
        prompt, sandbox, task_id=task_id, created_at=created_at,
    )

    task = {
        "id": task_id,
        "status": status,
        "created_at": created_at,
        "sandbox": sandbox,
        "cwd": str(cwd),
        "prompt": prompt,
        "summary": summary,
        "timeout": timeout,
        "cancel_requested": False,
        "source": source,
        "user_id": user_id,
        "trace_id": trace_id,
        "origin_message": origin_message,
        "intent": intent,
        "mode": mode,
        "delivery_status": delivery,
        "delivery_error": "",
        "delivery_attempts": 0,
        "delivery_next_at": "",
        "pending_messages": pending_messages or "[]",
        "delivery_recipient_id": delivery_recipient_id,
        "delivery_session": delivery_session,
        "source_task_id": source_task_id,
        "request_idempotency_key": request_idempotency_key,
        "automation_run_id": automation_run_id,
        "follow_up_source_task_id": follow_up_source_task_id,
        "artifact_revision_id": artifact_revision_id,
        "artifact_revision_base_version_id": artifact_revision_base_version_id,
        "network_mode": network_mode,
        "executor_provider_id": snapshot["provider_id"],
        "executor_model_id": snapshot["model_id"],
        "executor_model_name": snapshot["model_name"],
        "executor_adapter": snapshot["adapter"],
        "executor_config_version": snapshot["config_version"],
        "executor_profile_sha256": snapshot["profile_sha256"],
    }
    if isinstance(delivery_projection, dict) and delivery_projection:
        task["_delivery_projection"] = dict(delivery_projection)
    return task


def _create_task(
    prompt: str,
    sandbox: str,
    timeout: int,
    cwd: Path,
    initial_evidence: list[dict] | None = None,
    execution_presence_callback=None,
    interaction_plan: Mapping[str, object] | None = None,
    **metadata,
) -> dict:
    task = _build_task(prompt, sandbox, timeout, cwd, **metadata)
    if initial_evidence is not None:
        task["evidence"] = list(initial_evidence)
    with TASK_LOCK:
        idempotency_key = str(task.get("request_idempotency_key") or "")
        if idempotency_key:
            with _db_connect() as conn:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE request_idempotency_key=?", (idempotency_key,),
                ).fetchone()
            if row:
                existing = _row_to_task(row)
                return {**_public_task(existing), "position": 0, "idempotent_replay": True}
        _admit_current_response_cycle_effect("task_create")
        _save_task_db(task)
        TASKS[task["id"]] = task
        TASK_QUEUE.append(task["id"])
        position = len(TASK_QUEUE)
        execution_presence_attempted = callable(execution_presence_callback)
        execution_presence_queued = False
        if callable(execution_presence_callback):
            try:
                execution_presence_queued = bool(execution_presence_callback(
                    str(task["id"]),
                    dict(interaction_plan or {}),
                ))
            except Exception:
                # The Task is already durable and queued. Opening delivery is
                # best effort and cannot change its execution outcome.
                pass
        TASK_EVENT.set()
    payload = _public_task(task)
    payload["position"] = position
    if execution_presence_attempted:
        payload["_execution_presence_attempted"] = True
        payload["_execution_presence_queued"] = execution_presence_queued
    return payload


def _delegation_request_idempotency_key(
    *,
    source: str,
    user_id: str,
    delivery_session: str,
    inbound_context: dict,
    trace_id: str,
) -> str:
    """Bind one delegated execution to the existing stable inbound identity."""

    source_identity = str(
        inbound_context.get("_external_message_id") or trace_id or ""
    ).strip()
    if not source_identity:
        return ""
    scope = str(delivery_session or user_id or "").strip()
    digest = hashlib.sha256(
        f"{source}\0{scope}\0{source_identity}".encode("utf-8"),
    ).hexdigest()
    return f"delegated-execution:{digest}"


def _existing_execution(request_idempotency_key: str) -> dict | None:
    key = str(request_idempotency_key or "").strip()
    if not key:
        return None
    _init_task_db()
    with _db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE request_idempotency_key=?", (key,),
        ).fetchone()
        if row is None:
            return None
        task = _row_to_task(row)
        identity = conn.execute(
            "SELECT id,goal_id FROM runs WHERE legacy_task_id=?", (task["id"],),
        ).fetchone()
    if identity is None:
        raise RuntimeError("delegated_execution_identity_incomplete")
    task["run_id"] = str(identity["id"])
    task["goal_id"] = str(identity["goal_id"])
    with TASK_LOCK:
        TASKS.setdefault(str(task["id"]), task)
    payload = _public_task(task)
    payload["position"] = 0
    payload["idempotent_replay"] = True
    return payload


def _establish_execution(
    prompt: str,
    sandbox: str,
    timeout: int,
    cwd: Path,
    **metadata,
) -> dict:
    """Atomically persist one running Task/Run/Goal without scheduling Work.

    The running state covers synchronous Research.  Work is added to the
    existing in-memory queue only after grounded Research has updated this
    same Task.  A restart already closes persisted running Tasks as failed,
    so no ungrounded placeholder can be resumed as Work.
    """

    key = str(metadata.get("request_idempotency_key") or "").strip()
    existing = _existing_execution(key)
    if existing is not None:
        return existing
    _admit_current_response_cycle_effect("task_establish")

    task = _build_task(
        prompt, sandbox, timeout, cwd, status="running", **metadata,
    )
    task["started_at"] = str(task.get("created_at") or _utc_now())

    _init_task_db()
    with _db_connect() as conn:
        ensure_platform_schema(conn)
        from bridge_migrations import ensure_agent_platform_migrations
        ensure_agent_platform_migrations(conn)

    task_lookup = _phase2_task_lookup()
    with TASK_LOCK:
        with _db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if key:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE request_idempotency_key=?", (key,),
                ).fetchone()
                if row is not None:
                    replay = _row_to_task(row)
                    identity = conn.execute(
                        "SELECT id,goal_id FROM runs WHERE legacy_task_id=?",
                        (replay["id"],),
                    ).fetchone()
                    if identity is None:
                        raise RuntimeError("delegated_execution_identity_incomplete")
                    replay["run_id"] = str(identity["id"])
                    replay["goal_id"] = str(identity["goal_id"])
                    TASKS.setdefault(str(replay["id"]), replay)
                    payload = _public_task(replay)
                    payload["position"] = 0
                    payload["idempotent_replay"] = True
                    return payload
            _upsert_task_row(conn, task)
            projected = PlatformRepository(conn).sync_task(
                task,
                task_lookup={**task_lookup, str(task["id"]): task},
            )
            projection = projected.get("projection") or {}
            task["goal_id"] = str(projection.get("goal_id") or "")
            task["run_id"] = str(projection.get("run_id") or "")
            ensure_task_revision_binding(conn, task)
        if not task.get("goal_id") or not task.get("run_id"):
            raise RuntimeError("delegated_execution_identity_incomplete")
        TASKS[str(task["id"])] = task
        _trim_tasks()
    os.chmod(TASK_DB_PATH, 0o600)
    payload = _public_task(task)
    payload["position"] = 0
    payload["execution_established"] = True
    return payload


def _activate_established_execution(
    task_id: str,
    *,
    prompt: str,
    delivery_projection: dict | None,
    evidence: list[dict] | None,
    execution_presence_callback=None,
    interaction_plan: Mapping[str, object] | None = None,
    research_verified_callback=None,
) -> dict:
    """Attach grounded Research to the established Task and schedule its Work."""

    _admit_current_response_cycle_effect("task_activate")
    with TASK_LOCK:
        task = TASKS.get(str(task_id))
        if task is None:
            raise RuntimeError("delegated_execution_not_found")
        task["prompt"] = ARTIFACT_RUNTIME.decorate_prompt(
            prompt,
            str(task.get("sandbox") or "read-only"),
            task_id=str(task["id"]),
            created_at=str(task.get("created_at") or _utc_now()),
        )
        task["evidence"] = list(evidence or [])
        if isinstance(delivery_projection, dict) and delivery_projection:
            task["_delivery_projection"] = dict(delivery_projection)
        _save_task_db(task)
        if str(task["id"]) not in TASK_QUEUE:
            TASK_QUEUE.append(str(task["id"]))
        position = list(TASK_QUEUE).index(str(task["id"])) + 1
        execution_presence_attempted = callable(execution_presence_callback)
        execution_presence_queued = False
        if callable(execution_presence_callback):
            try:
                # The Task is durable and queued, but the worker cannot race
                # progress or terminal delivery until the opening is reserved.
                execution_presence_queued = bool(execution_presence_callback(
                    str(task["id"]),
                    dict(interaction_plan or {}),
                ))
            except Exception:
                # Presence is best effort and must never change Task outcome.
                pass
        if callable(research_verified_callback):
            try:
                # Reserve the truthful milestone before the worker can pop the
                # queued Task and race its terminal delivery ahead of progress.
                research_verified_callback("research_verified")
            except (sqlite3.Error, RuntimeError, ValueError):
                # Presence is best effort and must never change Task outcome.
                pass
        TASK_EVENT.set()
        payload = _public_task(task)
        payload["position"] = position
        payload["_execution_presence_attempted"] = execution_presence_attempted
        payload["_execution_presence_queued"] = execution_presence_queued
        return payload


def _fail_established_execution(
    task_id: str,
    *,
    error_kind: str,
    user_message: str,
) -> dict:
    """Close Research failure on the same canonical Task/Run/Goal."""

    with TASK_LOCK:
        task = TASKS.get(str(task_id))
        if task is None:
            raise RuntimeError("delegated_execution_not_found")
        task.update({
            "status": "failed",
            "finished_at": _utc_now(),
            "ok": False,
            "returncode": 1,
            "error_kind": str(error_kind or "research_execution_failed")[:120],
            "error": str(user_message or "")[:2000],
            "output": str(user_message or "")[:2000],
        })
        try:
            TASK_QUEUE.remove(str(task["id"]))
        except ValueError:
            pass
        _append_history(task)
        _trim_tasks()
        _save_task_db(task)
        return _public_task(task, include_output=True)


def _complete_established_research(
    task_id: str,
    *,
    research_result,
    reply: str,
    duration: float,
) -> dict:
    """Close a synchronous Research result on its pre-established lifecycle."""

    with TASK_LOCK:
        task = TASKS.get(str(task_id))
        if task is None:
            raise RuntimeError("delegated_execution_not_found")
        task.update({
            "status": "done",
            "finished_at": _utc_now(),
            "duration": round(max(0.0, duration), 3),
            "returncode": 0,
            "ok": True,
            "stdout": str(reply or ""),
            "stderr": "",
            "output": str(reply or ""),
            "error": "",
            "capability_id": RESEARCH_CAPABILITY_ID,
            "strategy": "grounded",
            "evidence": (
                list(research_result.evidence)
                if hasattr(research_result, "evidence")
                else []
            ),
        })
        try:
            TASK_QUEUE.remove(str(task["id"]))
        except ValueError:
            pass
        _append_history(task)
        _trim_tasks()
        _save_task_db(task)
        return _public_task(task, include_output=True)


def _create_approval_task(prompt: str, sandbox: str, timeout: int, cwd: Path, **metadata) -> dict:
    _admit_current_response_cycle_effect("approval_create")
    task = _build_task(prompt, sandbox, timeout, cwd, status="waiting_approval", **metadata)
    persisted = create_paused_task_approval(
        _assistant_db_connect,
        _db_connect,
        task,
        upsert_task=_upsert_task_row,
        task_lookup=_phase2_task_lookup,
        requested_channel=str(task.get("source") or "unknown"),
        requested_by=str(task.get("user_id") or "admin"),
        target_environment=os.environ.get("AGENT_ENVIRONMENT", "server"),
        action_summary=str(task.get("summary") or "待确认任务"),
    )
    _restore_task_delivery_projection(task)
    with TASK_LOCK:
        TASKS[task["id"]] = task
        _trim_tasks()
    return {"task": _public_task(task), "approval": persisted["approval"]}


def _generate_proactive_decision(policy: dict) -> dict:
    user_id = str(policy.get("user_id") or "default")
    is_group = (
        str(policy.get("policy_kind") or "") == "group_social"
        and user_id.startswith("group:")
        and bool(user_id[6:])
    )
    from bridge_social_opportunity import social_opportunity_enabled
    from bridge_social_start import (
        finalize_start_decision,
        finalize_start_failure,
        owner_social_start_preflight,
        prepare_start_opportunity,
    )

    # Authorization and Assistant identity are cheaper and more authoritative
    # than settings, private history, memories or a model call.  They therefore
    # gate the complete proactive path, not merely the eventual send.
    with _assistant_db_connect() as conn:
        preflight = owner_social_start_preflight(conn, policy)
        if not preflight["allowed"]:
            return {
                "action": "skip", "intent": "silence", "reason": preflight["reason"],
                "message": "", "topic_key": "", "next_check_minutes": 60,
            }
        if not social_opportunity_enabled(conn):
            return {
                "action": "skip", "intent": "silence", "reason": "social_opportunity_disabled",
                "message": "", "topic_key": "", "next_check_minutes": 60,
            }

    if is_group:
        group_id = user_id[6:]
        with _assistant_db_connect() as conn:
            group_items = group_context(conn, group_id, 12)
        history = [
            {
                "id": item.get("id"),
                "role": "assistant" if str(item.get("sender_id") or "") == "bot" else "user",
                "content": str(item.get("content") or ""),
                "created_at": str(item.get("created_at") or ""),
            }
            for item in group_items
        ]
        memories = []
    else:
        history = _conversation_history(user_id, 12)
        memories = _list_memories(user_id=user_id, limit=6, purpose="proactive")
    with _assistant_db_connect() as conn:
        social_prepared = prepare_start_opportunity(
            conn, policy, history=history, memories=memories,
        )
        if not social_prepared["candidates"]:
            return finalize_start_decision(
                conn,
                social_prepared,
                {"action": "skip", "reason": "no_admissible_situation", "confidence": 1.0},
            )

    settings = _assistant_settings(include_secrets=True)
    # Downstream expression and model inputs consume the exact body window
    # already admitted by the Situation builder, not a second raw-history path.
    history_lines = [
        {
            "id": str(item.get("id") or item.get("created_at") or ""),
            "role": "assistant" if item.get("role") == "assistant" else "user",
            "content": str(item.get("content") or "")[-800:],
            "created_at": str(item.get("created_at") or ""),
        }
        for item in social_prepared["situation"].get("recent_conversation") or []
        if str(item.get("content") or "").strip()
    ]
    voice_contract = build_voice_contract(settings)
    affect = dict(social_prepared["situation"].get("assistant_affect") or {})
    from bridge_assistant_affect_runtime import apply_assistant_affect_to_expression_plan

    last_user_text = next(
        (str(item.get("content") or "") for item in reversed(history_lines) if item.get("role") == "user"),
        "",
    )
    expression_plan = plan_expression(
        last_user_text,
        social_cues={},
        mode_decision={"mode": "daily"},
        voice_contract=voice_contract,
    )
    expression_plan = apply_assistant_affect_to_expression_plan(expression_plan, affect)
    context = {
        "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "relationship": social_prepared["relationship"],
        "voice_contract": voice_contract,
        "voice_contract_instructions": voice_contract_lines(voice_contract),
        "expression_plan": expression_plan,
        "expression_plan_instructions": expression_plan_lines(expression_plan),
        "initiative_mode": policy.get("initiative_mode") or "balanced",
        "allowed_intents": [item for item in str(policy.get("allowed_intents") or "").split(",") if item],
        "consecutive_unanswered": int(policy.get("consecutive_unanswered") or 0),
        "recent_conversation": history_lines,
        "social_opportunity": social_prepared["opportunity"],
        "topic_candidates": social_prepared["candidates"],
        "interaction_spine": social_prepared["situation"],
        "grounding_rules": {
            "topic_must_be_agent_derived": True,
            "candidate_is_evidence_not_topic": True,
            "continuation_must_retain_concrete_evidence_phrase": True,
            "independent_private_topic_must_be_hypothetical_question_without_claims": True,
            "every_claimed_action_must_be_observed": True,
            "emotion_must_reference_observed_detail": True,
            "affect_styles_expression_only": True,
            "fact_over_persona": True,
        },
    }
    system = proactive_system_prompt()
    model_settings = _settings_for_model_role("conversation_reply", settings)
    provider = str(model_settings.get("chat_provider") or "codex")
    if provider == "openai-compatible":
        model_settings = dict(model_settings)
        model_settings["chat_temperature"] = "0.6"
        model_settings["chat_max_tokens"] = str(
            STRUCTURED_SOCIAL_DECISION_MAX_TOKENS,
        )
        result = call_openai_with_empty_retry(
            model_settings,
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            timeout=60,
            user_id=user_id,
            call_model=_call_openai_compatible_chat,
            record_model=_record_model_call,
            empty_source="proactive_decision_empty_initial",
            retry_instruction="输出协议：必须输出非空 JSON 决策；不要只生成思考过程。",
        )
    else:
        result = _run_codex_assistant_chat(
            f"{system}\n\ncontext:\n{json.dumps(context, ensure_ascii=False)}",
            cwd=_default_cwd(),
            timeout=120,
            settings_override=model_settings,
        )
    _record_model_call(model_settings, result, source="proactive_decision", user_id=user_id)
    if not result.get("ok"):
        with _assistant_db_connect() as conn:
            finalize_start_failure(
                conn,
                social_prepared,
                str(result.get("error_kind") or result.get("error") or "model_failed"),
            )
        raise RuntimeError(str(result.get("error") or result.get("error_kind") or "proactive_model_failed"))
    value = _sanitize_proactive_decision(
        _parse_proactive_json(result.get("reply") or result.get("output") or ""),
    )
    with _assistant_db_connect() as conn:
        fresh = owner_social_start_preflight(conn, policy)
        if not fresh["allowed"]:
            value = {**value, "action": "skip", "reason": fresh["reason"]}
        try:
            return finalize_start_decision(conn, social_prepared, value)
        except (KeyError, StopIteration, ValueError):
            return finalize_start_decision(
                conn,
                social_prepared,
                {**value, "action": "skip", "reason": "invalid_model_social_contract"},
            )


def _automation_execution_preflight(action: dict) -> dict:
    return _automation_preflight(_resolve_executor_snapshot, executor_workspace_root, _validate_executor_sandbox_and_cwd)


def _automation_github_purpose_summaries(job: dict, items: list[dict]) -> dict[str, str]:
    settings = _assistant_settings(include_secrets=True)
    model_settings = _settings_for_model_role("conversation_reply", settings)
    return github_purpose_summaries(
        job, items, settings=settings, model_settings=model_settings,
        call_openai_retry=call_openai_with_empty_retry, call_openai=_call_openai_compatible_chat,
        run_codex=_run_codex_assistant_chat, record_model=_record_model_call, default_cwd=_default_cwd(),
    )


def _resolve_automation_conversation_target(actor_id: str, inbound_context: dict) -> dict:
    return resolve_automation_target(
        actor_id, inbound_context, outbox=_phase2_outbox(), assistant_connect=_assistant_db_connect,
    )


def _notify_automation_failure(job: dict, error: object) -> None:
    _notify_automation_failure_impl(_phase2_outbox().enqueue, job, error)


def _run_automation_job(job: dict) -> dict:
    if str(job.get("action_type") or "") == "reminder":
        payload = {
            "kind": "automation_reminder",
            "automation_job_id": job["id"],
            "automation_run_id": job["run_id"],
            "user_id": job["user_id"],
            "content": str(job.get("instruction") or "").strip(),
        }
        delivery = _phase2_outbox().enqueue(
            dedupe_key=f"qq:automation:{job['id']}:{job['scheduled_for']}",
            channel="qq",
            destination=str(job.get("user_id") or ""),
            payload=payload,
            max_attempts=100,
            thread_ref=_automation_thread_ref(job),
            delivery_class="operational",
        )
        return {"status": "dispatched", "dispatch": "reminder", "delivery_id": delivery.get("id") or ""}

    raw_contract_text = str(job.get("execution_contract_json") or "").strip()
    if raw_contract_text and raw_contract_text != "{}":
        try:
            raw_contract = json.loads(raw_contract_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("automation_execution_contract_invalid") from exc
        if not isinstance(raw_contract, dict):
            raise RuntimeError("automation_execution_contract_invalid")
    else:
        # Only an absent/legacy-empty field may be derived.  A non-empty
        # malformed persisted contract is an admission failure, never a hint
        # to infer another Action from the instruction.
        try:
            parameters = json.loads(str(job.get("parameters_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parameters = {}
        raw_contract = derive_execution_contract(
            str(job.get("instruction") or ""),
            parameters if isinstance(parameters, dict) else {},
            action_type=str(job.get("action_type") or "agent"),
        )
    try:
        execution_contract = normalize_execution_contract(raw_contract)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        # Persisted contracts are an execution boundary.  A schema-invalid
        # value must stop here rather than being reinterpreted as another
        # Action or reaching Skill/Capability/Task/Delivery.
        raise RuntimeError("automation_execution_contract_invalid") from exc
    if execution_contract.get("status") != "ready":
        raise RuntimeError("automation_execution_contract_needs_clarification")
    capability_id = str(execution_contract.get("capability_id") or "").strip()
    allowed_capabilities = (capability_id,) if capability_id else ()
    skill_context = ""
    skill_plan = {"status": "unavailable"}
    with _assistant_db_connect() as conn:
        skill_plan = discover_skill_plan(
            conn,
            message=str(job.get("instruction") or ""),
            intent="research" if capability_id == "github.trending.read" else "automation",
            capability_ids=PHASE2_CAPABILITY_CATALOG.ids(),
            allowed_capability_ids=allowed_capabilities,
        )
        if skill_plan.get("status") == "missing_capability":
            raise RuntimeError("automation_skill_capability_missing")
        if skill_plan.get("status") == "skill_contract_mismatch":
            raise RuntimeError("automation_skill_contract_mismatch")
        skill_context = str(skill_plan.get("context") or "")
    if skill_plan.get("status") not in {"ready", "no_match"}:
        raise RuntimeError("automation_skill_not_resolved")
    if skill_plan.get("selected_skills"):
        skill_contract_check = validate_skill_contract(
            {"capability_id": capability_id},
            skill_plan,
        )
        if not skill_contract_check.get("ok"):
            raise RuntimeError("automation_skill_contract_mismatch")
    skill_execution_contract = _build_skill_execution_contract(skill_plan)

    light_capabilities = {
        "weather.forecast.read",
        "clock.current.read",
        "github.trending.read",
    }
    if capability_id in light_capabilities:
        light_executor = LightExecutor(
            catalog=PHASE2_CAPABILITY_CATALOG,
            github_handler=_light_github_handler,
        )
        argument_overrides: dict[str, object] = {}
        if capability_id == "github.trending.read":
            with _assistant_db_connect() as conn:
                argument_overrides["exclude_repos"] = list_automation_seen_items(
                    conn,
                    str(job.get("id") or ""),
                )

        def build_capability_payload(
            light_result: dict,
            dispatch_contract: dict,
            _job: dict,
        ) -> dict:
            if capability_id == "github.trending.read":
                output = light_result.get("output") if isinstance(light_result.get("output"), dict) else {}
                items = output.get("items") if isinstance(output.get("items"), list) else []
                summaries = _automation_github_purpose_summaries(job, items)
                effective_github_arguments = dict(dispatch_contract.get("arguments") or {})
                payload = prepare_github_delivery_payload(
                    job,
                    light_result,
                    effective_github_arguments,
                    summaries,
                    assistant_connect=_assistant_db_connect,
                    reserve_items=reserve_automation_items,
                )
                payload["skill_ids"] = list(skill_execution_contract.skill_ids)
                return payload
            content = _format_light_result(dict(light_result))
            if not content:
                raise RuntimeError("automation_evidence_or_presentation_missing")
            return {
                "kind": "automation_result",
                "content": content,
                "job_revision": int(job.get("revision") or job.get("job_revision") or 1),
                "skill_ids": list(skill_execution_contract.skill_ids),
            }

        result = execute_automation_capability(
            job,
            execution_contract,
            executor=light_executor,
            enqueue=_phase2_outbox().enqueue,
            build_payload=build_capability_payload,
            argument_overrides=argument_overrides or None,
        )
        if result.get("status") != "dispatched":
            raise RuntimeError(str(result.get("error") or "automation_capability_failed"))
        if capability_id == "github.trending.read":
            _phase2_outbox().supersede_pending_dedupe_prefix(
                dedupe_prefix=f"qq:automation-failure:{job['id']}:",
                superseded_by=str(result.get("delivery_id") or ""),
            )
        return result

    preflight = _automation_execution_preflight(job)
    if not preflight.get("ok"):
        raise RuntimeError(str(preflight.get("error_kind") or "automation_preflight_failed"))
    prompt = "\n".join(
        [
            "这是由用户预先授权的定时 Agent 工作，不是刚刚收到的聊天消息。",
            f"计划名称：{job.get('title') or job.get('id')}",
            f"计划触发时间（UTC）：{job.get('scheduled_for')}",
            "请完成下面的目标，按项目规则验证结果；不要声称用户此刻在线，也不要主动扩大权限。",
            "",
            f"Skill discovery status: {skill_plan.get('status') or 'unknown'}",
            f"Skill execution contract: {json.dumps(skill_execution_contract.to_dict(), ensure_ascii=False, sort_keys=True)}",
            skill_context,
            str(job.get("instruction") or "").strip(),
            f"结构化约束：{job.get('parameters_json') or '{}'}",
        ]
    )
    task = _create_task(
        prompt=prompt,
        sandbox="read-only",
        timeout=WORK_TASK_TIMEOUT,
        cwd=executor_workspace_root(),
        source=QQ_TASK_SOURCE,
        user_id=str(job.get("user_id") or ""),
        trace_id=f"automation-{str(job.get('run_id') or '')[:12]}",
        origin_message=f"定时任务：{job.get('title') or job.get('instruction') or ''}",
        intent="automation",
        mode="work",
        automation_run_id=str(job.get("run_id") or ""),
    )
    return {"status": "dispatched", "dispatch": "task", "task_id": task.get("id") or ""}


def _process_automation_jobs() -> None:
    with _assistant_db_connect() as conn:
        jobs = claim_due_jobs(conn, limit=5)
    for job in jobs:
        try:
            result = _run_automation_job(job)
            result_status = str(result.get("status") or "dispatched")
            if result_status == "failed":
                classified = _classify_automation_failure(
                    result.get("error_code") or result.get("error") or "automation_execution_failed",
                    stage=str(result.get("failure_stage") or ""),
                )
                with _assistant_db_connect() as conn:
                    finish_automation_run(
                        conn,
                        job,
                        status="failed",
                        error=classified["error_code"],
                        failure_stage=classified["stage"],
                        retryable=classified["retryable"],
                    )
                try:
                    _notify_automation_failure(job, classified)
                except Exception:
                    pass
                continue
            with _assistant_db_connect() as conn:
                finish_automation_run(
                    conn,
                    job,
                    status=result_status,
                    dispatch=str(result.get("dispatch") or ""),
                    task_id=str(result.get("task_id") or ""),
                    delivery_id=str(result.get("delivery_id") or ""),
                )
        except Exception as exc:
            classified = _classify_automation_failure(exc)
            with _assistant_db_connect() as conn:
                finish_automation_run(
                    conn,
                    job,
                    status="failed",
                    error=classified["error_code"],
                    failure_stage=classified["stage"],
                    retryable=classified["retryable"],
                )
            try:
                _notify_automation_failure(job, classified)
            except Exception:
                pass


def _process_proactive_policies() -> None:
    process_proactive_policies(globals())


def _process_group_participation_queue(now: object | None = None) -> None:
    process_group_participation_queue(globals(), now=now)


def process_knowledge_ingestion(runtime: dict) -> dict:
    """Bounded knowledge-ingestion pass inside the existing automation worker.

    Returns the worker summary so the loop can consume it; fatal failures are
    surfaced via health + structured logs inside the worker module.
    """
    from bridge_knowledge_ingestion_worker import process_knowledge_ingestion_pass

    return process_knowledge_ingestion_pass(runtime)


def _automation_worker() -> None:
    run_automation_worker(globals())


def _automation_overview() -> dict:
    with _assistant_db_connect() as conn:
        jobs = list_automation_jobs(conn, limit=100)
        policies = list_proactive_policies(conn, limit=100)
        runs = list_automation_runs(conn, limit=20)
        events = list_proactive_events(conn, limit=20)
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(hours=24)
    candidates = [
        {"kind": "job", "id": item.get("id"), "title": item.get("title"), "due_at": item.get("next_due_at"), "state": item.get("state")}
        for item in jobs
        if item.get("enabled") and item.get("next_due_at")
    ] + [
        {"kind": "proactive", "id": item.get("user_id"), "title": f"主动联系 {item.get('user_id')}", "due_at": item.get("next_check_at"), "state": item.get("state")}
        for item in policies
        if item.get("enabled") and item.get("authorized") and item.get("next_check_at")
    ]
    candidates.sort(key=lambda item: str(item.get("due_at") or ""))
    recent_events = [
        item for item in events
        if (datetime.fromisoformat(str(item.get("decision_at") or "")) if item.get("decision_at") else now) >= recent_cutoff
    ]
    return {
        "ok": True,
        "summary": {
            "enabled_jobs": sum(1 for item in jobs if item.get("enabled")),
            "enabled_proactive": sum(1 for item in policies if item.get("enabled") and item.get("authorized")),
            "waiting_reply": sum(1 for item in policies if item.get("state") == "waiting_reply"),
            "recent_decisions": len(recent_events),
        },
        "next_items": candidates[:8],
        "jobs": jobs,
        "policies": policies,
        "runs": runs,
        "events": events,
    }


def _append_task_message(task_id: str, message: str, mode_decision: dict, trace_id: str = "") -> dict | None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        _admit_current_response_cycle_effect("task_append")
        now = _utc_now()
        pending = _pending_messages(task.get("pending_messages"))
        pending.append(
            {
                "at": now,
                "message": message,
                "trace_id": trace_id,
                "mode": mode_decision.get("mode"),
                "intent": mode_decision.get("intent"),
                "applied_to_prompt": task.get("status") == "queued",
            },
        )
        task["pending_messages"] = json.dumps(pending[-20:], ensure_ascii=False)
        if task.get("status") == "queued":
            supplement = f"\n\n[QQ 用户补充消息 {now}]\n{message.strip()}\n"
            updated_prompt = str(task.get("prompt") or "") + supplement
            if len(updated_prompt) <= MAX_PROMPT_CHARS:
                task["prompt"] = updated_prompt
        _save_task_db(task)
        return _public_task(task, include_output=False)


def _dispatch_history_lines(history: list[dict] | None, *, limit: int = 8, max_chars: int = 8000) -> list[str]:
    selected: list[str] = []
    used = 0
    for item in reversed(list(history or [])):
        role = "用户" if item.get("role") == "user" else "助手"
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if role == "助手" and content.startswith("收到，我把这条转成后台任务 #"):
            continue
        content = content[-2000:]
        line = f"{role}: {content}"
        if selected and used + len(line) > max_chars:
            break
        selected.append(line)
        used += len(line)
        if len(selected) >= limit:
            break
    selected.reverse()
    return selected or ["(无可用近期对话)"]


def _d1_artifact_prompt_lines(delivery_projection: dict | None) -> list[str]:
    """Canonical D1 Result Page Artifact constraints for Work prompts.

    Only rendered when the server already authorized the D1 report
    profile; the executor never gains result_page_v1 authority by
    declaring it in a manifest.
    """
    if (
        isinstance(delivery_projection, dict)
        and delivery_projection.get("d1_report_profile")
        and delivery_projection.get("presentation") == "result_page_v1"
    ):
        return [
            "",
            "D1 报告成品约束:",
            "- 只生成一个 UTF-8 Markdown 文件，作为报告唯一 canonical source；不要生成 HTML、PDF 或其他副本。",
            "- 使用面向读者的有意义标题；Markdown 文件必须同时是唯一 files 项与 entrypoint。",
            '- Artifact manifest 必须包含 "kind":"report" 与 "presentation":"result_page_v1"。',
            "- presentation 只是声明，是否获得 Result Page 能力由 server validation 决定。",
        ]
    return []


def _format_dispatch_task_prompt(
    *,
    user_id: str,
    message: str,
    intent: str,
    criteria: list[str],
    policy: dict,
    mode_decision: dict,
    history: list[dict] | None = None,
    continuity_target: dict | None = None,
    research_context: str | None = None,
    delivery_projection: dict | None = None,
) -> str:
    project = _current_project() or {}
    local_now = datetime.now().astimezone().isoformat(timespec="seconds")
    context_lines = _dispatch_history_lines(history)
    continuity_lines = followup_prompt_context(continuity_target)
    fresh_data_required = bool(mode_decision.get("fresh_data_required")) or requires_fresh_external_data(message, history)
    freshness_lines = []
    if fresh_data_required:
        freshness_lines = [
            "",
            "实时信息硬约束:",
            f"- 当前服务器本地时间: {local_now}",
            "- 必须查询当前可获得的权威来源，并写明来源名称、数据时间和不确定性。",
            "- 天气和灾害信息优先使用中央气象台、中国气象局及对应省市气象部门。",
            "- 不得用历史同名事件替代当前事件；如果权威实时来源不可用，明确说明无法可靠核验，禁止猜测确定结论。",
        ]
    skill_context = ""
    selected_skills: list[dict] = []
    skill_plan: dict = mode_decision.get("skill_plan") if isinstance(mode_decision.get("skill_plan"), dict) else {"status": "unavailable"}
    try:
        if skill_plan.get("status") == "unavailable":
            with _assistant_db_connect() as conn:
                skill_plan = discover_skill_plan(
                    conn,
                    message=message,
                    intent=intent,
                    capability_ids=PHASE2_CAPABILITY_CATALOG.ids(),
                )
        skill_context = str(skill_plan.get("context") or "")
        selected_skills = list(skill_plan.get("selected_skills") or [])
    except sqlite3.Error:
        pass
    skill_lines = []
    if skill_context:
        skill_lines = [
            "",
            "本轮启用的 Skills:",
            f"- 已选择: {', '.join(str(item.get('name') or item.get('id')) for item in selected_skills)}",
            f"- Skill admission: {skill_plan.get('status') or 'unknown'}",
            f"- Required capabilities: {', '.join(skill_plan.get('required_capabilities') or ()) or 'none'}",
            skill_context,
        ]
    research_lines: list[str] = []
    if research_context and str(research_context).strip():
        research_lines = [
            "",
            "已核验的外部资料（仅用于本任务，不得编造，需在文档中体现来源）：",
            str(research_context).strip()[:1500],
            "- 成品必须直接面向读者，只呈现研究事实、公开来源、适用范围与事实置信边界。",
            "- 禁止描述成品的生成过程、素材由来、联网能力、工具条件、系统条件、内部实现、标识、命令或执行细节。",
            "- 来源限制必须写成哪些事实由哪些公开来源支持；不得出现“任务提供”“本次执行”“执行环境”“无法联网”等过程性措辞。完成前逐个检查并删除这类表述。",
        ]
    artifact_lines: list[str] = _d1_artifact_prompt_lines(delivery_projection)
    return "\n".join(
        [
            "你是通过 QQ 私聊触发的 Codex 后台工作任务。最终输出会自动推送回 QQ，请只输出适合直接发给用户的中文结果。",
            "",
            "工作规则:",
            "- 这是工作模式，不要加入日常撒娇、表情包、角色扮演语气或无关寒暄。",
            "- 先完成用户目标，再给出证据、结论、风险和下一步；不要只给空泛建议。",
            "- 涉及代码时，先理解现有结构和约定；仓库存在 CodeGraph 时优先利用项目结构信息。",
            "- 涉及服务器、容器、日志或部署时，说明你检查了什么、发现了什么、哪些操作已经执行。",
            "- 涉及写入、部署、删除、重启等高风险操作时，如上下文未明确授权或风险较高，先停止并说明需要确认。",
            "- 工作结束条件: 输出清晰结果，不继续等待用户；如未完成，明确阻塞点和下一步。",
            "",
            "本轮识别:",
            f"- QQ 用户: {user_id}",
            f"- 模式: {mode_decision.get('mode_label') or mode_decision.get('mode')}",
            f"- 意图: {_intent_label(intent)}",
            f"- 判断理由: {mode_decision.get('reason') or ''}",
            *continuity_lines,
            "",
            "本轮验收标准:",
            *[f"- {item}" for item in criteria],
            *freshness_lines,
            "",
            "当前项目:",
            f"- {project.get('name', '?')}: {project.get('path', '?')}",
            *skill_lines,
            *research_lines,
            *artifact_lines,
            "",
            "近期对话上下文（仅用于理解指代和连续目标；与本轮冲突时以本轮消息为准）:",
            *context_lines,
            "",
            "本轮用户消息:",
            message,
        ],
    )


def _dispatch_task_reply(task: dict, mode_decision: dict) -> str:
    task_id = task.get("id", "?")
    sandbox = task.get("sandbox", "?")
    position = task.get("position")
    intent_text = mode_decision.get("intent_label") or _intent_label(str(mode_decision.get("intent") or "analysis"))
    queue_text = f"，当前队列位置 {position}" if position and int(position) > 1 else ""
    return (
        f"收到，我把这条转成后台任务 #{task_id}（{intent_text} / {sandbox}{queue_text}）。\n"
        "我会在完成后自动把结果推回 QQ；你也可以随时发“任务”查看队列，或发“结果 "
        f"{task_id}”手动查看。"
    )


def _dispatch_append_reply(task: dict) -> str:
    task_id = task.get("id", "?")
    status = task.get("status", "?")
    pending_count = task.get("pending_message_count") or 0
    if status == "queued":
        return f"已把这条补充进任务 #{task_id}，它还在排队中，会一起执行。当前补充 {pending_count} 条。"
    return (
        f"已记下这条补充，当前任务 #{task_id} 仍在运行中。"
        "这不会打断正在执行的步骤；完成后我会推送结果，需要继续处理补充点时再接着开后续任务。"
    )


def _assemble_task_status_response(
    settings: dict,
    plan: dict | None,
    factual_text: str,
    *,
    factual_type: str,
    action: str = "",
    variant: int = 0,
    event: str = "",
    fact_slots: dict | None = None,
    channel_shows_identity: bool = False,
) -> tuple[list[dict], str]:
    """Assemble a task status response, persona-consistent when the flag is on."""
    if task_expression_enabled(settings):
        if event and fact_slots is not None:
            return task_lifecycle_blocks(
                plan or {},
                settings,
                event=event,
                factual_text=factual_text,
                fact_slots=fact_slots,
                variant=variant,
                channel_shows_identity=channel_shows_identity,
            )
        return task_status_blocks(
            plan or {},
            settings,
            factual_text,
            factual_type=factual_type,
            action=action,
            variant=variant,
            channel_shows_identity=channel_shows_identity,
        )
    return assemble_response(plan or {}, factual_text, factual_type=factual_type)


def _set_task_delivery(task_id: str, status: str, error: str = "") -> dict | None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        previous_status = str(task.get("delivery_status") or "")
        status = (status or "").strip() or TASK_DELIVERY_NONE
        task["delivery_status"] = status
        task["delivery_error"] = (error or "").strip()
        if status in {"sending", "sent", "failed", "skipped"}:
            task["delivered_at"] = _utc_now()
        if status == "pending" and error:
            attempts = int(task.get("delivery_attempts") or 0)
            delay_seconds = min(300, 10 * (2 ** min(max(0, attempts - 1), 5)))
            task["delivery_next_at"] = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        elif status == "sent":
            task["delivery_next_at"] = ""
        _save_task_db(task)
        if status == "sent" and previous_status != "sent" and task.get("source") == QQ_TASK_SOURCE and task.get("user_id"):
            final_text = str(task.get("stdout") or task.get("output") or task.get("error") or "").strip()
            if final_text:
                _record_conversation(
                    str(task.get("user_id")),
                    "assistant",
                    f"任务 #{task_id} 最终结果：\n{_trim_output(final_text)[:6000]}",
                )
        return _public_task(task, include_output=True)


def _claim_pending_task_deliveries(limit: int = 5) -> list[dict]:
    deliveries = _claim_phase2_deliveries(
        "legacy-task-delivery-poller",
        wait_seconds=0,
        lease_seconds=180,
        limit=limit,
        channel="qq",
    )
    claimed: list[dict] = []
    for delivery in deliveries:
        payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
        task_id = _delivery_task_id(delivery)
        with TASK_LOCK:
            stored = TASKS.get(task_id)
            if stored and _is_terminal_task_delivery(delivery):
                stored["delivery_status"] = "sending"
                stored["delivery_error"] = ""
                stored["delivered_at"] = _utc_now()
                stored["delivery_attempts"] = int(delivery.get("attempt") or 0)
                _save_task_db(stored)
                task = _public_task(stored, include_output=True)
            else:
                raw_task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
                task = dict(raw_task)
        if not task:
            continue
        task["send_session"] = str(delivery.get("destination") or payload.get("send_session") or "")
        task["outbox_delivery_id"] = str(delivery.get("id") or "")
        task["outbox_lease_token"] = str(delivery.get("lease_token") or "")
        claimed.append(task)
    return claimed


def _light_github_handler(arguments: dict) -> dict:
    period = str(arguments.get("period") or "daily")
    limit = max(1, min(int(arguments.get("limit") or 10), 20))
    topic = str(arguments.get("topic") or "").strip()
    excluded = {
        str(value or "").strip().lower()
        for value in (arguments.get("exclude_repos") or [])
        if str(value or "").strip()
    }
    result = _github_trending(period, limit, topic=topic, exclude_repos=excluded)
    if not result.get("ok") or result.get("source_quality") == "cache":
        raise RuntimeError("github_trending_authoritative_source_unavailable")
    return {
        "items": result.get("repos") or [],
        "source_url": result.get("source_url") or f"https://github.com/trending?since={period}",
        "data_time": result.get("data_time") or _utc_now(),
        "source_quality": result.get("source_quality") or "official",
        "note": result.get("note") or "",
    }


def _normalise_light_evidence(items: object) -> list[dict]:
    result: list[dict] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        facts = raw.get("facts") if isinstance(raw.get("facts"), list) else []
        result.append(
            {
                **raw,
                "source_uri": str(raw.get("source_url") or ""),
                "published_at": str(raw.get("data_time") or ""),
                "retrieved_at": str(raw.get("fetched_at") or _utc_now()),
                "expires_at": str(raw.get("valid_until") or ""),
                "excerpt": json.dumps(facts, ensure_ascii=False)[:50000],
            },
        )
    return result


def _format_light_result(result: dict) -> str:
    capability_id = str(result.get("capability_id") or "")
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    lines: list[str] = []
    if capability_id == "clock.current.read":
        lines.append(f"当前时间：{output.get('local_time', '')}（{output.get('timezone', '')}）")
    elif capability_id == "weather.forecast.read":
        location = output.get("location") if isinstance(output.get("location"), dict) else {}
        current = output.get("current") if isinstance(output.get("current"), dict) else {}
        units = output.get("current_units") if isinstance(output.get("current_units"), dict) else {}
        place = " ".join(
            value
            for value in (
                str(location.get("name") or ""),
                str(location.get("admin1") or ""),
                str(location.get("country") or ""),
            )
            if value
        )
        lines.append(f"{place or location.get('query') or '所查地点'}普通天气查询")
        lines.append(
            "当前："
            f"{current.get('temperature_2m', '?')}{units.get('temperature_2m', '°C')}，"
            f"降水 {current.get('precipitation', '?')}{units.get('precipitation', 'mm')}，"
            f"风速 {current.get('wind_speed_10m', '?')}{units.get('wind_speed_10m', 'km/h')}。"
        )
        daily = output.get("daily") if isinstance(output.get("daily"), list) else []
        if daily:
            lines.append("短期预报：")
            for item in daily[:7]:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"- {item.get('date', '?')}："
                    f"{item.get('temperature_2m_min', '?')}～{item.get('temperature_2m_max', '?')}°C，"
                    f"最高降水概率 {item.get('precipitation_probability_max', '?')}%，"
                    f"最大风速 {item.get('wind_speed_10m_max', '?')} km/h"
                )
        lines.append("说明：轻量路径只回答普通天气；灾害、预警和出行风险会转入权威来源深度核验。")
    elif capability_id == "github.trending.read":
        items = output.get("items") if isinstance(output.get("items"), list) else []
        topic = str(output.get("topic") or "")
        topic_label = " · AI / AI Agent" if topic in {"ai", "ai-agent"} else ""
        lines.append(f"GitHub 热门项目（{output.get('period') or 'daily'}{topic_label}）")
        chinese_only = str(output.get("output_language") or "auto") == "zh-CN"
        for index, item in enumerate(items[:20], start=1):
            if isinstance(item, dict):
                lines.append(f"{index}. 项目：{item.get('repo') or item.get('name') or '?'}")
                if item.get("description") and not chinese_only:
                    lines.append(f"   {item.get('description')}")
                if item.get("stars"):
                    lines.append(f"   收藏数：{item.get('stars')}")
                lines.append(f"   链接：{item.get('url') or ''}")
    else:
        lines.append(json.dumps(output, ensure_ascii=False, indent=2)[:6000])

    if evidence:
        first = evidence[0] if isinstance(evidence[0], dict) else {}
        lines.extend(
            (
                "",
                f"来源：{first.get('source_name') or first.get('source_id') or '结构化来源'}",
                f"数据时间：{first.get('data_time') or '未提供'}",
                f"获取时间：{first.get('fetched_at') or '未提供'}",
            ),
        )
    return "\n".join(line for line in lines if line is not None).strip()


def _store_light_task(
    *,
    user_id: str,
    message: str,
    trace_id: str,
    mode_decision: dict,
    light_result: dict,
    reply: str,
    duration: float,
    source: str = QQ_TASK_SOURCE,
) -> dict:
    _admit_current_response_cycle_effect("light_task_projection")
    now = _utc_now()
    capability_id = str(light_result.get("capability_id") or "")
    strategy = "direct" if capability_id == "clock.current.read" else "grounded"
    task = {
        "id": uuid.uuid4().hex[:8],
        "status": "done",
        "created_at": now,
        "started_at": now,
        "finished_at": now,
        "sandbox": "read-only",
        "cwd": str(_default_cwd()),
        "summary": _task_summary(message),
        "prompt": message,
        "timeout": 10,
        "duration": round(max(0.0, duration), 3),
        "returncode": 0,
        "ok": True,
        "cancel_requested": False,
        "stdout": reply,
        "stderr": "",
        "output": reply,
        "error": "",
        "source": source,
        "user_id": user_id,
        "trace_id": trace_id,
        "origin_message": message,
        "intent": str(mode_decision.get("intent") or "research"),
        "mode": str(mode_decision.get("mode") or "daily"),
        "delivery_status": TASK_DELIVERY_NONE,
        "delivery_error": "",
        "delivery_attempts": 0,
        "delivery_next_at": "",
        "pending_messages": "[]",
        "capability_id": capability_id,
        "strategy": strategy,
        "evidence": _normalise_light_evidence(light_result.get("evidence")),
    }
    with TASK_LOCK:
        TASKS[task["id"]] = task
        _append_history(task)
        _trim_tasks()
        _save_task_db(task)
    return _public_task(task, include_output=True)


def _try_light_dispatch(
    *,
    user_id: str,
    message: str,
    trace_id: str,
    force: str,
    mode_decision: dict,
    criteria: list[str],
    source: str = QQ_TASK_SOURCE,
) -> dict | None:
    if str(force or "auto").strip().lower() == "task":
        return None
    executor = LightExecutor(
        catalog=PHASE2_CAPABILITY_CATALOG,
        github_handler=_light_github_handler,
    )
    started = time.monotonic()
    route = executor.route(message)
    if route.matched:
        with _assistant_db_connect() as conn:
            capability_allowed = network_capability_allowed(
                conn,
                str(route.capability_id or ""),
            )
        if not capability_allowed:
            reply = (
                "当前网络策略已关闭外部来源读取，因此没有执行这次联网查询。"
                "Owner 可以在控制台“工具与 Skill → 网络策略”重新开启受控 Capability。"
            )
            blocks, reply = assemble_response(
                mode_decision.get("interaction_plan") or {},
                reply,
                factual_type="status",
            )
            with ASSISTANT_LOCK:
                INTERACTION_STORE.record_exchange(
                    user_id,
                    message,
                    reply,
                    mode_decision,
                    source=source,
                )
            return {
                "ok": True,
                "dispatch": "network_policy_blocked",
                "reply": reply,
                "content_blocks": blocks,
                "capability_id": route.capability_id,
                "mode": mode_decision.get("mode"),
                "intent": mode_decision.get("intent"),
                "interaction_plan": mode_decision.get("interaction_plan"),
                "interaction_plan_record": mode_decision.get("interaction_plan_record"),
                "acceptance_criteria": criteria,
            }
        _admit_current_response_cycle_effect("light_capability")
    result = executor.execute(message)
    if result.get("status") != "completed" or result.get("fallback"):
        return None
    factual_reply = _format_light_result(result)
    if not factual_reply:
        return None
    blocks, reply = assemble_response(
        mode_decision.get("interaction_plan") or {},
        factual_reply,
        factual_type="fact",
    )
    task = _store_light_task(
        user_id=user_id,
        message=message,
        trace_id=trace_id,
        mode_decision=mode_decision,
        light_result=result,
        reply=reply,
        duration=time.monotonic() - started,
        source=source,
    )
    with ASSISTANT_LOCK:
        INTERACTION_STORE.record_exchange(
            user_id,
            message,
            reply,
            mode_decision,
            source=source,
        )
    return {
        "ok": True,
        "dispatch": "light",
        "reply": reply,
        "content_blocks": blocks,
        "task": task,
        "goal_id": task.get("goal_id"),
        "run_id": task.get("run_id"),
        "capability_id": result.get("capability_id"),
        "capability": get_fixed_capability(str(result.get("capability_id") or "")),
        "evidence": result.get("evidence") or [],
        "route": {
            "confidence": result.get("confidence"),
            "reason": result.get("reason"),
        },
        "mode": mode_decision.get("mode"),
        "intent": mode_decision.get("intent"),
        "interaction_plan": mode_decision.get("interaction_plan"),
        "interaction_plan_record": mode_decision.get("interaction_plan_record"),
        "acceptance_criteria": criteria,
    }


def _store_research_task(
    *,
    user_id: str,
    message: str,
    trace_id: str,
    mode_decision: dict,
    research_result,
    reply: str,
    duration: float,
    source: str = QQ_TASK_SOURCE,
) -> dict:
    _admit_current_response_cycle_effect("research_task_projection")
    now = _utc_now()
    # W0: research respects work_context routing (generic = /opt/agent-workspace, not project)
    research_cwd = str(_default_cwd())
    try:
        _settings = _assistant_settings()
        if work_context_routing_enabled(_settings):
            research_cwd = str(resolve_work_cwd(message, _current_project(), executor_workspace_root()))
    except Exception:
        research_cwd = str(_default_cwd())
    task = {
        "id": uuid.uuid4().hex[:8],
        "status": "done",
        "created_at": now,
        "started_at": now,
        "finished_at": now,
        "sandbox": "read-only",
        "cwd": research_cwd,
        "summary": _task_summary(message),
        "prompt": message,
        "timeout": 10,
        "duration": round(max(0.0, duration), 3),
        "returncode": 0,
        "ok": True,
        "cancel_requested": False,
        "stdout": reply,
        "stderr": "",
        "output": reply,
        "error": "",
        "source": source,
        "user_id": user_id,
        "trace_id": trace_id,
        "origin_message": message,
        "intent": str(mode_decision.get("intent") or "research"),
        "mode": str(mode_decision.get("mode") or "work"),
        "delivery_status": TASK_DELIVERY_NONE,
        "delivery_error": "",
        "delivery_attempts": 0,
        "delivery_next_at": "",
        "pending_messages": "[]",
        "capability_id": RESEARCH_CAPABILITY_ID,
        "strategy": "grounded",
        "evidence": research_result.evidence if hasattr(research_result, "evidence") else [],
    }
    with TASK_LOCK:
        TASKS[task["id"]] = task
        _append_history(task)
        _trim_tasks()
        _save_task_db(task)
    return _public_task(task, include_output=True)


def _planned_research_goal(mode_decision: dict, message: str) -> dict[str, str]:
    """Read the planner-owned goal, with the deterministic plan fallback only."""
    plan = mode_decision.get("interaction_plan")
    candidates = [
        plan.get("research") if isinstance(plan, dict) else None,
        mode_decision.get("research_goal"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        subject = str(candidate.get("subject") or "").strip()[:240]
        deliverable = str(candidate.get("deliverable") or "none").strip().lower()
        if subject and deliverable in {"document", "report", "file"}:
            return {"subject": subject, "deliverable": deliverable}
    return research_goal_from_message(message)


def _looks_like_delivered_artifact_revision(message: str) -> bool:
    """Narrow deterministic fallback for an already-delivered Artifact.

    The delivered Outbox scope remains the authority for *which* bytes may be
    revised.  This helper only recognizes an explicit edit operation plus a
    document-local target, so ordinary chat and new research requests still
    belong to the Interaction Planner.
    """
    text = " ".join(str(message or "").strip().split())
    if not text or len(text) > 500:
        return False
    edit_markers = (
        "缩短", "精简", "改短", "删掉", "删除", "修改", "调整",
        "改成", "重写", "润色", "补充", "加上", "替换",
    )
    document_targets = (
        "部分", "章节", "小节", "段落", "第", "标题", "结论", "摘要",
        "文档", "报告", "文件", "这份", "那份", "上一版", "刚才那版",
    )
    return any(marker in text for marker in edit_markers) and any(
        marker in text for marker in document_targets
    )


def _bounded_recovery_research_query(subject: str) -> str:
    """Return one less conversational query form, or no recovery opportunity."""
    original = str(subject or "").strip()[:240]
    candidate = re.sub(
        r"\s*(?:有(?:什么|哪些)?|的)?\s*(?:主要|重要)?"
        r"(?:变化|区别|差异|新增|更新)(?:呢|吗)?\s*[?？。！？!]*$",
        "",
        original,
    ).strip(" ，,;；。！？!?")
    return candidate if len(candidate) >= 4 and candidate != original else ""


def _research_failure_text(result=None, *, execution_failure: bool = False) -> str:
    """Keep execution and no-evidence failures truthful without runtime detail."""
    reason = str(getattr(result, "reason_code", "") or "")
    if not execution_failure and reason == "research_no_approved_sources":
        return "这次暂时没有找到足够可靠的资料，我不想拿没核验的信息凑答案。你可以稍后再试。"
    return "这次检索没有成功，我先不拿没核验的信息回答。你可以稍后再试。"


def _bounded_research_context(result, max_chars: int = 1500) -> str:
    """Render bounded grounded context from ResearchResult for Work prompt injection."""
    try:
        sources = list(getattr(result, "sources", None) or [])
        evidence = list(getattr(result, "evidence", None) or [])
    except Exception:
        return ""
    lines: list[str] = []
    for idx, src in enumerate(sources[:3], start=1):
        try:
            url = str(src.get("url") or "")[:200]
            title = str(src.get("title") or "")[:120]
            excerpt = str(src.get("excerpt") or "")[:500]
        except Exception:
            continue
        if not url or not excerpt:
            continue
        lines.append(f"{idx}. {title} — {url}")
        lines.append(f"   摘录: {excerpt[:300]}")
        # attach evidence hash if available
        try:
            ev = evidence[idx-1] if idx-1 < len(evidence) else {}
            sha = str(ev.get("content_sha256") or "")[:16]
            if sha:
                lines.append(f"   证据: {sha}…")
        except Exception:
            pass
    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…（已截断）"
    # 防泄漏：剥离内部 telemetry
    for leak in ("curl", "urllib", "MCP", "sandbox", "workspace", "provider", "runtime", "internal_detail"):
        if leak.lower() in text.lower():
            text = text.replace(leak, "")
    return text


D1_SUMMARY_FALLBACK = "完整调查结论已整理进文档，请查看结果页。"


def _revision_delivery_projection(target: dict) -> dict:
    """Server-authorized D1 presentation inheritance for natural Revisions.

    A Revision skips Research by design, so the projection is inherited
    from the trusted acknowledged Base Artifact Version's internal
    manifest (never from Research summary, never from the executor).  The
    executor cannot acquire result_page_v1 authority by declaring it.
    """
    if str(target.get("resolution") or "") != "resolved":
        return {}
    base_version_id = str(target.get("artifact_version_id") or "")
    if not base_version_id:
        return {}
    try:
        with _assistant_db_connect() as conn:
            assistant = assistant_identity.current_assistant(conn) or {}
        owner_id = str(assistant.get("owner_actor_id") or "admin")
        internal = ARTIFACT_RUNTIME.service._version_internal_manifest(
            base_version_id, owner_id=owner_id,
        )
    except Exception:
        return {}
    if str(internal.get("presentation") or "") != "result_page_v1":
        return {}
    return {
        "mode": "BOTH",
        "summary_points": [],
        "d1_report_profile": True,
        "presentation": "result_page_v1",
    }


def _research_delivery_projection(
    mode_decision: dict,
    message: str,
    research_goal: dict,
    result,
) -> dict:
    """Build the server-owned D1 runtime projection from verified Research."""

    plan = mode_decision.get("interaction_plan")
    plan_delivery = plan.get("delivery") if isinstance(plan, dict) else None
    mode = str(
        mode_decision.get("delivery_mode")
        or ((plan_delivery or {}).get("mode") if isinstance(plan_delivery, dict) else "")
        or delivery_mode_from_message(message, research_goal)
    ).upper()
    if (
        mode not in {"ARTIFACT", "BOTH"}
        or str(research_goal.get("deliverable") or "") not in {"document", "report"}
        or getattr(result, "status", "") != "succeeded"
    ):
        return {}
    output = getattr(result, "output", None)
    raw_points = output.get("summary_points") if isinstance(output, dict) else []
    points: list[str] = []
    if mode == "BOTH" and isinstance(raw_points, list):
        points = filter_summary_points(raw_points)[:3]
        if not points:
            points = [D1_SUMMARY_FALLBACK]
    return {
        "mode": mode,
        "summary_points": points,
        "d1_report_profile": True,
        "presentation": "result_page_v1",
    }


# W0 production adapter: reuse Codex --search without group flag; tests may patch to fake/None
_PRIVATE_RESEARCH_ADAPTER = _run_private_research_adapter  # type: ignore  # noqa: E402 - defined above, production wiring


def _try_research_dispatch(
    *,
    user_id: str,
    message: str,
    trace_id: str,
    force: str,
    mode_decision: dict,
    criteria: list[str],
    source: str = QQ_TASK_SOURCE,
    established_task_id: str = "",
) -> dict | None:
    if str(force or "auto").strip().lower() == "chat":
        return None
    # Resolve requirements; need a light route snapshot for accurate is_light check
    try:
        light_probe = LightExecutor(catalog=PHASE2_CAPABILITY_CATALOG).route(message)
    except Exception:
        light_probe = None  # type: ignore
    try:
        project = _current_project()
    except Exception:
        project = None
    try:
        requirements = resolve_capability_requirements(
            message=message,
            mode_decision=mode_decision,
            light_route=light_probe,
            project=project,
        )
        lane = select_execution_lane(
            requirements=requirements,
            mode_decision=mode_decision,
            light_route=light_probe,
        )
    except Exception:
        return None
    if lane.lane != "research":
        return None
    _admit_current_response_cycle_effect("private_research")
    # Research lane selected - execute bounded research
    started = time.monotonic()
    # Derive a safe query: use original message trimmed, but research runtime expects query 4..300
    query = str(message or "").strip()[:300]
    if len(query) < 4:
        query = str(message or "").strip()
    # Allow test injection
    adapter = _PRIVATE_RESEARCH_ADAPTER  # patched by tests
    # For offline test determinism, if adapter is None and we are in test mode where python docs fetch would fail,
    # tests should patch adapter to provide sources.  We keep allow_local_fallback False in production code.
    # Tests that need offline fallback can patch execute_private_research or set adapter.
    try:
        result = execute_private_research(
            query=query,
            original_message=message,
            connect=_assistant_db_connect,
            catalog=PHASE2_CAPABILITY_CATALOG,
            research_adapter=adapter,
            now=lambda: datetime.now(timezone.utc),
            allow_local_fallback=False,
        )
    except Exception as exc:
        result = None  # type: ignore
        internal = str(exc)[:500]
        reply = _research_failure_text(execution_failure=True)
        blocks, reply = assemble_response(
            mode_decision.get("interaction_plan") or {},
            reply,
            factual_type="status",
        )
        with ASSISTANT_LOCK:
            INTERACTION_STORE.record_exchange(user_id, message, reply, mode_decision, source=source)
        task = (
            _fail_established_execution(
                established_task_id,
                error_kind="research_execution_failed",
                user_message=reply,
            )
            if established_task_id
            else None
        )
        return {
            "ok": True,
            "dispatch": "research_blocked",
            "reply": reply,
            "content_blocks": blocks,
            "capability_id": RESEARCH_CAPABILITY_ID,
            "mode": mode_decision.get("mode"),
            "intent": mode_decision.get("intent"),
            "interaction_plan": mode_decision.get("interaction_plan"),
            "interaction_plan_record": mode_decision.get("interaction_plan_record"),
            "acceptance_criteria": criteria,
            "research_status": "blocked",
            "research_reason": "research_execution_error",
            "internal_detail": internal,
            **({"task": task} if task is not None else {}),
        }
    if result is None:
        return None
    if result.status == "succeeded":
        # Render user-facing reply with points and natural source note, no internal leaks
        factual_reply = render_research_reply(result, points=5)
        # Ensure blocked leak terms never appear
        for leak in ["curl", "urllib", "MCP", "sandbox", "workspace", "provider", "runtime", "model memory", "Operation not permitted", "mcp"]:
            if leak.lower() in factual_reply.lower():
                factual_reply = factual_reply.replace(leak, "")
        blocks, reply = assemble_response(
            mode_decision.get("interaction_plan") or {},
            factual_reply,
            factual_type="fact",
        )
        if established_task_id:
            task = _complete_established_research(
                established_task_id,
                research_result=result,
                reply=reply,
                duration=time.monotonic() - started,
            )
        else:
            task = _store_research_task(
                user_id=user_id,
                message=message,
                trace_id=trace_id,
                mode_decision=mode_decision,
                research_result=result,
                reply=reply,
                duration=time.monotonic() - started,
                source=source,
            )
        with ASSISTANT_LOCK:
            INTERACTION_STORE.record_exchange(user_id, message, reply, mode_decision, source=source)
        return {
            "ok": True,
            "dispatch": "research",
            "reply": reply,
            "content_blocks": blocks,
            "task": task,
            "capability_id": RESEARCH_CAPABILITY_ID,
            "capability": get_fixed_capability(RESEARCH_CAPABILITY_ID),
            "evidence": result.evidence,
            "sources": result.sources,
            "route": {"confidence": requirements.confidence, "reason": requirements.reason},
            "mode": mode_decision.get("mode"),
            "intent": mode_decision.get("intent"),
            "interaction_plan": mode_decision.get("interaction_plan"),
            "interaction_plan_record": mode_decision.get("interaction_plan_record"),
            "acceptance_criteria": criteria,
            "research_status": result.status,
            "research_reason": result.reason_code,
        }
    # blocked / failed -> truthful user-safe message, no synthesis
    user_msg = _research_failure_text(result)
    # Sanitize user_msg leak
    for leak in ["curl", "urllib", "MCP", "sandbox", "workspace", "provider", "runtime", "model memory", "Operation not permitted"]:
        if leak.lower() in user_msg.lower():
            user_msg = user_msg.replace(leak, "")
    blocks, reply = assemble_response(
        mode_decision.get("interaction_plan") or {},
        user_msg,
        factual_type="status",
    )
    with ASSISTANT_LOCK:
        INTERACTION_STORE.record_exchange(user_id, message, reply, mode_decision, source=source)
    task = (
        _fail_established_execution(
            established_task_id,
            error_kind=(
                "research_blocked"
                if result.status == "blocked"
                else "research_execution_failed"
            ),
            user_message=reply,
        )
        if established_task_id
        else None
    )
    return {
        "ok": True,
        "dispatch": "research_blocked" if result.status == "blocked" else "research_failed",
        "reply": reply,
        "content_blocks": blocks,
        "capability_id": RESEARCH_CAPABILITY_ID,
        "mode": mode_decision.get("mode"),
        "intent": mode_decision.get("intent"),
        "interaction_plan": mode_decision.get("interaction_plan"),
        "interaction_plan_record": mode_decision.get("interaction_plan_record"),
        "acceptance_criteria": criteria,
        "research_status": result.status,
        "research_reason": result.reason_code,
        "internal_detail": result.internal_detail,
        **({"task": task} if task is not None else {}),
    }


def _admit_current_response_cycle_effect(effect_kind: str) -> dict | None:
    """Admit a real private effect only under the current fenced cycle lease."""

    inbound = current_inbound_exchange_context()
    cycle_id = str(inbound.get("_response_cycle_id") or "").strip()
    if not cycle_id:
        return None
    source_hash = str(inbound.get("_response_cycle_source_set_hash") or "").strip()
    lease_token = str(inbound.get("_response_cycle_lease_token") or "").strip()
    if not source_hash or not lease_token:
        raise RuntimeError("response_cycle_effect_identity_missing")
    with _assistant_db_connect() as conn:
        row = conn.execute(
            "SELECT thread_id FROM conversation_response_cycles WHERE id=?",
            (cycle_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("response_cycle_effect_cycle_missing")
        admitted = assert_response_cycle_effects_allowed(
            conn,
            thread_id=str(row[0]),
            cycle_id=cycle_id,
            source_set_hash=source_hash,
            lease_token=lease_token,
        )
    return {**admitted, "effect_kind": str(effect_kind or "effect")[:80]}


def _dispatch_network_policy_control(
    *,
    user_id: str,
    message: str,
    trace_id: str,
    source: str,
) -> dict | None:
    is_owner = source == "admin" or user_id in qq_super_admin_ids(
        _assistant_db_connect,
    )
    command = parse_network_policy_command(message)
    if is_owner and command is not None and command.get("action") in {"enable", "disable"}:
        _admit_current_response_cycle_effect("network_policy_control")
    with _assistant_db_connect() as conn:
        result = apply_network_policy_command(
            conn,
            message=message,
            is_owner=is_owner,
            actor_ref=user_id or "owner",
            channel="web" if source == "admin" else "qq",
        )
    if result is None:
        return None
    reply = str(result.get("reply") or "")
    policy = result.get("policy") or {}
    if policy.get("owner_web_search_active"):
        try:
            executor_adapter = _resolve_executor_snapshot().get("adapter")
        except RuntimeError:
            executor_adapter = ""
        if executor_adapter != "codex_login":
            reply += (
                "\n当前工作执行器不支持 Web Search；授权已保存，但不会自动用于任务。"
                "需先把 work_executor 切换为 Codex 登录态。"
            )
    decision = {
        "mode": "work",
        "intent": "system",
        "reason": "deterministic_network_policy_control",
        "interaction_plan": {
            "intents": [{"type": "system", "confidence": 1.0}],
            "actions": [{"type": "respond", "requires_tools": False}],
            "reply_parts": [{"type": "status"}],
        },
    }
    with ASSISTANT_LOCK:
        INTERACTION_STORE.record_exchange(
            user_id,
            message,
            reply,
            decision,
            source=source,
        )
    return {
        "ok": True,
        "dispatch": "network_policy",
        "reply": reply,
        "policy": policy,
        "trace_id": trace_id,
        "mode": "work",
        "intent": "system",
        "mode_decision": decision,
        "interaction_plan": decision["interaction_plan"],
    }


def _maybe_accumulate_relationship_for_dispatch(user_id: str, message_text: str, source: str) -> None:
    """Best-effort relationship accumulation for any private dispatch path."""
    try:
        if str(source or "").strip() == "group":
            return
        # avoid accumulating for transient/system messages
        if not str(message_text or "").strip():
            return
        _settings_for_accum = _assistant_settings()
        _mode = _relationship_accumulation_mode(_settings_for_accum)
        if _mode == "off":
            return
        with _assistant_db_connect() as _acc_conn:
            _ensure_relationship_accumulation_tables(_acc_conn)
            _relationship_apply_accumulation(
                _acc_conn,
                user_id=str(user_id or "").strip(),
                scope_type="private_user",
                scope_id="",
                now=None,
                message_text=str(message_text or ""),
                mode=_mode,
            )
    except Exception:
        pass


def _failure_retry_request_key(
    target: Mapping[str, object],
    *,
    user_id: str,
    trace_id: str,
    inbound_context: Mapping[str, object],
) -> str:
    source_identity = str(
        inbound_context.get("_external_message_id") or trace_id or ""
    ).strip()
    if not source_identity:
        return ""
    payload = "\0".join((
        str(target.get("legacy_task_id") or ""),
        str(user_id or ""),
        source_identity,
    ))
    return "failure-recovery:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _retry_bound_failed_task(
    target: Mapping[str, object],
    *,
    user_id: str,
    trace_id: str,
    inbound_context: Mapping[str, object],
    execution_presence_callback=None,
    interaction_plan: Mapping[str, object] | None = None,
) -> tuple[dict | None, str]:
    """Create the next Run on the bound Goal and carry only valid retained facts."""

    source_task_id = str(target.get("legacy_task_id") or "").strip()
    source_run_id = str(target.get("run_id") or "").strip()
    source_goal_id = str(target.get("goal_id") or "").strip()
    if not source_task_id or not source_run_id or not source_goal_id:
        return None, "failure_recovery_identity_incomplete"
    with _db_connect() as conn:
        repo = PlatformRepository(conn)
        evidence_rows = repo.list_evidence(source_run_id, limit=100)
        delivery_projection = load_task_delivery_projection(
            conn,
            source_goal_id,
            source_task_id,
        )
    now = datetime.now(timezone.utc)
    evidence = []
    for item in evidence_rows:
        expires_text = str(item.get("expires_at") or "").strip()
        metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
        candidate = {
            "id": str(item.get("id") or ""),
            "source_name": str(item.get("source_name") or ""),
            "source_uri": str(item.get("source_uri") or ""),
            "published_at": str(item.get("published_at") or ""),
            "retrieved_at": str(item.get("retrieved_at") or ""),
            "expires_at": expires_text,
            "content_hash": str(item.get("content_hash") or ""),
            "excerpt": str(item.get("excerpt") or ""),
            "facts": metadata.get("facts"),
            "metadata": metadata,
        }
        if valid_retained_research_evidence(candidate, now=now):
            evidence.append(candidate)
    if not evidence:
        return None, "retained_research_unavailable"
    retried, error = _retry_task(
        source_task_id,
        request_idempotency_key=_failure_retry_request_key(
            target,
            user_id=user_id,
            trace_id=trace_id,
            inbound_context=inbound_context,
        ),
        trace_id=trace_id,
        initial_evidence=evidence,
        delivery_projection=delivery_projection,
        execution_presence_callback=execution_presence_callback,
        interaction_plan=interaction_plan,
    )
    if retried is None or error:
        return retried, error
    if retried.get("idempotent_replay"):
        return retried, ""
    return retried, ""


def _private_visual_turn_present(inbound_context: object, ordered_turn: object) -> bool:
    if not isinstance(inbound_context, dict):
        return False
    session = inbound_context.get("session")
    if not isinstance(session, str) or not session.startswith("qq:private:"):
        return False
    if isinstance(inbound_context.get("visual_media"), list) and inbound_context["visual_media"]:
        return True
    attachments = inbound_context.get("attachments")
    if isinstance(attachments, list) and any(
        isinstance(item, dict)
        and str(item.get("type") or "").strip().lower() in {"image", "video"}
        for item in attachments
    ):
        return True
    return has_complete_order_contract(ordered_turn) and any(
        isinstance(item, dict) and item.get("kind") == "visual"
        for item in ordered_turn.get("components", [])
    )


def _private_visual_deadline_result(payload: dict) -> dict:
    observation = {
        "schema_version": 1,
        "status": "unavailable",
        "media_kind": "unknown",
        "visible_elements": [],
        "visible_text": "",
        "depicted_expression_or_gesture": "",
        "uncertain_elements": [],
    }
    payload["visual_context_status"] = "unavailable"
    payload["visual_context_reason"] = "visual_deadline_exhausted"
    if isinstance(payload.get("attachments"), list):
        payload["attachments"] = [
            {
                **item,
                "visual_context_ready": False,
                "visual_context_state": "unavailable",
            }
            if isinstance(item, dict)
            and str(item.get("type") or "").strip().lower() in {"image", "video"}
            else item
            for item in payload["attachments"]
        ]
    return {
        "status": "unavailable",
        "reason": "visual_deadline_exhausted",
        "image_count": 0,
        "observation": observation,
    }


def _bounded_private_elapsed(started: object, finished: object, maximum: int) -> float:
    if type(started) not in {int, float} or type(finished) not in {int, float}:
        return 0.0
    return round(min(max(float(finished) - float(started), 0.0), float(maximum)), 3)


def _private_visual_event_id(inbound_context: Mapping[str, object], trace_id: str) -> str:
    """Prefer the durable response cycle for continuous private visual facts."""

    if not isinstance(inbound_context, Mapping):
        return str(trace_id or "").strip()
    return str(
        inbound_context.get("_response_cycle_id")
        or inbound_context.get("logical_turn_id")
        or inbound_context.get("_external_message_id")
        or trace_id
        or ""
    ).strip()


def _private_situation_context_recovery_terminal(
    inbound_context: Mapping[str, object],
) -> dict | None:
    status = str(inbound_context.get("_situation_context_status") or "")
    if status == "unavailable":
        return {
            "ok": False,
            "dispatch": "visual_context_recovery_required",
            "reply": (
                "我还记得你是在接着前面的对话，但前面那张图的临时视觉信息已经失效了。"
                "请把前面那张图重新发一次，我再接着回答，不会拿后面的图代替它。"
            ),
            "error_kind": "prior_visual_context_unavailable",
            "retryable": False,
        }
    if status == "invalid":
        return {
            "ok": False,
            "dispatch": "visual_context_recovery_required",
            "reply": (
                "我没法安全确认前面几条消息和图片的衔接关系。"
                "请把要接着讨论的图片重新发一次，我会从那条继续。"
            ),
            "error_kind": "prior_situation_chain_invalid",
            "retryable": False,
        }
    if status == "ambiguous":
        return {
            "ok": False,
            "dispatch": "visual_context_clarification_required",
            "reply": (
                "前面的聊天里有不止一张图，我不能确定你说的是哪一张。"
                "请直接回复那张图片，或重新发一次，我会只按你指定的那张继续。"
            ),
            "error_kind": "prior_visual_context_ambiguous",
            "retryable": False,
        }
    return None


def _project_prior_private_visual_situation(
    history: list[dict],
    *,
    user_id: str,
    message: str,
    inbound_context: dict,
    assistant_display_name: object = None,
    current_visual_present: bool = False,
    current_visual_ready: bool = False,
) -> list[dict]:
    """Project all validated prior visual revisions or record a typed hold."""

    cycle_id = str(inbound_context.get("_response_cycle_id") or "").strip()
    session = str(inbound_context.get("session") or "").strip()
    if not cycle_id or not session.startswith("qq:private:"):
        return list(history)
    scope = visual.visual_scope(channel="qq_private", thread_id=user_id)
    recent_context_external_message_ids = [
        str(item.get("external_message_id") or "").strip()
        for item in history
        if isinstance(item, Mapping)
        and str(item.get("role") or "") == "user"
        and str(item.get("external_message_id") or "").strip()
    ]
    conn = _assistant_db_connect()
    try:
        def observation_lookup(event_id: str) -> object:
            cached = visual.visual_observation_for(scope, event_id)
            if isinstance(cached, Mapping) and cached.get("status") == "ready":
                return cached
            durable = load_cycle_visual_observation(conn, event_id)
            return durable if durable is not None else cached

        try:
            selected = select_prior_visual_contexts(
                conn,
                current_cycle_id=cycle_id,
                message=message,
                reply_to_external_message_id=str(
                    inbound_context.get("reply_to_external_message_id") or ""
                ),
                observation_lookup=observation_lookup,
                recent_context_external_message_ids=recent_context_external_message_ids,
                resume_conversation=message_is_pure_assistant_address(
                    message,
                    assistant_display_name=assistant_display_name,
                ),
                current_visual_present=current_visual_present,
                current_visual_ready=current_visual_ready,
            )
        except (ConversationVisualObservationError, MigrationError) as exc:
            selected = {
                "status": "invalid",
                "reason": str(exc)[:200],
                "entries": [],
                "missing_cycle_ids": [],
                "missing_source_message_ids": [],
            }
    finally:
        conn.close()
    inbound_context["_situation_context_status"] = str(
        selected.get("status") or "none",
    )
    inbound_context["_situation_context_reason"] = str(
        selected.get("reason") or "",
    )
    lines = prior_visual_context_lines(selected)
    entries = selected.get("entries")
    if selected.get("status") != "ready" or not isinstance(entries, list) or not lines:
        return list(history)
    message_ids: list[str] = []
    cycle_ids: list[str] = []
    selections: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        for value in list(entry.get("source_message_ids") or [entry.get("source_message_id")]):
            token = str(value or "").strip()
            if token and token not in message_ids:
                message_ids.append(token)
        cycle_id = str(entry.get("cycle_id") or "").strip()
        if cycle_id and cycle_id not in cycle_ids:
            cycle_ids.append(cycle_id)
        selection = str(entry.get("selection_reason") or "").strip()
        if selection and selection not in selections:
            selections.append(selection)
    inbound_context["_situation_context_message_ids"] = message_ids
    inbound_context["_situation_context_cycle_ids"] = cycle_ids
    inbound_context["_situation_context_selection"] = ",".join(selections)
    return visual.history(list(history), lines)


def _persist_private_cycle_visual_observation(
    inbound_context: Mapping[str, object],
    visual_result: Mapping[str, object] | None,
) -> dict | None:
    """Persist one ready observation before the reply can be committed."""

    if not isinstance(visual_result, Mapping) or visual_result.get("status") != "ready":
        return None
    cycle_id = str(inbound_context.get("_response_cycle_id") or "").strip()
    if not cycle_id:
        return None
    try:
        with _assistant_db_connect() as conn:
            record_cycle_visual_observation(
                conn,
                cycle_id=cycle_id,
                observation=visual_result.get("observation"),
            )
    except (ConversationVisualObservationError, MigrationError, sqlite3.Error):
        return {
            "ok": False,
            "dispatch": "visual_context_persistence_required",
            "reply": (
                "这张图已经读取，但这次会话的视觉记录没有安全保存。"
                "请重新发一次，我再继续；这次不会假装已经记住。"
            ),
            "error_kind": "visual_observation_persistence_failed",
            "retryable": False,
        }
    return None


def _log_private_dispatch_stage(
    phase: str, *, cycle_id: str, started: float, previous: float,
    finished: float | None = None,
) -> float:
    """Observe a private cycle phase without persisting conversation content."""

    now = _PRIVATE_MULTIMODAL_CLOCK() if finished is None else float(finished)
    try:
        if not re.fullmatch(r"response-cycle-[0-9a-f]{32}", str(cycle_id or "")):
            return now
        allowed = {
            "activity_ready", "policy_ready", "history_ready",
            "planner_ready", "planner_returned",
        }
        if phase not in allowed:
            return now
        elapsed_ms = max(0, int((now - previous) * 1000))
        if elapsed_ms >= 1000 or phase in {"planner_ready", "planner_returned"}:
            print(
                "assistant_private_dispatch_stage "
                f"phase={phase} cycle_id={cycle_id} elapsed_ms={elapsed_ms} "
                f"total_ms={max(0, int((now - started) * 1000))}",
                flush=True,
            )
    except Exception:
        pass
    return now


def _log_private_cycle_timing(
    phase: str, *, cycle_id: str, started: float,
) -> None:
    try:
        if not re.fullmatch(r"response-cycle-[0-9a-f]{32}", str(cycle_id or "")):
            return
        if phase not in {"generation_marked", "delivery_queued", "error", "superseded"}:
            return
        print(
            "assistant_private_cycle_timing "
            f"phase={phase} cycle_id={cycle_id} "
            f"total_ms={max(0, int((_PRIVATE_MULTIMODAL_CLOCK() - started) * 1000))}",
            flush=True,
        )
    except Exception:
        pass


def _log_group_http_dispatch_timing(
    *, payload: dict, started: float, outcome: str,
    phase: str = "final", finished: float | None = None,
) -> None:
    try:
        trace = str(payload.get("trace_id") or "")
        if not re.fullmatch(r"[0-9a-f]{12}", trace):
            trace = "invalid"
        if phase not in {
            "receipt_ready", "delivery_gate_ready", "inbound_once",
            "memory_capture", "final",
        }:
            phase = "invalid"
        if outcome not in {"ok", "rejected", "conflict", "internal"}:
            outcome = "invalid"
        now = time.monotonic() if finished is None else float(finished)
        print(
            "assistant_group_http_timing "
            f"trace={trace} phase={phase} outcome={outcome} "
            f"elapsed_ms={max(0, int((now - started) * 1000))}",
            flush=True,
        )
    except Exception:
        pass


def _assistant_dispatch_impl(
    *,
    user_id: str,
    message: str,
    timeout: int = DISPATCH_CHAT_TIMEOUT,
    trace_id: str = "",
    force: str = "auto",
    source: str = QQ_TASK_SOURCE,
    cwd: Path | None = None,
    require_project: bool = False,
    delivery_recipient_id: str = "",
    delivery_session: str = "",
    inbound_context: dict | None = None,
) -> dict:
    user_id = (user_id or "default").strip()
    message = (message or "").strip()
    inbound_context = inbound_context if isinstance(inbound_context, dict) else {}
    execution_presence_start = inbound_context.pop("_execution_presence_start", None)
    execution_presence_replay = inbound_context.pop("_execution_presence_replay", None)
    execution_presence_milestone = inbound_context.pop("_execution_presence_milestone", None)
    execution_presence_finish = inbound_context.pop("_execution_presence_finish", None)
    source = "admin" if str(source or "").strip() == "admin" else QQ_TASK_SOURCE
    if not message:
        return {"ok": False, "error": "message is required"}
    dispatch_started = _PRIVATE_MULTIMODAL_CLOCK()
    private_cycle_id = str(inbound_context.get("_response_cycle_id") or "")
    private_stage_previous = dispatch_started
    active_private_turn_runtime = False
    session = inbound_context.get("session")
    logical_turn_id = inbound_context.get("logical_turn_id")
    external_message_id = (
        inbound_context.get("_external_message_id")
        or inbound_context.get("external_message_id")
    )
    if (
        source == QQ_TASK_SOURCE
        and isinstance(session, str)
        and session.startswith("qq:private:")
        and isinstance(logical_turn_id, str)
        and bool(logical_turn_id.strip())
        and isinstance(external_message_id, str)
        and bool(external_message_id.strip())
    ):
        initial_sources = [
            item for item in list(inbound_context.get("source_message_ids") or [])
            if isinstance(item, str) and item.strip()
        ]
        if not initial_sources:
            initial_sources = [external_message_id.strip()]
        initial_components = [
            item for item in list(inbound_context.get("message_components") or [])
            if isinstance(item, dict)
        ]
        active_private_turn_runtime = _record_active_private_turn_dispatch(
            inbound_context,
            dispatch_started=dispatch_started,
            initial_source_count=len(initial_sources),
            initial_component_count=len(initial_components),
            initial_visual_count=sum(
                1 for item in initial_components if "visual_index" in item
            ),
            initial_explicit_visual_evidence=explicit_visual_evidence_requested(message),
        )

    with _assistant_db_connect() as conn:
        note_user_activity(conn, user_id)
    AUTOMATION_EVENT.set()
    private_stage_previous = _log_private_dispatch_stage(
        "activity_ready", cycle_id=private_cycle_id,
        started=dispatch_started, previous=private_stage_previous,
    )

    network_control = _dispatch_network_policy_control(
        user_id=user_id,
        message=message,
        trace_id=trace_id,
        source=source,
    )
    if network_control is not None:
        return network_control

    formal_enabled = formal_feature_enabled(_assistant_db_connect)
    formal_decision = decide_formal_message(
        _assistant_db_connect,
        _db_connect,
        user_id=user_id,
        message=message,
        trace_id=trace_id,
        decision_applied=FORMAL_APPROVAL_CALLBACK,
        before_apply=lambda: _admit_current_response_cycle_effect("formal_approval_decision"),
    )
    if formal_decision is not None:
        return formal_decision

    approved = None if formal_enabled else consume_legacy_pending(
        _assistant_db_connect,
        user_id,
        message,
        now=_utc_now(),
        before_apply=lambda: _admit_current_response_cycle_effect("legacy_approval_decision"),
    )
    if approved:
        message = str(approved.get("message") or "").strip()
        force = "task"
    private_stage_previous = _log_private_dispatch_stage(
        "policy_ready", cycle_id=private_cycle_id,
        started=dispatch_started, previous=private_stage_previous,
    )

    ordered_turn = build_ordered_turn_view(message, inbound_context)
    private_visual_turn = (
        source == QQ_TASK_SOURCE
        and _private_visual_turn_present(inbound_context, ordered_turn)
    )
    settings = _assistant_settings(include_secrets=True) if private_visual_turn else None
    explicit_visual_evidence = (
        private_visual_turn and explicit_visual_evidence_requested(message)
    )
    visual_timeout_seconds = (
        _PRIVATE_MULTIMODAL_EVIDENCE_VISUAL_SECONDS
        if explicit_visual_evidence
        else _PRIVATE_MULTIMODAL_DAILY_VISUAL_SECONDS
    )
    private_visual_deadline = (
        dispatch_started
        + (
            _PRIVATE_MULTIMODAL_EVIDENCE_DEADLINE_SECONDS
            if explicit_visual_evidence
            else _PRIVATE_MULTIMODAL_DAILY_DEADLINE_SECONDS
        )
        if private_visual_turn
        else None
    )
    visual_started = None
    visual_finished = None
    visual_handle = None
    visual_result = None
    visual_completion_checked = False
    visual_persistence_terminal = None
    if private_visual_turn:
        visual_started = _PRIVATE_MULTIMODAL_CLOCK()
        visual_provider_timeout = remaining_timeout(
            visual_timeout_seconds,
            private_visual_deadline,
            _PRIVATE_MULTIMODAL_CLOCK,
        )
        visual_model_authority = (
            visual_provider_timeout > 0
            and (
                not active_private_turn_runtime
                or _reserve_active_private_turn_visual_call(inbound_context)
            )
        )
        if visual_model_authority:
            visual_handle = visual.begin_qq_visual_turn(
                inbound_context,
                "qq_private",
                user_id,
                _private_visual_event_id(inbound_context, trace_id),
                message,
                settings,
                allow_model=True,
                timeout_seconds=visual_provider_timeout,
            )
        else:
            visual.begin_qq_visual_turn(
                inbound_context,
                "qq_private",
                user_id,
                _private_visual_event_id(inbound_context, trace_id),
                message,
                settings,
                allow_model=False,
                timeout_seconds=visual_timeout_seconds,
            )
            visual_result = _private_visual_deadline_result(inbound_context)
            visual_finished = _PRIVATE_MULTIMODAL_CLOCK()

    def _finish_private_visual_before_response() -> dict | None:
        nonlocal visual_completion_checked
        nonlocal visual_finished
        nonlocal visual_handle
        nonlocal visual_persistence_terminal
        nonlocal visual_result

        if visual_completion_checked:
            return visual_persistence_terminal
        visual_completion_checked = True
        if active_private_turn_runtime:
            _mark_active_private_turn_join_ready(inbound_context)
        if visual_handle is not None:
            visual_wait_timeout = remaining_timeout(
                visual_timeout_seconds,
                private_visual_deadline,
                _PRIVATE_MULTIMODAL_CLOCK,
            )
            if visual_wait_timeout > 0:
                visual_result = visual.finish_qq_visual_turn(
                    visual_handle,
                    inbound_context,
                    wait_timeout_seconds=visual_wait_timeout,
                )
            else:
                visual_result = _private_visual_deadline_result(inbound_context)
            visual_finished = _PRIVATE_MULTIMODAL_CLOCK()
            visual_handle = None
        visual_persistence_terminal = _persist_private_cycle_visual_observation(
            inbound_context,
            visual_result,
        )
        return visual_persistence_terminal

    def _response_after_private_visual(result: dict) -> dict:
        terminal = _finish_private_visual_before_response()
        return terminal if terminal is not None else result

    followup_channel, followup_conversation_ref = followup_scope(source, user_id, delivery_recipient_id)
    with ASSISTANT_LOCK:
        history = followup_history(
            inbound_context, _conversation_history, followup_conversation_ref,
            followup_channel, ASSISTANT_HISTORY_LIMIT,
        )
    current_source_ids = {
        str(item or "").strip()
        for item in list(inbound_context.get("source_message_ids") or [])
        if str(item or "").strip()
    }
    current_external_id = str(inbound_context.get("_external_message_id") or "").strip()
    if current_external_id:
        current_source_ids.add(current_external_id)
    if current_source_ids:
        # Fast ingress has already written the current raw messages.  Keep
        # them in durable history, but do not inject them twice into this
        # provider request: the exact frozen source set is represented by
        # ``message`` below.
        history = [
            item for item in history
            if str(item.get("external_message_id") or "").strip() not in current_source_ids
        ]
    if source == QQ_TASK_SOURCE and not private_visual_turn:
        if settings is None:
            settings = _assistant_settings(include_secrets=True)
        history = _project_prior_private_visual_situation(
            history,
            user_id=user_id,
            message=message,
            inbound_context=inbound_context,
            assistant_display_name=(
                settings.get("display_name")
                if isinstance(settings, Mapping)
                else None
            ),
        )
        situation_terminal = _private_situation_context_recovery_terminal(
            inbound_context,
        )
        if situation_terminal is not None:
            return _response_after_private_visual(situation_terminal)
    private_stage_previous = _log_private_dispatch_stage(
        "history_ready", cycle_id=private_cycle_id,
        started=dispatch_started, previous=private_stage_previous,
    )
    action_followup = dispatch_action_followup_context(_assistant_db_connect, CONTINUITY_KERNEL, {
        "user_id": user_id, "source": source, "trace_id": trace_id, "message": message,
        "inbound_context": inbound_context, "delivery_recipient_id": delivery_recipient_id,
    })
    if action_followup is not None:
        return _response_after_private_visual(action_followup)
    if settings is None:
        settings = _assistant_settings(include_secrets=True)
    settings = _with_model_session_scope(settings, user_id)
    media_retry = None if private_visual_turn else inbound_media_retry_notice(
        message,
        history,
        vision_settings=_settings_for_model_role("vision_caption", settings),
    )
    if media_retry is not None:
        _record_conversation(followup_conversation_ref, "user", message, source=followup_channel)
        _record_conversation(
            followup_conversation_ref,
            "assistant",
            str(media_retry["reply"]),
            source=followup_channel,
        )
        return _response_after_private_visual(media_retry)

    routed, route_decision = dispatch_deterministic_route(
        assistant_connect=_assistant_db_connect, store=INTERACTION_STORE, actor_id=user_id,
        message=message, history=history, trace_id=trace_id, source=source,
        inbound_context=inbound_context, automation_preflight=_automation_execution_preflight,
        resolve_automation_target=_resolve_automation_conversation_target,
        get_fallback=lambda: dict(settings),
        get_role_settings=_settings_for_model_role, readiness_check=_assistant_provider_ready,
        action_commitments=ACTION_COMMITMENTS,
        before_effect=lambda: _admit_current_response_cycle_effect("deterministic_route"),
    )
    if routed is not None:
        if routed.get("intent") == "automation":
            AUTOMATION_EVENT.set()
        return _response_after_private_visual(routed)

    work_followup = None
    failure_recovery_user_message = ""
    followup_kind = classify_goal_followup(message)
    failure_followup_hint = followup_kind in {
        "explain_failure", "retry_failure", "change_method", "dismiss_failure",
    }
    if (
        not approved
        and not _new_task_requested(message)
        and (
            active_qq_task(TASKS, TASK_LOCK, QQ_TASK_SOURCE, user_id) is None
            or failure_followup_hint
        )
    ):
        work_followup = load_goal_followup(
            _db_connect, actor_id=user_id, channel=followup_channel,
            conversation_ref=followup_conversation_ref, message=message,
            recent_context=history,
        )
    clarification = unresolved_followup_result(
        work_followup, message=message, conversation_ref=followup_conversation_ref,
        channel=followup_channel, record_conversation=_record_conversation,
    )
    if clarification:
        return _response_after_private_visual(clarification)
    resolved_failure = resolved_failure_followup_result(
        work_followup,
        message=message,
        conversation_ref=followup_conversation_ref,
        channel=followup_channel,
        record_conversation=_record_conversation,
    )
    if resolved_failure:
        return _response_after_private_visual(resolved_failure)
    if (
        work_followup
        and work_followup.get("kind") == "retry_failure"
        and work_followup.get("retained_research_valid")
    ):
        _admit_current_response_cycle_effect("task_retry")
        retried, retry_error = _retry_bound_failed_task(
            work_followup,
            user_id=user_id,
            trace_id=trace_id,
            inbound_context=inbound_context,
            execution_presence_callback=execution_presence_start,
            interaction_plan={},
        )
        if retried is None:
            reply = "之前保留的资料现在不能安全复用，暂时没有开始新的执行。请再明确说一次是否要从头重查。"
            _record_conversation(followup_conversation_ref, "user", message, source=followup_channel)
            _record_conversation(followup_conversation_ref, "assistant", reply, source=followup_channel)
            return _response_after_private_visual({
                "ok": False,
                "dispatch": "failure_recovery_blocked",
                "reply": reply,
                "error_kind": retry_error or "failure_recovery_failed",
                "continuity_resolution": work_followup,
            })
        if retried.get("idempotent_replay") and callable(execution_presence_replay):
            durable_delivery = execution_presence_replay(
                str(retried.get("id") or ""),
                task_status=str(retried.get("status") or ""),
                error_kind=str(retried.get("error_kind") or ""),
            )
            if durable_delivery is None:
                reply = (
                    "已找到同一重试请求对应的任务记录，但无法确认其开场投递状态；"
                    "本次不会重复接单，请查看任务状态。"
                )
                return _response_after_private_visual({
                    "ok": False,
                    "dispatch": "blocked",
                    "reply": reply,
                    "task": retried,
                    "error": "failure_recovery_replay_unconfirmed",
                    "error_kind": "failure_recovery_replay_unconfirmed",
                    "recovery_semantic": "retry_failure",
                    "retained_state_reused": True,
                    "research_rerun": False,
                    "target_task_id": str(work_followup.get("legacy_task_id") or ""),
                    "target_run_id": str(work_followup.get("run_id") or ""),
                    "target_goal_id": str(work_followup.get("goal_id") or ""),
                    "continuity_resolution": work_followup,
                })
            return _response_after_private_visual({
                "ok": True,
                "dispatch": "task_replay",
                "reply": "这个重试任务已在现有执行链路中；本次没有重复创建任务或开场。",
                "task": retried,
                "_existing_durable_delivery": durable_delivery,
                "recovery_semantic": "retry_failure",
                "retained_state_reused": True,
                "research_rerun": False,
                "target_task_id": str(work_followup.get("legacy_task_id") or ""),
                "target_run_id": str(work_followup.get("run_id") or ""),
                "target_goal_id": str(work_followup.get("goal_id") or ""),
                "continuity_resolution": work_followup,
            })
        execution_presence_attempted = bool(
            retried.pop("_execution_presence_attempted", False)
        )
        execution_presence_queued = bool(
            retried.pop("_execution_presence_queued", False)
        )
        if execution_presence_attempted and not execution_presence_queued:
            reply = (
                "任务已进入执行队列，但开场回复未能写入投递队列。"
                "任务会继续执行；本次不会改用另一条接单消息。"
            )
            return _response_after_private_visual({
                "ok": True,
                "dispatch": "task",
                "reply": reply,
                "task": retried,
                "error_kind": "execution_presence_enqueue_failed",
                "execution_presence_enqueue_failed": True,
                "recovery_semantic": "retry_failure",
                "retained_state_reused": True,
                "research_rerun": False,
                "target_task_id": str(work_followup.get("legacy_task_id") or ""),
                "target_run_id": str(work_followup.get("run_id") or ""),
                "target_goal_id": str(work_followup.get("goal_id") or ""),
                "continuity_resolution": work_followup,
            })
        reply = "之前查到的资料仍可核验，我会沿用这些资料，从文档整理阶段继续，不会重新查一遍。"
        _record_conversation(followup_conversation_ref, "user", message, source=followup_channel)
        _record_conversation(followup_conversation_ref, "assistant", reply, source=followup_channel)
        return _response_after_private_visual({
            "ok": True,
            "dispatch": "task",
            "reply": reply,
            "task": retried,
            "recovery_semantic": "retry_failure",
            "retained_state_reused": True,
            "research_rerun": False,
            "target_task_id": str(work_followup.get("legacy_task_id") or ""),
            "target_run_id": str(work_followup.get("run_id") or ""),
            "target_goal_id": str(work_followup.get("goal_id") or ""),
            "continuity_resolution": work_followup,
        })
    if work_followup and work_followup.get("kind") == "retry_failure":
        original_objective = str(work_followup.get("original_objective") or "").strip()
        if not original_objective:
            reply = "我找到了刚才失败的任务，但原始目标已经无法可靠恢复，所以没有贸然新建执行。请把原目标再说一遍。"
            _record_conversation(followup_conversation_ref, "user", message, source=followup_channel)
            _record_conversation(followup_conversation_ref, "assistant", reply, source=followup_channel)
            return _response_after_private_visual({
                "ok": False,
                "dispatch": "failure_recovery_blocked",
                "reply": reply,
                "error_kind": "failure_recovery_objective_unavailable",
                "continuity_resolution": work_followup,
            })
        failure_recovery_user_message = message
        message = original_objective
    if work_followup and work_followup["kind"] in {"accepted", "rejected"}:
        _admit_current_response_cycle_effect("goal_feedback")
        with _db_connect() as conn:
            feedback = record_goal_feedback(
                conn,
                work_followup["goal_id"],
                work_followup["kind"],
                message=message,
                revision_id=work_followup["revision_id"],
                run_id=work_followup["run_id"],
                artifact_id=work_followup["artifact_id"],
                actor_id=user_id,
                channel=followup_channel,
                idempotency_key=f"goal-feedback:{work_followup['goal_id']}:{trace_id or uuid.uuid4().hex}",
            )
        reply = "好，这个目标已经按你确认的版本完成。" if work_followup["kind"] == "accepted" else "收到，这个结果不采用；目标保留为待继续状态。"
        _record_conversation(followup_conversation_ref, "user", message, source=followup_channel)
        _record_conversation(followup_conversation_ref, "assistant", reply, source=followup_channel)
        return _response_after_private_visual({
            "ok": True,
            "dispatch": "goal_feedback",
            "reply": reply,
            "goal_id": work_followup["goal_id"],
            "feedback": feedback,
        })
    if work_followup:
        force = "task"

    visual_persistence_terminal = _finish_private_visual_before_response()
    if visual_persistence_terminal is not None:
        return visual_persistence_terminal

    if source == QQ_TASK_SOURCE and private_visual_turn:
        history = _project_prior_private_visual_situation(
            history,
            user_id=user_id,
            message=message,
            inbound_context=inbound_context,
            assistant_display_name=(
                settings.get("display_name")
                if isinstance(settings, Mapping)
                else None
            ),
            current_visual_present=True,
            current_visual_ready=bool(
                isinstance(visual_result, Mapping)
                and visual_result.get("status") == "ready"
            ),
        )
        situation_terminal = _private_situation_context_recovery_terminal(
            inbound_context,
        )
        if situation_terminal is not None:
            return situation_terminal

    complete_order_contract = (
        private_visual_turn and has_complete_order_contract(ordered_turn)
    )
    detected_intent = _detect_agent_intent(message)
    effectful_work_requested = message_requests_effectful_work(message)
    daily_conversation_proven = message_is_proven_daily_conversation(
        message,
        assistant_display_name=(
            settings.get("display_name")
            if isinstance(settings, Mapping)
            else None
        ),
    )
    private_situation_turn = bool(
        not private_visual_turn
        and inbound_context.get("_situation_context_status") == "ready"
    )
    active_multimodal_task = (
        active_qq_task(TASKS, TASK_LOCK, QQ_TASK_SOURCE, user_id)
        if private_visual_turn or private_situation_turn
        else None
    )
    artifact_revision_request = (
        (private_visual_turn or private_situation_turn)
        and _looks_like_delivered_artifact_revision(message)
    )
    private_multimodal_fast_path = bool(
        private_visual_turn
        and not explicit_visual_evidence
        and active_multimodal_task is None
        and not artifact_revision_request
        and daily_multimodal_fast_path_allowed(
            source=source,
            force=force,
            detected_intent=detected_intent,
            effectful_work_requested=effectful_work_requested,
            daily_conversation_proven=daily_conversation_proven,
            inbound_context=inbound_context,
            ordered_turn=ordered_turn,
        )
    )
    private_situation_fast_path = bool(
        private_situation_turn
        and not explicit_visual_evidence
        and active_multimodal_task is None
        and not artifact_revision_request
        and daily_private_situation_fast_path_allowed(
            source=source,
            force=force,
            detected_intent=detected_intent,
            effectful_work_requested=effectful_work_requested,
            daily_conversation_proven=daily_conversation_proven,
            inbound_context=inbound_context,
        )
    )
    private_classifier_fast_path = bool(
        private_multimodal_fast_path or private_situation_fast_path
    )

    reply_settings = _settings_for_model_role("conversation_reply", settings)
    attachments = (inbound_context or {}).get("attachments")
    vision_settings = None
    if isinstance(attachments, list) and attachments:
        vision_settings = _settings_for_model_role("vision_caption", settings)
    media_notice = None if complete_order_contract else inbound_media_notice(
        reply_settings,
        attachments,
        vision_settings=vision_settings,
        allow_text_fallback=bool(inbound_context.get("logical_turn_has_text")),
    )
    if media_notice is not None:
        _record_conversation(followup_conversation_ref, "user", message, source=followup_channel)
        _record_conversation(
            followup_conversation_ref,
            "assistant",
            str(media_notice["reply"]),
            source=followup_channel,
        )
        return media_notice
    if complete_order_contract:
        history = append_multimodal_turn_history(
            history,
            ordered_turn,
            (visual_result or {}).get("observation"),
        )
    elif private_visual_turn:
        visual_event_id = _private_visual_event_id(inbound_context, trace_id)
        legacy_visual_context = visual.visual_context_lines(
            visual.visual_scope(channel="qq_private", thread_id=user_id),
            visual_event_id,
        )
        history = visual.history(
            history,
            list(legacy_visual_context or []) + (
                visual.visual_capability_context(inbound_context)
                if inbound_context.get("logical_turn_has_text") else []
            ),
        )
    policy = _agent_policy(settings)
    planner_kwargs = {
        "user_id": user_id,
        "message": message,
        "settings": settings,
        "policy": policy,
        "history": history,
        "timeout": min(int(timeout or DISPATCH_CHAT_TIMEOUT), 90),
    }
    if private_visual_deadline is not None:
        allow_classifier = not private_classifier_fast_path
        planner_kwargs.update({
            "allow_classifier": allow_classifier,
            "deadline_monotonic": (
                private_visual_deadline
                - _PRIVATE_MULTIMODAL_FINAL_REPLY_RESERVE_SECONDS
                if allow_classifier
                else private_visual_deadline
            ),
            "clock": _PRIVATE_MULTIMODAL_CLOCK,
        })
    elif private_situation_fast_path:
        planner_kwargs["allow_classifier"] = False
    private_stage_previous = _log_private_dispatch_stage(
        "planner_ready", cycle_id=private_cycle_id,
        started=dispatch_started, previous=private_stage_previous,
    )
    mode_decision, mode_session = INTERACTION_PLANNER.decide(**planner_kwargs)
    private_stage_previous = _log_private_dispatch_stage(
        "planner_returned", cycle_id=private_cycle_id,
        started=dispatch_started, previous=private_stage_previous,
    )
    try:
        with _assistant_db_connect() as conn:
            mode_decision["skill_plan"] = discover_skill_plan(
                conn,
                message=message,
                intent=str(mode_decision.get("intent") or _detect_agent_intent(message)),
                capability_ids=PHASE2_CAPABILITY_CATALOG.ids(),
            )
    except sqlite3.Error:
        mode_decision["skill_plan"] = {"status": "unavailable"}
    interaction_plan_record, action_gate = gate_actions(
        INTERACTION_STORE, ASSISTANT_LOCK, user_id, message, mode_decision, source, inbound_context)
    if action_gate is not None:
        return action_gate
    delivery_revision_target = None
    interaction_plan = mode_decision.get("interaction_plan")
    interaction_actions = interaction_plan.get("actions") if isinstance(interaction_plan, dict) else []
    planner_continuation = any(
        str(item.get("type") or "") == "continue_task"
        for item in list(interaction_actions or [])
        if isinstance(item, dict)
    )
    deterministic_artifact_revision = _looks_like_delivered_artifact_revision(message)
    if source == QQ_TASK_SOURCE and (
        planner_continuation or deterministic_artifact_revision
    ):
        delivery_revision_target = ARTIFACT_RUNTIME.latest_delivered_artifact(
            user_id, followup_channel, followup_conversation_ref,
        )
        if delivery_revision_target.get("resolution") == "ambiguous":
            reply = ARTIFACT_RUNTIME.delivery_revision_clarification(delivery_revision_target)
            _record_conversation(followup_conversation_ref, "user", message, source=followup_channel)
            _record_conversation(followup_conversation_ref, "assistant", reply, source=followup_channel)
            return {
                "ok": True,
                "dispatch": "artifact_revision_clarification",
                "reply": reply,
                "interaction_plan": mode_decision.get("interaction_plan"),
                "interaction_plan_record": interaction_plan_record,
            }
        if delivery_revision_target.get("resolution") != "resolved":
            delivery_revision_target = None
    intent = str(mode_decision.get("intent") or _detect_agent_intent(message))
    criteria = _acceptance_criteria(intent, message, policy, mode_decision)
    if delivery_revision_target is None:
        light = _try_light_dispatch(
            user_id=user_id,
            message=message,
            trace_id=trace_id,
            force=force,
            mode_decision=mode_decision,
            criteria=criteria,
            source=source,
        )
        if light is not None:
            return light
    # M1: a planner-owned research goal makes the Research -> Work handoff explicit.
    research_context = None
    research_result = None
    research_goal = _planned_research_goal(mode_decision, message)
    execution_lane_decision = None
    try:
        execution_light = LightExecutor(catalog=PHASE2_CAPABILITY_CATALOG).route(message)
    except Exception:
        execution_light = None
    try:
        execution_project = _current_project()
    except Exception:
        execution_project = None
    try:
        execution_requirements = resolve_capability_requirements(
            message=message,
            mode_decision=mode_decision,
            light_route=execution_light,
            project=execution_project,
        )
        execution_lane_decision = select_execution_lane(
            requirements=execution_requirements,
            mode_decision=mode_decision,
            light_route=execution_light,
        )
    except Exception:
        execution_lane_decision = None
    _should_task = bool(delivery_revision_target) or _should_dispatch_as_task(
        message, mode_decision, force, detect_intent=_detect_agent_intent,
    )
    explicit_delegation = (
        source == QQ_TASK_SOURCE
        and delivery_revision_target is None
        and has_explicit_delegation_contract(mode_decision)
        and _should_task
    )
    resolved_execution_context = None
    execution_mode_decision = mode_decision
    execution_intent = intent
    execution_criteria = criteria
    if explicit_delegation and execution_lane_decision is not None:
        resolved_execution_context = resolve_execution_context(
            mode_decision=mode_decision,
            lane_decision=execution_lane_decision,
        )
        execution_intent = str(resolved_execution_context["intent"])
        execution_mode_decision = {
            **mode_decision,
            "mode": resolved_execution_context["mode"],
            "mode_label": "",
            "intent": execution_intent,
            "execution_lane": resolved_execution_context["execution_lane"],
            "need_tools": True,
            "reason": resolved_execution_context["reason"],
        }
        execution_criteria = _acceptance_criteria(
            execution_intent,
            message,
            policy,
            execution_mode_decision,
        )
    delegation_key = _delegation_request_idempotency_key(
        source=source,
        user_id=user_id,
        delivery_session=delivery_session,
        inbound_context=inbound_context,
        trace_id=trace_id,
    ) if explicit_delegation else ""
    def replay_existing_execution(replayed_execution: dict) -> dict:
        task_id = str(replayed_execution.get("id") or "")
        durable_delivery = None
        if callable(execution_presence_replay):
            durable_delivery = execution_presence_replay(
                task_id,
                task_status=str(replayed_execution.get("status") or ""),
                error_kind=str(replayed_execution.get("error_kind") or ""),
            )
        status = str(replayed_execution.get("status") or "")
        if status in FINAL_STATUSES:
            reply = task_terminal_text(status)
        elif durable_delivery is not None:
            reply = "这个任务已在现有执行链路中；本次没有重复创建任务或开场。"
        else:
            reply = (
                "已找到同一请求对应的任务记录，但无法确认其开场投递与安全激活状态；"
                "本次不会重复接单，请查看任务状态。"
            )
        result = {
            "ok": durable_delivery is not None,
            "dispatch": "task_replay" if durable_delivery is not None else "blocked",
            "reply": reply,
            "task": replayed_execution,
            "intent": intent,
            "mode": mode_decision.get("mode"),
            "mode_decision": mode_decision,
            "mode_session": mode_session,
            "interaction_plan": mode_decision.get("interaction_plan"),
            "interaction_plan_record": interaction_plan_record,
            "acceptance_criteria": execution_criteria,
            "resolved_execution_context": resolved_execution_context,
        }
        if durable_delivery is not None:
            result["_existing_durable_delivery"] = durable_delivery
        else:
            result.update({
                "error": "delegated_execution_replay_unconfirmed",
                "error_kind": "delegated_execution_replay_unconfirmed",
            })
        return result

    replayed_execution = _existing_execution(delegation_key)
    if replayed_execution is not None:
        return replay_existing_execution(replayed_execution)

    existing_active = None
    if not approved and not _new_task_requested(message):
        existing_active = active_qq_task(
            TASKS, TASK_LOCK, QQ_TASK_SOURCE, user_id,
        )
    risky = _requires_risky_confirmation(message)
    established_task = None
    if explicit_delegation and existing_active is None and not risky:
        if require_project and cwd is None:
            return {"ok": False, "error": "qq_project_required"}
        if work_context_routing_enabled(settings):
            cwd = resolve_work_cwd(
                message, _current_project(), executor_workspace_root(),
            )
        sandbox = _dispatch_sandbox(message, execution_intent)
        if research_goal["deliverable"] != "none" and sandbox != "workspace-write":
            sandbox = "workspace-write"
        task_timeout = _dispatch_timeout(
            message,
            sandbox,
            raw_timeout=timeout if force == "task" else None,
            work_task_timeout=WORK_TASK_TIMEOUT,
        )
        provisional_prompt = _format_dispatch_task_prompt(
            user_id=user_id,
            message=message,
            intent=execution_intent,
            criteria=execution_criteria,
            policy=policy,
            mode_decision=execution_mode_decision,
            history=history,
            continuity_target=work_followup,
            research_context=None,
            delivery_projection=None,
        )
        is_owner_task = source == "admin" or user_id in qq_super_admin_ids(
            _assistant_db_connect,
        )
        with _assistant_db_connect() as conn:
            owner_search_allowed = is_owner_task and task_web_search_allowed(conn)
        network_mode = "controlled"
        if owner_search_allowed:
            try:
                active_executor = _resolve_executor_snapshot()
            except RuntimeError:
                active_executor = {}
            if active_executor.get("adapter") == "codex_login":
                network_mode = "search"
        try:
            established_task = _establish_execution(
                prompt=provisional_prompt,
                sandbox=sandbox,
                timeout=task_timeout,
                cwd=cwd or _default_cwd(),
                source=source,
                user_id=user_id,
                trace_id=trace_id,
                origin_message=message,
                intent=execution_intent,
                mode=str(execution_mode_decision.get("mode") or ""),
                delivery_recipient_id=delivery_recipient_id,
                delivery_session=delivery_session,
                source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                follow_up_source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                request_idempotency_key=delegation_key,
                network_mode=network_mode,
            )
        except (RuntimeError, ValueError, sqlite3.Error) as exc:
            error_kind = str(exc).split(":", 1)[0] or type(exc).__name__
            reply = task_blocked_error(error_kind)
            return {
                "ok": False,
                "dispatch": "blocked",
                "error_kind": error_kind,
                "error": error_kind,
                "reply": reply,
                "output": reply,
                "intent": intent,
                "mode": mode_decision.get("mode"),
                "mode_decision": mode_decision,
                "interaction_plan": mode_decision.get("interaction_plan"),
                "interaction_plan_record": interaction_plan_record,
                "resolved_execution_context": resolved_execution_context,
            }
        if established_task.get("idempotent_replay"):
            return replay_existing_execution(established_task)
    def close_research_failure(result, *, execution_failure: bool) -> dict:
        user_message = _research_failure_text(
            result, execution_failure=execution_failure,
        )
        for leak in [
            "curl", "urllib", "MCP", "sandbox", "workspace", "provider",
            "runtime", "model memory", "Operation not permitted",
        ]:
            if leak.lower() in str(user_message).lower():
                user_message = str(user_message).replace(leak, "")
        blocks, reply = _assemble_task_status_response(
            settings,
            mode_decision.get("interaction_plan") or {},
            user_message,
            factual_type="status",
            action="blocked",
            channel_shows_identity=True,
        )
        if established_task is not None:
            if callable(execution_presence_finish):
                execution_presence_finish()
            status = str(getattr(result, "status", "") or "")
            error_kind = (
                "research_blocked"
                if not execution_failure and status == "blocked"
                else "research_execution_failed"
            )
            failed_task = _fail_established_execution(
                str(established_task.get("id") or ""),
                error_kind=error_kind,
                user_message=user_message,
            )
            terminal_delivery = (
                execution_presence_replay(
                    str(failed_task.get("id") or ""),
                    task_status=str(failed_task.get("status") or ""),
                    error_kind=str(failed_task.get("error_kind") or ""),
                )
                if callable(execution_presence_replay)
                else None
            )
            failure_result = {
                "ok": True,
                "dispatch": "task",
                "reply": reply,
                "content_blocks": blocks,
                "task": failed_task,
                "capability_id": RESEARCH_CAPABILITY_ID,
                "mode": mode_decision.get("mode"),
                "intent": mode_decision.get("intent"),
                "interaction_plan": mode_decision.get("interaction_plan"),
                "interaction_plan_record": interaction_plan_record,
                "acceptance_criteria": execution_criteria,
                "research_status": status or "blocked",
                "research_reason": str(
                    getattr(result, "reason_code", "") or "research_no_source"
                ),
                "resolved_execution_context": resolved_execution_context,
            }
            if terminal_delivery is not None:
                failure_result["_existing_durable_delivery"] = terminal_delivery
            return failure_result
        with ASSISTANT_LOCK:
            INTERACTION_STORE.record_exchange(
                user_id, message, reply, mode_decision, source=source,
            )
        status = str(getattr(result, "status", "") or "")
        return {
            "ok": True,
            "dispatch": (
                "research_blocked"
                if execution_failure or status == "blocked"
                else "research_failed"
            ),
            "reply": reply,
            "content_blocks": blocks,
            "capability_id": RESEARCH_CAPABILITY_ID,
            "mode": mode_decision.get("mode"),
            "intent": mode_decision.get("intent"),
            "interaction_plan": mode_decision.get("interaction_plan"),
            "interaction_plan_record": interaction_plan_record,
            "acceptance_criteria": criteria,
            "research_status": status or "blocked",
            "research_reason": str(
                getattr(result, "reason_code", "") or "research_no_source"
            ),
        }
    if delivery_revision_target is not None:
        # This turn edits bytes already grounded and delivered; do not repeat
        # research or route it through a second revision classifier.
        research_context = None
    elif research_goal["deliverable"] != "none":
        # Consume the same server-resolved lane used by the execution context.
        try:
            _probe_lane = execution_lane_decision
            if _probe_lane is None:
                raise RuntimeError("execution_lane_unresolved")
            if _probe_lane.lane == "research":
                _admit_current_response_cycle_effect("private_research")
                _adapter = _PRIVATE_RESEARCH_ADAPTER
                _research_query = research_goal["subject"]
                _execution_failure = False
                try:
                    _r = execute_private_research(query=_research_query, original_message=message, connect=_assistant_db_connect, catalog=PHASE2_CAPABILITY_CATALOG, research_adapter=_adapter, now=lambda: datetime.now(timezone.utc), allow_local_fallback=False)
                except Exception:
                    _r = None
                    _execution_failure = True
                # One bounded recovery is allowed only after a real no-evidence result.
                # It never changes provider, planner, crawler, or execution lane.
                _retry_query = ""
                if (
                    not _execution_failure
                    and getattr(_r, "status", "") != "succeeded"
                    and getattr(_r, "reason_code", "") == "research_no_approved_sources"
                ):
                    _retry_query = _bounded_recovery_research_query(_research_query)
                if _retry_query:
                    try:
                        _r = execute_private_research(query=_retry_query, original_message=message, connect=_assistant_db_connect, catalog=PHASE2_CAPABILITY_CATALOG, research_adapter=_adapter, now=lambda: datetime.now(timezone.utc), allow_local_fallback=False)
                    except Exception:
                        _r = None
                        _execution_failure = True
                if _r is None or getattr(_r, "status", "") != "succeeded":
                    return close_research_failure(
                        _r, execution_failure=_execution_failure,
                    )
                research_result = _r
                research_context = _bounded_research_context(_r)
        except Exception:
            # A deliverable may only proceed from a succeeded ResearchResult.
            # Do not silently fall through to the generic Work lane, because it
            # would make an ungrounded artifact look like a completed research task.
            return close_research_failure(None, execution_failure=True)
    else:
        research = _try_research_dispatch(
            user_id=user_id,
            message=message,
            trace_id=trace_id,
            force=force,
            mode_decision=mode_decision,
            criteria=criteria,
            source=source,
            established_task_id=str((established_task or {}).get("id") or ""),
        )
        if research is not None:
            return research
    if research_context:
        _should_task = True
    if not _should_task:
        def active_private_turn_retry() -> dict | None:
            try:
                return _active_private_turn_retry_input(
                    transport=inbound_context,
                    user_id=user_id,
                    message=message,
                    history=history,
                    settings=settings,
                    source=source,
                    force=force,
                    initial_observation=(visual_result or {}).get("observation"),
                )
            except (RuntimeError, ValueError, sqlite3.Error):
                return None

        decision_context = {
            "history": history,
            "settings": settings,
            "policy": policy,
            "mode_decision": mode_decision,
            "mode_session": mode_session,
            "source": source,
            "inbound_context": inbound_context,
            "active_private_turn_retry": active_private_turn_retry,
        }
        reply_started = None
        if private_visual_deadline is not None:
            reply_started = _PRIVATE_MULTIMODAL_CLOCK()
            decision_context.update({
                "deadline_monotonic": private_visual_deadline,
                "clock": _PRIVATE_MULTIMODAL_CLOCK,
                "private_multimodal_truth_guard": (
                    str(mode_decision.get("mode") or "daily") == "daily"
                ),
                "visual_observation": (visual_result or {}).get("observation"),
            })
        result = _assistant_chat(
            user_id=user_id,
            message=message,
            timeout=timeout,
            decision_context=decision_context,
        )
        reply_finished = (
            _PRIVATE_MULTIMODAL_CLOCK()
            if private_visual_deadline is not None
            else None
        )
        result["dispatch"] = "chat"
        result["mode_decision"] = result.get("mode_decision") or mode_decision
        result["mode_session"] = result.get("mode_session") or mode_session
        result["interaction_plan"] = mode_decision.get("interaction_plan")
        result["interaction_plan_record"] = interaction_plan_record
        if private_situation_fast_path:
            result.update({
                "private_situation_fast_path": True,
                "classifier_bypassed": mode_decision.get("classifier_bypassed") is True,
            })
        if private_visual_deadline is not None:
            deadline_seconds = (
                _PRIVATE_MULTIMODAL_EVIDENCE_DEADLINE_SECONDS
                if explicit_visual_evidence
                else _PRIVATE_MULTIMODAL_DAILY_DEADLINE_SECONDS
            )
            result.update({
                "visual_observation_status": str(
                    (visual_result or {}).get("status") or "unavailable"
                )[:40],
                "visual_cache_hit": (
                    str((visual_result or {}).get("reason") or "")
                    == "visual_observation_cached"
                ),
                "vision_elapsed_seconds": _bounded_private_elapsed(
                    visual_started,
                    visual_finished,
                    deadline_seconds,
                ),
                "reply_elapsed_seconds": _bounded_private_elapsed(
                    reply_started,
                    reply_finished,
                    deadline_seconds,
                ),
                "dispatch_elapsed_seconds": _bounded_private_elapsed(
                    dispatch_started,
                    reply_finished,
                    deadline_seconds,
                ),
                "private_multimodal_fast_path": private_multimodal_fast_path,
                "classifier_bypassed": mode_decision.get("classifier_bypassed") is True,
            })
        return result

    if established_task is None:
        if require_project and cwd is None:
            return {"ok": False, "error": "qq_project_required"}
        if work_context_routing_enabled(settings):
            cwd = resolve_work_cwd(
                message, _current_project(), executor_workspace_root(),
            )
        sandbox = _dispatch_sandbox(message, execution_intent)
        # M1-A: deliverable document must be workspace-write even if intent is research
        if research_context and sandbox != "workspace-write":
            sandbox = "workspace-write"
        task_timeout = _dispatch_timeout(
            message,
            sandbox,
            raw_timeout=timeout if force == "task" else None,
            work_task_timeout=WORK_TASK_TIMEOUT,
        )
    delivery_projection = _research_delivery_projection(
        mode_decision,
        message,
        research_goal,
        research_result,
    )
    if not delivery_projection and delivery_revision_target is not None:
        delivery_projection = _revision_delivery_projection(delivery_revision_target)
    prompt = _format_dispatch_task_prompt(
        user_id=user_id,
        message=message,
        intent=execution_intent,
        criteria=execution_criteria,
        policy=policy,
        mode_decision=execution_mode_decision,
        history=history,
        continuity_target=work_followup,
        research_context=research_context if 'research_context' in locals() else None,
        delivery_projection=delivery_projection,
    )
    if established_task is None:
        is_owner_task = source == "admin" or user_id in qq_super_admin_ids(
            _assistant_db_connect,
        )
        with _assistant_db_connect() as conn:
            owner_search_allowed = is_owner_task and task_web_search_allowed(conn)
        network_mode = "controlled"
        if owner_search_allowed:
            try:
                active_executor = _resolve_executor_snapshot()
            except RuntimeError:
                active_executor = {}
            if active_executor.get("adapter") == "codex_login":
                network_mode = "search"
    continuity_feedback = None
    if work_followup and work_followup["kind"] == "change_method":
        _admit_current_response_cycle_effect("goal_revision")
        with _db_connect() as conn:
            continuity_feedback = {
                "new_revision": create_goal_revision(
                    conn,
                    work_followup["goal_id"],
                    message,
                    actor_id=user_id,
                    channel=followup_channel,
                    source_run_id=work_followup["run_id"],
                    parent_revision_id=work_followup["revision_id"],
                    idempotency_key=f"goal-method-change:{work_followup['goal_id']}:{trace_id or uuid.uuid4().hex}",
                ),
            }
    elif work_followup and work_followup["kind"] in {"needs_change", "corrected"}:
        _admit_current_response_cycle_effect("goal_feedback")
        with _db_connect() as conn:
            continuity_feedback = record_goal_feedback(
                conn,
                work_followup["goal_id"],
                work_followup["kind"],
                message=message,
                revision_id=work_followup["revision_id"],
                run_id=work_followup["run_id"],
                artifact_id=work_followup["artifact_id"],
                actor_id=user_id,
                channel=followup_channel,
                idempotency_key=f"goal-feedback:{work_followup['goal_id']}:{trace_id or uuid.uuid4().hex}",
            )
    if not approved and risky and (formal_enabled or not _has_explicit_authorization(message)):
        if formal_enabled:
            paused = _create_approval_task(
                prompt, sandbox, task_timeout, cwd or _default_cwd(),
                source=source, user_id=user_id, trace_id=trace_id,
                origin_message=message, intent=execution_intent,
                mode=str(execution_mode_decision.get("mode") or ""),
                delivery_recipient_id=delivery_recipient_id,
                delivery_session=delivery_session,
                source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                follow_up_source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                network_mode=network_mode,
                delivery_projection=delivery_projection,
            )
            approval, paused_task = paused["approval"], paused["task"]
            approval_code = approval["code"]
        else:
            _admit_current_response_cycle_effect("legacy_approval_create")
            approval = create_legacy_pending(
                _assistant_db_connect, user_id, message, trace_id=trace_id,
            )
            paused_task = None
            approval_code = approval["id"]
        reply = task_approval_text(approval_code)
        blocks, reply = _assemble_task_status_response(
            settings,
            mode_decision.get("interaction_plan") or {},
            reply,
            factual_type="approval",
            action="approval",
            channel_shows_identity=True,
        )
        with ASSISTANT_LOCK:
            INTERACTION_STORE.record_exchange(
                user_id,
                message,
                reply,
                mode_decision,
                source=source,
            )
        _maybe_accumulate_relationship_for_dispatch(user_id, message, source)
        return {
            "ok": True,
            "dispatch": "approval_required",
            "reply": reply,
            "content_blocks": blocks,
            "approval": approval,
            "task": paused_task,
            "intent": intent,
            "mode": mode_decision.get("mode"),
            "mode_decision": mode_decision,
            "interaction_plan": mode_decision.get("interaction_plan"),
            "interaction_plan_record": interaction_plan_record,
        }

    if established_task is None and not approved and not _new_task_requested(message):
        active = active_qq_task(TASKS, TASK_LOCK, QQ_TASK_SOURCE, user_id)
        if active:
            task = _append_task_message(str(active.get("id") or ""), message, mode_decision, trace_id=trace_id)
            if task:
                if task_expression_enabled(settings):
                    factual_reply = task_append_text(str(task.get("status") or ""))
                else:
                    factual_reply = _dispatch_append_reply(task)
                blocks, reply = _assemble_task_status_response(
                    settings,
                    mode_decision.get("interaction_plan") or {},
                    factual_reply,
                    factual_type="status",
                    action="append",
                    channel_shows_identity=True,
                )
                with ASSISTANT_LOCK:
                    INTERACTION_STORE.record_exchange(
                        user_id,
                        message,
                        reply,
                        mode_decision,
                        source=source,
                    )
                _maybe_accumulate_relationship_for_dispatch(user_id, message, source)
                return {
                    "ok": True,
                    "dispatch": "task_append",
                    "reply": reply,
                    "content_blocks": blocks,
                    "task": task,
                    "intent": intent,
                    "intent_label": _intent_label(intent),
                    "mode": mode_decision.get("mode"),
                    "mode_label": mode_decision.get("mode_label"),
                    "mode_decision": mode_decision,
                    "mode_session": mode_session,
                    "interaction_plan": mode_decision.get("interaction_plan"),
                    "interaction_plan_record": interaction_plan_record,
                }

    try:
        if delivery_revision_target is not None:
            _admit_current_response_cycle_effect("artifact_revision")
            task = ARTIFACT_RUNTIME.create_delivered_revision_task(
                delivery_revision_target,
                instruction=message,
                actor_id=user_id,
                channel=followup_channel,
                conversation_ref=followup_conversation_ref,
                delivery_session=delivery_session,
                trace_id=trace_id,
                timeout=task_timeout,
            )
            if delivery_projection:
                with TASK_LOCK:
                    real_task = TASKS.get(str(task.get("id") or ""))
                artifact_lines = _d1_artifact_prompt_lines(delivery_projection)
                if artifact_lines:
                    revised_prompt = (
                        str((real_task or task).get("prompt") or "").rstrip()
                        + "\n\n"
                        + "\n".join(artifact_lines)
                    )
                else:
                    revised_prompt = str((real_task or task).get("prompt") or "")
                if real_task is not None:
                    real_task["_delivery_projection"] = dict(delivery_projection)
                    real_task["prompt"] = revised_prompt
                task = dict(task)
                task["_delivery_projection"] = dict(delivery_projection)
                task["prompt"] = revised_prompt
        elif established_task is not None:
            task = _activate_established_execution(
                str(established_task.get("id") or ""),
                prompt=prompt,
                delivery_projection=delivery_projection,
                evidence=(
                    list(getattr(research_result, "evidence", None) or [])
                    if research_result is not None
                    else []
                ),
                execution_presence_callback=execution_presence_start,
                interaction_plan=mode_decision.get("interaction_plan") or {},
                research_verified_callback=(
                    execution_presence_milestone
                    if (
                        research_result is not None
                        and getattr(research_result, "status", "") == "succeeded"
                    )
                    else None
                ),
            )
        else:
            task = _create_task(
                prompt=prompt,
                sandbox=sandbox,
                timeout=task_timeout,
                cwd=cwd or _default_cwd(),
                source=source,
                user_id=user_id,
                trace_id=trace_id,
                origin_message=message,
                intent=execution_intent,
                mode=str(execution_mode_decision.get("mode") or ""),
                delivery_recipient_id=delivery_recipient_id,
                delivery_session=delivery_session,
                source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                follow_up_source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                request_idempotency_key=delegation_key,
                network_mode=network_mode,
                delivery_projection=delivery_projection,
            )
    except (RuntimeError, ValueError) as exc:
        error_kind = str(exc).split(":", 1)[0] or type(exc).__name__
        detail = _user_error_message(error_kind)
        if established_task is not None:
            if callable(execution_presence_finish):
                execution_presence_finish()
            failed_task = _fail_established_execution(
                str(established_task.get("id") or ""),
                error_kind="execution_activation_failed",
                user_message=task_blocked_error(error_kind),
            )
            terminal_delivery = (
                execution_presence_replay(
                    str(failed_task.get("id") or ""),
                    task_status=str(failed_task.get("status") or ""),
                    error_kind=str(failed_task.get("error_kind") or ""),
                )
                if callable(execution_presence_replay)
                else None
            )
            failure_result = {
                "ok": True,
                "dispatch": "task",
                "task": failed_task,
                "error_kind": "execution_activation_failed",
                "reply": task_blocked_error(error_kind),
                "intent": intent,
                "mode": mode_decision.get("mode"),
                "mode_decision": mode_decision,
                "interaction_plan": mode_decision.get("interaction_plan"),
                "interaction_plan_record": interaction_plan_record,
            }
            if terminal_delivery is not None:
                failure_result["_existing_durable_delivery"] = terminal_delivery
            return failure_result
        if task_expression_enabled(settings):
            frame = persona_frame(settings, action="blocked", channel_shows_identity=True)
            reply = task_blocked_error(error_kind)
            if frame:
                reply = f"{frame}\n{reply}"
        else:
            reply = detail
        return {
            "ok": False,
            "dispatch": "blocked",
            "error_kind": error_kind,
            "error": error_kind,
            "error_detail": detail,
            "reply": reply,
            "output": reply,
            "intent": intent,
            "mode": mode_decision.get("mode"),
            "mode_decision": mode_decision,
            "interaction_plan": mode_decision.get("interaction_plan"),
            "interaction_plan_record": interaction_plan_record,
        }
    execution_presence_attempted = bool(
        task.pop("_execution_presence_attempted", False)
        if isinstance(task, dict)
        else False
    )
    execution_presence_queued = bool(
        task.pop("_execution_presence_queued", False)
        if isinstance(task, dict)
        else False
    )
    if execution_presence_attempted and not execution_presence_queued:
        reply = (
            "任务已进入执行队列，但开场回复未能写入投递队列。"
            "任务会继续执行；本次不会改用另一条接单消息。"
        )
        return {
            "ok": True,
            "dispatch": "task",
            "reply": reply,
            "task": task,
            "error_kind": "execution_presence_enqueue_failed",
            "execution_presence_enqueue_failed": True,
            "intent": intent,
            "mode": mode_decision.get("mode"),
            "mode_decision": mode_decision,
            "interaction_plan": mode_decision.get("interaction_plan"),
            "interaction_plan_record": interaction_plan_record,
        }

    expression_event = ""
    expression_slots = None
    if task_expression_enabled(settings) and delivery_revision_target is not None:
        requested_change = " ".join(str(message or "").split())[:600] or "按当前请求修订"
        expression_event = REVISION_STARTED
        expression_slots = {
            "source_version": "当前已交付版本",
            "requested_change": requested_change,
            "preserved_scope": "原版本保持不变",
            "revision_established": True,
        }
        factual_reply = (
            "修订来源：当前已交付版本。\n"
            f"本次修改：{requested_change}。\n"
            "保留范围：原版本保持不变。\n"
            "修订执行已经开始。"
        )
    elif task_expression_enabled(settings):
        if failure_recovery_user_message:
            accepted_scope = "之前保留的资料不能安全复用；重新查资料并重新整理文档"
        elif work_followup and work_followup.get("kind") == "change_method":
            accepted_scope = "沿着同一个目标换一种方法继续"
        else:
            accepted_scope = " ".join(str(message or "").split())[:240] or "当前请求"
        expression_event = TASK_ACCEPTED
        expression_slots = {
            "execution_established": True,
            "accepted_scope": accepted_scope,
        }
        factual_reply = f"已接受范围：{accepted_scope}。执行链路已经建立。"
    elif failure_recovery_user_message:
        factual_reply = "之前保留的资料已经不能安全复用；这次会重新查资料，再重新整理文档。"
    elif work_followup and work_followup.get("kind") == "change_method":
        factual_reply = "我会沿着同一个目标换一种方法继续，不会把它另开成无关任务。"
    else:
        factual_reply = _dispatch_task_reply(task, mode_decision)
    expression_action = (
        "revision_started"
        if delivery_revision_target is not None and task_expression_enabled(settings)
        else "accepted"
    )
    blocks, reply = _assemble_task_status_response(
        settings,
        mode_decision.get("interaction_plan") or {},
        factual_reply,
        factual_type="status",
        action=expression_action,
        event=expression_event,
        fact_slots=expression_slots,
        channel_shows_identity=True,
    )
    with ASSISTANT_LOCK:
        INTERACTION_STORE.record_exchange(
            user_id,
            failure_recovery_user_message or message,
            reply,
            mode_decision,
            source=source,
        )
    _maybe_accumulate_relationship_for_dispatch(user_id, message, source)
    return {
        "ok": True,
        "dispatch": "task",
        "reply": reply,
        "content_blocks": blocks,
        "task": task,
        "intent": intent,
        "intent_label": _intent_label(intent),
        "mode": mode_decision.get("mode"),
        "mode_label": mode_decision.get("mode_label"),
        "mode_decision": mode_decision,
        "mode_session": mode_session,
        "interaction_plan": mode_decision.get("interaction_plan"),
        "interaction_plan_record": interaction_plan_record,
        "acceptance_criteria": execution_criteria,
        "resolved_execution_context": resolved_execution_context,
        "continuity": continuity_feedback,
        **({
            "recovery_semantic": "retry_failure",
            "retained_state_reused": False,
            "research_rerun": True,
            "target_task_id": str((work_followup or {}).get("legacy_task_id") or ""),
            "target_run_id": str((work_followup or {}).get("run_id") or ""),
            "target_goal_id": str((work_followup or {}).get("goal_id") or ""),
        } if failure_recovery_user_message else {}),
        **({
            "recovery_semantic": "change_method",
            "target_task_id": str(work_followup.get("legacy_task_id") or ""),
            "target_run_id": str(work_followup.get("run_id") or ""),
            "target_goal_id": str(work_followup.get("goal_id") or ""),
        } if work_followup and work_followup.get("kind") == "change_method" else {}),
    }


def _assistant_dispatch_with_effect_hold(**kwargs) -> dict:
    try:
        return _assistant_dispatch_impl(**kwargs)
    except ResponseCycleEffectBlockedError:
        reply = (
            "前一轮操作的结果还没有核对清楚，所以这次没有执行新的任务、审批、"
            "自动化或控制操作。普通聊天仍然可以继续。"
        )
        return {
            "ok": True,
            "dispatch": "effect_hold",
            "reply": reply,
            "output": reply,
            "effect_blocked": True,
            "error_kind": "response_cycle_effects_blocked",
        }


_assistant_dispatch = CONTINUITY_KERNEL.wrap_dispatch(_assistant_dispatch_with_effect_hold)


def _execute_continuous_private_cycle(
    *,
    cycle: Mapping[str, object],
    dispatch_payload: dict,
    user_id: str,
    message: str,
    timeout: int = DISPATCH_CHAT_TIMEOUT,
    trace_id: str = "",
    force: str = "auto",
) -> dict:
    """Run one already-frozen durable cycle through the existing Outbox path."""

    cycle_started = _PRIVATE_MULTIMODAL_CLOCK()
    payload = dispatch_payload
    cycle_trace_id = str(trace_id or f"coordinator:{cycle['id']}")
    payload.update({
        "_response_cycle_id": str(cycle["id"]),
        "_response_cycle_source_set_hash": str(cycle["source_set_hash"]),
        "_response_cycle_source_message_ids": list(cycle["source_message_ids"]),
        "_response_cycle_lease_token": str(cycle["lease_token"]),
        "_response_cycle_logical_response_id": str(cycle["logical_response_id"]),
        "_response_cycle_outbox_dedupe_key": str(cycle["outbox_dedupe_key"]),
        "source_message_ids": list(cycle["source_external_message_ids"]),
    })
    try:
        with _assistant_db_connect() as conn:
            mark_response_cycle_generation_started(
                conn,
                cycle_id=str(cycle["id"]),
                lease_token=str(cycle["lease_token"]),
            )
    except Exception:
        _log_private_cycle_timing(
            "error", cycle_id=str(cycle["id"]), started=cycle_started,
        )
        raise
    _log_private_cycle_timing(
        "generation_marked", cycle_id=str(cycle["id"]), started=cycle_started,
    )
    try:
        with inbound_exchange_context(payload):
            result = _dispatch_qq_response_if_enabled(
                lambda: _assistant_dispatch(
                    user_id=user_id,
                    message=message,
                    timeout=timeout,
                    trace_id=cycle_trace_id,
                    force=force,
                    source=QQ_TASK_SOURCE,
                    cwd=None,
                    require_project=False,
                    delivery_recipient_id=user_id,
                    delivery_session=str(payload.get("session") or ""),
                    inbound_context=payload,
                ),
                payload,
                scope="private",
                response_cycle=cycle,
            )
            delivery = result.get("delivery") if isinstance(result.get("delivery"), Mapping) else {}
            delivery_id = str(delivery.get("id") or delivery.get("delivery_id") or "").strip()
            logical_response = str(
                result.get("logical_response_id") or delivery.get("logical_response_id") or ""
            ).strip()
            stored_delivery = (
                _phase2_outbox().get_delivery(delivery_id) if delivery_id else None
            ) or {}
            if delivery_id and not logical_response:
                logical_response = str(stored_delivery.get("logical_response_id") or "").strip()
            if not delivery_id or not logical_response:
                raise RuntimeError("response_cycle_delivery_not_queued")
            with _assistant_db_connect() as conn:
                bind_prepared_response_delivery(
                    conn,
                    cycle_id=str(cycle["id"]),
                    lease_token=str(cycle["lease_token"]),
                    source_set_hash=str(cycle["source_set_hash"]),
                    delivery=stored_delivery,
                )
        _log_private_cycle_timing(
            "delivery_queued", cycle_id=str(cycle["id"]), started=cycle_started,
        )
        return result
    except ResponseCycleSupersededError:
        _log_private_cycle_timing(
            "superseded", cycle_id=str(cycle["id"]), started=cycle_started,
        )
        return {
            "ok": True,
            "dispatch": "superseded_precommit",
            "should_reply": False,
            "delivery_queued": False,
            "response_cycle_id": str(cycle["id"]),
        }
    except Exception as exc:
        _log_private_cycle_timing(
            "error", cycle_id=str(cycle["id"]), started=cycle_started,
        )
        try:
            with _assistant_db_connect() as conn:
                mark_response_cycle_failure(
                    conn,
                    cycle_id=str(cycle["id"]),
                    lease_token=str(cycle["lease_token"]),
                    error=type(exc).__name__,
                )
        except (ResponseCycleLeaseError, sqlite3.Error):
            pass
        raise


def _process_continuous_private_cycle(cycle: dict, context: dict) -> None:
    result = _execute_continuous_private_cycle(
        cycle=cycle,
        dispatch_payload=context,
        user_id=str(context.get("user_id") or ""),
        message=str(context.get("message") or ""),
        timeout=DISPATCH_CHAT_TIMEOUT,
        trace_id=str(context.get("trace_id") or f"coordinator:{cycle['id']}"),
        force="auto",
    )
    if str(result.get("dispatch") or "") == "superseded_precommit":
        return
    observation = observe_private_participation(
        _assistant_db_connect, context, result,
    )
    try:
        bind_qq_response_decision(_phase2_outbox(), result, observation)
    except (sqlite3.Error, ValueError) as exc:
        print(
            "delivery_decision_bind_failed "
            f"scope=private error={type(exc).__name__}",
            flush=True,
        )


def _log_group_dispatch_timing(
    *,
    started: float,
    finished: float,
    prepare_direct_seconds: float,
    reply_seconds: float,
    complete_seconds: float,
    settle_seconds: float,
    finalize_seconds: float,
    payload: Mapping[str, object],
    result: Mapping[str, object],
) -> None:
    """Emit one body-free timing event without affecting dispatch completion."""

    try:
        event = {
            "event": "assistant_group_dispatch_timing",
            "prepare_direct_seconds": round(max(0.0, prepare_direct_seconds), 3),
            "reply_seconds": round(max(0.0, reply_seconds), 3),
            "complete_seconds": round(max(0.0, complete_seconds), 3),
            "settle_seconds": round(max(0.0, settle_seconds), 3),
            "finalize_seconds": round(max(0.0, finalize_seconds), 3),
            "total_seconds": round(max(0.0, finished - started), 3),
            "ok": bool(result.get("ok")),
            "dispatch": str(result.get("dispatch") or ""),
        }
        identity = payload.get("_group_response_identity")
        if isinstance(identity, Mapping):
            event["commitment_id"] = str(identity.get("commitment_id") or "")
            event["logical_response_id"] = str(identity.get("logical_response_id") or "")
        print(
            "assistant_group_dispatch_timing "
            + json.dumps(event, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            flush=True,
        )
    except Exception:
        pass


def _log_group_single_plan_invalid(
    model_result: Mapping[str, object],
    raw_plan: object,
    candidate_plan: Mapping[str, object],
    *,
    expected_anchor_message_id: int,
) -> None:
    """Log only response shape needed to diagnose a rejected SinglePlan."""

    try:
        candidate_anchor = candidate_plan.get("anchor_message_id")
        finish_reason = _public_model_probe_finish_reason(
            model_result.get("finish_reason"),
        ) or "unknown"
        error_kind = str(model_result.get("error_kind") or "").strip().lower()
        if error_kind not in {
            "auth", "deadline_exhausted", "empty", "http", "invalid_model",
            "network", "parse", "provider_config", "quota", "rate_limit",
            "timeout", "upstream",
        }:
            error_kind = "none" if not error_kind else "unknown"
        response_shape = str(model_result.get("response_shape") or "").strip()
        if response_shape not in {
            "payload_not_object",
            "choices_missing",
            "choices_empty",
            "choice_not_object",
            "message_missing",
            "content_missing",
            "content_text",
            "content_empty",
            "content_parts",
            "content_unsupported",
        }:
            response_shape = "unknown"
        event = {
            "event": "assistant_group_single_plan_invalid",
            "ok": bool(model_result.get("ok")),
            "error_kind": error_kind,
            "finish_reason": finish_reason,
            "reasoning_only": bool(model_result.get("reasoning_only")),
            "response_shape": response_shape,
            "output_length": len(str(raw_plan or "")),
            "parsed_decision_present": bool(
                type(candidate_anchor) is int
                or str(candidate_plan.get("model_reason") or "").strip()
                or str(candidate_plan.get("silent_reason") or "").strip()
            ),
            "anchor_valid": (
                type(candidate_anchor) is int
                and candidate_anchor == expected_anchor_message_id
            ),
        }
        print(
            "assistant_group_single_plan_invalid "
            + json.dumps(event, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            flush=True,
        )
    except Exception:
        pass


def _group_payload_freshness(payload: dict, *, now: float | None = None) -> str:
    """Check source event age again before any group planning or Outbox work."""
    value = payload.get("source_event_at")
    if value is None:
        value = payload.get("adapter_received_at")
    try:
        received_at = float(value)
    except (TypeError, ValueError, OverflowError):
        return "age_unknown"
    age = (time.time() if now is None else now) - received_at
    if not (0 <= received_at < float("inf")) or age < -60:
        return "age_unknown"
    return "stale" if age > 300 else "fresh"


def _assistant_group_dispatch_ingress(payload: dict, *, timeout: int = 120) -> dict:
    """Enforce external QQ event age without changing trusted in-process planning callers."""
    group_id = str(payload.get("group_id") or "").strip()
    freshness = _group_payload_freshness(payload)
    print(
        f"group_ingress_stage stage=bridge group_id={group_id or 'unknown'} freshness={freshness}",
        flush=True,
    )
    if freshness != "fresh":
        payload.pop("visual_media", None)
        return {"ok": True, "dispatch": "silent", "should_reply": False, "reason": f"group_ingress_{freshness}"}
    return _assistant_group_dispatch(payload, timeout=timeout)


def _assistant_group_dispatch(
    payload: dict,
    timeout: int = 120,
    *,
    _continuity_started: bool = False,
    _continuity_turn_id: str = "",
) -> dict:
    group_id = str(payload.get("group_id") or "").strip()
    sender_id = str(payload.get("sender_id") or "").strip()
    sender_name = str(payload.get("sender_name") or sender_id or "群成员").strip()
    message, is_mention = normalize_group_inbound(payload)
    if not message and isinstance(payload.get("attachments"), list) and payload["attachments"]:
        message = "（发送了一项媒体内容）"
    session = str(payload.get("session") or "").strip()
    if not group_id or not sender_id or not message:
        return {"ok": False, "error": "group_id_sender_id_and_message_required"}

    try:
        access = qq_group_access(_assistant_db_connect, sender_id, group_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not access.get("allowed"):
        observe_group_access_denied(_assistant_db_connect, payload, group_id, sender_id, is_mention, str(access.get("reason") or "group_access_denied"))
        return {
            "ok": True, "dispatch": "silent", "should_reply": False,
            "reason": access.get("reason") or "group_access_denied",
            "config_version": access.get("config_version"),
        }
    with _assistant_db_connect() as conn:
        availability = group_gate(conn, group_id)
    if not availability["allowed"]:
        payload.pop("visual_media", None)
        return {"ok": True, "dispatch": "silent", "should_reply": False, "reason": availability["reason"]}
    if not _continuity_started:
        return run_admitted_group_turn(
            CONTINUITY_KERNEL,
            payload=payload,
            group_id=group_id,
            sender_id=sender_id,
            message=message,
            timeout=timeout,
            operation=lambda turn_id: _assistant_group_dispatch(
                payload,
                timeout=timeout,
                _continuity_started=True,
                _continuity_turn_id=turn_id,
            ),
        )
    handler_started = time.monotonic()
    prepare_direct_seconds = 0.0
    reply_seconds = 0.0
    fallback_settings = _with_model_session_scope(
        _assistant_settings(include_secrets=True, integrity_scope="identity"),
        f"group:{group_id}",
    )
    ingress_started = time.monotonic()
    try:
        with _assistant_db_connect() as conn:
            capture_owner_group_expression_candidate(
                conn,
                message=message,
                owner_authorized=str(access.get("role") or "") == "super_admin",
                owner_actor_id=sender_id,
                group_id=group_id,
                thread_id=f"qq:group:{group_id}",
                source_message_id=str(payload.get("_external_message_id") or payload.get("trace_id") or ""),
            )
            prepared = prepare_group_dispatch(
                conn, payload, group_id=group_id, sender_id=sender_id,
                sender_name=sender_name, session=session, message=message,
                is_mention=is_mention,
            )
    except sqlite3.OperationalError as exc:
        code = "sqlite_busy" if "locked" in str(exc).lower() or "busy" in str(exc).lower() else "sqlite_error"
        print(f"group_voice_stage stage=ingress_transaction group_id={group_id} elapsed_ms={int((time.monotonic()-ingress_started)*1000)} status={code}", flush=True)
        raise
    print(f"group_voice_stage stage=ingress_transaction group_id={group_id} elapsed_ms={int((time.monotonic()-ingress_started)*1000)} status=ok", flush=True)
    policy, current, context_items, participation_event = (
        prepared["policy"], prepared["current"], prepared["context"], prepared["event"])
    deterministic_decision = prepared.get("deterministic_decision")
    conversation_frame = prepared.get("conversation_frame") or {}
    bare_mention = conversation_frame.get("message_kind") == "mention_only"
    natural_queue = (
        prepared["blocked"].get("natural_queue")
        if isinstance(prepared.get("blocked"), Mapping)
        else None
    )
    response_commitment = None
    response_owner_kind = "ambient" if natural_queue else "direct"
    visual_event_id = str(
        current.get("external_message_id")
        or payload.get("_external_message_id")
        or payload.get("trace_id")
        or current.get("id")
        or ""
    )
    raw_visual_present = bool(
        isinstance(payload.get("visual_media"), list)
        and payload.get("visual_media")
    )
    visual_attachment_present = any(
        isinstance(item, dict)
        and str(item.get("type") or "").strip().lower() in {"image", "video"}
        for item in list(payload.get("attachments") or [])
    )
    observation = (
        conversation_frame.get("media_observation_decision")
        or conversation_frame.get("media_observation")
    )
    if isinstance(observation, dict):
        observation = observation.get("decision")
    allow_visual_model = str(observation or "").strip().lower() == "observe"

    def commitment_block(reason: str) -> dict:
        # Reply ownership is independent of the existing bounded observation
        # worker.  A silent image may still inform one later proven follow-up.
        if isinstance(payload.get("visual_media"), list) and payload["visual_media"]:
            observe_group_visual_without_reply(
                payload, group_id=group_id, event_id=visual_event_id,
                message=message, fallback_settings=fallback_settings,
                allow_model=allow_visual_model,
            )
        return {
            "ok": True,
            "dispatch": "silent",
            "should_reply": False,
            "reason": reason,
            "group": policy,
        }

    if prepared["blocked"] is None or natural_queue:
        event_id = str(getattr(participation_event, "event_id", "") or "")
        assistant_id = str(getattr(participation_event, "assistant_id", "") or "")
        try:
            with _assistant_db_connect() as conn:
                response_commitment = claim_group_response_commitment(
                    conn,
                    event_id=event_id,
                    assistant_id=assistant_id,
                    group_id=group_id,
                    source_message_id=str(payload.get("_external_message_id") or ""),
                    source_set_hash=str(conversation_frame.get("source_set_hash") or ""),
                    situation_revision=str(conversation_frame.get("revision") or ""),
                    owner_kind=response_owner_kind,
                )
        except ValueError as exc:
            reason = (
                "group_response_binding_conflict"
                if str(exc) == "group_response_commitment_binding_conflict"
                else "group_response_commitment_unavailable"
            )
            return commitment_block(reason)
        if not response_commitment.get("acquired"):
            return commitment_block("group_response_already_owned")
        payload["_group_response_identity"] = group_response_identity(
            response_commitment,
        )
    if prepared["blocked"] and prepared["blocked"].get("natural_queue"):
        if raw_visual_present:
            if allow_visual_model:
                with _assistant_db_connect() as conn:
                    if not begin_group_response_phase(
                        conn,
                        commitment_id=str(response_commitment["id"]),
                        owner_kind=response_owner_kind,
                        phase="vision",
                    ):
                        return commitment_block("group_response_vision_already_started")
            begin_group_visual_context(
                payload,
                group_id=group_id,
                event_id=visual_event_id,
                message=message,
                fallback_settings=fallback_settings,
                allow_model=allow_visual_model,
                retain_for_queue=True,
            )
        AUTOMATION_EVENT.set()
        return prepared["blocked"]
    if prepared["blocked"]:
        if raw_visual_present:
            observe_group_visual_without_reply(
                payload,
                group_id=group_id,
                event_id=visual_event_id,
                message=message,
                fallback_settings=fallback_settings,
                allow_model=allow_visual_model,
            )
        return prepared["blocked"]

    visual_handle = None
    if raw_visual_present:
        if allow_visual_model:
            with _assistant_db_connect() as conn:
                if not begin_group_response_phase(
                    conn,
                    commitment_id=str(response_commitment["id"]),
                    owner_kind=response_owner_kind,
                    phase="vision",
                ):
                    return commitment_block("group_response_vision_already_started")
        visual_handle = begin_group_visual_context(
            payload,
            group_id=group_id,
            event_id=visual_event_id,
            message=message,
            fallback_settings=fallback_settings,
            allow_model=allow_visual_model,
        )
    if visual_handle is not None or visual_attachment_present:
        visual_context = finish_group_visual_context(
            payload,
            group_id=group_id,
            event_id=visual_event_id,
            conversation_frame=conversation_frame,
            wait_timeout_seconds=max(1, min(int(timeout or 45), 45)),
            handle=visual_handle,
        )
    else:
        visual_context = []
    if not raw_visual_present and not visual_attachment_present:
        visual_context = prior_group_visual_context(
            group_id=group_id, message=message,
            reply_to_external_message_id=str(payload.get("reply_to_external_message_id") or ""),
            conversation_frame=conversation_frame,
        )
    current, group_history = project_group_visual_context(
        current,
        context_items,
        visual_context,
        max_context=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
    )
    try:
        with _assistant_db_connect() as conn:
            response_commitment = advance_group_response_situation(
                conn,
                commitment_id=str(response_commitment["id"]),
                owner_kind=response_owner_kind,
                source_set_hash=str(conversation_frame.get("source_set_hash") or ""),
                situation_revision=str(conversation_frame.get("revision") or ""),
            )
            plan_owner = begin_group_response_phase(
                conn,
                commitment_id=str(response_commitment["id"]),
                owner_kind=response_owner_kind,
                phase="plan",
            )
    except ValueError:
        return commitment_block("group_response_situation_conflict")
    if not plan_owner:
        return commitment_block("group_response_plan_already_started")
    payload["_group_response_identity"] = group_response_identity(
        response_commitment,
    )
    if deterministic_decision is not None:
        classifier_settings = {}
        decision = {
            "should_reply": True,
            "confidence": 1.0,
            "reason": deterministic_decision.reason.value,
            "mode": "daily",
            "intent": "chat",
            "deterministic": True,
            "participation_action": deterministic_decision.action.value,
        }
    else:
        classifier_settings = _with_model_session_scope(
            _settings_for_model_role("conversation_engagement", fallback_settings),
            f"group:{group_id}",
        )
        decision_messages = build_group_decision_messages(
            policy, context_items, current, conversation_frame,
        )
        provider = str(classifier_settings.get("chat_provider") or "codex")
        if provider == "openai-compatible":
            classifier_settings = dict(classifier_settings)
            classifier_settings["chat_temperature"] = "0"
            classifier_settings["chat_max_tokens"] = str(
                STRUCTURED_SOCIAL_DECISION_MAX_TOKENS,
            )
            classifier_result = _call_openai_compatible_chat(
                classifier_settings,
                decision_messages,
                timeout=max(10, min(int(timeout or 60), 60)),
            )
        else:
            decision_prompt = "\n\n".join(item["content"] for item in decision_messages)
            classifier_result = _run_codex_assistant_chat(
                decision_prompt,
                cwd=_default_cwd(),
                timeout=max(20, min(int(timeout or 60), 90)),
                settings_override=classifier_settings,
            )
        _record_model_call(classifier_settings, classifier_result, source="group_engagement", user_id=f"group:{group_id}")
        raw_decision = classifier_result.get("reply") or classifier_result.get("output") or ""
        decision = parse_group_decision(raw_decision, is_mention=False)
        with _assistant_db_connect() as conn:
            rhythm_history = group_recent_turn_metadata(conn, group_id, 8)
        decision = apply_group_turn_policy(
            policy,
            context_items,
            current,
            decision,
            conversation_frame,
            rhythm_history=rhythm_history,
        )
        decision["classifier_ok"] = bool(classifier_result.get("ok"))
        decision["classifier_provider"] = classifier_result.get("provider") or provider
        if not classifier_result.get("ok"):
            decision.update({"should_reply": False, "reason": "group_classifier_failed"})
        if decision.get("should_reply"):
            threshold = group_participation_confidence_floor(policy)
            if float(decision.get("confidence") or 0) < threshold:
                decision.update({"should_reply": False, "reason": "participation_threshold"})

    if not decision.get("should_reply"):
        with _assistant_db_connect() as conn:
            finalize_group_shadow(
                conn, participation_event, False,
                str(decision.get("reason") or "engagement_below_threshold"),
                group_id, payload, classifier_settings,
                conversation_frame=conversation_frame,
                interaction_decision=decision,
                paired_shadow_runtime=paired_shadow_runtime_config(
                    conn,
                    assistant_id=participation_event.assistant_id,
                ),
            )
            mark_group_decision(
                conn,
                message_id=int(current["id"]),
                group_id=group_id,
                decision=decision,
                replied=False,
            )
        silent_result = {
            "ok": True,
            "dispatch": "silent",
            "should_reply": False,
            "reason": decision.get("reason") or "model_silent",
            "decision": decision,
            "group": policy,
        }
        _settle_group_response_result(payload, silent_result)
        return silent_result

    group_user_id = f"group:{group_id}"
    mode_session = None
    direct_daily_single_plan = False
    direct_work_permission = None
    if deterministic_decision is not None:
        prepare_direct_started = time.monotonic()
        direct_work_permission = group_work_allowed(policy, sender_id)
        direct = prepare_direct_group_turn(
            connect=_assistant_db_connect,
            event=participation_event,
            deterministic_decision=deterministic_decision,
            decision=decision,
            group_id=group_id,
            payload=payload,
            classifier_settings=classifier_settings,
            current=current,
            context_items=context_items,
            message=message,
            fallback_settings=fallback_settings,
            timeout=timeout,
            get_role_settings=_settings_for_model_role,
            planner=INTERACTION_PLANNER,
            agent_policy=_agent_policy,
            conversation_frame=conversation_frame,
            work_permission=direct_work_permission,
        )
        prepare_direct_seconds = time.monotonic() - prepare_direct_started
        if direct["result"] is not None:
            direct["result"]["group"] = policy
            _settle_group_response_result(payload, direct["result"])
            return direct["result"]
        decision = direct["decision"]
        mode_session = direct["mode_session"]
        group_history = direct["group_history"]
        direct_daily_single_plan = bool(direct.get("daily_single_plan"))
    group_history = visual.history(group_history, visual_context)
    if session:
        with _assistant_db_connect() as conn:
            update_qq_session(conn, group_user_id, session)
    control = dispatch_group_control_action(
        _assistant_dispatch, decision, conversation_frame, group_history,
        group_id, sender_id, message, payload, session, timeout,
        continuity_turn_id=_continuity_turn_id,
    )
    if control is not None:
        result, decision, replied = control
    else:
        decision, mode, intent, work_allowed = apply_group_work_boundary(
            decision, policy=policy, sender_id=sender_id, intent_label=_intent_label,
        )
    if control is None and direct_daily_single_plan:
        # The server admits either its positive daily contract or a non-control
        # turn whose sender explicitly lacks work permission.  The latter may
        # be answered but cannot execute work.  The model supplies a reply
        # candidate, never a mode, execution decision or action authority.
        decision.update({
            "mode": "daily",
            "mode_label": "日常聊天",
            "intent": "chat",
            "intent_label": _intent_label("chat"),
            "need_tools": False,
            "work_lifecycle": "none",
        })
        chat_settings = _with_model_session_scope(
            _settings_for_model_role("conversation_reply", fallback_settings),
            group_user_id,
        )
        if not int(policy.get("meme_enabled") or 0):
            chat_settings = dict(chat_settings)
            chat_settings["meme_daily_enabled"] = "0"
        research_context = maybe_group_research(
            group_id=group_id,
            anchor=current,
            message=message,
        )
        group_context = {
            "group_id": group_id,
            "group_name": policy.get("group_name") or payload.get("group_name") or "",
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_id": str(
                payload.get("_external_message_id") or payload.get("trace_id") or ""
            ),
            "assistant_id": str(getattr(participation_event, "assistant_id", "") or ""),
            "topic_revision": int(current.get("id") or 0),
            "observed_user_expression": dict(
                conversation_frame.get("observed_user_expression") or {}
            ),
            "is_mention": is_mention,
            "allow_group_feedback": False,
            "research": research_context,
        }
        single_plan_context = _prepare_group_single_plan_messages(
            settings=chat_settings,
            user_id=group_user_id,
            message=message,
            history=group_history,
            group=group_context,
            decision_messages=build_group_decision_messages(
                policy, context_items, current, conversation_frame,
            ),
            research_context=research_context,
            current=current,
        )
        if bare_mention:
            first_message, *remaining_messages = single_plan_context["messages"]
            single_plan_context["messages"] = [
                {
                    **first_message,
                    "content": (
                        f"{str(first_message.get('content') or '').rstrip()}\n\n"
                        "本轮只是无正文的明确 @，不是新话题、请求或工作授权。"
                        "仅接可解析引用或同一成员近期无插话对话，简短回应；"
                        "不要提出、承接或声称执行任务，mode=daily、intent=chat。"
                    ),
                },
                *remaining_messages,
            ]
        if direct_work_permission is False:
            capability_prompt = (
                "服务端能力事实：本群未对当前成员开放工作执行权限；本轮不会创建任务或执行动作，"
                "也没有动作回执。请仍按用户真实请求判断 mode/intent：执行类请求保持 work/mixed "
                "及 code/ops，不要因无权改判 daily；reply 不得声称任务已接受或动作已开始、完成、交付。"
            )
            first_message, *remaining_messages = single_plan_context["messages"]
            single_plan_context["messages"] = [
                {
                    **first_message,
                    "content": f"{str(first_message.get('content') or '').rstrip()}\n\n{capability_prompt}",
                },
                *remaining_messages,
            ]
        reply_started = time.monotonic()
        model_result = run_group_single_plan(
            chat_settings,
            single_plan_context["messages"],
            deadline_monotonic=time.monotonic() + max(1, int(timeout or 45)),
            call_openai=_call_openai_compatible_chat,
            run_codex=_run_codex_assistant_chat,
            default_cwd=_default_cwd(),
            record_model=_record_model_call,
            user_id=group_user_id,
        )
        reply_seconds = time.monotonic() - reply_started
        raw_plan = model_result.get("reply") or model_result.get("output") or ""
        candidate_plan = parse_group_single_plan(
            raw_plan,
            expected_anchor_message_id=int(current.get("id") or 0),
            parse_decision=parse_group_decision,
        ) if model_result.get("ok") else {}
        if bare_mention and candidate_plan:
            if group_single_plan_requires_work(candidate_plan):
                candidate_plan["reply"] = "我在，刚才那件事你想接着聊哪部分？"
            candidate_plan["mode"] = "daily"
            candidate_plan["intent"] = "chat"
        expected_anchor_message_id = int(current.get("id") or 0)
        candidate_anchor_message_id = candidate_plan.get("anchor_message_id")
        candidate_reply = (
            str(candidate_plan.get("reply") or "").strip()
            if type(candidate_anchor_message_id) is int
            and candidate_anchor_message_id == expected_anchor_message_id
            else ""
        )
        if not candidate_reply:
            _log_group_single_plan_invalid(
                model_result,
                raw_plan,
                candidate_plan,
                expected_anchor_message_id=expected_anchor_message_id,
            )
            decision.update({
                "should_reply": False,
                "social_action": "silent",
                "reason": "group_direct_single_plan_reply_invalid",
            })
            result = {
                **model_result,
                "ok": True,
                "reply": "",
                "output": "",
                "dispatch": "silent",
                "group_generation_invalid": True,
                "error": str(model_result.get("error") or "group_direct_single_plan_reply_invalid"),
                "error_kind": str(model_result.get("error_kind") or "group_direct_single_plan_reply_invalid"),
            }
            replied = False
        else:
            permission_guarded = (
                direct_work_permission is False
                and group_single_plan_requires_work(candidate_plan)
            )
            if permission_guarded:
                candidate_reply = (
                    "本群未对当前成员开放工作执行权限，本轮没有产生可验证的动作回执，"
                    "相关操作没有开始、完成或交付。"
                )
            decision.update({
                "single_plan_schema_version": int(
                    candidate_plan.get("single_plan_schema_version") or 1
                ),
                "reply_candidate": candidate_reply,
                "single_plan_requested_mode": str(candidate_plan.get("mode") or ""),
                "single_plan_requested_intent": str(candidate_plan.get("intent") or ""),
                "meme_intent": str(candidate_plan.get("meme_intent") or "none"),
                "memory_candidates": list(candidate_plan.get("memory_candidates") or []),
                "emotion": str(candidate_plan.get("emotion") or decision.get("emotion") or ""),
            })
            if str(candidate_plan.get("social_action") or "") == "meme_reaction":
                # The deterministic admission remains authoritative.  The
                # SinglePlan may only refine its expression form after it has
                # supplied a valid anchored reply.
                decision["social_action"] = "meme_reaction"
            recent_replies = [
                str(item.get("content") or "")
                for item in group_history[-14:]
                if str(item.get("role") or "") == "assistant"
                or str(item.get("sender_id") or "") == "bot"
            ]

            def _finalize_direct_single_plan_reply(reply_text: str, candidate: dict) -> tuple[str, dict]:
                finalized, guarded = enforce_action_truth(
                    reply_text,
                    candidate.get("action_receipts")
                    if isinstance(candidate.get("action_receipts"), list) else None,
                )
                return finalized, {
                    "action_truth_guarded": bool(guarded or permission_guarded),
                    "group_work_permission_guarded": permission_guarded,
                }

            delivery_reply, style_issues, style_metadata = group_reply_style_issues_for_delivery(
                message,
                candidate_reply,
                recent_replies=recent_replies,
                uninvited=False,
                expression_plan=(
                    single_plan_context.get("social_context") or {}
                ).get("expression_plan"),
                candidate=model_result,
                finalizer=_finalize_direct_single_plan_reply,
            )
            result = {
                **model_result,
                "reply": delivery_reply,
                "output": delivery_reply,
                "dispatch": "chat",
                "group_single_plan": True,
                "group_style_gate": "passed" if not style_issues else "degraded",
                "group_style_retry_attempted": False,
                "group_style_initial_issues": list(style_issues),
                "group_style_final_issues": list(style_issues),
                **style_metadata,
            }
            if single_plan_context.get("research_truth_context"):
                result["group_research"] = single_plan_context["research_truth_context"]
            repair_group_risky_reply(
                result, settings=chat_settings, messages=single_plan_context['messages'],
                current=current, history=context_items,
                deadline_monotonic=handler_started + max(1, int(timeout or 45)),
                call_openai=_call_openai_compatible_chat, run_codex=_run_codex_assistant_chat,
                default_cwd=_default_cwd(), record_model=_record_model_call,
                user_id=group_user_id, recent_replies=recent_replies,
                expression_plan=(single_plan_context.get('social_context') or {}).get('expression_plan'),
            )
            replied = bool(result.get("ok") and result.get("reply"))
    elif control is None and mode in {"work", "mixed"} and work_allowed:
        if not str(payload.get("_qq_cwd") or "").strip():
            return {"ok": False, "error": "qq_project_required"}
        reply_started = time.monotonic()
        result = _assistant_dispatch(
            user_id=str(payload.get("_qq_actor_id") or group_user_id),
            message=message,
            timeout=timeout,
            trace_id=str(payload.get("trace_id") or ""),
            force="task",
            cwd=Path(str(payload.get("_qq_cwd"))),
            delivery_recipient_id=group_user_id,
            delivery_session=session,
            inbound_context={
                "history": group_history,
                "attachments": list(payload.get("attachments") or []),
                "group_id": group_id,
                "sender_id": sender_id,
                "_external_message_id": str(payload.get("_external_message_id") or ""),
                "_continuity_turn_id": _continuity_turn_id,
            },
        )
        reply_seconds = time.monotonic() - reply_started
        replied = bool(result.get("reply"))
    elif control is None:
        chat_settings = dict(fallback_settings)
        if not int(policy.get("meme_enabled") or 0):
            chat_settings["meme_daily_enabled"] = "0"
        research_context = maybe_group_research(
            group_id=group_id,
            anchor=current,
            message=message,
        )
        reply_started = time.monotonic()
        result = _assistant_chat(
            user_id=group_user_id,
            message=message,
            timeout=timeout,
            decision_context={
                "history": group_history,
                "settings": chat_settings,
                "policy": _agent_policy(chat_settings),
                "mode_decision": decision,
                "mode_session": mode_session,
                "source": "qq_group",
                "raw_message": message,
                "display_message": f"{sender_name}: {message}",
                "group": {
                    "group_id": group_id,
                    "group_name": policy.get("group_name") or payload.get("group_name") or "",
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "message_id": str(
                        payload.get("_external_message_id") or payload.get("trace_id") or ""
                    ),
                    "assistant_id": str(getattr(participation_event, "assistant_id", "") or ""),
                    "topic_revision": int(current.get("id") or 0),
                    "observed_user_expression": dict(
                        conversation_frame.get("observed_user_expression") or {}
                    ),
                    "is_mention": is_mention,
                    "allow_group_feedback": False,
                    "research": research_context,
                },
                "research_context": research_context,
                "conversation_frame": conversation_frame,
            },
        )
        reply_seconds = time.monotonic() - reply_started
        result["dispatch"] = str(result.get("dispatch") or "chat")
        replied = bool(result.get("ok") and result.get("reply"))

    finalize_started = time.monotonic()
    with _assistant_db_connect() as conn:
        complete_started = time.monotonic()
        replied = complete_group_dispatch(
            conn,
            event=participation_event,
            deterministic_decision=deterministic_decision,
            decision=decision,
            group_id=group_id,
            payload=payload,
            classifier_settings=classifier_settings,
            current=current,
            result=result,
            assistant_name=str(fallback_settings.get("display_name") or "助手"),
            conversation_frame=conversation_frame,
            paired_shadow_runtime=paired_shadow_runtime_config(
                conn,
                assistant_id=str(getattr(participation_event, "assistant_id", "") or ""),
            ),
        )
        complete_seconds = time.monotonic() - complete_started
    result.update(should_reply=replied, group=policy, group_decision=decision)
    if replied and result.get("group_single_plan"):
        social_context = (
            single_plan_context.get("social_context")
            if isinstance(single_plan_context.get("social_context"), dict)
            else {}
        )
        voice_contract = social_context.get("voice_contract") if isinstance(social_context, dict) else {}
        if (manual_meme_request(message) and str(decision.get("social_action") or "") == "meme_reaction"
                and str(decision.get("meme_intent") or "") == "strong"):
            attachment_context, meme = prepare_group_meme_attachment(
                db_connect=_assistant_db_connect, settings=fallback_settings,
                policy=_agent_policy(fallback_settings), group_policy=policy,
                message=message, decision=decision, user_id=group_user_id,
                persona_meme_policy=str((voice_contract or {}).get("meme_policy_key") or "contextual"),
                selection_runtime=(select_and_reserve_meme, None, None, None),
            )
            attachment_context["decision_source"] = "model_decision"
        else:
            attachment_context, meme = choose_meme_expression(
                db_connect=_assistant_db_connect,
                settings={**fallback_settings, **chat_settings},
                policy=_agent_policy(fallback_settings), group_policy=policy,
                scope="group", message=message, reply=str(result.get("reply") or ""),
                mode=str(decision.get("mode") or "daily"),
                intent=str(decision.get("intent") or "chat"),
                user_id=group_user_id, session=session,
                persona_meme_policy=str((voice_contract or {}).get("meme_policy_key") or "contextual"),
                visual_ready=(str(current.get("message_kind") or "text") not in
                              {"attachment", "image", "mixed", "video", "audio"}
                              or str(current.get("visual_context_status") or "") == "ready"),
                deadline_monotonic=handler_started + max(1, int(timeout or 45)),
                call_openai=_call_openai_compatible_chat,
                run_codex=_run_codex_assistant_chat,
                default_cwd=_default_cwd(), record_model=_record_model_call,
                approved=bool(result.get("ok") and result.get("group_style_gate") == "passed"
                              and not result.get("group_truth_blocked")
                              and not result.get("group_safety_blocked")),
            )
            if meme:
                result["delivery_form"] = attachment_context["delivery_form"]
        result["meme_attachment"] = attachment_context
        record_meme_funnel(attachment_context, scope="group")
        if meme:
            result["meme"] = meme
    settle_started = time.monotonic()
    _settle_group_response_result(payload, result)
    settle_seconds = time.monotonic() - settle_started
    finished = time.monotonic()
    _log_group_dispatch_timing(
        started=handler_started,
        finished=finished,
        prepare_direct_seconds=prepare_direct_seconds,
        reply_seconds=reply_seconds,
        complete_seconds=complete_seconds,
        settle_seconds=settle_seconds,
        finalize_seconds=finished - finalize_started,
        payload=payload,
        result=result,
    )
    return result


def _list_tasks(limit: int = 10, status: str | None = None, offset: int = 0) -> list[dict]:
    return query_tasks(
        _db_connect, _row_to_task, _public_task, limit=limit, status=status, offset=offset,
    )


def _get_task(task_id: str) -> dict | None:
    return query_task(
        task_id, lock=TASK_LOCK, hot_tasks=TASKS, db_connect=_db_connect,
        row_to_task=_row_to_task, public_task=_public_task,
    )


def _cancel_task(task_id: str) -> dict | None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            with _db_connect() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return _public_task(_row_to_task(row), include_output=True) if row else None
        if task.get("status") in FINAL_STATUSES:
            return _public_task(task, include_output=True)
        task["cancel_requested"] = True
        proc = task.get("process")
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            _save_task_db(task)
        else:
            task["status"] = "cancelled"
            task["finished_at"] = _utc_now()
            task["ok"] = False
            task["returncode"] = 130
            task["duration"] = 0
            task["output"] = "Task cancelled."
            _append_history(task)
            _save_task_db(task)
        return _public_task(task, include_output=True)


def _retry_task(
    task_id: str,
    *,
    request_idempotency_key: str = "",
    trace_id: str = "",
    initial_evidence: list[dict] | None = None,
    delivery_projection: dict | None = None,
    execution_presence_callback=None,
    interaction_plan: Mapping[str, object] | None = None,
) -> tuple[dict | None, str]:
    def create_prepared_task(**kwargs):
        return _create_task(
            initial_evidence=initial_evidence,
            delivery_projection=delivery_projection,
            execution_presence_callback=execution_presence_callback,
            interaction_plan=interaction_plan,
            **kwargs,
        )

    return retry_task(
        task_id, lock=TASK_LOCK, hot_tasks=TASKS, db_connect=_db_connect,
        row_to_task=_row_to_task, retryable_statuses=RETRYABLE_STATUSES,
        default_cwd=DEFAULT_CWD, safe_cwd=_safe_cwd, create_task=create_prepared_task,
        request_idempotency_key=request_idempotency_key,
        trace_id=trace_id,
        prepare_prompt=ARTIFACT_RUNTIME.retry_source_prompt,
    )


def _human_bytes(value: int) -> str:
    return _human_bytes_impl(value)


def _read_meminfo() -> dict[str, int]:
    return _read_meminfo_impl()


def _short_command(
    args: list[str],
    timeout: int = 8,
    *,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            args,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=env or _command_env(),
        )
        text = (completed.stdout or completed.stderr or "").strip()
        return completed.returncode == 0, text
    except Exception as exc:
        return False, str(exc)

def _capture_command(
    args: list[str],
    timeout: int = 8,
    *,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    return _capture_command_via_broker(
        args,
        timeout,
        env=env,
        broker_required=OPS_BROKER_REQUIRED,
        broker_request=_ops_broker_request,
        command_env=_command_env,
    )

def _binary_command(args: list[str], timeout: int = 8) -> tuple[bool, bytes, str]:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            env=_command_env(),
        )
        error = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        return completed.returncode == 0, completed.stdout or b"", error
    except Exception as exc:
        return False, b"", str(exc)

def _service_status() -> dict:
    specs = [
        {"name": "codex-qq-bridge", "type": "systemd", "target": "codex-qq-bridge"},
        {"name": "docker", "type": "systemd", "target": "docker"},
        {"name": "astrbot", "type": "docker", "target": ASTRBOT_CONTAINER},
        (
            {"name": "llbot", "type": "systemd", "target": LLBOT_SERVICE}
            if QQ_ADAPTER == "llbot"
            else {"name": "napcat", "type": "docker", "target": NAPCAT_CONTAINER}
        ),
        {"name": "mihomo", "type": "docker", "target": MIHOMO_CONTAINER},
        {"name": "maim-bot-core", "type": "docker", "target": MAIM_BOT_CORE_CONTAINER},
    ]
    return collect_service_status(
        specs,
        required=OPS_BROKER_REQUIRED,
        shadow=OPS_BROKER_SHADOW,
        broker_request=_ops_broker_request,
        direct_status=_short_command,
    )

def _docker_containers() -> dict:
    return collect_containers(
        required=OPS_BROKER_REQUIRED,
        shadow=OPS_BROKER_SHADOW,
        broker_request=_ops_broker_request,
        capture_command=_capture_command,
    )

def _mihomo_api(path: str, method: str = "GET", payload: dict | None = None, timeout: int = 8) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if MIHOMO_CONTROLLER_SECRET:
        headers["Authorization"] = f"Bearer {MIHOMO_CONTROLLER_SECRET}"
    request = urllib.request.Request(
        f"{MIHOMO_CONTROLLER_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace").strip()
        return response.status, json.loads(body) if body else {}


def _proxy_node_candidates(proxies: dict, group: str) -> tuple[str, list[str]]:
    group_info = proxies.get(group) or {}
    current = str(group_info.get("now") or "")
    raw_names = group_info.get("all") or []
    skip_names = {"DIRECT", "REJECT", "HK", "JP", "SG", "TW", "US", "GLOBAL"}
    candidates = []
    for raw_name in raw_names:
        name = str(raw_name or "").strip()
        if not name or name in skip_names:
            continue
        if name.startswith("Traffic:") or name.startswith("Expire:"):
            continue
        info = proxies.get(name) or {}
        if info.get("all"):
            continue
        candidates.append(name)
    if current and current in candidates:
        candidates = [current] + [name for name in candidates if name != current]
    return current, candidates


def _proxy_groups() -> dict:
    started = time.monotonic()
    try:
        _, data = _mihomo_api("/proxies", timeout=10)
    except Exception as exc:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": f"mihomo_controller_unreachable: {exc}",
            "groups": [],
        }

    proxies = data.get("proxies") or {}
    groups = []
    for name, item in sorted(proxies.items(), key=lambda pair: pair[0].lower()):
        node_names = item.get("all") or []
        if not node_names:
            continue
        nodes = []
        for node_name in node_names:
            node = proxies.get(node_name) or {}
            nodes.append(
                {
                    "name": node_name,
                    "type": node.get("type", ""),
                    "udp": bool(node.get("udp")),
                    "history": node.get("history") or [],
                    "is_group": bool(node.get("all")),
                    "now": node.get("now", ""),
                },
            )
        groups.append(
            {
                "name": name,
                "type": item.get("type", ""),
                "now": item.get("now", ""),
                "count": len(node_names),
                "nodes": nodes,
            },
        )
    preferred = next((item for item in groups if item["name"] == "Proxies"), groups[0] if groups else None)
    return {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "controller": MIHOMO_CONTROLLER_URL,
        "proxy_url": MIHOMO_PROXY_URL,
        "preferred_group": preferred["name"] if preferred else "",
        "groups": groups,
    }


def _proxy_delay(group: str = "Proxies", names: list[str] | None = None, timeout_ms: int = 6000) -> dict:
    started = time.monotonic()
    group = (group or "Proxies").strip() or "Proxies"
    timeout_ms = max(1000, min(int(timeout_ms or 6000), 15000))
    test_url = "https://www.gstatic.com/generate_204"
    try:
        _, data = _mihomo_api("/proxies", timeout=10)
    except Exception as exc:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": f"mihomo_controller_unreachable: {exc}",
            "results": [],
        }
    proxies = data.get("proxies") or {}
    if names:
        candidates = [str(name or "").strip() for name in names if str(name or "").strip()]
    else:
        _, candidates = _proxy_node_candidates(proxies, group)
    candidates = [name for name in candidates if name in proxies][:80]
    results = concurrent_node_delays(
        _mihomo_api,
        candidates,
        test_url=test_url,
        timeout_ms=timeout_ms,
    )
    return {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "group": group,
        "url": test_url,
        "timeout_ms": timeout_ms,
        "results": results,
    }


def _proxy_config() -> dict:
    started = time.monotonic()
    try:
        _, data = _mihomo_api("/configs", timeout=8)
    except Exception as exc:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": f"mihomo_config_unreachable: {exc}",
        }
    return {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "mode": data.get("mode", ""),
        "port": data.get("port"),
        "socks_port": data.get("socks-port"),
        "mixed_port": data.get("mixed-port"),
        "allow_lan": data.get("allow-lan"),
        "log_level": data.get("log-level", ""),
        "raw": {key: data.get(key) for key in ("mode", "port", "socks-port", "mixed-port", "allow-lan", "log-level")},
    }


def _ip_probe_one(name: str, use_proxy: bool) -> dict:
    started = time.monotonic()
    targets = (
        ("ip-api", "http://ip-api.com/json/?fields=status,message,country,regionName,city,isp,org,as,query,timezone"),
        ("ipinfo", "https://ipinfo.io/json"),
        ("ipify", "https://api.ipify.org?format=json"),
    )
    env = _command_env()
    if not use_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
    last_error = ""
    for service, url in targets:
        command = ["curl", "-sS", "--connect-timeout", "6", "--max-time", "14"]
        if use_proxy:
            command.extend(["--proxy", MIHOMO_PROXY_URL])
        else:
            command.extend(["--noproxy", "*"])
        command.append(url)
        try:
            completed = subprocess.run(command, text=True, capture_output=True, timeout=18, env=env)
        except Exception as exc:
            last_error = str(exc)
            continue
        if completed.returncode != 0:
            last_error = (completed.stderr or completed.stdout or "").strip()[:240]
            continue
        try:
            data = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            last_error = "invalid ip response"
            continue
        if data.get("status") == "fail":
            last_error = str(data.get("message") or "ip api failed")
            continue
        ip = data.get("query") or data.get("ip")
        if not ip:
            last_error = "ip not found"
            continue
        region = ", ".join(
            str(part)
            for part in (data.get("country"), data.get("regionName") or data.get("region"), data.get("city"))
            if part
        )
        org = data.get("isp") or data.get("org") or data.get("as") or ""
        return {
            "ok": True,
            "name": name,
            "service": service,
            "ip": ip,
            "region": region,
            "org": org,
            "timezone": data.get("timezone", ""),
            "duration": round(time.monotonic() - started, 2),
        }
    return {
        "ok": False,
        "name": name,
        "error": last_error or "ip_probe_failed",
        "duration": round(time.monotonic() - started, 2),
    }


def _proxy_ip_check() -> dict:
    started = time.monotonic()
    direct = _ip_probe_one("direct", use_proxy=False)
    proxied = _ip_probe_one("proxy", use_proxy=True)
    return {
        "ok": bool(direct.get("ok") or proxied.get("ok")),
        "duration": round(time.monotonic() - started, 2),
        "direct": direct,
        "proxy": proxied,
    }


def _load_managed_subscription_document() -> dict:
    try:
        data = json.loads(MIHOMO_SUBSCRIPTION_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if isinstance(data, list):
        return {"management_revision": 0, "subscriptions": data}
    return data if isinstance(data, dict) else {"management_revision": 0, "subscriptions": []}


def _load_managed_subscriptions() -> list[dict]:
    items = _load_managed_subscription_document().get("subscriptions", [])
    return items if isinstance(items, list) else []


def _subscription_summary_from_config(config: dict | None = None) -> dict:
    if config is None:
        if yaml is None:
            config = {}
        else:
            try:
                config = yaml.safe_load(MIHOMO_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                config = {}
    document = _load_managed_subscription_document()
    managed = document.get("subscriptions", [])
    managed = managed if isinstance(managed, list) else []
    active = next((item for item in managed if isinstance(item, dict) and item.get("active")), None)
    enabled_count = sum(
        1 for item in managed
        if isinstance(item, dict) and bool(item.get("enabled", True))
    )
    return {
        "ok": True,
        "management_revision": int(document.get("management_revision") or 0),
        "active_key": str((active or {}).get("key") or ""),
        "managed": [
            {
                "key": item.get("key", ""),
                "name": item.get("name", ""),
                "provider": item.get("provider", ""),
                "group": item.get("group", ""),
                "format": item.get("format", "unknown"),
                "node_count": item.get("node_count"),
                "last_status": item.get("last_status", "unknown"),
                "last_error": item.get("last_error", ""),
                "enabled": bool(item.get("enabled", True)),
                "active": bool(item.get("active")),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
                "dependencies": list(dict.fromkeys([
                    *(["current_selector"] if item.get("active") else []),
                    *(
                        ["only_enabled_subscription"]
                        if bool(item.get("enabled", True)) and enabled_count <= 1
                        else []
                    ),
                ])),
            }
            for item in managed
            if isinstance(item, dict)
        ],
    }


def _proxy_subscription_request(
    action: str,
    payload: dict,
    *,
    request_id: str = "",
) -> dict:
    if yaml is None:
        return {"ok": False, "error": "pyyaml_missing"}
    args: dict = {"expected_revision": payload.get("expected_revision")}
    if action in {"create", "update"}:
        args["name"] = payload.get("name")
        args["enabled"] = payload.get("enabled")
    if action == "create":
        args["url"] = payload.get("url")
    if action == "update":
        args["subscription_key"] = payload.get("key")
        args["url_update_present"] = payload.get("url_update_present")
        if payload.get("url_update_present"):
            args["url"] = payload.get("url")
    if action in {"refresh", "switch", "enable", "disable", "delete"}:
        args["subscription_key"] = payload.get("key")
    try:
        broker = _ops_broker_write_request(
            f"proxy_subscription_{action}",
            "mihomo",
            args,
            request_id=request_id,
        )
    except OpsBrokerClientError as exc:
        return {"ok": False, "error": str(exc)}
    result = broker.get("data") if isinstance(broker.get("data"), dict) else {}
    if not broker.get("ok") or not result.get("ok"):
        error = str(
            broker.get("error")
            or result.get("error")
            or "subscription_operation_failed"
        )[:96]
        rollback_error = safe_subscription_rollback_error(result.get("rollback_error"))
        receipt = dict(result.get("receipt")) if isinstance(result.get("receipt"), dict) else {}
        if receipt:
            receipt["rollback_error"] = rollback_error
        return {
            "ok": False,
            "error": error,
            "error_kind": error,
            "current_revision": int(result.get("current_revision") or result.get("revision") or 0),
            "transaction_stage": str(result.get("transaction_stage") or "")[:64],
            "rolled_back": bool(result.get("rolled_back")),
            "rollback_error": rollback_error,
            "dependencies": [
                str(item)[:64] for item in (result.get("dependencies") or [])
            ][:12],
            "receipt": receipt,
        }
    summary = _subscription_summary_from_config()
    summary.update(result)
    return summary


def _proxy_subscription_operation(
    action: str,
    key: str,
    *,
    expected_revision: int | None = None,
    request_id: str = "",
) -> dict:
    return _proxy_subscription_request(
        action,
        {
            "key": key,
            "expected_revision": (
                int(_subscription_summary_from_config().get("management_revision") or 0)
                if expected_revision is None
                else expected_revision
            ),
        },
        request_id=request_id,
    )


def _proxy_select_request(payload: dict, *, request_id: str = "") -> dict:
    args = {
        "subscription_key": payload.get("subscription_key"),
        "node": payload.get("node"),
        "expected_revision": payload.get("expected_revision"),
    }
    try:
        broker = _ops_broker_write_request(
            "proxy_select",
            "mihomo",
            args,
            request_id=request_id,
        )
    except OpsBrokerClientError as exc:
        return {
            "ok": False,
            "changed": False,
            "error": str(exc)[:96],
            "rolled_back": False,
            "rollback_error": "",
        }
    data = broker.get("data") if isinstance(broker.get("data"), dict) else {}
    if not broker.get("ok") or not data.get("ok"):
        return {
            "ok": False,
            "changed": False,
            "error": str(broker.get("error") or data.get("error") or "proxy_select_failed")[:96],
            "rolled_back": bool(data.get("rolled_back")),
            "rollback_error": str(data.get("rollback_error") or "")[:160],
        }
    return {
        "ok": True,
        "changed": bool(data.get("changed")),
        "subscription_key": str(data.get("subscription_key") or "")[:128],
        "group": "Proxies",
        "node": str(data.get("node") or "")[:192],
        "revision": int(data.get("revision") or 0),
    }


def _proxy_select_http_status(result: dict) -> int:
    if result.get("ok"):
        return 200
    error = str(result.get("error") or "")
    if error == "subscription_revision_changed":
        return 409
    if error.startswith("args.") or error in {
        "managed_subscription_not_found",
        "managed_subscription_not_active",
        "managed_node_not_found",
        "managed_provider_invalid",
    }:
        return 422
    if (
        error.startswith("broker_")
        or error.startswith("ops_")
        or error.startswith("mihomo_")
        or error == "managed_proxy_state_invalid"
    ):
        return 503
    return 400


def _subscription_http_status(result: dict) -> int:
    if result.get("ok"):
        return 200
    error = str(result.get("error") or "")
    if error in {
        "subscription_revision_changed", "dependency_in_use",
        "subscription_already_exists", "subscription_name_conflict",
    }:
        return 409
    if error.startswith("args.") or error in {
        "args.url_invalid", "invalid_subscription_url",
        "subscription_url_credentials_forbidden", "subscription_url_port_forbidden",
        "subscription_host_not_public",
        "subscription_name_required", "subscription_name_invalid",
        "subscription_not_found", "args.expected_revision_invalid",
        "subscription_payload_empty", "subscription_payload_too_large",
        "subscription_payload_not_utf8", "unsupported_subscription_format",
        "subscription_has_no_proxies", "subscription_converter_input_invalid",
        "subscription_payload_invalid", "subscription_payload_parse_failed",
    }:
        return 422
    if result.get("rollback_error"):
        return 503
    if (
        error.startswith("broker_")
        or error.startswith("ops_")
        or error.startswith("mihomo_")
        or error in {
            "pyyaml_missing",
            "subscription_backup_prepare_failed",
            "subscription_candidate_write_failed",
            "provider_cleanup_failed",
            "subscription_state_commit_failed",
            "subscription_transaction_failed",
            "subscription_operation_failed",
            "subscription_download_failed",
            "subscription_source_tls_failed",
            "subscription_source_timeout",
            "subscription_source_connection_failed",
            "subscription_source_http_4xx",
            "subscription_source_http_5xx",
            "subscription_source_redirect_failed",
            "subscription_peer_mismatch",
            "subscription_proxy_environment_forbidden",
            "subscription_host_resolution_failed",
            "subscription_converter_config_missing",
            "subscription_converter_permission_failed",
            "subscription_converter_execution_failed",
            "subscription_converter_failed",
            "subscription_converter_output_invalid",
        }
        or str(result.get("transaction_stage") or "") in {
            "backup_prepare", "write_candidate", "mihomo_config_test",
            "mihomo_reload", "provider_cleanup", "state_commit",
        }
    ):
        return 503
    return 400


def _safe_subscription_audit_code(value: object, fallback: str = "") -> str:
    candidate = str(value or "")
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,95}", candidate):
        return candidate
    return fallback


def _safe_subscription_audit_request_id(value: object) -> str:
    candidate = str(value or "")
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}", candidate):
        return candidate
    return "unavailable"


def _audit_proxy_subscription_result(
    action: str,
    result: dict,
    http_status: int,
    request_id: str,
) -> None:
    rollback_error = safe_subscription_rollback_error(result.get("rollback_error"))
    if result.get("rolled_back"):
        rollback_status = "succeeded"
    elif rollback_error:
        rollback_status = "failed"
    else:
        rollback_status = "not_reported"
    detail = {
        "request_id": _safe_subscription_audit_request_id(request_id),
        "action": _safe_subscription_audit_code(action, "unknown"),
        "http_status": int(http_status),
        "error": _safe_subscription_audit_code(result.get("error")),
        "error_kind": _safe_subscription_audit_code(
            result.get("error_kind"),
            _safe_subscription_audit_code(result.get("error")),
        ),
        "stage": _safe_subscription_audit_code(result.get("transaction_stage")),
        "rollback": {
            "status": rollback_status,
            "error": rollback_error,
        },
    }
    am.audit(
        _assistant_db_connect,
        f"proxy_subscription_{detail['action']}",
        "success" if result.get("ok") else "failed",
        "admin",
        detail,
    )


def _curl_proxy_probe(target: dict, timeout: int = 12) -> dict:
    return proxy_target_probe(target, proxy=MIHOMO_PROXY_URL, timeout=timeout)


def _aiclient_proxy_consumer_status() -> dict:
    role_settings = _settings_for_model_role(
        "vision_caption",
        _assistant_settings(include_secrets=True),
    )
    if OPS_BROKER_REQUIRED or OPS_BROKER_SHADOW:
        try:
            result = _ops_broker_request("aiclient_proxy_status", "aiclient2api")
        except OpsBrokerClientError as exc:
            status = {
                "ok": False,
                "ready": False,
                "error": str(exc),
                "direct_allowed": False,
                "fallback_allowed": False,
            }
        else:
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            status = dict(data)
            if not result.get("ok") or not data.get("ok"):
                status.update({
                    "ok": False,
                    "ready": False,
                    "error": str(result.get("error") or data.get("error") or "aiclient_proxy_status_failed"),
                    "direct_allowed": False,
                    "fallback_allowed": False,
                })
    else:
        status = runtime_proxy_consumer_status(role_settings)
    role_binding_matches = bool(
        role_settings.get("model_registry_provider_id") == AICLIENT_PROVIDER_ID
        and role_settings.get("model_registry_id") == AICLIENT_MODEL_ID
    )
    status["role_binding_matches"] = role_binding_matches
    if not role_binding_matches:
        status["ready"] = False
        status["error"] = "vision_caption_binding_mismatch"
    return status


def _aiclient_proxy_consumer_write(action: str, args: dict | None = None, *, request_id: str = "") -> dict:
    try:
        result = _ops_broker_write_request(action, "aiclient2api", args or {}, request_id=request_id)
    except OpsBrokerClientError as exc:
        return {"ok": False, "error": str(exc), "consumer": _aiclient_proxy_consumer_status()}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if not result.get("ok") or not data.get("ok"):
        return {
            "ok": False,
            "error": str(result.get("error") or data.get("error") or "proxy_consumer_write_failed"),
            "rolled_back": bool(data.get("rolled_back")),
            "rollback_error": str(data.get("rollback_error") or "")[:160],
            "consumer": _aiclient_proxy_consumer_status(),
        }
    if action in {"aiclient_proxy_apply", "aiclient_proxy_rollback"}:
        status = _aiclient_proxy_consumer_status()
        runtime_verified = bool(data.get("runtime_verified"))
        return {
            "ok": runtime_verified,
            "applied": action == "aiclient_proxy_apply" and runtime_verified,
            "rolled_back": bool(data.get("rolled_back")),
            "rollback_error": str(data.get("rollback_error") or "")[:160],
            "runtime_verified": runtime_verified,
            "error": "" if runtime_verified else "broker_runtime_verification_missing",
            "consumer": status,
        }
    return {
        "ok": bool(data.get("ok")),
        "saved": action == "aiclient_proxy_save",
        "tested": action == "aiclient_proxy_test",
        "consumer": _aiclient_proxy_consumer_status(),
        "receipt": data.get("receipt"),
    }


def _aiclient_proxy_consumer_test(payload: dict | None = None, *, request_id: str = "") -> dict:
    if payload is not None:
        return _aiclient_proxy_consumer_write("aiclient_proxy_test", payload, request_id=request_id)
    status = _aiclient_proxy_consumer_status()
    if not status.get("ready"):
        return {"ok": False, "error": status.get("error") or "proxy_consumer_not_ready", "consumer": status}
    settings = _settings_for_model_role("vision_caption", _assistant_settings(include_secrets=True))
    red_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z"
        "TAAAAABJRU5ErkJggg=="
    )
    result = _call_openai_compatible_chat(
        settings,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Identify the dominant color. Reply with one word."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{red_png}"}},
            ],
        }],
        timeout=45,
    )
    return {
        "ok": bool(result.get("ok")),
        "error": "" if result.get("ok") else str(result.get("error_kind") or "vision_probe_failed"),
        "consumer": status,
        "vision": {
            "ok": bool(result.get("ok")),
            "provider": status["provider_id"],
            "model": status["model_id"],
            "error_kind": str(result.get("error_kind") or ""),
        },
    }


def _proxy_diagnostics(group: str = "Proxies", limit: int = 12, auto_switch: bool = False) -> dict:
    started = time.monotonic()
    group = (group or "Proxies").strip() or "Proxies"
    limit = max(1, min(int(limit or 12), 80))
    try:
        _, data = _mihomo_api("/proxies", timeout=10)
    except Exception as exc:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": f"mihomo_controller_unreachable: {exc}",
            "group": group,
            "results": [],
        }

    proxies = data.get("proxies") or {}
    if group not in proxies:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": "proxy_group_not_found",
            "group": group,
            "available_groups": sorted(name for name, item in proxies.items() if (item or {}).get("all")),
            "results": [],
        }

    current, candidates = _proxy_node_candidates(proxies, group)
    candidates = candidates[:limit]
    delay_probe = _proxy_delay(group, candidates, timeout_ms=6000)
    delay_results = delay_probe.get("results") or []
    if auto_switch:
        return {
            "ok": False,
            "error": "proxy_diagnostics_write_removed",
            "group": group,
            "results": delay_results,
        }
    return read_only_diagnostics(
        group=group,
        proxy_url=MIHOMO_PROXY_URL,
        current=current,
        candidates=candidates,
        delay_results=delay_results,
        duration=time.monotonic() - started,
    )


def _sanitize_log_text(text: str) -> str:
    return _sanitize_log_text_impl(text)


def _safe_log_text(text: str) -> str:
    return _safe_log_text_impl(text)


def _last_index(text: str, needles: tuple[str, ...]) -> int:
    return _last_index_impl(text, needles)


def _recent_matching_lines(text: str, needles: tuple[str, ...], limit: int = 8) -> list[str]:
    return _recent_matching_lines_impl(text, needles, limit, safe_log_text_fn=_safe_log_text)


def _container_env_value(container: str, name: str) -> str:
    ok, output = _capture_command(["docker", "exec", container, "printenv", name], timeout=5)
    return output.strip() if ok and output.strip() else ""


def _container_file_exists(container: str, path: str) -> bool:
    ok, _ = _short_command(["docker", "exec", container, "test", "-s", path], timeout=5)
    return ok


def _llbot_service_status() -> dict:
    if not (OPS_BROKER_REQUIRED or OPS_BROKER_SHADOW):
        ok, output = _short_command(["systemctl", "is-active", LLBOT_SERVICE], timeout=8)
        status = str(output or "unknown").strip()
        return {"ok": bool(ok and status == "active"), "status": status, "error": "" if ok else status}
    try:
        result = _ops_broker_request("service_status", LLBOT_SERVICE, {})
    except OpsBrokerClientError as exc:
        return {"ok": False, "status": "broker_unavailable", "error": str(exc)}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        "ok": bool(result.get("ok") and data.get("ok")),
        "status": str(data.get("status") or "unknown"),
        "error": str(result.get("error") or data.get("error") or ""),
    }


def _llbot_service_logs() -> tuple[bool, str]:
    return _capture_command(
        ["journalctl", "-u", LLBOT_SERVICE, "-n", "300", "--no-pager"],
        timeout=12,
    )


def _napcat_qrcode_info() -> dict:
    if OPS_BROKER_REQUIRED:
        try:
            result = _ops_broker_request("qq_qrcode_info", NAPCAT_CONTAINER, {})
        except OpsBrokerClientError as exc:
            return {"available": False, "path": "", "size": 0, "mtime": 0, "error": str(exc)}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if not result.get("ok") or not data.get("ok"):
            return {
                "available": False,
                "path": str(data.get("path") or ""),
                "size": int(data.get("size") or 0),
                "mtime": int(data.get("mtime") or 0),
                "error": str(result.get("error") or data.get("error") or "qrcode_not_ready"),
            }
        return {
            "available": True,
            "path": str(data.get("path") or NAPCAT_QRCODE_PATH),
            "size": int(data.get("size") or 0),
            "mtime": int(data.get("mtime") or 0),
            "error": "",
        }
    candidates = list(dict.fromkeys(NAPCAT_QRCODE_CANDIDATES))
    script = r"""
for p in "$@"; do
  if [ -s "$p" ]; then
    stat -c '%s %Y %n' "$p"
    exit 0
  fi
done
found="$(find /app /root /tmp -maxdepth 7 \( -iname '*qrcode*.png' -o -iname '*qr*.png' \) -type f -size +0c 2>/dev/null | head -n 1)"
if [ -n "$found" ] && [ -s "$found" ]; then
  stat -c '%s %Y %n' "$found"
  exit 0
fi
exit 1
"""
    ok, output = _capture_command(
        ["docker", "exec", NAPCAT_CONTAINER, "sh", "-lc", script, "qrcode-find", *candidates],
        timeout=8,
    )
    if not ok or not output:
        return {"available": False, "path": "", "size": 0, "mtime": 0, "error": _safe_log_text(output)}
    line = output.splitlines()[-1].strip()
    parts = line.split(maxsplit=2)
    if len(parts) < 3:
        return {"available": False, "path": "", "size": 0, "mtime": 0, "error": _safe_log_text(output)}
    try:
        size = int(parts[0])
        mtime = int(float(parts[1]))
    except ValueError:
        return {"available": False, "path": "", "size": 0, "mtime": 0, "error": _safe_log_text(output)}
    return {"available": size > 0, "path": parts[2], "size": size, "mtime": mtime, "error": ""}


def _napcat_qrcode_decode_url(logs: str) -> str:
    matches = re.findall(r"二维码解码URL:\s*(https?://\S+)", logs or "")
    return matches[-1] if matches else ""


def _llbot_qrcode_info() -> dict:
    if not (OPS_BROKER_REQUIRED or OPS_BROKER_SHADOW):
        return {"available": False, "path": "", "size": 0, "mtime": 0, "error": "broker_required"}
    try:
        result = _ops_broker_request("qq_qrcode_info", LLBOT_SERVICE, {})
    except OpsBrokerClientError as exc:
        return {"available": False, "path": "", "size": 0, "mtime": 0, "error": str(exc)}
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return {
        "available": bool(result.get("ok") and data.get("ok")),
        "path": str(data.get("path") or ""),
        "size": int(data.get("size") or 0),
        "mtime": int(data.get("mtime") or 0),
        "error": str(result.get("error") or data.get("error") or ""),
    }


def _qq_refresh_qrcode(wait_seconds: int = 25) -> dict:
    if QQ_ADAPTER == "llbot":
        previous_mtime = int(_llbot_qrcode_info().get("mtime") or 0)
        try:
            restarted = _ops_broker_write_request("service_restart", LLBOT_SERVICE, {})
        except OpsBrokerClientError as exc:
            return {"ok": False, "error": str(exc), "diagnostics": _qq_diagnostics()}
        restart_data = restarted.get("data") if isinstance(restarted.get("data"), dict) else {}
        if not restarted.get("ok") or not restart_data.get("ok"):
            return {
                "ok": False,
                "error": str(restarted.get("error") or restart_data.get("error") or "llbot_restart_failed"),
                "diagnostics": _qq_diagnostics(),
            }
        deadline = time.monotonic() + max(1, min(int(wait_seconds), 45))
        while time.monotonic() < deadline:
            diagnostics = _qq_diagnostics()
            if diagnostics.get("qq_status") == "online":
                return {"ok": True, "state": "online", "diagnostics": diagnostics}
            qrcode = _llbot_qrcode_info()
            age = max(0, int(time.time()) - int(qrcode.get("mtime") or 0))
            if (
                qrcode.get("available")
                and int(qrcode.get("mtime") or 0) >= previous_mtime
                and age <= LLBOT_QRCODE_MAX_AGE_SECONDS
            ):
                diagnostics = _qq_diagnostics()
                return {"ok": True, "state": "login_required", "diagnostics": diagnostics}
            time.sleep(1)
        return {"ok": False, "error": "llbot_login_not_ready", "diagnostics": _qq_diagnostics()}
    return refresh_napcat_qrcode(
        wait_seconds=wait_seconds,
        qrcode_info=_napcat_qrcode_info,
        restart=lambda: restart_napcat(
            broker_required=OPS_BROKER_REQUIRED, broker_write=broker_write,
            capture_command=_capture_command, container=NAPCAT_CONTAINER,
        ),
        diagnostics=_qq_diagnostics,
        safe_error=_safe_log_text,
    )


def _bridge_reachable_from_astrbot() -> dict:
    bridge_url = (_container_env_value(ASTRBOT_CONTAINER, "ASSISTANT_PLATFORM_BRIDGE_URL") or "").rstrip("/")
    return probe_bridge(
        bridge_url=bridge_url,
        required=OPS_BROKER_REQUIRED,
        broker_request=_ops_broker_request,
        capture_command=_capture_command,
        safe_log_text=_safe_log_text,
        container=ASTRBOT_CONTAINER,
    )


def _qq_diagnostics() -> dict:
    if QQ_ADAPTER == "llbot":
        return collect_llbot_diagnostics(
            assistant_connect=_assistant_db_connect,
            task_connect=_db_connect,
            service_status=_llbot_service_status,
            service_logs=_llbot_service_logs,
            bridge_probe=_bridge_reachable_from_astrbot,
            container_file_exists=_container_file_exists,
            list_events=_list_qq_events,
            astrbot_container=ASTRBOT_CONTAINER,
            qrcode_info=_llbot_qrcode_info,
            qrcode_max_age_seconds=LLBOT_QRCODE_MAX_AGE_SECONDS,
        )
    return collect_qq_diagnostics(
        assistant_connect=_assistant_db_connect,
        task_connect=_db_connect,
        capture_command=_capture_command,
        safe_log_text=_safe_log_text,
        last_index=_last_index,
        bridge_probe=_bridge_reachable_from_astrbot,
        qrcode_info=_napcat_qrcode_info,
        qrcode_decode_url=_napcat_qrcode_decode_url,
        recent_matching_lines=_recent_matching_lines,
        container_file_exists=_container_file_exists,
        list_events=_list_qq_events,
        napcat_container=NAPCAT_CONTAINER,
        astrbot_container=ASTRBOT_CONTAINER,
        qrcode_max_age_seconds=NAPCAT_QRCODE_MAX_AGE_SECONDS,
    )


def _qq_qrcode_png() -> tuple[bool, bytes, str]:
    if QQ_ADAPTER == "llbot":
        qrcode = _llbot_qrcode_info()
        if not qrcode.get("available"):
            return False, b"", str(qrcode.get("error") or "qrcode_not_found")
        age = max(0, int(time.time()) - int(qrcode.get("mtime") or 0))
        if age > LLBOT_QRCODE_MAX_AGE_SECONDS:
            return False, b"", "qrcode_stale"
        try:
            result = _ops_broker_request("qq_qrcode_png", LLBOT_SERVICE, {})
        except OpsBrokerClientError as exc:
            return False, b"", str(exc)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        encoded = str(data.get("content_base64") or "")
        if not result.get("ok") or not data.get("ok") or not encoded:
            return False, b"", str(result.get("error") or data.get("error") or "qrcode_read_failed")
        try:
            return True, base64.b64decode(encoded, validate=True), ""
        except (ValueError, TypeError):
            return False, b"", "qrcode_payload_invalid"
    qrcode = _napcat_qrcode_info()
    path = str(qrcode.get("path") or "")
    if not qrcode.get("available") or not path:
        return False, b"", qrcode.get("error") or "qrcode_not_found"
    fresh, _age_seconds = qrcode_freshness(qrcode, NAPCAT_QRCODE_MAX_AGE_SECONDS)
    if not fresh:
        return False, b"", "qrcode_stale"
    if OPS_BROKER_REQUIRED:
        try:
            result = _ops_broker_request("qq_qrcode_png", NAPCAT_CONTAINER, {})
        except OpsBrokerClientError as exc:
            return False, b"", str(exc)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        encoded = str(data.get("content_base64") or "")
        if not result.get("ok") or not data.get("ok") or not encoded:
            return False, b"", str(result.get("error") or data.get("error") or "qrcode_read_failed")
        try:
            return True, base64.b64decode(encoded, validate=True), ""
        except (ValueError, TypeError):
            return False, b"", "qrcode_payload_invalid"
    return _binary_command(["docker", "exec", NAPCAT_CONTAINER, "cat", path], timeout=6)


def _service_logs(target: str, lines: int = 120) -> dict:
    started = time.monotonic()
    target = (target or "bridge").strip().lower()
    lines = max(20, min(int(lines or 120), 300))
    commands = {
        "bridge": ["journalctl", "-u", "codex-qq-bridge", "-n", str(lines), "--no-pager"],
        "astrbot": ["docker", "logs", "--tail", str(lines), ASTRBOT_CONTAINER],
        "napcat": ["docker", "logs", "--tail", str(lines), NAPCAT_CONTAINER],
        "llbot": ["journalctl", "-u", LLBOT_SERVICE, "-n", str(lines), "--no-pager"],
        "mihomo": ["docker", "logs", "--tail", str(lines), MIHOMO_CONTAINER],
    }
    command = commands.get(target)
    if not command:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": "invalid_log_target",
        }
    ok, output = _capture_command(command, timeout=12)
    return {
        "ok": ok,
        "duration": round(time.monotonic() - started, 2),
        "target": target,
        "lines": lines,
        "output": _sanitize_log_text(output),
        # The failure path is still returned to the Owner Console, so it must
        # follow the exact same redaction boundary as the visible log output.
        "error": "" if ok else _sanitize_log_text(output),
    }


def _server_status(*, deep: bool = True) -> dict:
    return build_server_status(
        WORKSPACE_BASE,
        lambda: _executor_health_probe(),
        lambda: _codegraph_status(DEFAULT_CWD),
        include_runtime=deep,
    )


def _clean_html_text(raw: str) -> str:
    raw = re.sub(r"<[^>]+>", " ", raw or "")
    raw = html.unescape(raw)
    return " ".join(raw.split())


def _read_trending_cache(since: str) -> dict | None:
    try:
        data = json.loads(TRENDING_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    item = data.get(since) if isinstance(data, dict) else None
    if not isinstance(item, dict) or not item.get("repos"):
        return None
    return item


def _write_trending_cache(since: str, url: str, repos: list[dict]) -> None:
    try:
        try:
            data = json.loads(TRENDING_CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[since] = {
            "cached_at": _utc_now(),
            "url": url,
            "repos": repos,
        }
        TRENDING_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = TRENDING_CACHE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(TRENDING_CACHE_PATH)
    except OSError:
        pass


def _format_github_trending(
    since: str,
    repos: list[dict],
    url: str,
    started: float,
    *,
    cached: bool = False,
    cached_at: str | None = None,
    note: str | None = None,
    source_quality: str = "official",
    topic: str = "",
) -> dict:
    label = {"daily": "今日", "weekly": "本周", "monthly": "本月"}[since]
    source_label = {"official": "GitHub Trending 官方页面", "fallback": "GitHub Search API 近似结果",
                    "cache": "本地缓存"}.get(source_quality, "未知来源")
    if cached:
        source_quality = "cache"
        source_label = "本地缓存"
    topic_label = "（AI / AI Agent）" if topic in {"ai", "ai-agent"} else ""
    lines = [f"{label} GitHub 热门项目{topic_label}"]
    lines.append(f"数据来源：{source_label}")
    if cached_at:
        lines.append(f"缓存时间：{cached_at}")
    if note:
        lines.append(f"说明：{note}")
    if source_quality in {"fallback", "cache"}:
        lines.append("可信度提示：这不是严格的官方实时 Trending 榜，可作为临时参考。")
    lines.append("")
    for idx, item in enumerate(repos, start=1):
        language = item["language"] or "未标注"
        heat = f"今日新增 {item['stars_today']} stars" if item["stars_today"] else (
            f"总计 {item['stars']} stars" if item["stars"] else "热度未返回")
        description = item["description"] or "仓库未提供简介。"
        lines.extend([f"{idx}. {item['repo']}", f"   技术栈：{language}", f"   热度：{heat}",
                      f"   用途：{description}", f"   链接：{item['url']}"])
    lines.append("")
    lines.append(f"原始来源：{url}")
    return {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "cached": cached,
        "repos": repos,
        "source_url": url,
        "data_time": datetime.now(timezone.utc).isoformat(),
        "source_quality": source_quality,
        "fallback": source_quality in {"fallback", "cache"},
        "note": note or "",
        "output": "\n".join(lines),
    }


def _is_reasonable_repo_candidate(repo: str, description: str) -> bool:
    text = f"{repo} {description}".lower()
    blocked_terms = (
        "crypto miner",
        "silent miner",
        "flash usdt",
        "fake balance",
        "balance overlay",
        "wallet spoof",
        "stealer",
        "keylogger",
        "phishing",
        "malware",
    )
    return not any(term in text for term in blocked_terms)


def _github_search_fallback(
    since: str,
    limit: int,
    *,
    topic: str = "",
    exclude_repos: set[str] | None = None,
) -> tuple[dict | None, str]:
    days = {"daily": 1, "weekly": 7, "monthly": 30}[since]
    created_after = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    topic_query = "AI agent in:name,description,topics " if topic == "ai-agent" else (
        "AI in:name,description,topics " if topic == "ai" else "")
    query = f"{topic_query}created:>{created_after} fork:false"
    excluded = {str(value).strip().lower() for value in (exclude_repos or set()) if str(value).strip()}
    per_page = min(max((limit + len(excluded)) * 3, 30), 100)
    url = "https://api.github.com/search/repositories" + f"?q={quote(query, safe=':')}&sort=stars&order=desc&per_page={per_page}"
    gh_token = os.environ.get("GITHUB_TOKEN") or ""  # authenticated Search quota
    curl_args = ["curl", "-fsSL", "--http1.1", "--connect-timeout", "8", "--max-time", "20",
                 "-A", "Mozilla/5.0", "-H", "Accept: application/vnd.github+json"]
    if gh_token.strip():
        curl_args += ["-H", "Authorization: Bearer " + gh_token.strip()]
    curl_args.append(url)
    ok, body = _short_command(curl_args, timeout=25, env=_direct_command_env())
    if not ok:
        return None, body or "GitHub Search API fetch failed"

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, f"GitHub Search API parse failed: {exc}"

    repos = []
    for item in data.get("items") or []:
        repo = str(item.get("full_name") or "").strip()
        if not repo:
            continue
        if repo.lower() in excluded:
            continue
        description = str(item.get("description") or "").strip()
        if not _is_reasonable_repo_candidate(repo, description):
            continue
        stars = item.get("stargazers_count")
        repos.append({
            "repo": repo,
            "url": item.get("html_url") or f"https://github.com/{repo}",
            "description": description,
            "language": str(item.get("language") or "").strip(),
            "stars_today": "",
            "stars": f"{stars:,}" if isinstance(stars, int) else "",
        })
        if len(repos) >= limit:
            break

    if not repos:
        return None, "GitHub Search API returned no repositories"
    return {"url": url, "repos": repos}, ""


def _github_trending(
    since: str = "daily",
    limit: int = 10,
    *,
    topic: str = "",
    exclude_repos: set[str] | None = None,
) -> dict:
    started = time.monotonic()
    since = since if since in {"daily", "weekly", "monthly"} else "daily"
    topic = topic if topic in {"ai", "ai-agent"} else ""
    if topic:
        search, search_error = _github_search_fallback(since, limit, topic=topic, exclude_repos=exclude_repos)
        if search:
            return _format_github_trending(since, search["repos"], search["url"], started,
                                           note="按任务主题使用 GitHub 官方 Search API，并排除该任务历史已推送仓库。",
                                           source_quality="fallback", topic=topic)
        return {"ok": False, "duration": round(time.monotonic() - started, 2),
                "error": search_error or "GitHub topic search failed",
                "output": "GitHub AI / AI Agent 热门项目实时获取失败。"}
    url = f"https://github.com/trending?since={since}"
    ok, body = _short_command(
        ["curl", "-fsSL", "--compressed", "--http1.1", "--retry", "2",
         "--retry-delay", "2", "--connect-timeout", "8", "--max-time", "25",
         "-A", "Mozilla/5.0", url],
        timeout=35,
    )
    if not ok:
        fallback, fallback_error = _github_search_fallback(since, limit)
        if fallback:
            _write_trending_cache(since, fallback["url"], fallback["repos"])
            return _format_github_trending(since, fallback["repos"], fallback["url"], started,
                                           note="GitHub Trending 官方页面暂时访问失败，已改用近似搜索结果。",
                                           source_quality="fallback")
        cached = _read_trending_cache(since)
        if cached:
            return _format_github_trending(since, cached["repos"][:limit], cached.get("url") or url, started,
                                           cached=True, cached_at=cached.get("cached_at"),
                                           note=f"实时获取失败，已使用缓存。{fallback_error or body}",
                                           source_quality="cache")
        return {"ok": False, "duration": round(time.monotonic() - started, 2),
                "error": body or "failed to fetch GitHub Trending",
                "output": "GitHub 热榜实时获取失败，且没有可用缓存。\n"
                + (fallback_error or body or "failed to fetch GitHub Trending")}

    articles = re.findall(r'<article class="Box-row".*?</article>', body, flags=re.S)
    repos = []
    for article in articles[:limit]:
        href_match = re.search(r'href="/([^"/\s]+/[^"/\s]+)"', article)
        if not href_match:
            continue
        repo = html.unescape(href_match.group(1)).strip()
        desc_match = re.search(r'<p class="[^"]*col-9[^"]*">(.*?)</p>', article, flags=re.S)
        lang_match = re.search(r'itemprop="programmingLanguage"[^>]*>(.*?)</span>', article, flags=re.S)
        today_match = re.search(r'([\d,]+)\s+stars?\s+today', article, flags=re.I)
        stars_match = re.search(r'<a[^>]+href="/' + re.escape(repo) + r'/stargazers"[^>]*>(.*?)</a>', article, flags=re.S)
        repos.append({
            "repo": repo,
            "url": f"https://github.com/{repo}",
            "description": _clean_html_text(desc_match.group(1)) if desc_match else "",
            "language": _clean_html_text(lang_match.group(1)) if lang_match else "",
            "stars_today": _clean_html_text(today_match.group(1)) if today_match else "",
            "stars": _clean_html_text(stars_match.group(1)) if stars_match else "",
        })

    if not repos:
        fallback, fallback_error = _github_search_fallback(since, limit)
        if fallback:
            _write_trending_cache(since, fallback["url"], fallback["repos"])
            return _format_github_trending(since, fallback["repos"], fallback["url"], started,
                                           note="GitHub Trending 页面解析不到仓库，已改用近似搜索结果。",
                                           source_quality="fallback")
        cached = _read_trending_cache(since)
        if cached:
            return _format_github_trending(since, cached["repos"][:limit], cached.get("url") or url, started,
                                           cached=True, cached_at=cached.get("cached_at"),
                                           note="实时页面解析不到仓库，已使用缓存。", source_quality="cache")
        return {"ok": False, "duration": round(time.monotonic() - started, 2),
                "error": "failed to parse GitHub Trending",
                "output": f"GitHub 热榜解析失败。\n{fallback_error}"}

    _write_trending_cache(since, url, repos)
    return _format_github_trending(since, repos, url, started)


MEME_HTTP_API = MemeHttpApi(_assistant_db_connect, _json_response, _binary_response)
PET_HTTP_API = PetHttpApi(_assistant_db_connect, _json_response, _binary_response)
ASSISTANT_IDENTITY_HTTP_API = AssistantIdentityHttpApi(_assistant_db_connect, _json_response)
PERSONA_RUNTIME_HTTP_API = PersonaRuntimeHttpApi(_assistant_db_connect, _json_response)
B2_PRODUCT_HTTP_RUNTIME = B2ProductHttpRuntime(
    _assistant_db_connect, _db_connect,
    lambda limit: _phase2_outbox().list_deliveries(limit=limit), _json_response,
)
KNOWLEDGE_HTTP_API = KnowledgeHttpApi(_assistant_db_connect, _json_response)
LEARNING_HTTP_API = LearningHttpApi(_assistant_db_connect, _json_response)
BEHAVIOR_EVOLUTION_HTTP_API = BehaviorEvolutionHttpApi(_assistant_db_connect, _json_response)
BEHAVIOR_EVIDENCE_COLLECTION_HTTP_API = BehaviorEvidenceCollectionHttpApi(_assistant_db_connect, _json_response)
BEHAVIOR_POLICY_OPTIMIZER_HTTP_API = BehaviorPolicyOptimizerHttpApi(_assistant_db_connect, _json_response)
BEHAVIOR_OWNER_AUTHORIZATION_HTTP_API = BehaviorOwnerAuthorizationHttpApi(_assistant_db_connect, _json_response)
BEHAVIOR_PAIRED_SHADOW_HTTP_API = BehaviorPairedShadowHttpApi(_assistant_db_connect, _json_response)
BEHAVIOR_PRIVATE_EVALUATOR_HTTP_API = BehaviorPrivateEvaluatorHttpApi(_assistant_db_connect, _json_response)
NETWORK_POLICY_HTTP_API = NetworkPolicyHttpApi(
    _assistant_db_connect,
    _json_response,
)
GROUP_RESEARCH_HTTP_API = GroupResearchHttpApi(_assistant_db_connect, _json_response)
INTERACTION_PLAN_HTTP_API = InteractionPlanHttpApi(_assistant_db_connect, _json_response)
ASSISTANT_HOME_SERVICE = AssistantHomeService(
    _assistant_db_connect,
    _db_connect,
    lambda limit: _phase2_outbox().list_deliveries(limit=limit),
)
ASSISTANT_HOME_HTTP_API = AssistantHomeHttpApi(ASSISTANT_HOME_SERVICE, _json_response)
PROJECT_SERVICE = ProjectService(
    _assistant_db_connect, _db_connect, workspace_base=lambda: WORKSPACE_BASE,
    allowed_roots=_allowed_cwd_roots, slugify=_slugify,
    ensure_codegraph=lambda *args,**kwargs:_ensure_codegraph(*args,**kwargs),
)
PROJECT_HTTP_API = ProjectHttpApi(PROJECT_SERVICE, _json_response)
FORMAL_APPROVAL_CALLBACK = lambda result: sync_runtime_task(
    result, _db_connect, _row_to_task, TASKS, TASK_QUEUE, TASK_EVENT, TASK_LOCK,
)
FORMAL_APPROVAL_HTTP_API = FormalApprovalHttpApi(
    _assistant_db_connect,
    _db_connect,
    _json_response,
    FORMAL_APPROVAL_CALLBACK,
)
GOAL_CONTINUITY_HTTP_API = GoalContinuityHttpApi(_db_connect, _json_response)
ARTIFACT_RUNTIME = ArtifactRuntime(
    _assistant_db_connect, _db_connect, _json_response, _create_task, _safe_cwd,
)
VOICE_OUTPUT_RUNTIME = VoiceOutputRuntime(_assistant_db_connect, ARTIFACT_RUNTIME.service)
VOICE_DELIVERY_RUNTIME = VoiceDeliveryRuntime(_phase2_outbox, ARTIFACT_RUNTIME.service)
WORKER_HEALTH = WorkerHealthRegistry()
for worker_id in (
    "approval_expiry", "automation", "knowledge_ingestion",
    "continuous_private_response_coordinator",
):
    WORKER_HEALTH.register(worker_id,stale_after_seconds=180)
CONTINUOUS_PRIVATE_COORDINATOR = BridgeResponseCoordinator(
    _assistant_db_connect,
    _process_continuous_private_cycle,
    debounce_seconds=0.9,
    media_deadline_seconds=15.0,
    scan_interval_seconds=1.0,
    health_registry=WORKER_HEALTH,
    outbox=_phase2_outbox(),
)


def _executor_health_probe() -> dict:
    with _assistant_db_connect() as conn:
        return probe_executor(conn)


BUSINESS_HEALTH_SERVICE = BusinessHealthService(
    _assistant_db_connect,
    _db_connect,
    lambda limit: _phase2_outbox().list_deliveries(limit=limit),
    qq_probe=_qq_diagnostics,
    codex_probe=_executor_health_probe,
    artifact_probe=ARTIFACT_RUNTIME.cutover_plan,
    worker_health_reader=WORKER_HEALTH.snapshot,
)
GATE8_HTTP_API = Gate8HttpApi(
    _assistant_db_connect,
    BUSINESS_HEALTH_SERVICE,
    _json_response,
)
SOCIAL_VIRTUAL_HTTP_API = SocialVirtualHttpApi(_assistant_db_connect, _json_response)
GROUP_PARTICIPATION_HTTP_API = GroupParticipationHttpApi(_assistant_db_connect, _json_response)
QQ_ACCESS_HTTP_API = QqAccessHttpApi(_assistant_db_connect, _json_response)
QQ_RUNTIME_HTTP_API = QqRuntimeHttpApi(_assistant_db_connect,_json_response,None,globals())
QQ_OBJECT_RUNTIME = QqObjectRuntime(
    _assistant_db_connect, _db_connect, _json_response,
    row_to_task=_row_to_task, public_task=_public_task, get_task=_get_task,
    task_stats=_task_stats, list_tasks=_list_tasks, cancel_task=_cancel_task,
    retry_task=_retry_task, create_project=_create_project, list_projects=_list_projects,
    current_project=_current_project, set_current_project=_set_current_project,
    slugify=_slugify, list_memories=_list_memories, add_memory=_add_memory,
    delete_memory=_delete_memory,
    channel_token_distinct=lambda: bool(_read_token() and read_secret(CHANNEL_TOKEN_PATH)),
)
RELIABILITY_HTTP_API = ReliabilityHttpApi(
    _assistant_db_connect, _json_response,
    lambda: bool(_read_token() and read_secret(CHANNEL_TOKEN_PATH)),
    lambda: bool(
        (diagnostics := _qq_diagnostics()).get("onebot_connected")
        and not diagnostics.get("needs_login")
    ),
    lambda state,limit:_phase2_outbox().list_deliveries(state=state,limit=limit),
    lambda x: requeue_delivery(_phase2_outbox(),_set_task_delivery,TASK_DELIVERY_PENDING,x),
)


class BridgeHandler(AssistantIdentityPatchMixin, http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    timeout = 30
    identity_http_api = ASSISTANT_IDENTITY_HTTP_API
    interaction_plan_http_api = INTERACTION_PLAN_HTTP_API

    def log_message(self, fmt: str, *args):
        path = self.path.split("?", 1)[0]
        print(f"{self.client_address[0]} {self.command} {path}", flush=True)

    def on_successful_mutation(self, _status: int, _payload: dict) -> None:
        """Invalidate cross-domain Home projections after committed HTTP writes."""

        ASSISTANT_HOME_SERVICE.invalidate()

    def _client_allowed(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(ip in network for network in ALLOWED_NETWORKS)

    def _principal(self) -> PrincipalKind:
        return resolve_principal(
            _has_admin_session(self), _read_token(), read_secret(CHANNEL_TOKEN_PATH),
            self.headers.get("X-Bridge-Token", ""), self.headers.get("X-Channel-Token", ""),
            self._client_allowed(), ALLOW_PUBLIC_TOKEN_AUTH,
            gateway_token=read_secret(ADMIN_GATEWAY_TOKEN_PATH),
            supplied_gateway_token=self.headers.get("X-Admin-Gateway-Token", ""),
            gateway_client_allowed=self._gateway_client_allowed(),
        )

    def _gateway_client_allowed(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _authorized(self) -> bool:
        return self._principal() in {
            PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN, PrincipalKind.ADMIN_GATEWAY,
        }

    def _request_authorized(self, method: str, path: str) -> bool:
        return route_allowed(self._principal(), method, path)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        principal = self._principal()
        if principal is PrincipalKind.ADMIN_GATEWAY and not route_allowed(principal, "GET", self.path):
            _json_response(self, 403, {"ok": False, "error": "gateway_route_denied"})
            return
        if path == "/":
            _redirect_response(self, "/admin")
            return
        if path in {"/admin", "/admin/"}:
            _html_response(self, 200, ADMIN_CONSOLE_HTML or ADMIN_HTML)
            return
        if path.startswith("/admin/static/"):
            asset_name = unquote(path[len("/admin/static/"):])
            asset = admin_asset(asset_name) if admin_asset is not None else None
            if asset is None:
                _json_response(self, 404, {"ok": False, "error": "admin_asset_not_found"})
                return
            payload, content_type, etag = asset
            _binary_response(
                self,
                200,
                payload,
                content_type,
                cache_control="public, max-age=0, immutable",
                etag=etag,
            )
            return
        if path == "/admin/session":
            _json_response(self, 200, {"ok": True, "authenticated": _has_admin_session(self)})
            return
        if path == "/admin/version":
            _json_response(self, 200, {"ok": True, "version": ADMIN_ASSET_VERSION})
            return
        if path == "/admin/bootstrap":
            authenticated = _has_admin_session(self)
            appearance = _admin_appearance() if authenticated else dict(DEFAULT_ADMIN_APPEARANCE_SETTINGS)
            appearance["sample_background_url"] = DEFAULT_SAMPLE_BACKGROUND_URL
            payload = {"ok": True, "authenticated": authenticated, "appearance": appearance}
            _json_response(self, 200, payload)
            return
        if path == "/admin/appearance":
            if self._authorized():
                appearance = _admin_appearance()
            else:
                appearance = dict(DEFAULT_ADMIN_APPEARANCE_SETTINGS)
                appearance["sample_background_url"] = DEFAULT_SAMPLE_BACKGROUND_URL
            _json_response(self, 200, {"ok": True, "appearance": appearance})
            return
        if path == DEFAULT_SAMPLE_BACKGROUND_URL:
            try:
                payload = SAMPLE_BACKGROUND_ASSET_PATH.read_bytes()
            except OSError:
                _json_response(self, 404, {"ok": False, "error": "background_asset_not_found"})
                return
            _binary_response(
                self,
                200,
                payload,
                "image/jpeg",
                cache_control="public, max-age=86400",
                etag=hashlib.sha256(payload).hexdigest(),
            )
            return
        if path.startswith("/memes/assets/"):
            asset_name = unquote(path.rsplit("/", 1)[-1])
            asset = public_asset(asset_name)
            if asset is None:
                _json_response(self, 404, {"ok": False, "error": "meme_asset_not_found"})
                return
            payload, mime = asset
            _binary_response(self, 200, payload, mime)
            return
        if path == "/health":
            _json_response(self, 200, {"ok": True, "service": "codex-qq-bridge"})
            return
        if not self._request_authorized("GET", self.path):
            _json_response(self, 403, {"ok": False, "error": "forbidden"})
            return
        voice_media_match = re.fullmatch(r"/deliveries/([^/]+)/media", path)
        if voice_media_match:
            try:
                payload, content_type, etag = VOICE_DELIVERY_RUNTIME.media(
                    unquote(voice_media_match.group(1)),
                    str(self.headers.get("X-Delivery-Lease-Token") or ""),
                )
            except Exception as exc:
                error = str(exc).split(":", 1)[0] or "voice_delivery_media_failed"
                status = 409 if error == "voice_delivery_lease_invalid" else 404
                _json_response(self, status, {"ok": False, "error": error})
                return
            _binary_response(self, 200, payload, content_type, etag=etag)
            return
        if ASSISTANT_IDENTITY_HTTP_API.handle_get(self, path):
            return
        if PERSONA_RUNTIME_HTTP_API.handle_get(self, path):
            return
        if B2_PRODUCT_HTTP_RUNTIME.handle_get(self, path, query):
            return
        if KNOWLEDGE_HTTP_API.handle_get(self, path, query):
            return
        if INTERACTION_PLAN_HTTP_API.handle_get(self, path, query):
            return
        if ASSISTANT_HOME_HTTP_API.handle_get(self, path, query):
            return
        if FORMAL_APPROVAL_HTTP_API.handle_get(self, path, query):
            return
        if GOAL_CONTINUITY_HTTP_API.handle_get(self, path, query):
            return
        if LEARNING_HTTP_API.handle_get(self, path, query):
            return
        if BEHAVIOR_EVOLUTION_HTTP_API.handle_get(self, path, query):
            return
        if BEHAVIOR_EVIDENCE_COLLECTION_HTTP_API.handle_get(self, path):
            return
        if BEHAVIOR_POLICY_OPTIMIZER_HTTP_API.handle_get(self, path):
            return
        if BEHAVIOR_OWNER_AUTHORIZATION_HTTP_API.handle_get(self, path):
            return
        if BEHAVIOR_PAIRED_SHADOW_HTTP_API.handle_get(self, path):
            return
        if BEHAVIOR_PRIVATE_EVALUATOR_HTTP_API.handle_get(self, path):
            return
        if NETWORK_POLICY_HTTP_API.handle_get(self, path, query):
            return
        if GROUP_RESEARCH_HTTP_API.handle_get(self, path, query):
            return
        if ARTIFACT_RUNTIME.api.handle_get(self, path, query):
            return
        if GATE8_HTTP_API.handle_get(self, path, query, principal):
            return
        if SOCIAL_VIRTUAL_HTTP_API.handle_get(self, path, query):
            return
        if GROUP_PARTICIPATION_HTTP_API.handle_get(self, path):
            return
        if QQ_RUNTIME_HTTP_API.handle_get(self, path, self._principal()):
            return
        if QQ_ACCESS_HTTP_API.handle_get(self, path, query):
            return
        if PROJECT_HTTP_API.handle_get(self, path, query, self._principal()):
            return
        if QQ_OBJECT_RUNTIME.handle_get(self, path, query, self._principal()):
            return
        if RELIABILITY_HTTP_API.handle_get(self, path, self._principal()):
            return
        if MEME_HTTP_API.handle_get(self, path, query):
            return
        if PET_HTTP_API.handle_get(self, path):
            return
        if path == "/status":
            status = _executor_health_probe()
            _json_response(self, 200 if status.get("ok") else 503, status)
            return
        if path == "/server/status":
            depth = str((query.get("depth") or ["deep"])[0]).strip().lower()
            _json_response(self, 200, _server_status(deep=depth != "quick"))
            return
        if path == "/admin/security/token":
            _json_response(self, 200, {"ok": True, "token": _fixed_token_status()})
            return
        if path == "/system/audit":
            if _run_system_audit is None:
                _json_response(self, 500, {"ok": False, "error": "system_audit_module_unavailable"})
                return
            _json_response(self, 200, _run_system_audit())
            return
        if path == "/system/framework":
            audit = None
            if _run_system_audit is not None:
                try:
                    audit = _run_system_audit()
                except Exception:
                    audit = None
            _json_response(self, 200, build_system_framework(_assistant_settings(), audit))
            return
        if path == "/execution/overview":
            try:
                limit = int(query.get("limit", ["20"])[0])
                detailed = str(query.get("details", [""])[0]).lower() in {"1", "true", "yes"}
                result = _execution_snapshot(limit=limit, detailed=detailed)
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, result)
            return
        if path == "/goals":
            try:
                status = str(query.get("status", [""])[0] or "").strip()
                limit = int(query.get("limit", ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
                with _db_connect() as conn:
                    goals = PlatformRepository(conn).list_goals(status=status, limit=limit, offset=offset)
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "goals": goals})
            return
        if path.startswith("/goals/") and path.count("/") == 2:
            goal_id = unquote(path.rsplit("/", 1)[-1])
            with _db_connect() as conn:
                goal = PlatformRepository(conn).get_goal(goal_id)
            _json_response(
                self,
                200 if goal else 404,
                {"ok": bool(goal), "goal": goal, "error": "" if goal else "goal_not_found"},
            )
            return
        if path == "/runs":
            try:
                goal_id = str(query.get("goal_id", [""])[0] or "").strip()
                status = str(query.get("status", [""])[0] or "").strip()
                limit = int(query.get("limit", ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
                with _db_connect() as conn:
                    runs = PlatformRepository(conn).list_runs(
                        goal_id=goal_id,
                        status=status,
                        limit=limit,
                        offset=offset,
                    )
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "runs": runs})
            return
        if path.startswith("/runs/") and path.endswith("/events"):
            run_id = unquote(path.split("/")[2])
            try:
                limit = int(query.get("limit", ["100"])[0])
                with _db_connect() as conn:
                    repo = PlatformRepository(conn)
                    run = repo.get_run(run_id)
                    events = repo.list_run_events(run_id, limit=limit) if run else []
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(
                self,
                200 if run else 404,
                {"ok": bool(run), "run": run, "events": events, "error": "" if run else "run_not_found"},
            )
            return
        if path.startswith("/runs/") and path.endswith("/evidence"):
            run_id = unquote(path.split("/")[2])
            try:
                limit = int(query.get("limit", ["100"])[0])
                with _db_connect() as conn:
                    repo = PlatformRepository(conn)
                    run = repo.get_run(run_id)
                    evidence = repo.list_evidence(run_id, limit=limit) if run else []
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(
                self,
                200 if run else 404,
                {"ok": bool(run), "run": run, "evidence": evidence, "error": "" if run else "run_not_found"},
            )
            return
        if path.startswith("/runs/") and path.count("/") == 2:
            run_id = unquote(path.rsplit("/", 1)[-1])
            with _db_connect() as conn:
                run = PlatformRepository(conn).get_run(run_id)
            _json_response(
                self,
                200 if run else 404,
                {"ok": bool(run), "run": run, "error": "" if run else "run_not_found"},
            )
            return
        if path == "/capabilities/manifests":
            _json_response(self, 200, {"ok": True, "capabilities": list_fixed_capabilities()})
            return
        if path.startswith("/capabilities/manifests/"):
            capability_id = unquote(path.rsplit("/", 1)[-1])
            try:
                capability = get_fixed_capability(capability_id)
            except KeyError:
                _json_response(self, 404, {"ok": False, "error": "capability_not_found"})
                return
            _json_response(self, 200, {"ok": True, "capability": capability})
            return
        if path == "/deliveries":
            try:
                state = str(query.get("state", ["all"])[0] or "all").strip()
                channel = str(query.get("channel", [""])[0] or "").strip() or None
                limit = int(query.get("limit", ["100"])[0])
                deliveries = _phase2_outbox().list_deliveries(state=state, channel=channel, limit=limit)
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "deliveries": deliveries})
            return
        if path == "/codegraph/status":
            try:
                cwd = _safe_cwd((query.get("cwd") or [None])[0])
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, _codegraph_status(cwd))
            return
        if path == "/assistant/settings":
            _json_response(self, 200, {"ok": True, "settings": _assistant_settings()})
            return
        if path == "/assistant/provider/presets":
            _json_response(self, 200, {"ok": True, "presets": provider_presets_public()})
            return
        if path == "/assistant/models":
            with _assistant_db_connect() as conn:
                registry = list_model_registry(conn)
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    **registry,
                    "connection_templates": connection_templates(),
                    "contracts": contract_catalog(),
                    "runtime_inventories": runtime_inventories(registry),
                },
            )
            return
        if path == "/assistant/models/usage":
            try:
                days = int(query.get("days", ["7"])[0])
                limit = int(query.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                _json_response(self, 400, {"ok": False, "error": "invalid_usage_range"})
                return
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, **usage_report(conn, days=days, limit=limit)})
            return
        if path == "/system/codex":
            force = str(query.get("refresh", [""])[0]).lower() in {"1", "true", "yes"}
            _json_response(self, 200, codex_operations_status(force=force))
            return
        if path == "/system/proxy/status":
            _json_response(self, 200, proxy_status())
            return
        if path == "/system/proxy/probe-log":
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            with _assistant_db_connect() as conn:
                log = list_proxy_probe_log(conn, limit=limit)
            _json_response(self, 200, {"ok": True, "log": log})
            return
        if path == "/system/model-role/change-log":
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "log": list_role_change_log(conn, limit=limit)})
            return
        if path == "/assistant/expressions":
            enabled = (query.get("enabled", [""])[0] or "").strip()
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "habits": list_expression_habits(conn, enabled=enabled)})
            return
        if path == "/assistant/groups":
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {
                    "ok": True,
                    "groups": list_group_policies(conn),
                    "natural_participation": natural_group_cutover_plan(conn),
                })
            return
        if path == "/assistant/groups/messages":
            group_id = (query.get("group_id", [""])[0] or "").strip()
            try:
                limit = int(query.get("limit", ["30"])[0])
            except (TypeError, ValueError):
                limit = 30
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "messages": group_context(conn, group_id, limit)})
            return
        if path == "/capabilities/plugins":
            _json_response(self, 200, {"ok": True, "plugins": list_capability_plugins()})
            return
        if path == "/capabilities/skills":
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "skills": list_skills(conn)})
            return
        if path == "/capabilities/summary":
            with _assistant_db_connect() as conn:
                skills = list_skills(conn)
                network_policy = get_network_policy(conn)
            plugins = list_capability_plugins()
            capabilities = list_fixed_capabilities()
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "capabilities": capabilities,
                    "plugins": plugins,
                    "skills": skills,
                    "network_policy": network_policy,
                    "counts": {
                        "capabilities": len(capabilities),
                        "plugins": len(plugins),
                        "plugins_healthy": sum(1 for item in plugins if item.get("healthy")),
                        "skills": len(skills),
                        "skills_enabled": sum(1 for item in skills if item.get("enabled")),
                    },
                },
            )
            return
        if path == "/capabilities/marketplace":
            force_refresh = _truthy_setting(query.get("force_refresh", [""])[0])
            with _assistant_db_connect() as conn:
                result = get_marketplace(conn, force_refresh=force_refresh)
            _json_response(self, 200 if result.get("ok") else 503, result)
            return
        if path == "/capabilities/marketplace/operations":
            try:
                limit = int(query.get("limit", ["30"])[0])
            except (TypeError, ValueError):
                limit = 30
            with _assistant_db_connect() as conn:
                operations = list_market_operations(conn, limit=limit)
            _json_response(self, 200, {"ok": True, "operations": operations})
            return
        if path == "/assistant/proactive/plans":
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "plans": list_proactive_plans(conn)})
            return
        if path == "/automations/overview":
            _json_response(self, 200, _automation_overview())
            return
        if path == "/automations/jobs":
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "jobs": list_automation_jobs(conn)})
            return
        if path == "/automations/runs":
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "runs": list_automation_runs(conn)})
            return
        if path == "/assistant/proactive/policies":
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "policies": list_proactive_policies(conn)})
            return
        if path == "/assistant/proactive/events":
            user_id = (query.get("user_id", [""])[0] or "").strip()
            with _assistant_db_connect() as conn:
                _json_response(self, 200, {"ok": True, "events": list_proactive_events(conn, user_id=user_id)})
            return
        if path == "/assistant/proactive/due":
            try:
                limit = int(query.get("limit", ["3"])[0])
            except (TypeError, ValueError):
                limit = 3
            settings = _assistant_settings()
            if str(settings.get("proactive_enabled") or "0").lower() not in {"1", "true", "yes", "on"}:
                _json_response(self, 200, {"ok": True, "plans": [], "disabled": True})
                return
            with _assistant_db_connect() as conn:
                plans = due_proactive_plans(conn, limit=limit)
                for item in plans:
                    item["meme"] = None
                    if int(item.get("include_meme") or 0):
                        item["meme"] = choose_meme(
                            conn,
                            text=item.get("message") or "",
                            mode="daily",
                            intent="chat",
                            increment_usage=False,
                        )
                _json_response(self, 200, {"ok": True, "plans": plans})
            return
        if path == "/assistant/conversation":
            user_id = (query.get("user_id", ["web-console"])[0] or "web-console").strip()
            try:
                limit = int(query.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "messages": _conversation_history(
                        user_id=user_id,
                        limit=limit,
                        source="web",
                    ),
                },
            )
            return
        if path == "/assistant/quality":
            user_id = (query.get("user_id", [""])[0] or "").strip()
            status = (query.get("status", [""])[0] or "").strip()
            try:
                limit = int(query.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            _json_response(
                self,
                200,
                {"ok": True, "events": _list_quality_events(user_id=user_id, status=status, limit=limit)},
            )
            return
        if path == "/assistant/mode-sessions":
            user_id = (query.get("user_id", [""])[0] or "").strip()
            mode = (query.get("mode", [""])[0] or "").strip()
            try:
                limit = int(query.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            _json_response(
                self,
                200,
                {"ok": True, "sessions": _list_mode_sessions(user_id=user_id, mode=mode, limit=limit)},
            )
            return
        if path == "/services":
            _json_response(self, 200, _service_status())
            return
        if path == "/docker/containers":
            _json_response(self, 200, _docker_containers())
            return
        if path == "/proxy/groups":
            _json_response(self, 200, _proxy_groups())
            return
        if path == "/proxy/config":
            _json_response(self, 200, _proxy_config())
            return
        if path == "/proxy/ip":
            _json_response(self, 200, _proxy_ip_check())
            return
        if path == "/proxy/subscriptions":
            _json_response(self, 200, _subscription_summary_from_config())
            return
        if path == "/proxy/consumers/aiclient2api":
            _json_response(self, 200, _aiclient_proxy_consumer_status())
            return
        if path == "/proxy/consumers/aiclient2api/model-observation":
            with _assistant_db_connect() as conn:
                events = recent_model_observations(conn, AICLIENT_PROVIDER_ID)
            _json_response(self, 200, {"ok": True, "events": events, "model_identity": "provider_self_report_only"})
            return
        if path == "/proxy/diagnostics":
            group = (query.get("group", ["Proxies"])[0] or "Proxies").strip()
            try:
                limit = int(query.get("limit", ["12"])[0])
            except (TypeError, ValueError):
                limit = 12
            _json_response(self, 200, _proxy_diagnostics(group=group, limit=limit, auto_switch=False))
            return
        if path == "/qq/diagnostics":
            _json_response(self, 200, _qq_diagnostics())
            return
        if path == "/qq/events":
            user_id = (query.get("user_id", [""])[0] or "").strip()
            trace_id = (query.get("trace_id", [""])[0] or "").strip()
            try:
                limit = int(query.get("limit", ["30"])[0])
            except (TypeError, ValueError):
                limit = 30
            _json_response(
                self,
                200,
                {"ok": True, "events": _list_qq_events(user_id=user_id, trace_id=trace_id, limit=limit)},
            )
            return
        if path == "/qq/qrcode":
            ok, payload, error = _qq_qrcode_png()
            if not ok or not payload:
                _json_response(self, 404, {"ok": False, "error": error or "qrcode_not_found"})
                return
            _binary_response(self, 200, payload, "image/png")
            return
        if path == "/logs":
            raw_lines = (query.get("lines", ["120"])[0] or "120").strip()
            try:
                lines = int(raw_lines)
            except ValueError:
                lines = 120
            target = (query.get("target", ["bridge"])[0] or "bridge").strip()
            _json_response(self, 200, _service_logs(target=target, lines=lines))
            return
        if path == "/github/trending":
            raw_since = (query.get("since", ["daily"])[0] or "daily").strip()
            try:
                limit = int(query.get("limit", ["10"])[0])
            except (TypeError, ValueError):
                limit = 10
            _json_response(self, 200, _github_trending(raw_since, max(1, min(limit, 30))))
            return
        if path == "/tasks/delivery/pending":
            try:
                limit = int(query.get("limit", ["5"])[0])
            except (TypeError, ValueError):
                limit = 5
            _json_response(self, 200, {"ok": True, "tasks": _claim_pending_task_deliveries(limit=limit)})
            return
        _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        principal = self._principal()
        if principal is PrincipalKind.ADMIN_GATEWAY and not route_allowed(principal, "POST", self.path):
            _json_response(self, 403, {"ok": False, "error": "gateway_route_denied"})
            return
        if path == "/admin/logout":
            cookie = _clear_admin_session(self)
            am.audit(_assistant_db_connect, "admin_logout", "success", self.client_address[0])
            _json_response_with_cookie(self, 200, {"ok": True}, cookie)
            return
        if path == "/admin/login":
            client_ip = self.client_address[0]
            if _login_rate_limited(client_ip):
                am.audit(_assistant_db_connect, "admin_login", "rate_limited", client_ip)
                _json_response(self, 429, {"ok": False, "error": "too_many_login_attempts"})
                return
            payload, status, error = read_json_object(self, 65536)
            if error:
                _json_response(self, status, {"ok": False, "error": error})
                return
            supplied_build = str(payload.get("build") or "").strip()
            if ADMIN_ASSET_VERSION and supplied_build != ADMIN_ASSET_VERSION:
                _json_response(
                    self,
                    409,
                    {"ok": False, "error": "console_update_required", "version": ADMIN_ASSET_VERSION},
                )
                return
            supplied = str(payload.get("token", "")).strip()
            expected = _read_token()
            if not expected or not hmac.compare_digest(
                supplied.encode("utf-8"),
                expected.encode("utf-8"),
            ):
                _record_login_failure(client_ip)
                am.audit(_assistant_db_connect, "admin_login", "denied", client_ip)
                _json_response(self, 403, {"ok": False, "error": "invalid_token"})
                return
            _clear_login_failures(client_ip)
            cookie = _create_admin_session()
            am.audit(
                _assistant_db_connect, "admin_login", "success", client_ip,
                {"session_ttl_seconds": ADMIN_SESSION_TTL},
            )
            _json_response_with_cookie(
                self,
                200,
                {
                    "ok": True,
                    "authenticated": True,
                    "expires_in": ADMIN_SESSION_TTL,
                    "appearance": dict(_admin_appearance(), sample_background_url=DEFAULT_SAMPLE_BACKGROUND_URL),
                },
                cookie,
            )
            return
        if not self._request_authorized("POST", self.path):
            _json_response(self, 403, {"ok": False, "error": "forbidden"})
            return
        if (
            principal is PrincipalKind.ADMIN_SESSION
            and str(self.headers.get("X-Admin-Build") or "").strip() != ADMIN_ASSET_VERSION
        ):
            _json_response(
                self,
                409,
                {"ok": False, "error": "console_update_required", "version": ADMIN_ASSET_VERSION},
            )
            return
        if path == "/deliveries/claim" or re.fullmatch(
            r"/deliveries/[^/]+/(send-start|ack|retry|ambiguous)", path,
        ):
            if (
                path == "/deliveries/claim"
                and self._principal() is PrincipalKind.QQ_CHANNEL
                and not qq_channel_runtime_enabled(_assistant_db_connect)
            ):
                _json_response(self, 409, {"ok": False, "error": "qq_channel_disabled"})
                return
            payload, status, error = read_json_object(self, 65536)
            if error:
                _json_response(self, status, {"ok": False, "error": error})
                return
            try:
                if path == "/deliveries/claim":
                    deliveries = _claim_phase2_deliveries(
                        str(payload.get("lease_owner") or "").strip(),
                        wait_seconds=float(payload.get("wait_seconds") or 20),
                        lease_seconds=float(payload.get("lease_seconds") or 60),
                        limit=int(payload.get("limit") or 1),
                        channel=str(payload.get("channel") or "qq").strip(),
                    )
                    _json_response(self, 200, {"ok": True, "deliveries": deliveries})
                    return

                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    _json_response(self, 404, {"ok": False, "error": "not_found"})
                    return
                delivery_id = unquote(parts[1])
                lease_token = str(payload.get("lease_token") or "").strip()
                if parts[2] == "send-start":
                    delivery = _begin_phase2_delivery(delivery_id, lease_token)
                elif parts[2] == "ack":
                    delivery = _ack_phase2_delivery(
                        delivery_id,
                        lease_token,
                        platform_message_id=str(payload.get("platform_message_id") or "")[:180],
                    )
                elif parts[2] == "ambiguous":
                    delivery = _mark_phase2_delivery_ambiguous(
                        delivery_id,
                        lease_token,
                        error=str(payload.get("error") or "")[:2000],
                    )
                else:
                    delivery = _retry_phase2_delivery(
                        delivery_id,
                        lease_token,
                        error=str(payload.get("error") or "")[:2000],
                        delay_seconds=float(payload.get("delay_seconds") or 0),
                        known_not_sent=bool(payload.get("known_not_sent")),
                    )
            except LeaseLostError as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except DeliveryPolicyBlockedError as exc:
                _json_response(self, 409, {"ok": False, "error": "delivery_policy_blocked", "action": exc.action, "reason": exc.reason})
                return
            except (TypeError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            if not delivery:
                _json_response(self, 404, {"ok": False, "error": "delivery_not_found"})
                return
            _json_response(self, 200, {"ok": True, "delivery": delivery})
            return
        if QQ_OBJECT_RUNTIME.handle_task_action(self, path, self._principal()):
            return
        if path.startswith("/tasks/") and path.endswith("/delivery"):
            payload, status, error = read_json_object(self, 65536)
            if error:
                _json_response(self, status, {"ok": False, "error": error})
                return
            task_id = unquote(path.split("/")[-2])
            delivery_status = str(payload.get("delivery_status") or payload.get("status") or "").strip()
            delivery_error = str(payload.get("delivery_error") or payload.get("error") or "").strip()
            try:
                terminal_delivery = _legacy_phase2_delivery_marker(
                    task_id, delivery_status, delivery_error,
                )
            except LeaseLostError as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            if not terminal_delivery:
                _json_response(
                    self,
                    409,
                    {"ok": False, "error": "terminal_task_delivery_not_found"},
                )
                return
            task = _set_task_delivery(
                task_id,
                delivery_status,
                delivery_error,
            )
            if not task:
                _json_response(self, 404, {"ok": False, "error": "task_not_found"})
                return
            _json_response(self, 200, {"ok": True, "task": task})
            return
        if (
            path not in BRIDGE_POST_ROUTES
            and not FORMAL_APPROVAL_HTTP_API.matches_post(path)
            and not GOAL_CONTINUITY_HTTP_API.matches_post(path)
            and not ARTIFACT_RUNTIME.api.matches_post(path)
            and not GATE8_HTTP_API.matches_post(path)
            and not SOCIAL_VIRTUAL_HTTP_API.matches_post(path)
            and not GROUP_PARTICIPATION_HTTP_API.matches_post(path)
            and not QQ_ACCESS_HTTP_API.matches_post(path)
            and path not in {QQ_RUNTIME_HTTP_API.HEARTBEAT_PATH, "/qq/channel/groups"}
            and not RELIABILITY_HTTP_API.matches_post(path)
            and not PROJECT_HTTP_API.matches_post(path)
            and not KNOWLEDGE_HTTP_API.matches_post(path)
            and not LEARNING_HTTP_API.matches_post(path)
            and not BEHAVIOR_EVIDENCE_COLLECTION_HTTP_API.matches_post(path)
            and not BEHAVIOR_POLICY_OPTIMIZER_HTTP_API.matches_post(path)
            and not BEHAVIOR_OWNER_AUTHORIZATION_HTTP_API.matches_post(path)
            and not BEHAVIOR_PAIRED_SHADOW_HTTP_API.matches_post(path)
            and not BEHAVIOR_PRIVATE_EVALUATOR_HTTP_API.matches_post(path)
            and not NETWORK_POLICY_HTTP_API.matches_post(path)
            and not GROUP_RESEARCH_HTTP_API.matches_post(path)
            and not PERSONA_RUNTIME_HTTP_API.matches_post(path)
        ):
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        maximum = 24 * 1024 * 1024 if path == "/assistant/private-conversation/media" else 65536 if (
            ARTIFACT_RUNTIME.api.matches_post(path)
            or PROJECT_HTTP_API.matches_post(path)
            or KNOWLEDGE_HTTP_API.matches_post(path)
            or PERSONA_RUNTIME_HTTP_API.matches_post(path)
            or path == "/assistant/private-response-ownership"
            or path == "/assistant/private-response-handoff"
            or path == "/assistant/private-conversation/ingress"
            or path == "/assistant/private-conversation/cutover"
        ) else 16 * 1024 * 1024
        payload, status, error = read_json_object(self, maximum)
        if error:
            _json_response(self, status, {"ok": False, "error": error})
            return

        if path in {
            "/assistant/dispatch",
            "/assistant/private-conversation/ingress",
            "/assistant/private-conversation/media",
            "/assistant/private-response-handoff",
        } and principal is PrincipalKind.QQ_CHANNEL:
            body_actor = str(payload.get("user_id") or "").strip()
            header_actor = str(self.headers.get("X-QQ-Actor-ID") or "").strip()
            if not body_actor or not header_actor:
                _json_response(self, 400, {"ok": False, "error": "qq_actor_required"})
                return
            if header_actor != body_actor:
                _json_response(self, 403, {"ok": False, "error": "qq_actor_payload_mismatch"})
                return
            body_message_id = str(payload.get("external_message_id") or "").strip()
            header_message_id = str(self.headers.get("X-QQ-Message-ID") or "").strip()
            if not body_message_id or not header_message_id:
                _json_response(self, 400, {"ok": False, "error": "qq_message_id_required"})
                return
            if header_message_id != body_message_id:
                _json_response(self, 400, {"ok": False, "error": "qq_message_payload_mismatch"})
                return

        if path == "/assistant/private-conversation/cutover":
            if principal not in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}:
                _json_response(self, 403, {"ok": False, "error": "admin_required"})
                return
            if type(payload.get("enabled")) is not bool:
                _json_response(self, 400, {"ok": False, "error": "continuous_private_enabled_boolean_required"})
                return
            try:
                with _assistant_db_connect() as conn:
                    result = set_continuous_private_conversation_feature(
                        conn, bool(payload["enabled"]),
                    )
            except (ResponseCycleBusyError, ValueError) as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except (RuntimeError, sqlite3.Error):
                _json_response(self, 503, {"ok": False, "error": "continuous_private_cutover_unavailable"})
                return
            _json_response(self, 200, {"ok": True, **result})
            return

        if path == "/assistant/private-conversation/ingress":
            if principal is not PrincipalKind.QQ_CHANNEL:
                _json_response(self, 403, {"ok": False, "error": "qq_channel_required"})
                return
            allowed_fields = {
                "channel", "conversation_type", "user_id", "session",
                "external_message_id", "raw_text", "text", "trace_id", "self_id",
                "reply_to_external_message_id", "reply_to_assistant",
                "message_components", "attachments", "response_coordinator_version",
            }
            if set(payload) - allowed_fields:
                _json_response(self, 400, {"ok": False, "error": "private_ingress_field_not_allowed"})
                return
            user_id = str(payload.get("user_id") or "").strip()
            try:
                owner_access = qq_private_owner_access(_assistant_fast_read_connect, user_id)
            except (sqlite3.Error, ValueError):
                _json_response(self, 503, {"ok": False, "error": "private_ingress_auth_unavailable"})
                return
            if not owner_access.get("allowed"):
                _json_response(self, 403, {"ok": False, "error": "qq_owner_required"})
                return
            try:
                transport = with_qq_transport_metadata(payload, self.headers, default_actor=user_id)
                ingress_payload = {
                    **payload,
                    "self_id": str(transport.get("_qq_self_id") or payload.get("self_id") or ""),
                }
                with _assistant_ingress_connect() as conn:
                    result = accept_private_ingress(conn, ingress_payload)
            except InboundConflictError as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except InboundRequestValidationError as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            except ContinuousPrivateConversationDisabledError as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except (RuntimeError, sqlite3.Error):
                _json_response(self, 503, {"ok": False, "error": "private_ingress_unavailable"})
                return
            CONTINUOUS_PRIVATE_COORDINATOR.wake()
            _json_response(self, 200, {**result, "authorized": True})
            return

        if path == "/assistant/private-conversation/media":
            if principal is not PrincipalKind.QQ_CHANNEL:
                _json_response(self, 403, {"ok": False, "error": "qq_channel_required"})
                return
            allowed_fields = {
                "channel", "conversation_type", "user_id", "session",
                "external_message_id", "source_message_id", "visual_media", "trace_id",
            }
            if set(payload) - allowed_fields:
                _json_response(self, 400, {"ok": False, "error": "private_media_field_not_allowed"})
                return
            user_id = str(payload.get("user_id") or "").strip()
            if (
                str(payload.get("channel") or "").strip() != "qq"
                or str(payload.get("conversation_type") or "").strip() != "private"
                or str(self.headers.get("X-QQ-Group-ID") or "").strip()
            ):
                _json_response(self, 400, {"ok": False, "error": "private_media_scope_invalid"})
                return
            try:
                owner_access = qq_private_owner_access(_assistant_fast_read_connect, user_id)
            except (sqlite3.Error, ValueError):
                _json_response(self, 503, {"ok": False, "error": "private_media_auth_unavailable"})
                return
            if not owner_access.get("allowed"):
                _json_response(self, 403, {"ok": False, "error": "qq_owner_required"})
                return
            visual_media = payload.get("visual_media")
            try:
                result = CONTINUOUS_PRIVATE_COORDINATOR.bind_media(
                    source_message_id=str(payload.get("source_message_id") or ""),
                    external_message_id=str(payload.get("external_message_id") or ""),
                    actor_id=user_id,
                    visual_media=visual_media,
                )
            except ValueError as exc:
                error = str(exc)
                status = 403 if error == "private_media_binding_mismatch" else 409 if (
                    error == "private_media_source_unavailable"
                ) else 400
                _json_response(self, status, {"ok": False, "error": error})
                return
            finally:
                if isinstance(visual_media, list):
                    for item in visual_media:
                        if isinstance(item, dict):
                            item.clear()
                    visual_media.clear()
            _json_response(self, 200, result)
            return

        if path == "/assistant/private-response-handoff":
            if principal is not PrincipalKind.QQ_CHANNEL:
                _json_response(self, 403, {"ok": False, "error": "qq_channel_required"})
                return
            allowed_fields = {
                "channel",
                "conversation_type",
                "user_id",
                "session",
                "external_message_id",
                "trace_id",
                "reply",
                "ownership_required",
            }
            if set(payload) - allowed_fields:
                _json_response(self, 400, {"ok": False, "error": "private_handoff_field_not_allowed"})
                return
            if "ownership_required" in payload and not isinstance(payload["ownership_required"], bool):
                _json_response(self, 400, {"ok": False, "error": "private_handoff_ownership_flag_invalid"})
                return
            if (
                str(payload.get("channel") or "").strip() != "qq"
                or str(payload.get("conversation_type") or "").strip() != "private"
                or str(self.headers.get("X-QQ-Group-ID") or "").strip()
            ):
                _json_response(self, 400, {"ok": False, "error": "private_handoff_scope_invalid"})
                return
            try:
                identity_payload = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"reply", "ownership_required"}
                }
                transport = with_qq_transport_metadata(
                    identity_payload,
                    self.headers,
                    default_actor=str(payload.get("user_id") or ""),
                )
                result = handoff_private_owned_direct_response(
                    transport,
                    str(payload.get("reply") or ""),
                    ownership_required=payload.get("ownership_required") is not False,
                )
            except ValueError as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            except (RuntimeError, sqlite3.Error):
                _json_response(self, 503, {"ok": False, "error": "private_direct_handoff_unavailable"})
                return
            _json_response(self, 200, {**result, "authorized": True})
            return

        if path == "/assistant/private-turn-join":
            if self._principal() is not PrincipalKind.QQ_CHANNEL:
                _json_response(self, 403, {"ok": False, "error": "qq_channel_required"})
                return
            detached_visual_media = payload.pop("visual_media", None)
            allowed_fields = {
                "channel", "conversation_type", "user_id", "session",
                "external_message_id", "primary_message_id", "logical_turn_id",
                "source_message_ids", "raw_text", "text", "attachments",
                "message_components", "trace_id", "private_turn_protocol_version",
                "logical_turn_member_index", "logical_turn_component_start",
                "logical_turn_visual_start", "logical_turn_member_has_text",
            }
            if set(payload) - allowed_fields:
                _clear_private_turn_join_media(detached_visual_media)
                _json_response(self, 400, {"ok": False, "error": "private_turn_join_field_not_allowed"})
                return
            if (
                str(payload.get("channel") or "").strip() != "qq"
                or str(payload.get("conversation_type") or "").strip() != "private"
            ):
                _clear_private_turn_join_media(detached_visual_media)
                _json_response(self, 400, {"ok": False, "error": "private_turn_join_scope_invalid"})
                return
            user_id = str(payload.get("user_id") or "").strip()
            header_actor = str(self.headers.get("X-QQ-Actor-ID") or "").strip()
            source_message_id = str(payload.get("external_message_id") or "").strip()
            header_message_id = str(self.headers.get("X-QQ-Message-ID") or "").strip()
            if not user_id or header_actor != user_id:
                _clear_private_turn_join_media(detached_visual_media)
                _json_response(self, 403, {"ok": False, "error": "qq_actor_mismatch"})
                return
            if not source_message_id or header_message_id != source_message_id:
                _clear_private_turn_join_media(detached_visual_media)
                _json_response(self, 400, {"ok": False, "error": "qq_message_mismatch"})
                return
            access_error = qq_private_access_http_error(_assistant_db_connect, user_id, "chat")
            if access_error:
                _clear_private_turn_join_media(detached_visual_media)
                _json_response(self, *access_error)
                return
            try:
                transport = with_qq_transport_metadata(payload, self.headers, default_actor=user_id)
                result = register_active_private_turn_join(
                    transport,
                    primary_message_id=payload.get("primary_message_id"),
                    source_message_ids=payload.get("source_message_ids"),
                    raw_text=payload.get("raw_text"),
                    text=payload.get("text"),
                    attachments=payload.get("attachments"),
                    message_components=payload.get("message_components"),
                    visual_media=detached_visual_media,
                    private_turn_protocol_version=payload.get(
                        "private_turn_protocol_version", _PRIVATE_TURN_JOIN_UNSET,
                    ),
                    logical_turn_member_index=payload.get(
                        "logical_turn_member_index", _PRIVATE_TURN_JOIN_UNSET,
                    ),
                    logical_turn_component_start=payload.get(
                        "logical_turn_component_start", _PRIVATE_TURN_JOIN_UNSET,
                    ),
                    logical_turn_visual_start=payload.get(
                        "logical_turn_visual_start", _PRIVATE_TURN_JOIN_UNSET,
                    ),
                    logical_turn_member_has_text=payload.get(
                        "logical_turn_member_has_text", _PRIVATE_TURN_JOIN_UNSET,
                    ),
                )
            except (InboundConflictError, InboundProcessingError, InboundOutcomeUnknownError) as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except (InboundRequestValidationError, ValueError) as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            except (RuntimeError, sqlite3.Error):
                _json_response(self, 503, {"ok": False, "error": "private_turn_join_unavailable"})
                return
            finally:
                _clear_private_turn_join_media(detached_visual_media)
            _json_response(self, 200, result)
            return

        if path == "/assistant/private-response-ownership":
            if self._principal() is not PrincipalKind.QQ_CHANNEL:
                _json_response(self, 403, {"ok": False, "error": "qq_channel_required"})
                return
            if any(key in payload for key in ("message", "content", "raw_message")):
                _json_response(self, 400, {"ok": False, "error": "response_ownership_body_not_allowed"})
                return
            allowed_fields = {
                "operation",
                "channel",
                "conversation_type",
                "user_id",
                "session",
                "external_message_id",
                "trace_id",
                "logical_turn_id",
                "source_message_ids",
            }
            if set(payload) - allowed_fields:
                _json_response(self, 400, {"ok": False, "error": "response_ownership_field_not_allowed"})
                return
            operation = str(payload.get("operation") or "register").strip()
            if operation not in {"register", "response_started"}:
                _json_response(self, 400, {"ok": False, "error": "response_ownership_operation_invalid"})
                return
            if (
                str(payload.get("channel") or "").strip() != "qq"
                or str(payload.get("conversation_type") or "").strip() != "private"
                or str(payload.get("group_id") or "").strip()
                or str(self.headers.get("X-QQ-Group-ID") or "").strip()
            ):
                _json_response(self, 400, {"ok": False, "error": "response_ownership_scope_invalid"})
                return
            user_id = str(payload.get("user_id") or "").strip()
            header_actor = str(self.headers.get("X-QQ-Actor-ID") or "").strip()
            if not user_id or not header_actor:
                _json_response(self, 400, {"ok": False, "error": "response_ownership_actor_required"})
                return
            if header_actor != user_id:
                _json_response(self, 403, {"ok": False, "authorized": False, "error": "qq_actor_mismatch"})
                return
            source_message_id = str(payload.get("external_message_id") or "").strip()
            header_message_id = str(self.headers.get("X-QQ-Message-ID") or "").strip()
            if not source_message_id or not header_message_id:
                _json_response(self, 400, {"ok": False, "error": "response_ownership_message_required"})
                return
            if header_message_id != source_message_id:
                _json_response(self, 400, {"ok": False, "error": "qq_message_mismatch"})
                return
            if operation == "response_started":
                try:
                    transport = with_qq_transport_metadata(payload, self.headers, default_actor=user_id)
                    result = complete_private_response_ownership(transport)
                except ValueError as exc:
                    _json_response(self, 400, {"ok": False, "error": str(exc)})
                    return
                _json_response(self, 200, {**result, "authorized": True})
                return
            try:
                access = qq_private_owner_access(_assistant_fast_read_connect, user_id)
            except (sqlite3.Error, ValueError):
                _json_response(self, 503, {
                    "ok": False,
                    "authorized": False,
                    "error": "response_ownership_auth_unavailable",
                })
                return
            if not access.get("allowed"):
                _json_response(self, 403, {
                    "ok": False,
                    "authorized": False,
                    "error": "qq_owner_required",
                    "reason": access.get("reason") or "owner_required",
                })
                return
            try:
                transport = with_qq_transport_metadata(payload, self.headers, default_actor=user_id)
                logical_turn_id = str(payload.get("logical_turn_id") or "").strip()
                source_message_ids = payload.get("source_message_ids")
                if logical_turn_id:
                    register_private_turn_member_aliases(
                        _assistant_db_connect,
                        primary_message_id=source_message_id,
                        source_message_ids=source_message_ids,
                        actor_id=user_id,
                        conversation_ref=user_id,
                    )
                elif source_message_ids is not None:
                    raise InboundRequestValidationError("qq_private_turn_logical_id_required")
                joined_replay = replay_private_turn_member(
                    _assistant_db_connect,
                    str(transport.get("_qq_message_id") or source_message_id),
                    str(transport.get("_qq_actor_id") or user_id),
                    user_id,
                )
                if joined_replay is not None:
                    # The plugin may have restarted after an image joined a
                    # text-led turn. Do not create a second response owner or
                    # execution presence for that already-owned source ID.
                    _json_response(self, 200, {
                        "ok": True,
                        "authorized": True,
                        "ownership_established": False,
                        "idempotent_replay": True,
                        "logical_turn_replay": True,
                    })
                    return
                with _assistant_fast_read_connect() as conn:
                    delivery_enabled = unified_delivery_enabled(conn)
                if not delivery_enabled:
                    raise RuntimeError("unified_delivery_disabled")
                result = register_private_response_ownership(transport)
            except (InboundProcessingError, InboundOutcomeUnknownError) as exc:
                _json_response(self, 409, {"ok": False, "authorized": True, "error": str(exc)})
                return
            except ValueError as exc:
                _json_response(self, 400, {"ok": False, "authorized": True, "error": str(exc)})
                return
            except (RuntimeError, sqlite3.Error):
                _json_response(self, 503, {
                    "ok": False,
                    "authorized": True,
                    "error": "response_ownership_unavailable",
                    "error_kind": "bridge_http_5xx",
                })
                return
            _json_response(self, 200, {**result, "authorized": True})
            return

        if PROJECT_HTTP_API.handle_post(self, path, payload, self._principal()):
            return
        if KNOWLEDGE_HTTP_API.handle_post(self, path, payload):
            return
        if LEARNING_HTTP_API.handle_post(self, path, payload):
            return
        if BEHAVIOR_EVIDENCE_COLLECTION_HTTP_API.handle_post(self, path, payload):
            return
        if BEHAVIOR_POLICY_OPTIMIZER_HTTP_API.handle_post(self, path, payload):
            return
        if BEHAVIOR_OWNER_AUTHORIZATION_HTTP_API.handle_post(self, path, payload):
            return
        if BEHAVIOR_PAIRED_SHADOW_HTTP_API.handle_post(self, path, payload):
            return
        if BEHAVIOR_PRIVATE_EVALUATOR_HTTP_API.handle_post(self, path, payload):
            return
        if NETWORK_POLICY_HTTP_API.handle_post(self, path, payload):
            return
        if GROUP_RESEARCH_HTTP_API.handle_post(self, path, payload):
            return
        if PERSONA_RUNTIME_HTTP_API.handle_post(self, path, payload):
            return
        if QQ_OBJECT_RUNTIME.handle_post(self, path, payload, self._principal()):
            return
        if RELIABILITY_HTTP_API.handle_post(self, path, payload, self._principal()):
            return
        if ASSISTANT_IDENTITY_HTTP_API.handle_post(self, path, payload):
            return
        if FORMAL_APPROVAL_HTTP_API.handle_post(self, path, payload):
            return
        if GOAL_CONTINUITY_HTTP_API.handle_post(self, path, payload):
            return
        if ARTIFACT_RUNTIME.api.handle_post(self, path, payload):
            return
        if GATE8_HTTP_API.handle_post(self, path, payload, principal):
            return
        if SOCIAL_VIRTUAL_HTTP_API.handle_post(self, path, payload):
            return
        if GROUP_PARTICIPATION_HTTP_API.handle_post(self, path, payload):
            return
        if QQ_RUNTIME_HTTP_API.handle_post(self, path, payload, self._principal()):
            return
        if QQ_ACCESS_HTTP_API.handle_post(self, path, payload):
            return
        if MEME_HTTP_API.handle_post(self, path, payload):
            return
        if PET_HTTP_API.handle_post(self, path, payload):
            return

        if path == "/admin/security/token":
            try:
                token = _validate_fixed_token(
                    payload.get("new_token"),
                    payload.get("confirm_token"),
                )
                if payload.get("confirm_logout") is not True:
                    raise ValueError("token_logout_confirmation_required")
                broker_write(
                    "admin_token_rotate",
                    "bridge-admin-token",
                    {"new_token": token},
                    idempotency_key=f"admin-token-rotate-{uuid.uuid4().hex}",
                )
                active_token = _read_token()
                if not active_token or not hmac.compare_digest(
                    active_token.encode("utf-8"),
                    token.encode("utf-8"),
                ):
                    raise RuntimeError("token_rotation_readback_failed")
                _clear_all_admin_sessions()
                am.audit(
                    _assistant_db_connect, "admin_token_rotation", "success",
                    self.client_address[0],
                    {"all_sessions_revoked": True, "source": "ops_broker"},
                )
            except ValueError as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            except RuntimeError as exc:
                client_error = admin_token_client_error(str(exc))
                if client_error:
                    _json_response(self, 400, {"ok": False, "error": client_error})
                    return
                _json_response(self, 500, {"ok": False, "error": "token_write_failed"})
                return
            except OSError:
                _json_response(self, 500, {"ok": False, "error": "token_write_failed"})
                return
            cookie = _cookie_header(ADMIN_SESSION_COOKIE, "", max_age=0)
            _json_response_with_cookie(
                self,
                200,
                {"ok": True, "changed": True, "token": _fixed_token_status()},
                cookie,
            )
            return

        if path in {"/system/proxy/probe", "/system/proxy/test-exec"}:
            upstream = bool(payload.get("upstream"))
            paid_action = upstream or path.endswith("test-exec")
            if paid_action and not bool(payload.get("confirm_cost")):
                _json_response(self, 400, {"ok": False, "error": "cost_confirmation_required"})
                return
            if path.endswith("test-exec"):
                executor_snapshot = _resolve_executor_snapshot()
                result = proxy_executor_test(
                    timeout=int(payload.get("timeout") or 60),
                    executor=executor_snapshot,
                )
                probe_type = "executor"
            else:
                result = proxy_full_probe() if upstream else proxy_status()
                probe_type = "upstream" if upstream else "local"
            with _assistant_db_connect() as conn:
                log_item = record_proxy_probe(
                    conn,
                    probe_type=probe_type,
                    result=result,
                    executor_id=str((executor_snapshot if path.endswith("test-exec") else {}).get("provider_id") or "proxy"),
                    triggered_by=f"admin:{self.client_address[0]}",
                )
            result = dict(result)
            result["probe_id"] = log_item["id"]
            _json_response(self, 200, result)
            return

        if path == "/admin/appearance":
            try:
                appearance = _update_admin_appearance(payload)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "appearance": appearance})
            return

        if path == "/codegraph/ensure":
            try:
                cwd = _safe_cwd(payload.get("cwd"))
                result = _ensure_codegraph(cwd, phase="manual", force=True)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": bool(result.get("ok")), "codegraph": result})
            return

        if path == "/assistant/settings":
            if self._principal() is PrincipalKind.QQ_CHANNEL:
                forbidden = QQ_CHANNEL_FORBIDDEN_SETTING_KEYS & set(payload)
                if forbidden:
                    _json_response(
                        self,
                        403,
                        {
                            "ok": False,
                            "error": "qq_channel_settings_forbidden",
                            "fields": sorted(forbidden),
                        },
                    )
                    return
            try:
                settings = _update_assistant_settings(payload)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "settings": settings})
            return

        if path == "/assistant/provider/test":
            try:
                result = _assistant_provider_test(timeout=45, payload=payload)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_validation_error(exc)})
                return
            _json_response(self, 200, _public_model_probe_result(result))
            return

        if path == "/assistant/models/provider":
            try:
                with _assistant_db_connect() as conn:
                    before_runtime = provider_runtime_dependency_fingerprint(conn, str(payload.get("id") or ""))
                    provider = upsert_provider(conn, payload)
                    runtime_changed = before_runtime != provider_runtime_dependency_fingerprint(conn, provider["id"])
                with _assistant_db_connect() as conn:
                    apply_results = apply_profiles_for_dependency(
                        conn, provider_id=provider["id"], provider_runtime_changed=runtime_changed,
                    )
                with _assistant_db_connect() as conn:
                    registry = list_model_registry(conn)
                with _assistant_db_connect() as conn:
                    prune_unreferenced_provider_secrets(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_registry_error(exc)})
                return
            _json_response(self, 200, {
                "ok": not any(not item.get("ok") for item in apply_results),
                "provider": provider, "executor_apply": apply_results, **registry,
            })
            return

        if path == "/assistant/models/provider/delete":
            try:
                with _assistant_db_connect() as conn:
                    deleted = delete_provider(conn, str(payload.get("id") or ""))
                    registry = list_model_registry(conn)
                with _assistant_db_connect() as conn:
                    prune_unreferenced_provider_secrets(conn)
            except ValueError as exc:
                _json_response(self, 409, _public_model_registry_conflict(exc))
                return
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_registry_error(exc)})
                return
            _json_response(self, 200, {"ok": True, **deleted, **registry})
            return

        if path == "/assistant/models/model":
            try:
                with _assistant_db_connect() as conn:
                    before_runtime = model_runtime_dependency_fingerprint(conn, str(payload.get("id") or ""))
                    model = upsert_model(conn, payload)
                    runtime_changed = before_runtime != model_runtime_dependency_fingerprint(conn, model["id"])
                with _assistant_db_connect() as conn:
                    apply_results = apply_profiles_for_dependency(
                        conn, model_id=model["id"], model_runtime_changed=runtime_changed,
                    )
                with _assistant_db_connect() as conn:
                    registry = list_model_registry(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_registry_error(exc)})
                return
            _json_response(self, 200, {
                "ok": not any(not item.get("ok") for item in apply_results),
                "model": model, "executor_apply": apply_results, **registry,
            })
            return

        if path == "/assistant/models/model/delete":
            try:
                with _assistant_db_connect() as conn:
                    deleted = delete_model(conn, str(payload.get("id") or ""))
                    registry = list_model_registry(conn)
            except ValueError as exc:
                _json_response(self, 409, _public_model_registry_conflict(exc))
                return
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_registry_error(exc)})
                return
            _json_response(self, 200, {"ok": True, **deleted, **registry})
            return

        if path == "/assistant/models/bind":
            try:
                with _assistant_db_connect() as conn:
                    binding = bind_model_role(conn, payload)
                    registry = list_model_registry(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_registry_error(exc)})
                return
            _json_response(self, 200, {"ok": True, "binding": binding, **registry})
            return

        if path == "/assistant/models/work-executor/activate":
            try:
                with _assistant_db_connect() as conn:
                    result = activate_work_executor(conn, str(payload.get("model_id") or ""))
                    registry = list_model_registry(conn)
            except Exception as exc:
                _json_response(self, 400, {
                    "ok": False,
                    "error": _public_work_executor_activation_error(exc),
                })
                return
            _json_response(self, 200, {**result, **registry})
            return

        if path == "/assistant/models/executor/verify":
            try:
                with _assistant_db_connect() as conn:
                    pv = str(payload.get("provider_id") or "").strip()
                    result = verify_executor_work_mode(conn, pv, timeout=max(20, min(int(payload.get("timeout") or 120), 300)))
                    registry = list_model_registry(conn)
            except Exception as exc:
                _json_response(self, 400, {
                    "ok": False,
                    "error": _public_executor_verification_error(exc),
                })
                return
            # G2: ok mirrors status; failure never shown as success.
            _json_response(self, 200, {"ok": result.get("status") == "verified", "verification": result, **registry})
            return


        if path == "/assistant/models/test":
            try:
                result = _assistant_provider_test(timeout=45, payload=payload)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_validation_error(exc)})
                return
            _json_response(self, 200, _public_model_probe_result(result))
            return

        if path == "/assistant/models/discover":
            is_validation = str(payload.get("action") or "").strip() == "validate"
            try:
                if is_validation:
                    result = _assistant_discovered_model_playground(
                        payload, timeout=max(20, min(int(payload.get("timeout") or 90), 300)),
                    )
                else:
                    with _assistant_db_connect() as conn:
                        result = discover_provider_models(
                            conn, payload.get("provider_id"), opener_for_url=_provider_request_opener,
                        )
            except ValueError as exc:
                _json_response(
                    self,
                    400,
                    {"ok": False, "error": _public_model_discovery_error(
                        exc,
                        validation=is_validation,
                    )},
                )
                return
            except Exception as exc:
                _json_response(
                    self,
                    500,
                    {"ok": False, "error": _public_model_discovery_error(
                        exc,
                        validation=is_validation,
                    )},
                )
                return
            # Provider answered even when rejecting the model; keep typed guidance.
            _json_response(self, 200, _public_model_probe_result(result) if is_validation else result)
            return

        if path == "/assistant/models/playground":
            try:
                result = _assistant_model_playground(
                    payload,
                    timeout=max(20, min(int(payload.get("timeout") or 90), 300)),
                )
            except ValueError as exc:
                _json_response(self, 400, {"ok": False, "error": _public_model_validation_error(exc)})
                return
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": _public_model_validation_error(exc)})
                return
            public_result = _public_model_probe_result(result)
            _json_response(self, 200 if public_result["ok"] else 502, public_result)
            return

        if path == "/assistant/expressions":
            try:
                with _assistant_db_connect() as conn:
                    habit = upsert_expression_habit(conn, payload)
                    habits = list_expression_habits(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "habit": habit, "habits": habits})
            return

        if path == "/assistant/groups":
            try:
                with _assistant_db_connect() as conn:
                    group = upsert_group_policy(conn, payload)
                    groups = list_group_policies(conn)
                    natural = natural_group_cutover_plan(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {
                "ok": True,
                "group": group,
                "groups": groups,
                "natural_participation": natural,
            })
            return

        if path == "/assistant/group/dispatch":
            group_dispatch_started = time.monotonic()
            try:
                timeout = max(20, min(int(payload.get("timeout") or 120), 300))
                dispatch_payload = with_qq_transport_metadata(
                    payload,
                    self.headers,
                    default_actor=str(payload.get("sender_id") or ""),
                )
                def _group_operation_after_receipt():
                    _log_group_http_dispatch_timing(
                        payload=payload, started=group_dispatch_started,
                        phase="receipt_ready", outcome="ok",
                    )
                    return _dispatch_qq_response_if_enabled(
                        _group_dispatch_after_delivery_gate,
                        dispatch_payload,
                        scope="group",
                    )

                def _group_dispatch_after_delivery_gate():
                    _log_group_http_dispatch_timing(
                        payload=payload, started=group_dispatch_started,
                        phase="delivery_gate_ready", outcome="ok",
                    )
                    return _assistant_group_dispatch_ingress(dispatch_payload, timeout=timeout)

                result = execute_inbound_once(
                    _assistant_db_connect, self.headers.get("X-QQ-Message-ID", ""),
                    self.headers.get("X-QQ-Actor-ID", ""),
                    str(payload.get("group_id") or payload.get("session") or ""), payload,
                    _group_operation_after_receipt,
                )
                _log_group_http_dispatch_timing(
                    payload=payload, started=group_dispatch_started,
                    phase="inbound_once", outcome="ok",
                )
                group_decision = (
                    result.get("group_decision")
                    if isinstance(result.get("group_decision"), dict)
                    else {}
                )
                if result.get("delivery_queued") and group_decision.get("memory_candidates"):
                    continuity_candidates = capture_group_single_plan_memory_candidates(
                        _assistant_db_connect,
                        explicit_memories=[],
                        legacy_user_id=f"group:{str(payload.get('group_id') or '').strip()}",
                        message=normalize_group_inbound(dispatch_payload)[0],
                        interaction_plan=group_decision,
                        source="qq_group",
                        group={
                            "group_id": str(payload.get("group_id") or "").strip(),
                            "sender_id": str(dispatch_payload.get("sender_id") or "").strip(),
                            "source_external_message_id": str(
                                dispatch_payload.get("_external_message_id") or ""
                            ).strip(),
                        },
                    )
                    if continuity_candidates:
                        result["continuity_memory_candidates"] = continuity_candidates
                    _log_group_http_dispatch_timing(
                        payload=payload, started=group_dispatch_started,
                        phase="memory_capture", outcome="ok",
                    )
            except (InboundConflictError, InboundProcessingError) as exc:
                _log_group_http_dispatch_timing(
                    payload=payload, started=group_dispatch_started, outcome="conflict",
                )
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                _log_group_http_dispatch_timing(
                    payload=payload, started=group_dispatch_started, outcome="internal",
                )
                traceback.print_exc()
                print(
                    "assistant_group_dispatch_failed "
                    f"error={type(exc).__name__} detail={str(exc)[:300]!r}",
                    flush=True,
                )
                _json_response(self, 500, {
                    "ok": False,
                    "error": "assistant_group_dispatch_failed",
                    "error_kind": "internal",
                })
                return
            _log_group_http_dispatch_timing(
                payload=payload, started=group_dispatch_started,
                outcome="ok" if result.get("ok") else "rejected",
            )
            _json_response(self, 200 if result.get("ok") else 400, result)
            return

        if path == "/capabilities/skills":
            try:
                with _assistant_db_connect() as conn:
                    skill = upsert_skill(conn, payload)
                    skills = list_skills(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "skill": skill, "skills": skills})
            return

        if path == "/capabilities/skills/toggle":
            try:
                with _assistant_db_connect() as conn:
                    skill = set_skill_enabled(
                        conn,
                        str(payload.get("id") or payload.get("skill_id") or "").strip(),
                        _truthy_setting(payload.get("enabled")),
                    )
                    skills = list_skills(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200 if skill else 404, {"ok": bool(skill), "skill": skill, "skills": skills})
            return

        if path == "/capabilities/plugins/toggle":
            try:
                result = set_capability_plugin_enabled(
                    str(payload.get("id") or payload.get("plugin_id") or "").strip(),
                    _truthy_setting(payload.get("enabled")),
                    idempotency_key=str(self.headers.get("Idempotency-Key") or ""),
                )
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            result["plugins"] = list_capability_plugins()
            _json_response(self, 200, result)
            return

        if path == "/capabilities/plugins/reload":
            result = reload_capability_plugins(
                idempotency_key=str(self.headers.get("Idempotency-Key") or ""),
            )
            result["plugins"] = list_capability_plugins()
            _json_response(self, 200 if result.get("ok") else 500, result)
            return

        if path == "/capabilities/marketplace/operate":
            try:
                payload["_idempotency_key"] = str(self.headers.get("Idempotency-Key") or "")
                with _assistant_db_connect() as conn:
                    result = operate_market_plugin(conn, payload)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200 if result.get("ok") else 500, result)
            return

        if path == "/assistant/proactive/plans":
            try:
                with _assistant_db_connect() as conn:
                    plan = upsert_proactive_plan(conn, payload)
                    plans = list_proactive_plans(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "plan": plan, "plans": plans})
            return

        if path == "/automations/jobs":
            try:
                with _assistant_db_connect() as conn:
                    operation = str(payload.get("operation") or "").strip()
                    if operation in {"archive", "restore"}:
                        job = transition_automation_job_archive(
                            conn, str(payload.get("id") or ""),
                            expected_revision=payload.get("expected_revision"),
                            operation=operation,
                        )
                    elif operation:
                        raise ValueError("automation_operation_invalid")
                    else:
                        job = upsert_automation_job(conn, payload, require_revision_on_existing=True)
                    jobs = list_automation_jobs(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            AUTOMATION_EVENT.set()
            _json_response(self, 200, {"ok": True, "job": job, "jobs": jobs})
            return

        if path == "/assistant/proactive/policies":
            try:
                with _assistant_db_connect() as conn:
                    policy = upsert_proactive_policy(conn, payload)
                    policies = list_proactive_policies(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            AUTOMATION_EVENT.set()
            _json_response(self, 200, {"ok": True, "policy": policy, "policies": policies})
            return

        if path == "/assistant/proactive/mark":
            plan_id = str(payload.get("id") or payload.get("plan_id") or "").strip()
            status = str(payload.get("status") or "sent").strip() or "sent"
            with _assistant_db_connect() as conn:
                plan = mark_proactive_plan(
                    conn,
                    plan_id,
                    status=status,
                    error=str(payload.get("error") or ""),
                    meme_id=str(payload.get("meme_id") or ""),
                )
            _json_response(self, 200 if plan else 404, {"ok": bool(plan), "plan": plan, "error": "" if plan else "plan_not_found"})
            return

        if path == "/proxy/select":
            request_id = _safe_request_id(self)
            result = _with_request_id(
                _proxy_select_request(payload, request_id=request_id),
                request_id,
            )
            _json_response(self, _proxy_select_http_status(result), result)
            return

        if path == "/proxy/consumers/aiclient2api/save":
            request_id = _safe_request_id(self)
            result = _with_request_id(_aiclient_proxy_consumer_write("aiclient_proxy_save", payload, request_id=request_id), request_id)
            status = 200 if result.get("ok") else 409 if result.get("error") == "consumer_revision_conflict" else 400
            _json_response(self, status, result)
            return

        if path == "/proxy/consumers/aiclient2api/test":
            request_id = _safe_request_id(self)
            result = _with_request_id(_aiclient_proxy_consumer_test(payload, request_id=request_id), request_id)
            _json_response(self, 200 if result.get("ok") else 422, result)
            return

        if path == "/proxy/consumers/aiclient2api/apply":
            request_id = _safe_request_id(self)
            result = _with_request_id(_aiclient_proxy_consumer_write("aiclient_proxy_apply", payload, request_id=request_id), request_id)
            _json_response(self, 200 if result.get("ok") else 409, result)
            return

        if path == "/proxy/consumers/aiclient2api/rollback":
            request_id = _safe_request_id(self)
            result = _with_request_id(_aiclient_proxy_consumer_write("aiclient_proxy_rollback", payload, request_id=request_id), request_id)
            _json_response(self, 200 if result.get("ok") else 409, result)
            return

        if path == "/proxy/delay":
            group = str(payload.get("group") or "Proxies").strip() or "Proxies"
            raw_names = payload.get("names") or []
            names = raw_names if isinstance(raw_names, list) else []
            try:
                timeout_ms = int(payload.get("timeout_ms") or 6000)
            except (TypeError, ValueError):
                timeout_ms = 6000
            _json_response(self, 200, _proxy_delay(group=group, names=names, timeout_ms=timeout_ms))
            return

        if path in {
            "/proxy/subscriptions/create",
            "/proxy/subscriptions/update",
        }:
            request_id = _safe_request_id(self)
            key = str(payload.get("key") or "").strip()
            action = path.rsplit("/", 1)[-1]
            result = _with_request_id(
                _proxy_subscription_request(
                    action,
                    {
                        "key": key,
                        "name": str(payload.get("name") or "").strip(),
                        "url": str(payload.get("url") or "").strip(),
                        "url_update_present": payload.get("url_update_present", bool(payload.get("url"))),
                        "enabled": payload.get("enabled"),
                        "expected_revision": payload.get("expected_revision"),
                    },
                    request_id=request_id,
                ),
                request_id,
            )
            response_status = _subscription_http_status(result)
            _audit_proxy_subscription_result(action, result, response_status, request_id)
            _json_response(self, response_status, result)
            return

        if path in {
            "/proxy/subscriptions/refresh", "/proxy/subscriptions/switch",
            "/proxy/subscriptions/enable", "/proxy/subscriptions/disable",
            "/proxy/subscriptions/delete",
        }:
            request_id = _safe_request_id(self)
            key = str(payload.get("key") or "").strip()
            if not key:
                result = _with_request_id(
                    {"ok": False, "error": "subscription_key_required"},
                    request_id,
                )
                _audit_proxy_subscription_result(
                    path.rsplit("/", 1)[-1], result, 400, request_id,
                )
                _json_response(self, 400, result)
                return
            action = path.rsplit("/", 1)[-1]
            result = _with_request_id(
                _proxy_subscription_operation(
                    action,
                    key,
                    expected_revision=payload.get("expected_revision"),
                    request_id=request_id,
                ),
                request_id,
            )
            response_status = _subscription_http_status(result)
            _audit_proxy_subscription_result(action, result, response_status, request_id)
            _json_response(self, response_status, result)
            return

        if path == "/proxy/diagnostics":
            try:
                limit = int(payload.get("limit") or 12)
            except (TypeError, ValueError):
                limit = 12
            auto_switch = str(payload.get("auto_switch") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            group = str(payload.get("group") or "Proxies").strip() or "Proxies"
            _json_response(
                self,
                200,
                _proxy_diagnostics(group=group, limit=limit, auto_switch=auto_switch),
            )
            return

        if path == "/qq/events":
            event = _record_qq_event(payload)
            _json_response(self, 201, {"ok": True, "event": event})
            return

        if path == "/qq/qrcode/refresh":
            if payload.get("confirm_restart") is not True:
                error = (
                    "qq_restart_confirmation_required"
                    if QQ_ADAPTER == "llbot"
                    else "napcat_restart_confirmation_required"
                )
                _json_response(self, 409, {"ok": False, "error": error})
                return
            diagnostics = _qq_diagnostics()
            if diagnostics.get("qq_status") == "online" and not diagnostics.get("needs_login"):
                _json_response(self, 409, {"ok": False, "error": "qq_login_active"})
                return
            try:
                wait_seconds = int(payload.get("wait_seconds") or 25)
            except (TypeError, ValueError):
                wait_seconds = 25
            result = _qq_refresh_qrcode(wait_seconds=wait_seconds)
            _json_response(self, 200, result)
            return

        if path == "/assistant/chat":
            message = str(payload.get("message") or "").strip()
            user_id = str(payload.get("user_id") or "default").strip()
            try:
                timeout = int(payload.get("timeout") or ASSISTANT_CHAT_TIMEOUT)
            except (TypeError, ValueError):
                timeout = ASSISTANT_CHAT_TIMEOUT
            if not RUN_LOCK.acquire(blocking=False):
                _json_response(self, 409, {"ok": False, "error": "codex is busy"})
                return
            try:
                result = _assistant_chat(user_id=user_id, message=message, timeout=timeout)
            finally:
                RUN_LOCK.release()
            _json_response(self, 200 if result.get("ok") else 500, result)
            return

        if path == "/assistant/dispatch":
            message = str(payload.get("message") or "").strip()
            user_id = str(payload.get("user_id") or "default").strip()
            trace_id = str(payload.get("trace_id") or "").strip()
            force = str(payload.get("force") or "auto").strip()
            principal = self._principal()
            is_admin_web_dispatch = principal in {
                PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN, PrincipalKind.ADMIN_GATEWAY,
            }
            if principal is PrincipalKind.QQ_CHANNEL:
                access_error = qq_private_access_http_error(
                    _assistant_db_connect, user_id,
                    str(payload.get("requested_action") or "chat"),
                )
                if access_error:
                    _json_response(self, *access_error)
                    return
            try:
                timeout = int(payload.get("timeout") or DISPATCH_CHAT_TIMEOUT)
            except (TypeError, ValueError):
                timeout = DISPATCH_CHAT_TIMEOUT
            try:
                dispatch_payload = with_qq_transport_metadata(
                    payload,
                    self.headers,
                    default_actor=user_id,
                )
                receipt_context = (
                    _web_dispatch_receipt_context(self, principal)
                    if is_admin_web_dispatch else {
                        "platform_message_id": self.headers.get("X-QQ-Message-ID", ""),
                        "actor_id": self.headers.get("X-QQ-Actor-ID", ""),
                        "conversation_ref": user_id,
                    }
                )
                if not is_admin_web_dispatch:
                    joined_replay = replay_private_turn_member(
                        _assistant_db_connect,
                        receipt_context["platform_message_id"], receipt_context["actor_id"],
                        receipt_context["conversation_ref"],
                    )
                    if joined_replay is not None:
                        replay_status = 202 if joined_replay.get("dispatch") in {"task", "task_append"} else 200
                        _json_response(self, replay_status, joined_replay)
                        return
                    if str(payload.get("logical_turn_id") or "").strip():
                        register_private_turn_member_aliases(
                            _assistant_db_connect,
                            primary_message_id=receipt_context["platform_message_id"],
                            source_message_ids=payload.get("source_message_ids"),
                            actor_id=receipt_context["actor_id"],
                            conversation_ref=receipt_context["conversation_ref"],
                        )

                def dispatch_operation() -> dict:
                    cycle = None
                    dispatch_message = message
                    if not is_admin_web_dispatch:
                        with _assistant_db_connect() as conn:
                            continuous_enabled = continuous_private_conversation_enabled(conn)
                        if continuous_enabled:
                            source_external_ids = payload.get("source_message_ids")
                            if not isinstance(source_external_ids, list) or not source_external_ids:
                                source_external_ids = [receipt_context["platform_message_id"]]
                            acquire_deadline = time.monotonic() + 20.0
                            while True:
                                try:
                                    with _assistant_db_connect() as conn:
                                        cycle = acquire_response_cycle(
                                            conn,
                                            legacy_user_id=user_id,
                                            source_external_message_ids=source_external_ids,
                                            lease_owner=f"bridge:{trace_id or uuid.uuid4().hex[:12]}",
                                        )
                                    break
                                except ResponseCycleBusyError as exc:
                                    if "manual_hold" in str(exc) or "retryable" in str(exc):
                                        raise
                                    if time.monotonic() >= acquire_deadline:
                                        raise
                                    time.sleep(0.05)
                            exact_parts = [
                                str(item or "").strip()
                                for item in cycle.get("source_contents") or []
                                if str(item or "").strip()
                            ]
                            if exact_parts:
                                dispatch_message = "\n".join(exact_parts)
                    if cycle is not None:
                        return _execute_continuous_private_cycle(
                            cycle=cycle,
                            dispatch_payload=dispatch_payload,
                            user_id=user_id,
                            message=dispatch_message,
                            timeout=timeout,
                            trace_id=trace_id,
                            force=force,
                        )
                    with inbound_exchange_context(dispatch_payload):
                        return _dispatch_qq_response_if_enabled(
                            lambda: _assistant_dispatch(
                                user_id=user_id, message=dispatch_message, timeout=timeout, trace_id=trace_id,
                                force=force,
                                source="admin" if is_admin_web_dispatch else QQ_TASK_SOURCE,
                                cwd=Path(str(payload["_qq_cwd"])) if payload.get("_qq_cwd") else None,
                                require_project=bool(payload.get("_qq_project_guard")),
                                delivery_recipient_id=user_id,
                                delivery_session=str(dispatch_payload.get("session") or ""),
                                inbound_context=dispatch_payload,
                            ),
                            dispatch_payload,
                            scope="private",
                        )

                result = execute_inbound_once(
                    _assistant_db_connect,
                    receipt_context["platform_message_id"], receipt_context["actor_id"],
                    receipt_context["conversation_ref"], payload,
                    dispatch_operation,
                    require_receipt=is_admin_web_dispatch,
                )
                observation = observe_private_participation(
                    _assistant_db_connect, dispatch_payload, result,
                )
                try:
                    bind_qq_response_decision(_phase2_outbox(), result, observation)
                except (sqlite3.Error, ValueError) as exc:
                    print(
                        "delivery_decision_bind_failed "
                        f"scope=private error={type(exc).__name__}",
                        flush=True,
                    )
            except (
                InboundConflictError, InboundProcessingError, InboundOutcomeUnknownError,
                ResponseCycleBusyError, ResponseCycleLeaseError,
            ) as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except InboundIdempotencyUnavailableError as exc:
                _json_response(self, 503, {"ok": False, "error": str(exc)})
                return
            except InboundRequestValidationError as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                last_trace = exc.__traceback__
                while last_trace and last_trace.tb_next:
                    last_trace = last_trace.tb_next
                error_site = (
                    f"{Path(last_trace.tb_frame.f_code.co_filename).name}:"
                    f"{last_trace.tb_lineno}:"
                    f"{last_trace.tb_frame.f_code.co_name}"
                    if last_trace
                    else "unknown"
                )
                print(
                    "assistant_dispatch_failed "
                    f"error={type(exc).__name__} site={error_site}",
                    flush=True,
                )
                _json_response(self, 500, {
                    "ok": False,
                    "error": "assistant_dispatch_failed",
                    "error_kind": "internal",
                })
                return
            status = 202 if result.get("dispatch") in {"task", "task_append"} else 200
            failure = 409 if (
                result.get("error") == "qq_project_required"
                or result.get("dispatch") == "blocked"
            ) else 500
            _json_response(self, status if result.get("ok") else failure, result)
            return

        try:
            prompt = str(payload.get("prompt", "")).strip()
            sandbox = str(payload.get("sandbox", "read-only"))
            timeout = int(payload.get("timeout", 240))
            cwd = _safe_cwd(payload.get("cwd"))
        except Exception as exc:
            _json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if not prompt:
            _json_response(self, 400, {"ok": False, "error": "prompt is required"})
            return
        if len(prompt) > MAX_PROMPT_CHARS:
            _json_response(self, 400, {"ok": False, "error": "prompt too long"})
            return
        if sandbox not in {"read-only", "workspace-write"}:
            _json_response(self, 400, {"ok": False, "error": "invalid sandbox"})
            return
        timeout = max(30, min(timeout, 900))

        if path == "/tasks":
            try:
                requested_network = str(
                    payload.get("network_mode") or "controlled",
                ).strip()
                if requested_network not in {"controlled", "search"}:
                    raise ValueError("invalid_task_network_mode")
                source = str(payload.get("source") or "admin").strip() or "admin"
                user_id = str(payload.get("user_id") or "").strip()
                if self._principal() is PrincipalKind.QQ_CHANNEL:
                    # SEC-001: task creation must run the same QQ admission as
                    # the dispatch paths.  A channel may create a task only for
                    # an admitted QQ actor; it may never impersonate an
                    # admin/source actor, and it must fail closed when the
                    # actor is missing or unadmitted.  Admitted actors keep
                    # their full existing task capability (LLBot /code write,
                    # /ask read).
                    if source != "qq":
                        _json_response(
                            self,
                            403,
                            {"ok": False, "error": "qq_channel_task_source_invalid"},
                        )
                        return
                    if not user_id:
                        _json_response(
                            self,
                            403,
                            {"ok": False, "error": "qq_channel_task_actor_required"},
                        )
                        return
                    access_error = qq_private_access_http_error(
                        _assistant_db_connect,
                        user_id,
                        "code" if sandbox == "workspace-write" else "ask",
                    )
                    if access_error:
                        _json_response(self, *access_error)
                        return
                if requested_network == "search":
                    is_owner = (
                        self._principal()
                        in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}
                        or (
                            source != "admin"
                            and user_id in qq_super_admin_ids(_assistant_db_connect)
                        )
                    )
                    with _assistant_db_connect() as conn:
                        if not is_owner or not task_web_search_allowed(conn):
                            raise ValueError("task_web_search_not_authorized")
                task = _create_task(
                    prompt=prompt,
                    sandbox=sandbox,
                    timeout=timeout,
                    cwd=cwd,
                    source=source,
                    user_id=user_id,
                    trace_id=str(payload.get("trace_id") or "").strip(),
                    origin_message=str(payload.get("origin_message") or "").strip(),
                    intent=str(payload.get("intent") or "").strip(),
                    mode=str(payload.get("mode") or "").strip(),
                    network_mode=requested_network,
                )
            except (RuntimeError, ValueError) as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 202, {"ok": True, "task": task})
            return

        if not RUN_LOCK.acquire(blocking=False):
            _json_response(self, 409, {"ok": False, "error": "codex is busy"})
            return
        try:
            codegraph = {"before": _ensure_codegraph(cwd, phase="before")}
            result = _run_command(
                [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    *codex_model_args(_settings_for_model_role("work_executor")),
                    "--sandbox",
                    sandbox,
                ],
                input_text=prompt,
                cwd=cwd,
                timeout=timeout,
            )
            if sandbox == "workspace-write":
                codegraph["after"] = _ensure_codegraph(cwd, phase="after", force=True)
            result["codegraph"] = codegraph
            _json_response(self, 200, result)
        finally:
            RUN_LOCK.release()

class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


if __name__ == "__main__":
    WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)
    _init_assistant_db()
    _warm_assistant_settings_cache()
    _init_phase2_state()
    ARTIFACT_RUNTIME.start()
    _load_history()
    _backfill_phase2_state()
    threading.Thread(target=_task_worker, daemon=True).start()
    threading.Thread(
        target=formal_expiry_worker,
        args=(_assistant_db_connect, _db_connect, FORMAL_APPROVAL_CALLBACK),
        kwargs={"health": WORKER_HEALTH, "log_event": lambda event: print(
            "approval:"+event["error_type"],flush=True,
        )},
        daemon=True,
    ).start()
    threading.Thread(target=_automation_worker, daemon=True).start()
    threading.Thread(target=CONTINUOUS_PRIVATE_COORDINATOR.run, daemon=True).start()
    server = ThreadingServer((LISTEN_HOST, LISTEN_PORT), BridgeHandler)
    print(f"codex-qq-bridge listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()
