#!/usr/bin/env python3
import base64
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
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
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
from bridge_proxy_service import (
    concurrent_node_delays,
    read_only_diagnostics,
    redact_url as _redact_url,
)
from bridge_proxy_instances import UserSubscriptionStore
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
from bridge_meme_http import MemeHttpApi
from bridge_meme_attachment import (
    align_reply_with_attachment,
    mark_failed_attachment,
    prepare_meme_attachment,
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
from bridge_qq_object_runtime import QqObjectRuntime
from bridge_qq_access_runtime import (
    channel_runtime_enabled as qq_channel_runtime_enabled,
    diagnostic_access_snapshot,
    group_access as qq_group_access,
    private_access_http_error as qq_private_access_http_error,
    super_admin_ids as qq_super_admin_ids,
)
import bridge_assistant_identity as assistant_identity
from bridge_assistant_identity_http import AssistantIdentityHttpApi, AssistantIdentityPatchMixin
from bridge_persona_runtime_http import PersonaRuntimeHttpApi
from bridge_assistant_home import AssistantHomeService
from bridge_assistant_home_http import AssistantHomeHttpApi
from bridge_continuity_kernel import ContinuityKernel
from bridge_action_commitment import ActionCommitmentRepository
from bridge_action_followup import dispatch_action_followup_context
from bridge_route_dispatch import dispatch_deterministic_route
from bridge_pet_http import PetHttpApi
from bridge_pet_service import ensure_pet_tables
from bridge_conversation_memory import (
    add_memory as scoped_add_memory,
    conversation_history as scoped_conversation_history,
    delete_memory as scoped_delete_memory,
    list_memories as scoped_list_memories,
    record_conversation as scoped_record_conversation,
)
from bridge_conversation_memory_http import ConversationMemoryHttpApi
from bridge_qq_participation_shadow import complete_group_dispatch,finalize_group_shadow,observe_group_access_denied,observe_private_participation,prepare_group_dispatch,transition_group_participation,with_qq_transport_metadata
from bridge_inbound_media import inbound_media_notice, inbound_media_retry_notice
from bridge_conversation_model_runtime import run_conversation_model_reply
import bridge_visual_context as visual
from bridge_group_media_context import prepare_group_visual_context, project_group_visual_context
from bridge_group_direct_dispatch import apply_group_work_boundary, dispatch_group_control_action, prepare_direct_group_turn, run_admitted_group_turn
from bridge_group_context_frame import DEFAULT_GROUP_CONTEXT_LIMIT
from bridge_task_dispatch_policy import active_qq_task, dispatch_sandbox as _dispatch_sandbox, dispatch_timeout as _dispatch_timeout, new_task_requested as _new_task_requested, pending_messages as _pending_messages, should_dispatch_as_task as _should_dispatch_as_task
from bridge_group_participation_policy import natural_group_cutover_plan
from bridge_group_participation_http import GroupParticipationHttpApi
from bridge_continuity_service import capture_plan_candidate_metadata, expire_stale_memories
from bridge_assistant_chat_context import (
    attach_chat_result,
    build_social_context,
    merge_shared_knowledge,
    social_result,
    summarize_prompt as _task_summary,
)
from bridge_knowledge_http import KnowledgeHttpApi
from bridge_interaction_contract import assemble_response
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
from bridge_goal_continuity import ensure_task_revision_binding, record_goal_feedback
from bridge_goal_continuity_http import GoalContinuityHttpApi
from bridge_learning_service import capture_owner_group_expression_candidate
from bridge_learning_http import LearningHttpApi
from bridge_network_policy import (
    apply_network_policy_command,
    get_network_policy,
    network_capability_allowed,
    task_web_search_allowed,
)
from bridge_network_policy_http import NetworkPolicyHttpApi
from bridge_goal_followup import followup_prompt_context
from bridge_goal_followup_runtime import (
    followup_history,
    followup_scope,
    load_goal_followup,
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
from bridge_delivery_operations import delivery_task_id as _delivery_task_id,requeue_delivery
from bridge_delivery_continuity import unified_delivery_enabled
from bridge_delivery_claim import claim_deliveries
from bridge_delivery_settlement import settle_ack, settle_ambiguous, settle_retry
from bridge_proactive_decision_contract import (
    parse_proactive_json as _parse_proactive_json,
    proactive_system_prompt,
    sanitize_proactive_decision as _sanitize_proactive_decision,
)
from bridge_outbound_policy import DeliveryPolicyBlockedError, begin_delivery_with_policy, filter_claimed_deliveries, social_proactive_globally_enabled
from bridge_qq_delivery import bind_qq_response_decision, dispatch_qq_response, load_qq_delivery_sessions
from bridge_group_participation_worker import process_group_participation_queue
from bridge_proactive_runtime import process_proactive_policies
from bridge_task_delivery import enqueue_task_result
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
    InboundConflictError, InboundProcessingError, execute_once as execute_inbound_once,
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
from bridge_platform_repository import PlatformRepository, ensure_platform_schema
from bridge_model_registry import (
    bind_model_role,
    ensure_model_registry_tables,
    list_model_registry,
    list_role_change_log,
    provider_test_settings,
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
from bridge_executor_apply import apply_profiles_for_dependency
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
    record_model_usage,
    usage_report,
)
from bridge_model_adapters import openai_response_facts, prepare_model_request, parse_model_response
from bridge_conversation_reply_runtime import (
    call_openai_conversation_reply,
    call_openai_with_empty_retry,
)
from bridge_provider_errors import provider_http_error_facts, provider_transport_error_kind
from bridge_action_truth import enforce_action_truth
from bridge_model_playground import run_model_playground
from bridge_executor_profiles import (
    executor_workspace_root,
    get_executor_profile,
    profile_sha256,
    read_executor_credential,
)
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
PHASE2_CAPABILITY_CATALOG = CapabilityCatalog()
FINAL_STATUSES = {"done", "failed", "timeout", "cancelled"}
TASK_STATUSES = FINAL_STATUSES | {"queued", "running", "waiting_approval"}
RETRYABLE_STATUSES = {"failed", "timeout", "cancelled"}
QQ_TASK_SOURCE = "qq"
TASK_DELIVERY_NONE = "none"
TASK_DELIVERY_PENDING = "pending"
WORK_TASK_TIMEOUT = int(os.environ.get("WORK_TASK_TIMEOUT", "600"))
DISPATCH_CHAT_TIMEOUT = int(os.environ.get("DISPATCH_CHAT_TIMEOUT", "180"))
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
} | AGENT_POLICY_SETTING_KEYS
ASSISTANT_SECRET_SETTING_KEYS = {"chat_api_key"}
CHAT_PROVIDERS = {"codex", "openai-compatible"}
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


def _invalidate_assistant_home_cache() -> None:
    service = globals().get("ASSISTANT_HOME_SERVICE")
    invalidate = getattr(service, "invalidate", None)
    if callable(invalidate):
        invalidate()


def _db_connect() -> sqlite3.Connection:
    return connect_home_database(
        TASK_DB_PATH,
        ASSISTANT_HOME_TASK_TABLES,
        _invalidate_assistant_home_cache,
    )


def _phase2_outbox() -> DeliveryOutbox:
    key = os.path.normcase(str(TASK_DB_PATH.resolve()))
    with PHASE2_OUTBOX_LOCK:
        outbox = PHASE2_OUTBOXES.get(key)
        if outbox is None:
            outbox = DeliveryOutbox(
                TASK_DB_PATH,
                mutation_callback=_invalidate_assistant_home_cache,
            )
            PHASE2_OUTBOXES[key] = outbox
    return outbox
def _dispatch_qq_response_if_enabled(operation, transport: dict, *, scope: str) -> dict:
    with _assistant_db_connect() as conn:
        enabled = unified_delivery_enabled(conn)
    result = dispatch_qq_response(
        _phase2_outbox(), operation, transport, scope=scope, enabled=enabled,
        voice_output=VOICE_OUTPUT_RUNTIME.prepare,
    )
    CONTINUITY_KERNEL.bind_delivery(result)
    return result
def _init_phase2_state() -> None:
    _init_task_db()
    with _db_connect() as conn:
        ensure_platform_schema(conn)
        from bridge_migrations import ensure_agent_platform_migrations
        ensure_agent_platform_migrations(conn)
    _phase2_outbox()
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
def _enqueue_phase2_delivery(task: dict, projection: dict | None = None) -> dict | None:
    return enqueue_task_result(
        _phase2_outbox(), task, projection,
        sessions=_qq_delivery_sessions(), public_task=_public_task, trim_output=_trim_output,
    )
def _sync_and_enqueue_phase2_task(task: dict) -> dict:
    projection = _sync_phase2_task(task)
    with _db_connect() as conn:
        from bridge_migrations import ensure_agent_platform_migrations
        ensure_agent_platform_migrations(conn)
        projection["revision_binding"] = ensure_task_revision_binding(conn, task)
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
    return settle_ack(
        _phase2_outbox(),
        delivery_id,
        lease_token,
        platform_message_id=platform_message_id,
        assistant_db_connect=_assistant_db_connect,
        set_task_delivery=_set_task_delivery,
        record_conversation=_record_conversation,
    )


def _retry_phase2_delivery(
    delivery_id: str,
    lease_token: str,
    *,
    error: str = "",
    delay_seconds: float = 10,
    known_not_sent: bool = False,
) -> dict | None:
    return settle_retry(
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


def _mark_phase2_delivery_ambiguous(
    delivery_id: str,
    lease_token: str,
    *,
    error: str = "",
) -> dict | None:
    return settle_ambiguous(
        _phase2_outbox(),
        delivery_id,
        lease_token,
        error=error,
        assistant_db_connect=_assistant_db_connect,
    )


def _legacy_phase2_delivery_marker(task_id: str, status: str, error: str = "") -> dict | None:
    """Map the old task delivery callback onto the durable Outbox state."""

    delivery = next(
        (
            item
            for item in _phase2_outbox().list_deliveries(state="all", channel="qq", limit=500)
            if _delivery_task_id(item) == task_id
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


def _assistant_settings(*, include_secrets: bool = False) -> dict:
    settings = dict(DEFAULT_ASSISTANT_SETTINGS)
    settings["settings_degraded"] = False
    settings["settings_degraded_reason"] = ""
    try:
        with _assistant_db_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT key, value FROM settings
                WHERE key IN ({",".join("?" for _ in DEFAULT_ASSISTANT_SETTINGS)})
                """,
                tuple(DEFAULT_ASSISTANT_SETTINGS),
            ).fetchall()
            for row in rows:
                settings[row["key"]] = row["value"]
            settings = assistant_identity.identity_overlay_settings(conn, settings)
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


def _settings_for_model_role(role: str, fallback_settings: dict | None = None) -> dict:
    return with_role_cache_contract(runtime_settings_for_role_safe(
        _assistant_db_connect, role, fallback_settings or _assistant_settings(include_secrets=True),
    ), role=role)


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
            r["error_summary"] = str(e.get("message") or "")[:500]
        elif et == "error":
            r["saw_error_event"] = True
            if not r["terminal_event"]:
                r["final_status"] = "failed"
                r["error_type"] = r["error_type"] or "error"
                r["error_summary"] = str(ev.get("message") or "")[:500]
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
        return {
            "ok": False,
            "status": "failed",
            "returncode": proc.returncode if proc else 1,
            "duration": round(time.monotonic() - started, 2),
            "output": final_output if final_output else error_summary,
            "error_kind": error_type or "task_execution_failed",
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
    "task_network_authorization_expired": (
        "这项任务的网页搜索授权已关闭或到期，未执行联网步骤。"
        "Owner 可在控制台重新限时授权后新建任务。"
    ),
}


def _user_error_message(error_key: str) -> str:
    """返回脱敏后的用户可读错误消息。"""
    return _USER_ERROR_MAP.get(error_key, f"工作执行器不可用（{error_key}）。")


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
    except sqlite3.Error:
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
        with opener.open(request, timeout=max(10, min(int(timeout or 60), 300))) as response:
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
        "usage": usage,
        **openai_response_facts(data, reply, usage),
        "duration": round(time.monotonic() - started, 3),
        "error": "" if reply else "empty_provider_reply",
        "error_kind": "" if reply else "empty",
    }


def _assistant_provider_test(timeout: int = 45, payload: dict | None = None) -> dict:
    fallback = _assistant_settings(include_secrets=True)
    model_item = None
    with _assistant_db_connect() as conn:
        try:
            settings, model_item = provider_test_settings(conn, payload or {}, fallback)
        except ValueError:
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
    raw_message = str(context.get("raw_message") or message or "").strip()
    display_message = str(context.get("display_message") or raw_message).strip()
    if not raw_message:
        return {"ok": False, "error": "message is required"}
    project = context.get("project") if isinstance(context.get("project"), dict) else (_current_project() or {})
    project_id = str(context.get("project_id") or project.get("id") or "").strip() or None
    request_source = str(context.get("source") or ("qq_group" if context.get("group") else "")).strip()
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
    settings = dict(context.get("settings") or _assistant_settings(include_secrets=True))
    policy = dict(context.get("policy") or _agent_policy(settings))
    mode_decision = context.get("mode_decision")
    mode_session = context.get("mode_session")
    if not mode_decision:
        mode_decision, mode_session = INTERACTION_PLANNER.decide(
            user_id=user_id,
            message=raw_message,
            settings=settings,
            policy=policy,
            history=history,
            timeout=min(int(timeout or ASSISTANT_CHAT_TIMEOUT), 90),
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
    )
    runtime_role = "conversation_reply" if str(mode_decision.get("mode") or "daily") != "work" else "work_planner"
    chat_settings = _settings_for_model_role(runtime_role, settings)
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
    attachment_context, meme = prepare_meme_attachment(
        db_connect=_assistant_db_connect,
        settings=attachment_settings,
        policy=attachment_policy,
        message=raw_message,
        mode_decision=mode_decision,
        social_cues=social_cues,
        user_id=user_id,
        intent=intent,
        selection_runtime=(select_and_reserve_meme, _settings_for_model_role("vision_caption", settings),
                           _call_openai_compatible_chat, _record_model_call),
    )
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

    result, cache_replay_metadata = run_conversation_model_reply(
        provider, chat_settings, user_id, display_message, memories, history,
        intent=intent, criteria=criteria, policy=policy, mode_decision=mode_decision,
        social_context=social_context, attachment_context=attachment_context,
        timeout=timeout,
        build_messages=_assistant_chat_messages,
        format_prompt=_format_assistant_prompt, call_model=_call_openai_compatible_chat,
        record_model=_record_model_call, run_codex=_run_codex_assistant_chat,
        cwd=_default_cwd(),
        group_reply_finalizer=group_reply_finalizer,
    )
    _record_model_call(chat_settings, result, source="assistant_chat", user_id=user_id)
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
    quality_event = None
    saved = []
    if result.get("ok") and reply:
        with ASSISTANT_LOCK:
            for fact in memory_candidates:
                saved.append(_add_memory(
                    user_id,
                    fact,
                    kind="fact",
                    source="auto",
                    score=7,
                    request_source=request_source,
                    project_id=project_id,
                ))
            INTERACTION_STORE.record_exchange(
                user_id,
                display_message,
                reply,
                mode_decision,
                source=request_source, inbound_context=context.get("inbound_context"),
                exchange_metadata=cache_replay_metadata,
            )
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
    _sync_and_enqueue_phase2_task(task)
    if task.get("status") in FINAL_STATUSES:
        consume_running_supplements(
            task, pending_messages=_pending_messages, create_task=_create_task,
            safe_cwd=_safe_cwd, save_task=_save_task_db,
        )


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
                task["started_at"] = _utc_now()
                _save_task_db(task)
            try:
                result = _run_codex_for_task(task)
                if result.get("ok"):
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
    return task


def _create_task(
    prompt: str,
    sandbox: str,
    timeout: int,
    cwd: Path,
    **metadata,
) -> dict:
    task = _build_task(prompt, sandbox, timeout, cwd, **metadata)
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
        _save_task_db(task)
        TASKS[task["id"]] = task
        TASK_QUEUE.append(task["id"])
        position = len(TASK_QUEUE)
        TASK_EVENT.set()
    payload = _public_task(task)
    payload["position"] = position
    return payload


def _create_approval_task(prompt: str, sandbox: str, timeout: int, cwd: Path, **metadata) -> dict:
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
    settings = _assistant_settings(include_secrets=True)
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
        history = _conversation_history(user_id, 8)
        memories = _list_memories(user_id=user_id, limit=6, purpose="proactive")
    social_prepared = None
    with _assistant_db_connect() as conn:
        from bridge_social_opportunity import social_opportunity_enabled
        from bridge_social_start import prepare_start_opportunity

        if social_opportunity_enabled(conn):
            social_prepared = prepare_start_opportunity(
                conn, policy, history=history, memories=memories,
            )
    history_lines = [
        {
            "role": "assistant" if item.get("role") == "assistant" else "user",
            "content": str(item.get("content") or "")[-800:],
            "created_at": str(item.get("created_at") or ""),
        }
        for item in history
        if str(item.get("content") or "").strip()
    ]
    context = {
        "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "relationship": social_prepared["relationship"] if social_prepared else (
            "群聊伙伴" if is_group else (settings.get("relationship") or "朋友")
        ),
        "persona": settings.get("persona") or "",
        "style": settings.get("style") or "",
        "topic_notes": policy.get("topic_notes") or "",
        "initiative_mode": policy.get("initiative_mode") or "balanced",
        "allowed_intents": [item for item in str(policy.get("allowed_intents") or "").split(",") if item],
        "consecutive_unanswered": int(policy.get("consecutive_unanswered") or 0),
        "recent_conversation": (
            history_lines
            if is_group
            else ([] if social_prepared else history_lines)
        ),
        "memories": [] if social_prepared else [str(item.get("content") or "")[:300] for item in memories],
        "social_opportunity": social_prepared["opportunity"] if social_prepared else None,
        "topic_candidates": social_prepared["candidates"] if social_prepared else [],
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
        if social_prepared:
            from bridge_social_start import finalize_start_failure

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
    if social_prepared:
        from bridge_social_start import finalize_start_decision

        with _assistant_db_connect() as conn:
            try:
                return finalize_start_decision(conn, social_prepared, value)
            except ValueError:
                return finalize_start_decision(
                    conn,
                    social_prepared,
                    {**value, "action": "skip", "reason": "invalid_model_social_contract"},
                )
    return value


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


def _process_group_participation_queue() -> None:
    process_group_participation_queue(globals())


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
            if stored:
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
    inbound_context = dict(inbound_context or {})
    source = "admin" if str(source or "").strip() == "admin" else QQ_TASK_SOURCE
    if not message:
        return {"ok": False, "error": "message is required"}

    with _assistant_db_connect() as conn:
        note_user_activity(conn, user_id)
    AUTOMATION_EVENT.set()

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
    )
    if formal_decision is not None:
        return formal_decision

    approved = None if formal_enabled else consume_legacy_pending(
        _assistant_db_connect,
        user_id,
        message,
        now=_utc_now(),
    )
    if approved:
        message = str(approved.get("message") or "").strip()
        force = "task"

    followup_channel, followup_conversation_ref = followup_scope(source, user_id, delivery_recipient_id)
    with ASSISTANT_LOCK:
        history = followup_history(
            inbound_context, _conversation_history, followup_conversation_ref,
            followup_channel, ASSISTANT_HISTORY_LIMIT,
        )
    action_followup = dispatch_action_followup_context(_assistant_db_connect, CONTINUITY_KERNEL, {
        "user_id": user_id, "source": source, "trace_id": trace_id, "message": message,
        "inbound_context": inbound_context, "delivery_recipient_id": delivery_recipient_id,
    })
    if action_followup is not None:
        return action_followup
    settings = _assistant_settings(include_secrets=True)
    visual_context = visual.prepare(inbound_context, "qq_private", user_id, inbound_context.get("_external_message_id") or trace_id, message, settings)
    media_retry = inbound_media_retry_notice(
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
        return media_retry

    routed, route_decision = dispatch_deterministic_route(
        assistant_connect=_assistant_db_connect, store=INTERACTION_STORE, actor_id=user_id,
        message=message, history=history, trace_id=trace_id, source=source,
        inbound_context=inbound_context, automation_preflight=_automation_execution_preflight,
        resolve_automation_target=_resolve_automation_conversation_target,
        get_fallback=lambda: _assistant_settings(include_secrets=True),
        get_role_settings=_settings_for_model_role, readiness_check=_assistant_provider_ready,
        action_commitments=ACTION_COMMITMENTS,
    )
    if routed is not None:
        if routed.get("intent") == "automation":
            AUTOMATION_EVENT.set()
        return routed

    work_followup = None
    if not approved and not _new_task_requested(message) and active_qq_task(TASKS, TASK_LOCK, QQ_TASK_SOURCE, user_id) is None:
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
        return clarification
    if work_followup and work_followup["kind"] in {"accepted", "rejected"}:
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
        return {"ok": True, "dispatch": "goal_feedback", "reply": reply, "goal_id": work_followup["goal_id"], "feedback": feedback}
    if work_followup:
        force = "task"

    reply_settings = _settings_for_model_role("conversation_reply", settings)
    attachments = (inbound_context or {}).get("attachments")
    vision_settings = None
    if isinstance(attachments, list) and attachments:
        vision_settings = _settings_for_model_role("vision_caption", settings)
    media_notice = inbound_media_notice(
        reply_settings,
        attachments,
        vision_settings=vision_settings,
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
    history = visual.history(history, visual_context)
    policy = _agent_policy(settings)
    mode_decision, mode_session = INTERACTION_PLANNER.decide(
        user_id=user_id,
        message=message,
        settings=settings,
        policy=policy,
        history=history,
        timeout=min(int(timeout or DISPATCH_CHAT_TIMEOUT), 90),
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
    intent = str(mode_decision.get("intent") or _detect_agent_intent(message))
    criteria = _acceptance_criteria(intent, message, policy, mode_decision)
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
    if not _should_dispatch_as_task(message, mode_decision, force, detect_intent=_detect_agent_intent):
        result = _assistant_chat(
            user_id=user_id,
            message=message,
            timeout=timeout,
            decision_context={
                "history": history,
                "settings": settings,
                "policy": policy,
                "mode_decision": mode_decision,
                "mode_session": mode_session,
                "source": source, "inbound_context": inbound_context,
            },
        )
        result["dispatch"] = "chat"
        result["mode_decision"] = result.get("mode_decision") or mode_decision
        result["mode_session"] = result.get("mode_session") or mode_session
        result["interaction_plan"] = mode_decision.get("interaction_plan")
        result["interaction_plan_record"] = interaction_plan_record
        return result

    if require_project and cwd is None:
        return {"ok": False, "error": "qq_project_required"}

    sandbox = _dispatch_sandbox(message, intent)
    task_timeout = _dispatch_timeout(
        message,
        sandbox,
        raw_timeout=timeout if force == "task" else None,
        work_task_timeout=WORK_TASK_TIMEOUT,
    )
    prompt = _format_dispatch_task_prompt(
        user_id=user_id,
        message=message,
        intent=intent,
        criteria=criteria,
        policy=policy,
        mode_decision=mode_decision,
        history=history,
        continuity_target=work_followup,
    )
    is_owner_task = source == "admin" or user_id in qq_super_admin_ids(
        _assistant_db_connect,
    )
    with _assistant_db_connect() as conn:
        owner_search_allowed = (
            is_owner_task and task_web_search_allowed(conn)
        )
    network_mode = "controlled"
    if owner_search_allowed:
        try:
            active_executor = _resolve_executor_snapshot()
        except RuntimeError:
            active_executor = {}
        if active_executor.get("adapter") == "codex_login":
            network_mode = "search"
    continuity_feedback = None
    if work_followup and work_followup["kind"] in {"needs_change", "corrected"}:
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
    risky = _requires_risky_confirmation(message)
    if not approved and risky and (formal_enabled or not _has_explicit_authorization(message)):
        if formal_enabled:
            paused = _create_approval_task(
                prompt, sandbox, task_timeout, cwd or _default_cwd(),
                source=source, user_id=user_id, trace_id=trace_id,
                origin_message=message, intent=intent,
                mode=str(mode_decision.get("mode") or ""),
                delivery_recipient_id=delivery_recipient_id,
                delivery_session=delivery_session,
                source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                follow_up_source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
                network_mode=network_mode,
            )
            approval, paused_task = paused["approval"], paused["task"]
            approval_code = approval["code"]
        else:
            approval = create_legacy_pending(
                _assistant_db_connect, user_id, message, trace_id=trace_id,
            )
            paused_task = None
            approval_code = approval["id"]
        reply = (
            "这项操作可能涉及重启、删除、端口、权限、密钥或生产环境变更，我还没有开始执行。\n"
            f"确认编号：{approval_code}（30 分钟有效）\n"
            f"确认后请回复：确认执行 {approval_code}；也可以回复：拒绝执行 {approval_code}"
        )
        blocks, reply = assemble_response(
            mode_decision.get("interaction_plan") or {},
            reply,
            factual_type="approval",
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

    if not approved and not _new_task_requested(message):
        active = active_qq_task(TASKS, TASK_LOCK, QQ_TASK_SOURCE, user_id)
        if active:
            task = _append_task_message(str(active.get("id") or ""), message, mode_decision, trace_id=trace_id)
            if task:
                factual_reply = _dispatch_append_reply(task)
                blocks, reply = assemble_response(
                    mode_decision.get("interaction_plan") or {},
                    factual_reply,
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
        task = _create_task(
            prompt=prompt,
            sandbox=sandbox,
            timeout=task_timeout,
            cwd=cwd or _default_cwd(),
            source=source,
            user_id=user_id,
            trace_id=trace_id,
            origin_message=message,
            intent=intent,
            mode=str(mode_decision.get("mode") or ""),
            delivery_recipient_id=delivery_recipient_id,
            delivery_session=delivery_session,
            source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
            follow_up_source_task_id=str((work_followup or {}).get("legacy_task_id") or ""),
            network_mode=network_mode,
        )
    except (RuntimeError, ValueError) as exc:
        error_kind = str(exc).split(":", 1)[0] or type(exc).__name__
        reply = _user_error_message(error_kind)
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
        }
    factual_reply = _dispatch_task_reply(task, mode_decision)
    blocks, reply = assemble_response(
        mode_decision.get("interaction_plan") or {},
        factual_reply,
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
        "acceptance_criteria": criteria,
        "continuity": continuity_feedback,
    }


_assistant_dispatch = CONTINUITY_KERNEL.wrap_dispatch(_assistant_dispatch_impl)


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
    fallback_settings = _assistant_settings(include_secrets=True)
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
    if prepared["blocked"] and prepared["blocked"].get("natural_queue"):
        AUTOMATION_EVENT.set()
    if prepared["blocked"]:
        return prepared["blocked"]
    policy, current, context_items, participation_event = (
        prepared["policy"], prepared["current"], prepared["context"], prepared["event"])
    deterministic_decision = prepared.get("deterministic_decision")
    conversation_frame = prepared.get("conversation_frame") or {}
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
        classifier_settings = _settings_for_model_role("conversation_engagement", fallback_settings)
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

    visual_context = prepare_group_visual_context(
        payload,
        group_id=group_id,
        message=message,
        fallback_settings=fallback_settings,
        is_mention=is_mention,
        conversation_frame=conversation_frame,
    )

    if not decision.get("should_reply"):
        with _assistant_db_connect() as conn:
            finalize_group_shadow(
                conn, participation_event, False,
                str(decision.get("reason") or "engagement_below_threshold"),
                group_id, payload, classifier_settings,
                conversation_frame=conversation_frame,
                interaction_decision=decision,
            )
            mark_group_decision(
                conn,
                message_id=int(current["id"]),
                group_id=group_id,
                decision=decision,
                replied=False,
            )
        return {
            "ok": True,
            "dispatch": "silent",
            "should_reply": False,
            "reason": decision.get("reason") or "model_silent",
            "decision": decision,
            "group": policy,
        }

    current, group_history = project_group_visual_context(
        current,
        context_items,
        visual_context,
        max_context=int(policy.get("max_context") or DEFAULT_GROUP_CONTEXT_LIMIT),
    )
    group_user_id = f"group:{group_id}"
    mode_session = None
    if deterministic_decision is not None:
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
        )
        if direct["result"] is not None:
            direct["result"]["group"] = policy
            return direct["result"]
        decision = direct["decision"]
        mode_session = direct["mode_session"]
        group_history = direct["group_history"]
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
    if control is None and mode in {"work", "mixed"} and work_allowed:
        if not str(payload.get("_qq_cwd") or "").strip():
            return {"ok": False, "error": "qq_project_required"}
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
        replied = bool(result.get("reply"))
    elif control is None:
        chat_settings = dict(fallback_settings)
        if not int(policy.get("meme_enabled") or 0):
            chat_settings["meme_daily_enabled"] = "0"
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
                    "is_mention": is_mention,
                    "allow_group_feedback": False,
                },
                "conversation_frame": conversation_frame,
            },
        )
        result["dispatch"] = str(result.get("dispatch") or "chat")
        replied = bool(result.get("ok") and result.get("reply"))

    with _assistant_db_connect() as conn:
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
        )
    result.update(should_reply=replied, group=policy, group_decision=decision)
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


def _retry_task(task_id: str) -> tuple[dict | None, str]:
    return retry_task(
        task_id, lock=TASK_LOCK, hot_tasks=TASKS, db_connect=_db_connect,
        row_to_task=_row_to_task, retryable_statuses=RETRYABLE_STATUSES,
        default_cwd=DEFAULT_CWD, safe_cwd=_safe_cwd, create_task=_create_task,
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


def _mihomo_set_proxy(group: str, node: str) -> tuple[bool, str]:
    try:
        status, _ = _mihomo_api(
            f"/proxies/{quote(group, safe='')}",
            method="PUT",
            payload={"name": node},
            timeout=8,
        )
        return status in {200, 204}, ""
    except Exception as exc:
        return False, str(exc)


def _mihomo_set_related_groups(proxies: dict, primary_group: str, node: str) -> dict:
    results = {}
    groups = [primary_group]
    for group in ("GLOBAL", "Proxies", "OpenAI", "Final"):
        if group not in groups:
            groups.append(group)
    for group in groups:
        info = proxies.get(group) or {}
        allowed = info.get("all") or []
        if node not in allowed:
            continue
        ok, error = _mihomo_set_proxy(group, node)
        results[group] = {"ok": ok, "error": error}
    return results


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


def _set_proxy_mode(mode: str) -> dict:
    started = time.monotonic()
    mode = (mode or "").strip().lower()
    if mode not in {"rule", "global", "direct"}:
        return {"ok": False, "error": "invalid_proxy_mode", "mode": mode}
    try:
        status, _ = _mihomo_api("/configs", method="PATCH", payload={"mode": mode}, timeout=8)
    except Exception as exc:
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": f"set_proxy_mode_failed: {exc}",
            "mode": mode,
        }
    return {
        "ok": status in {200, 204},
        "duration": round(time.monotonic() - started, 2),
        "mode": mode,
        "status": status,
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


def _load_managed_subscriptions() -> list[dict]:
    try:
        data = json.loads(MIHOMO_SUBSCRIPTION_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data if isinstance(data, list) else data.get("subscriptions", [])
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
    managed = _load_managed_subscriptions()
    active = next((item for item in managed if isinstance(item, dict) and item.get("active")), None)
    return {
        "ok": True,
        "active_key": str((active or {}).get("key") or ""),
        "managed": [
            {
                "key": item.get("key", ""),
                "name": item.get("name", ""),
                "provider": item.get("provider", ""),
                "group": item.get("group", ""),
                "url": _redact_url(str(item.get("url") or "")),
                "format": item.get("format", "unknown"),
                "node_count": item.get("node_count"),
                "last_status": item.get("last_status", "unknown"),
                "last_error": item.get("last_error", ""),
                "active": bool(item.get("active")),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
            }
            for item in managed
            if isinstance(item, dict)
        ],
    }


def _mihomo_config_test() -> tuple[bool, str]:
    if OPS_BROKER_REQUIRED:
        try:
            result = _ops_broker_request("config_test", "mihomo", {"timeout_seconds": 20})
        except OpsBrokerClientError as exc:
            return False, str(exc)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return bool(result.get("ok") and data.get("ok")), str(data.get("output") or result.get("error") or "")
    ok, output = _capture_command(
        ["docker", "exec", MIHOMO_CONTAINER, "/mihomo", "-t", "-d", "/root/.config/mihomo"],
        timeout=20,
    )
    return ok, output


def _reload_mihomo_config() -> tuple[bool, str]:
    container_path = os.environ.get("MIHOMO_CONTAINER_CONFIG_PATH", "/root/.config/mihomo/config.yaml")
    try:
        status, _ = _mihomo_api(
            "/configs?force=true",
            method="PUT",
            payload={"path": container_path},
            timeout=15,
        )
    except Exception as exc:
        return False, str(exc)
    if status not in {200, 204}:
        return False, f"mihomo_reload_status_{status}"
    deadline = time.monotonic() + 12
    last_error = ""
    while time.monotonic() < deadline:
        try:
            _mihomo_api("/configs", timeout=5)
            return True, ""
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.5)
    return False, last_error or "mihomo_reload_unhealthy"


def _proxy_subscription_manager() -> UserSubscriptionStore:
    return UserSubscriptionStore(
        config_path=MIHOMO_CONFIG_PATH,
        state_path=MIHOMO_SUBSCRIPTION_STATE_PATH,
        provider_dir=MIHOMO_CONFIG_DIR / "proxy-providers",
        backup_root=Path(os.environ.get("AGENT_BACKUP_ROOT", "/opt/agent-stack/backups")),
        config_test=_mihomo_config_test,
        reload_config=_reload_mihomo_config,
    )


def _save_proxy_subscription(name: str, url: str, key: str = "") -> dict:
    if yaml is None:
        return {"ok": False, "error": "pyyaml_missing"}
    result = _proxy_subscription_manager().save(name, url, key=key)
    if not result.get("ok"):
        return result
    summary = _subscription_summary_from_config()
    summary.update(result)
    return summary


def _proxy_subscription_operation(action: str, key: str) -> dict:
    manager = _proxy_subscription_manager()
    if action == "refresh":
        result = manager.refresh(key)
    elif action == "switch":
        result = manager.switch(key)
    else:
        result = manager.delete(key)
    if result.get("ok"):
        summary = _subscription_summary_from_config()
        summary.update(result)
        return summary
    return result


def _curl_proxy_probe(target: dict, timeout: int = 12) -> dict:
    return proxy_target_probe(target, proxy=MIHOMO_PROXY_URL, timeout=timeout)


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
    usable_delays = [item for item in delay_results if item.get("ok")]
    usable_delays.sort(key=lambda item: (item.get("delay") is None, item.get("delay") or 999999))
    if not auto_switch:
        return read_only_diagnostics(
            group=group,
            proxy_url=MIHOMO_PROXY_URL,
            current=current,
            candidates=candidates,
            delay_results=delay_results,
            duration=time.monotonic() - started,
        )

    ordered_names = [str(item.get("name") or "") for item in usable_delays]
    results = []
    first_usable = ""
    restore_error = ""
    switch_results = {}
    for node in ordered_names:
        switched, switch_error = _mihomo_set_proxy(group, node)
        if not switched:
            results.append(
                {
                    "name": node,
                    "ok": False,
                    "switch_error": switch_error or "switch_failed",
                    "tests": [],
                },
            )
            continue
        tests = [_curl_proxy_probe(target) for target in PROXY_TEST_TARGETS]
        required_tests = [test for test in tests if test.get("required")]
        usable = bool(required_tests) and all(test.get("ok") for test in required_tests)
        results.append({"name": node, "ok": usable, "tests": tests})
        if usable and not first_usable:
            first_usable = node
            if auto_switch:
                break

    if auto_switch and first_usable:
        current_after = first_usable
        switch_results = _mihomo_set_related_groups(proxies, group, first_usable)
    else:
        current_after = current
        if current:
            restored, restore_error = _mihomo_set_proxy(group, current)
            if not restored:
                restore_error = restore_error or "restore_failed"

    if first_usable and auto_switch:
        recommendation = f"已切换到真实可用节点：{first_usable}。"
    elif first_usable:
        recommendation = f"检测到真实可用节点：{first_usable}。当前未切换，可点击“检测并切换可用节点”。"
    elif results:
        recommendation = "已测试节点都无法稳定访问 ChatGPT/OpenAI；需要更换代理订阅、节点或出站策略。"
    else:
        recommendation = "mihomo 未返回可检测的真实节点。"

    return {
        "ok": True,
        "duration": round(time.monotonic() - started, 2),
        "group": group,
        "proxy_url": MIHOMO_PROXY_URL,
        "current_before": current,
        "current_after": current_after,
        "auto_switch": auto_switch,
        "switched": bool(auto_switch and first_usable),
        "usable": bool(first_usable),
        "usable_node": first_usable,
        "tested": len(results),
        "candidate_count": len(candidates),
        "restore_error": restore_error,
        "switch_results": switch_results,
        "targets": PROXY_TEST_TARGETS,
        "results": results,
        "recommendation": recommendation,
    }


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


def _qq_refresh_qrcode(wait_seconds: int = 25) -> dict:
    if QQ_ADAPTER == "llbot":
        return {
            "ok": False,
            "error": "llbot_webui_tunnel_required",
            "diagnostics": _qq_diagnostics(),
        }
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
        return False, b"", "llbot_webui_tunnel_required"
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
        "error": "" if ok else output,
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
    source_label = {
        "official": "GitHub Trending 官方页面",
        "fallback": "GitHub Search API 近似结果",
        "cache": "本地缓存",
    }.get(source_quality, "未知来源")
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
        if item["stars_today"]:
            heat = f"今日新增 {item['stars_today']} stars"
        elif item["stars"]:
            heat = f"总计 {item['stars']} stars"
        else:
            heat = "热度未返回"
        description = item["description"] or "仓库未提供简介。"
        lines.extend(
            [
                f"{idx}. {item['repo']}",
                f"   技术栈：{language}",
                f"   热度：{heat}",
                f"   用途：{description}",
                f"   链接：{item['url']}",
            ],
        )
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
        "AI in:name,description,topics " if topic == "ai" else ""
    )
    query = f"{topic_query}created:>{created_after} fork:false"
    excluded = {str(value).strip().lower() for value in (exclude_repos or set()) if str(value).strip()}
    per_page = min(max((limit + len(excluded)) * 3, 30), 100)
    url = (
        "https://api.github.com/search/repositories"
        f"?q={quote(query, safe=':')}&sort=stars&order=desc&per_page={per_page}"
    )
    ok, body = _short_command(
        [
            "curl",
            "-fsSL",
            "--http1.1",
            "--connect-timeout",
            "8",
            "--max-time",
            "20",
            "-A",
            "Mozilla/5.0",
            "-H",
            "Accept: application/vnd.github+json",
            url,
        ],
        timeout=25,
        env=_direct_command_env(),
    )
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
        repos.append(
            {
                "repo": repo,
                "url": item.get("html_url") or f"https://github.com/{repo}",
                "description": description,
                "language": str(item.get("language") or "").strip(),
                "stars_today": "",
                "stars": f"{stars:,}" if isinstance(stars, int) else "",
            },
        )
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
        search, search_error = _github_search_fallback(
            since,
            limit,
            topic=topic,
            exclude_repos=exclude_repos,
        )
        if search:
            return _format_github_trending(
                since,
                search["repos"],
                search["url"],
                started,
                note="按任务主题使用 GitHub 官方 Search API，并排除该任务历史已推送仓库。",
                source_quality="fallback",
                topic=topic,
            )
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": search_error or "GitHub topic search failed",
            "output": "GitHub AI / AI Agent 热门项目实时获取失败。",
        }
    url = f"https://github.com/trending?since={since}"
    ok, body = _short_command(
        [
            "curl",
            "-fsSL",
            "--compressed",
            "--http1.1",
            "--retry",
            "2",
            "--retry-delay",
            "2",
            "--connect-timeout",
            "8",
            "--max-time",
            "25",
            "-A",
            "Mozilla/5.0",
            url,
        ],
        timeout=35,
    )
    if not ok:
        fallback, fallback_error = _github_search_fallback(since, limit)
        if fallback:
            _write_trending_cache(since, fallback["url"], fallback["repos"])
            return _format_github_trending(
                since,
                fallback["repos"],
                fallback["url"],
                started,
                note="GitHub Trending 官方页面暂时访问失败，已改用近似搜索结果。",
                source_quality="fallback",
            )

        cached = _read_trending_cache(since)
        if cached:
            return _format_github_trending(
                since,
                cached["repos"][:limit],
                cached.get("url") or url,
                started,
                cached=True,
                cached_at=cached.get("cached_at"),
                note=f"实时获取失败，已使用缓存。{fallback_error or body}",
                source_quality="cache",
            )
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": body or "failed to fetch GitHub Trending",
            "output": "GitHub 热榜实时获取失败，且没有可用缓存。\n"
            + (fallback_error or body or "failed to fetch GitHub Trending"),
        }

    articles = re.findall(
        r'<article class="Box-row".*?</article>',
        body,
        flags=re.S,
    )
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
        repos.append(
            {
                "repo": repo,
                "url": f"https://github.com/{repo}",
                "description": _clean_html_text(desc_match.group(1)) if desc_match else "",
                "language": _clean_html_text(lang_match.group(1)) if lang_match else "",
                "stars_today": _clean_html_text(today_match.group(1)) if today_match else "",
                "stars": _clean_html_text(stars_match.group(1)) if stars_match else "",
            },
        )

    if not repos:
        fallback, fallback_error = _github_search_fallback(since, limit)
        if fallback:
            _write_trending_cache(since, fallback["url"], fallback["repos"])
            return _format_github_trending(
                since,
                fallback["repos"],
                fallback["url"],
                started,
                note="GitHub Trending 页面解析不到仓库，已改用近似搜索结果。",
                source_quality="fallback",
            )

        cached = _read_trending_cache(since)
        if cached:
            return _format_github_trending(
                since,
                cached["repos"][:limit],
                cached.get("url") or url,
                started,
                cached=True,
                cached_at=cached.get("cached_at"),
                note="实时页面解析不到仓库，已使用缓存。",
                source_quality="cache",
            )
        return {
            "ok": False,
            "duration": round(time.monotonic() - started, 2),
            "error": "failed to parse GitHub Trending",
            "output": f"GitHub 热榜解析失败。\n{fallback_error}",
        }

    _write_trending_cache(since, url, repos)
    return _format_github_trending(since, repos, url, started)


MEME_HTTP_API = MemeHttpApi(_assistant_db_connect, _json_response, _binary_response)
PET_HTTP_API = PetHttpApi(_assistant_db_connect, _json_response, _binary_response)
ASSISTANT_IDENTITY_HTTP_API = AssistantIdentityHttpApi(_assistant_db_connect, _json_response)
PERSONA_RUNTIME_HTTP_API = PersonaRuntimeHttpApi(_assistant_db_connect, _json_response)
CONVERSATION_MEMORY_HTTP_API = ConversationMemoryHttpApi(_assistant_db_connect, _json_response)
KNOWLEDGE_HTTP_API = KnowledgeHttpApi(_assistant_db_connect, _json_response)
LEARNING_HTTP_API = LearningHttpApi(_assistant_db_connect, _json_response)
NETWORK_POLICY_HTTP_API = NetworkPolicyHttpApi(
    _assistant_db_connect,
    _json_response,
)
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
for worker_id in ("approval_expiry","automation","knowledge_ingestion"):
    WORKER_HEALTH.register(worker_id,stale_after_seconds=180)


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
    conversation_memory_http_api = CONVERSATION_MEMORY_HTTP_API
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
        )

    def _authorized(self) -> bool:
        return self._principal() in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}

    def _request_authorized(self, method: str, path: str) -> bool:
        return route_allowed(self._principal(), method, path)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
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
        if not self._request_authorized("GET", path):
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
        if CONVERSATION_MEMORY_HTTP_API.handle_get(self, path, query):
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
        if NETWORK_POLICY_HTTP_API.handle_get(self, path, query):
            return
        if ARTIFACT_RUNTIME.api.handle_get(self, path, query):
            return
        if GATE8_HTTP_API.handle_get(self, path, query):
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
        if not self._request_authorized("POST", path):
            _json_response(self, 403, {"ok": False, "error": "forbidden"})
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
                _legacy_phase2_delivery_marker(task_id, delivery_status, delivery_error)
            except LeaseLostError as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
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
            and path != QQ_RUNTIME_HTTP_API.HEARTBEAT_PATH
            and not RELIABILITY_HTTP_API.matches_post(path)
            and not PROJECT_HTTP_API.matches_post(path)
            and not KNOWLEDGE_HTTP_API.matches_post(path)
            and not LEARNING_HTTP_API.matches_post(path)
            and not NETWORK_POLICY_HTTP_API.matches_post(path)
            and not PERSONA_RUNTIME_HTTP_API.matches_post(path)
        ):
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        maximum = 65536 if (
            ARTIFACT_RUNTIME.api.matches_post(path)
            or PROJECT_HTTP_API.matches_post(path)
            or KNOWLEDGE_HTTP_API.matches_post(path)
            or PERSONA_RUNTIME_HTTP_API.matches_post(path)
        ) else 16 * 1024 * 1024
        payload, status, error = read_json_object(self, maximum)
        if error:
            _json_response(self, status, {"ok": False, "error": error})
            return

        if PROJECT_HTTP_API.handle_post(self, path, payload, self._principal()):
            return
        if KNOWLEDGE_HTTP_API.handle_post(self, path, payload):
            return
        if LEARNING_HTTP_API.handle_post(self, path, payload):
            return
        if NETWORK_POLICY_HTTP_API.handle_post(self, path, payload):
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
        if GATE8_HTTP_API.handle_post(self, path, payload):
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
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, result)
            return

        if path == "/assistant/models/provider":
            try:
                with _assistant_db_connect() as conn:
                    provider = upsert_provider(conn, payload)
                with _assistant_db_connect() as conn:
                    apply_results = apply_profiles_for_dependency(conn, provider_id=provider["id"])
                with _assistant_db_connect() as conn:
                    registry = list_model_registry(conn)
                with _assistant_db_connect() as conn:
                    prune_unreferenced_provider_secrets(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
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
                _json_response(self, 409, dependency_error_payload(exc))
                return
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, **deleted, **registry})
            return

        if path == "/assistant/models/model":
            try:
                with _assistant_db_connect() as conn:
                    model = upsert_model(conn, payload)
                with _assistant_db_connect() as conn:
                    apply_results = apply_profiles_for_dependency(conn, model_id=model["id"])
                with _assistant_db_connect() as conn:
                    registry = list_model_registry(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
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
                _json_response(self, 409, dependency_error_payload(exc))
                return
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, **deleted, **registry})
            return

        if path == "/assistant/models/bind":
            try:
                with _assistant_db_connect() as conn:
                    binding = bind_model_role(conn, payload)
                    registry = list_model_registry(conn)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "binding": binding, **registry})
            return

        if path == "/assistant/models/test":
            try:
                result = _assistant_provider_test(timeout=45, payload=payload)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, result)
            return

        if path == "/assistant/models/discover":
            try:
                if str(payload.get("action") or "").strip() == "validate":
                    result = _assistant_discovered_model_playground(
                        payload, timeout=max(20, min(int(payload.get("timeout") or 90), 300)),
                    )
                else:
                    with _assistant_db_connect() as conn:
                        result = discover_provider_models(
                            conn, payload.get("provider_id"), opener_for_url=_provider_request_opener,
                        )
            except ValueError as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": str(exc)})
                return
            # The provider completed the request even when it rejected the
            # selected model. Preserve typed guidance instead of a generic 5xx.
            _json_response(self, 200, result)
            return

        if path == "/assistant/models/playground":
            try:
                result = _assistant_model_playground(
                    payload,
                    timeout=max(20, min(int(payload.get("timeout") or 90), 300)),
                )
            except ValueError as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200 if result.get("ok") else 502, result)
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
            try:
                timeout = max(20, min(int(payload.get("timeout") or 120), 300))
                dispatch_payload = with_qq_transport_metadata(
                    payload,
                    self.headers,
                    default_actor=str(payload.get("sender_id") or ""),
                )
                result = execute_inbound_once(
                    _assistant_db_connect, self.headers.get("X-QQ-Message-ID", ""),
                    self.headers.get("X-QQ-Actor-ID", ""),
                    str(payload.get("group_id") or payload.get("session") or ""), payload,
                    lambda: _dispatch_qq_response_if_enabled(
                        lambda: _assistant_group_dispatch(dispatch_payload, timeout=timeout),
                        dispatch_payload,
                        scope="group",
                    ),
                )
            except (InboundConflictError, InboundProcessingError) as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                print(
                    "assistant_group_dispatch_failed "
                    f"error={type(exc).__name__}",
                    flush=True,
                )
                _json_response(self, 500, {
                    "ok": False,
                    "error": "assistant_group_dispatch_failed",
                    "error_kind": "internal",
                })
                return
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
                    job = upsert_automation_job(conn, payload)
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
            group = str(payload.get("group") or "Proxies").strip() or "Proxies"
            node = str(payload.get("node") or payload.get("name") or "").strip()
            if not node:
                _json_response(self, 400, {"ok": False, "error": "node is required"})
                return
            switched, error = _mihomo_set_proxy(group, node)
            _json_response(
                self,
                200 if switched else 400,
                {"ok": switched, "group": group, "node": node, "error": error},
            )
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

        if path == "/proxy/config":
            mode = str(payload.get("mode") or "").strip()
            result = _set_proxy_mode(mode)
            _json_response(self, 200 if result.get("ok") else 400, result)
            return

        if path == "/proxy/subscriptions":
            name = str(payload.get("name") or "").strip()
            url = str(payload.get("url") or "").strip()
            key = str(payload.get("key") or "").strip()
            result = _save_proxy_subscription(name, url, key)
            _json_response(self, 200 if result.get("ok") else 400, result)
            return

        if path in {"/proxy/subscriptions/refresh", "/proxy/subscriptions/switch", "/proxy/subscriptions/delete"}:
            key = str(payload.get("key") or "").strip()
            if not key:
                _json_response(self, 400, {"ok": False, "error": "subscription_key_required"})
                return
            action = path.rsplit("/", 1)[-1]
            result = _proxy_subscription_operation(action, key)
            _json_response(self, 200 if result.get("ok") else 400, result)
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
            if QQ_ADAPTER == "llbot":
                _json_response(
                    self,
                    409,
                    {
                        "ok": False,
                        "error": "llbot_webui_tunnel_required",
                        "diagnostics": _qq_diagnostics(),
                    },
                )
                return
            if payload.get("confirm_restart") is not True:
                _json_response(self, 409, {"ok": False, "error": "napcat_restart_confirmation_required"})
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
            if self._principal() is PrincipalKind.QQ_CHANNEL:
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
                result = execute_inbound_once(
                    _assistant_db_connect, self.headers.get("X-QQ-Message-ID", ""),
                    self.headers.get("X-QQ-Actor-ID", ""), user_id, payload,
                    lambda: _dispatch_qq_response_if_enabled(
                        lambda: _assistant_dispatch(
                            user_id=user_id, message=message, timeout=timeout, trace_id=trace_id,
                            force=force,
                            source="admin" if str(payload.get("source") or "").strip() == "web-console" else QQ_TASK_SOURCE,
                            cwd=Path(str(payload["_qq_cwd"])) if payload.get("_qq_cwd") else None,
                            require_project=bool(payload.get("_qq_project_guard")),
                            delivery_recipient_id=user_id,
                            delivery_session=str(dispatch_payload.get("session") or ""),
                            inbound_context=dispatch_payload,
                        ),
                        dispatch_payload,
                        scope="private",
                    ),
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
            except (InboundConflictError, InboundProcessingError) as exc:
                _json_response(self, 409, {"ok": False, "error": str(exc)})
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
                if requested_network == "search":
                    source = str(payload.get("source") or "admin").strip() or "admin"
                    user_id = str(payload.get("user_id") or "").strip()
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
                    source=str(payload.get("source") or "admin").strip() or "admin",
                    user_id=str(payload.get("user_id") or "").strip(),
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
    server = ThreadingServer((LISTEN_HOST, LISTEN_PORT), BridgeHandler)
    print(f"codex-qq-bridge listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()
