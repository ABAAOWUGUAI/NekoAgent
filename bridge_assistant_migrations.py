#!/usr/bin/env python3
"""Versioned migrations and drift checks for ``assistant.sqlite3``."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from bridge_migrations import (
    Migration,
    MigrationDriftError,
    MigrationError,
    applied_migrations,
    apply_migrations,
)
from bridge_assistant_identity_schema import (
    IDENTITY_MIGRATION_CHECKSUM,
    apply_assistant_identity_v2,
    identity_source_preflight,
    require_identity_schema,
)
from bridge_conversation_memory_schema import (
    SCOPE_MIGRATION_CHECKSUM,
    apply_conversation_memory_scope_v2,
    conversation_memory_source_preflight,
    require_conversation_memory_schema,
)
from bridge_interaction_plan_schema import (
    INTERACTION_PLAN_MIGRATION_CHECKSUM,
    apply_interaction_plan_v2,
    require_interaction_plan_schema,
)
from bridge_formal_approval_schema import FORMAL_APPROVAL_FEATURE_FLAG
from bridge_artifact_schema import ARTIFACT_PREVIEW_FEATURE_FLAG
from bridge_relationship_proactive_schema import (
    RELATIONSHIP_PROACTIVE_MIGRATION_CHECKSUM,
    apply_relationship_proactive_v2,
    require_relationship_proactive_schema,
)
from bridge_qq_access_schema import (
    QQ_ACCESS_MIGRATION_CHECKSUM,
    apply_qq_access_control_v2,
    require_qq_access_schema,
)
from bridge_qq_object_schema import (
    QQ_OBJECT_MIGRATION_CHECKSUM,
    apply_qq_object_authorization_v2,
    require_qq_object_schema,
)
from bridge_reliability_schema import (
    RELIABILITY_MIGRATION_CHECKSUM,
    apply_task_message_reliability_v2,
    require_reliability_schema,
)
from bridge_qq_runtime_schema import (
    QQ_RUNTIME_MIGRATION_CHECKSUM,
    apply_qq_channel_runtime_v2,
    require_qq_runtime_schema,
)
from bridge_project_schema import (
    PROJECT_LIFECYCLE_MIGRATION_CHECKSUM,
    apply_project_lifecycle_v2,
    require_project_lifecycle_schema,
)
from bridge_knowledge_schema import (
    KNOWLEDGE_MIGRATION_CHECKSUM,
    apply_assistant_knowledge_v1,
    require_assistant_knowledge_schema,
)
from bridge_continuity_schema import (
    CONTINUITY_MIGRATION_CHECKSUM,
    apply_assistant_continuity_v1,
    require_assistant_continuity_schema,
)
from bridge_living_wiki_schema import (
    LIVING_WIKI_MIGRATION_CHECKSUM,
    apply_living_wiki_v2,
    require_living_wiki_schema,
)
from bridge_executor_profiles import (
    EXECUTOR_PROFILE_MIGRATION_CHECKSUM,
    apply_executor_profiles_v1,
    require_executor_profile_schema,
)
from bridge_conversation_participation_schema import (
    PARTICIPATION_MIGRATION_CHECKSUM,
    apply_conversation_participation_v1,
    require_conversation_participation_schema,
)
from bridge_conversation_participation_routing_schema import (
    PARTICIPATION_ROUTING_MIGRATION_CHECKSUM,
    apply_conversation_participation_routing_v1,
    require_conversation_participation_routing_schema,
)
from bridge_delivery_continuity_schema import UNIFIED_DELIVERY_FEATURE_FLAG
from bridge_automation_conversation_schema import (
    AUTOMATION_CONVERSATION_MIGRATION_CHECKSUM,
    apply_automation_conversation_v1,
    require_automation_conversation_schema,
)
from bridge_group_participation_schema import (
    GROUP_PARTICIPATION_MIGRATION_CHECKSUM,
    apply_group_participation_v1,
    require_group_participation_schema,
)
from bridge_group_topic_window_schema import (
    GROUP_TOPIC_WINDOW_MIGRATION_CHECKSUM,
    apply_group_topic_window_v1,
    require_group_topic_window_schema,
)
from bridge_action_commitment_schema import (
    ACTION_COMMITMENT_MIGRATION_CHECKSUM,
    apply_action_commitment_v1,
    require_action_commitment_schema,
)
from bridge_knowledge_ingestion_schema import KNOWLEDGE_INGESTION_MIGRATION_CHECKSUM, apply_knowledge_ingestion_v1, require_knowledge_ingestion_schema
from bridge_social_virtual_schema import (
    SOCIAL_VIRTUAL_MIGRATION_CHECKSUM,
    apply_social_virtual_v1,
    require_social_virtual_schema,
)
from bridge_proactive_messaging_schema import (
    PROACTIVE_MESSAGING_MIGRATION_CHECKSUM,
    apply_proactive_messaging_policy_v1,
    require_proactive_messaging_schema,
)
from bridge_learning_schema import (
    LEARNING_MIGRATION_CHECKSUM,
    LEARNING_POLICY_V2_MIGRATION_CHECKSUM,
    apply_learning_continuity_v1,
    apply_learning_policy_v2,
    require_learning_schema,
)
from bridge_network_policy_schema import (
    NETWORK_POLICY_MIGRATION_CHECKSUM,
    apply_network_policy_v1,
    require_network_policy_schema,
)
from bridge_continuity_kernel_schema import (
    CONTINUITY_KERNEL_MIGRATION_CHECKSUM,
    apply_continuity_kernel_v1,
    require_continuity_kernel_schema,
)
from bridge_voice_transport_probe_schema import (
    VOICE_TRANSPORT_PROBE_MIGRATION_CHECKSUM,
    apply_voice_transport_probe_v1,
    require_voice_transport_probe_schema,
)
from bridge_voice_migration_registry import VOICE_MIGRATIONS, require_voice_schemas
from bridge_assistant_audit import audit, record_security_audit
from bridge_assistant_schema_result import registered_assistant_schema_result


ASSISTANT_CORE_NAMESPACE = "assistant-core"

LEGACY_REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "settings": ("key", "value", "updated_at"),
    "projects": ("id", "name", "path", "active", "created_at", "updated_at"),
    "memories": (
        "id", "user_id", "kind", "content", "source", "score", "deleted",
        "created_at", "updated_at", "last_used_at",
    ),
    "conversations": ("id", "user_id", "role", "content", "created_at"),
    "qq_events": (
        "id", "trace_id", "user_id", "stage", "action", "status", "task_id",
        "message", "detail", "created_at",
    ),
    "quality_events": (
        "id", "user_id", "intent", "provider", "request", "response", "checks",
        "status", "issues", "tool", "fallback", "duration", "created_at",
    ),
    "mode_sessions": (
        "user_id", "mode", "intent", "confidence", "reason", "source",
        "work_lifecycle", "turn_count", "work_turns", "expires_at",
        "ended_reason", "updated_at",
    ),
    "pending_approvals": (
        "id", "user_id", "message", "trace_id", "status", "created_at",
        "expires_at", "decided_at",
    ),
}

LEGACY_REQUIRED_INDEXES = (
    "idx_memories_user",
    "idx_conversations_user",
    "idx_pending_approvals_user",
)


def _contract_payload() -> str:
    return json.dumps(
        {
            "columns": {key: list(value) for key, value in LEGACY_REQUIRED_COLUMNS.items()},
            "indexes": list(LEGACY_REQUIRED_INDEXES),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


LEGACY_CONTRACT_CHECKSUM = hashlib.sha256(_contract_payload().encode("utf-8")).hexdigest()


def inspect_legacy_schema(conn: sqlite3.Connection) -> dict:
    """Return a data-free audit of the legacy assistant schema."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    for table, required in LEGACY_REQUIRED_COLUMNS.items():
        if table not in tables:
            missing_tables.append(table)
            continue
        present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(set(required) - present)
        if missing:
            missing_columns[table] = missing
    missing_indexes = sorted(set(LEGACY_REQUIRED_INDEXES) - indexes)
    return {
        "ok": not missing_tables and not missing_columns and not missing_indexes,
        "contract_checksum": LEGACY_CONTRACT_CHECKSUM,
        "missing_tables": sorted(missing_tables),
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
    }


def require_legacy_schema(conn: sqlite3.Connection) -> dict:
    audit = inspect_legacy_schema(conn)
    if not audit["ok"]:
        parts = []
        if audit["missing_tables"]:
            parts.append("tables=" + ",".join(audit["missing_tables"]))
        if audit["missing_columns"]:
            columns = [
                f"{table}:{','.join(names)}"
                for table, names in sorted(audit["missing_columns"].items())
            ]
            parts.append("columns=" + ";".join(columns))
        if audit["missing_indexes"]:
            parts.append("indexes=" + ",".join(audit["missing_indexes"]))
        raise MigrationDriftError("assistant_legacy_schema_drift:" + "|".join(parts))
    return audit


def _baseline_legacy_schema(conn: sqlite3.Connection) -> None:
    require_legacy_schema(conn)


def _apply_provider_secret_references(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_providers'",
    ).fetchone()
    if not table:
        return
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_providers)").fetchall()}
    for name, definition in (
        ("secret_ref", "TEXT NOT NULL DEFAULT ''"),
        ("secret_version", "INTEGER NOT NULL DEFAULT 0"),
        ("secret_rotated_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE model_providers ADD COLUMN {name} {definition}")


def _require_provider_secret_schema(conn: sqlite3.Connection) -> dict | None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_providers'",
    ).fetchone()
    if not table:
        return None
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_providers)").fetchall()}
    required = {"secret_ref", "secret_version", "secret_rotated_at"}
    missing = sorted(required - columns)
    if missing:
        raise MigrationDriftError("provider_secret_schema_drift:" + ",".join(missing))
    return {"ok": True, "columns": sorted(required)}


ASSISTANT_CORE_MIGRATIONS = (
    Migration(
        version=1,
        name="legacy_schema_baseline",
        apply=_baseline_legacy_schema,
        checksum=LEGACY_CONTRACT_CHECKSUM,
    ),
    Migration(
        version=2,
        name="security_audit_events",
        statements=(
            """
            CREATE TABLE security_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                actor_type TEXT NOT NULL DEFAULT 'admin',
                channel TEXT NOT NULL DEFAULT 'web',
                client_ip TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_security_audit_created ON security_audit_events(created_at DESC)",
            "CREATE INDEX idx_security_audit_event ON security_audit_events(event_type, outcome, created_at DESC)",
        ),
    ),
    Migration(
        version=3,
        name="assistant_identity_resource_ownership",
        apply=apply_assistant_identity_v2,
        checksum=IDENTITY_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=4,
        name="conversation_memory_scope",
        apply=apply_conversation_memory_scope_v2,
        checksum=SCOPE_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=5,
        name="daily_assistant_home_projection",
        statements=(
            """
            INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
            VALUES('daily_shell_v2',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
        ),
    ),
    Migration(
        version=6,
        name="multi_intent_interaction_plan",
        apply=apply_interaction_plan_v2,
        checksum=INTERACTION_PLAN_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=7,
        name="formal_approval_cutover_flag",
        statements=(
            f"""
            INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
            VALUES('{FORMAL_APPROVAL_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
        ),
    ),
    Migration(
        version=8,
        name="artifact_preview_cutover_flag",
        statements=(
            f"""
            INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
            VALUES('{ARTIFACT_PREVIEW_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
        ),
    ),
    Migration(
        version=9,
        name="relationship_proactive_management",
        apply=apply_relationship_proactive_v2,
        checksum=RELATIONSHIP_PROACTIVE_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=10,
        name="qq_identity_role_access_control",
        apply=apply_qq_access_control_v2,
        checksum=QQ_ACCESS_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=11,
        name="qq_object_authorization",
        apply=apply_qq_object_authorization_v2,
        checksum=QQ_OBJECT_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=12,
        name="task_message_reliability",
        apply=apply_task_message_reliability_v2,
        checksum=RELIABILITY_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=13,
        name="qq_channel_runtime_configuration",
        apply=apply_qq_channel_runtime_v2,
        checksum=QQ_RUNTIME_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=14,
        name="provider_secret_file_references",
        apply=_apply_provider_secret_references,
        checksum="70b2b5bf778b908786a7c440ef5d2460c410586c8f61f06d8b4aabbd4c4c9620",
    ),
    Migration(
        version=15,
        name="project_lifecycle",
        apply=apply_project_lifecycle_v2,
        checksum=PROJECT_LIFECYCLE_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=16,
        name="curated_shared_knowledge",
        apply=apply_assistant_knowledge_v1,
        checksum=KNOWLEDGE_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=17,
        name="assistant_continuity_memory_intelligence",
        apply=apply_assistant_continuity_v1,
        checksum=CONTINUITY_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=18,
        name="living_wiki_auditable_lifecycle",
        apply=apply_living_wiki_v2,
        checksum=LIVING_WIKI_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=19,
        name="provider_owned_executor_profiles",
        apply=apply_executor_profiles_v1,
        checksum=EXECUTOR_PROFILE_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=20,
        name="conversation_participation_shadow",
        apply=apply_conversation_participation_v1,
        checksum=PARTICIPATION_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=21,
        name="deterministic_participation_routing",
        apply=apply_conversation_participation_routing_v1,
        checksum=PARTICIPATION_ROUTING_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=22,
        name="unified_delivery_cutover_flag",
        statements=(
            f"""
            INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at)
            VALUES('{UNIFIED_DELIVERY_FEATURE_FLAG}',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
        ),
    ),
    Migration(
        version=23,
        name="natural_group_participation_guardrails",
        apply=apply_group_participation_v1,
        checksum=GROUP_PARTICIPATION_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=24,
        name="social_opportunity_virtual_life_v1",
        apply=apply_social_virtual_v1,
        checksum=SOCIAL_VIRTUAL_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=25,
        name="configurable_proactive_messaging_policy_v1",
        apply=apply_proactive_messaging_policy_v1,
        checksum=PROACTIVE_MESSAGING_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=26,
        name="unified_learning_continuity_v1",
        apply=apply_learning_continuity_v1,
        checksum=LEARNING_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=27,
        name="assistant_network_policy_v1",
        apply=apply_network_policy_v1,
        checksum=NETWORK_POLICY_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=28,
        name="assistant_continuity_kernel_v1",
        apply=apply_continuity_kernel_v1,
        checksum=CONTINUITY_KERNEL_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=29,
        name="learning_admission_policy_v2",
        apply=apply_learning_policy_v2,
        checksum=LEARNING_POLICY_V2_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=30,
        name="automation_conversation_contract_v1",
        apply=apply_automation_conversation_v1,
        checksum=AUTOMATION_CONVERSATION_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=31,
        name="qq_voice_transport_probe_v1",
        apply=apply_voice_transport_probe_v1,
        checksum=VOICE_TRANSPORT_PROBE_MIGRATION_CHECKSUM,
    ),
    *VOICE_MIGRATIONS,
    Migration(
        version=36,
        name="group_topic_window_candidate_v1",
        apply=apply_group_topic_window_v1,
        checksum=GROUP_TOPIC_WINDOW_MIGRATION_CHECKSUM,
    ),
    Migration(
        version=37,
        name="interaction_action_commitment_v1",
        apply=apply_action_commitment_v1,
        checksum=ACTION_COMMITMENT_MIGRATION_CHECKSUM,
    ),
    Migration(version=38, name="knowledge_ingestion_v1", apply=apply_knowledge_ingestion_v1, checksum=KNOWLEDGE_INGESTION_MIGRATION_CHECKSUM),
)


def _validate_migration_history(conn: sqlite3.Connection) -> list[dict]:
    applied = applied_migrations(conn, ASSISTANT_CORE_NAMESPACE)
    definitions = {item.version: item for item in ASSISTANT_CORE_MIGRATIONS}
    unknown = sorted({int(item["version"]) for item in applied} - set(definitions))
    if unknown:
        raise MigrationDriftError(
            "database_has_unknown_migrations:" + ",".join(str(value) for value in unknown),
        )
    for row in applied:
        expected = definitions[int(row["version"])]
        if row["name"] != expected.name or row["checksum"] != expected.resolved_checksum():
            raise MigrationDriftError(f"migration_drift:{row['version']}")
    return applied


def assistant_core_migration_plan(conn: sqlite3.Connection) -> dict:
    """Compute the registered Assistant Core plan without mutating the database."""

    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise MigrationError("assistant_database_integrity_failed")
        schema = require_legacy_schema(conn)
        applied = _validate_migration_history(conn)
    except sqlite3.Error as exc:
        raise MigrationError("assistant_core_preflight_failed") from exc
    versions = {int(item["version"]) for item in applied}
    identity_source = None
    identity_schema = None
    conversation_memory_source = None
    conversation_memory_schema = None
    interaction_plan_schema = None
    relationship_proactive_schema = None
    qq_access_schema = None
    qq_object_schema = None
    reliability_schema = None
    qq_runtime_schema = None
    provider_secret_schema = None
    project_lifecycle_schema = None
    assistant_knowledge_schema = None
    assistant_continuity_schema = None
    living_wiki_schema = None
    executor_profile_schema = None
    conversation_participation_schema = None
    conversation_participation_routing_schema = None
    group_participation_schema = None
    group_topic_window_schema = None
    action_commitment_schema = None
    knowledge_ingestion_schema = None
    social_virtual_schema = None
    proactive_messaging_schema = None
    learning_schema = None
    network_policy_schema = None
    continuity_kernel_schema = None
    automation_conversation_schema = None
    voice_transport_probe_schema = None
    voice_message_schema = None
    voice_input_schema = None
    voice_output_schema = None
    if 3 in versions:
        identity_schema = require_identity_schema(conn)
        if 4 in versions:
            conversation_memory_schema = require_conversation_memory_schema(conn)
            if 6 in versions:
                interaction_plan_schema = require_interaction_plan_schema(conn)
                if 9 in versions:
                    relationship_proactive_schema = require_relationship_proactive_schema(conn)
                    if 10 in versions:
                        qq_access_schema = require_qq_access_schema(conn)
                        if 11 in versions:
                            qq_object_schema = require_qq_object_schema(conn)
                            if 12 in versions:
                                reliability_schema = require_reliability_schema(conn)
                                if 13 in versions:
                                    qq_runtime_schema = require_qq_runtime_schema(conn)
                                    if 14 in versions:
                                        provider_secret_schema = _require_provider_secret_schema(conn)
                                        if 15 in versions:
                                            project_lifecycle_schema = require_project_lifecycle_schema(conn)
                                            if 16 in versions:
                                                assistant_knowledge_schema = require_assistant_knowledge_schema(conn)
                                                if 17 in versions:
                                                    assistant_continuity_schema = require_assistant_continuity_schema(conn)
                                                    if 18 in versions:
                                                        living_wiki_schema = require_living_wiki_schema(conn)
                                                        if 19 in versions:
                                                            executor_profile_schema = require_executor_profile_schema(conn)
                                                            if 20 in versions:
                                                                conversation_participation_schema = require_conversation_participation_schema(conn)
                                                                if 21 in versions:
                                                                    conversation_participation_routing_schema = require_conversation_participation_routing_schema(conn)
        else:
            conversation_memory_source = conversation_memory_source_preflight(conn)
    else:
        identity_source = identity_source_preflight(conn)
    if 23 in versions:
        group_participation_schema = require_group_participation_schema(conn)
    if 36 in versions:
        group_topic_window_schema = require_group_topic_window_schema(conn)
    if 37 in versions:
        action_commitment_schema = require_action_commitment_schema(conn)
    if 38 in versions:
        knowledge_ingestion_schema = require_knowledge_ingestion_schema(conn)
    if 24 in versions:
        social_virtual_schema = require_social_virtual_schema(conn)
    if 25 in versions:
        proactive_messaging_schema = require_proactive_messaging_schema(conn)
    if 26 in versions:
        learning_schema = require_learning_schema(conn)
    if 27 in versions:
        network_policy_schema = require_network_policy_schema(conn)
    if 28 in versions:
        continuity_kernel_schema = require_continuity_kernel_schema(conn)
    if 30 in versions:
        automation_conversation_schema = require_automation_conversation_schema(conn)
    if 31 in versions:
        voice_transport_probe_schema = require_voice_transport_probe_schema(conn)
    (
        voice_message_schema,
        voice_input_schema,
        voice_output_schema,
        voice_response_policy_schema,
    ) = require_voice_schemas(conn, versions)
    pending = [
        {"version": item.version, "name": item.name, "checksum": item.resolved_checksum()}
        for item in ASSISTANT_CORE_MIGRATIONS
        if item.version not in versions
    ]
    plan = {
        "ok": True,
        "namespace": ASSISTANT_CORE_NAMESPACE,
        "integrity": integrity,
        "schema": schema,
        "identity_source": identity_source,
        "identity_schema": identity_schema,
        "conversation_memory_source": conversation_memory_source,
        "conversation_memory_schema": conversation_memory_schema,
        "interaction_plan_schema": interaction_plan_schema,
        "relationship_proactive_schema": relationship_proactive_schema,
        "qq_access_schema": qq_access_schema,
        "qq_object_schema": qq_object_schema,
        "reliability_schema": reliability_schema,
        "qq_runtime_schema": qq_runtime_schema,
        "provider_secret_schema": provider_secret_schema,
        "project_lifecycle_schema": project_lifecycle_schema,
        "assistant_knowledge_schema": assistant_knowledge_schema,
        "assistant_continuity_schema": assistant_continuity_schema,
        "living_wiki_schema": living_wiki_schema,
        "executor_profile_schema": executor_profile_schema,
        "conversation_participation_schema": conversation_participation_schema,
        "conversation_participation_routing_schema": conversation_participation_routing_schema,
        "group_participation_schema": group_participation_schema,
        "group_topic_window_schema": group_topic_window_schema,
        "social_virtual_schema": social_virtual_schema,
        "proactive_messaging_schema": proactive_messaging_schema,
        "learning_schema": learning_schema,
        "network_policy_schema": network_policy_schema,
        "continuity_kernel_schema": continuity_kernel_schema,
        "automation_conversation_schema": automation_conversation_schema,
        "voice_transport_probe_schema": voice_transport_probe_schema,
        "voice_message_schema": voice_message_schema,
        "voice_input_schema": voice_input_schema,
        "voice_output_schema": voice_output_schema,
        "voice_response_policy_schema": voice_response_policy_schema,
        "knowledge_ingestion_schema": knowledge_ingestion_schema,
        "applied": applied,
        "pending": pending,
        "would_apply": [item["version"] for item in pending],
    }
    checksum_payload = json.dumps(
        {
            "namespace": plan["namespace"],
            "contract_checksum": schema["contract_checksum"],
            "applied": [
                (item["version"], item["name"], item["checksum"])
                for item in applied
            ],
            "pending": [
                (item["version"], item["name"], item["checksum"])
                for item in pending
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    plan["plan_checksum"] = hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest()
    return plan


def validate_registered_assistant_core(conn: sqlite3.Connection) -> dict:
    """Fail closed before legacy DDL when this database has been registered."""

    try:
        applied = _validate_migration_history(conn)
    except sqlite3.Error as exc:
        raise MigrationError("assistant_core_history_unreadable") from exc
    if not applied:
        return {"registered": False, "applied": []}
    schema = require_legacy_schema(conn)
    versions = {int(item["version"]) for item in applied}
    if 2 in versions:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_audit_events'",
        ).fetchone()
        if not table:
            raise MigrationDriftError("assistant_security_audit_schema_drift")
    identity_schema = require_identity_schema(conn) if 3 in versions else None
    conversation_memory_schema = (
        require_conversation_memory_schema(conn) if 4 in versions else None
    )
    interaction_plan_schema = (
        require_interaction_plan_schema(conn) if 6 in versions else None
    )
    relationship_proactive_schema = (
        require_relationship_proactive_schema(conn) if 9 in versions else None
    )
    qq_access_schema = require_qq_access_schema(conn) if 10 in versions else None
    qq_object_schema = require_qq_object_schema(conn) if 11 in versions else None
    reliability_schema = require_reliability_schema(conn) if 12 in versions else None
    qq_runtime_schema = require_qq_runtime_schema(conn) if 13 in versions else None
    provider_secret_schema = _require_provider_secret_schema(conn) if 14 in versions else None
    project_lifecycle_schema = require_project_lifecycle_schema(conn) if 15 in versions else None
    assistant_knowledge_schema = require_assistant_knowledge_schema(conn) if 16 in versions else None
    assistant_continuity_schema = require_assistant_continuity_schema(conn) if 17 in versions else None
    living_wiki_schema = require_living_wiki_schema(conn) if 18 in versions else None
    executor_profile_schema = require_executor_profile_schema(conn) if 19 in versions else None
    conversation_participation_schema = (
        require_conversation_participation_schema(conn) if 20 in versions else None
    )
    conversation_participation_routing_schema = (
        require_conversation_participation_routing_schema(conn) if 21 in versions else None
    )
    group_participation_schema = (
        require_group_participation_schema(conn) if 23 in versions else None
    )
    group_topic_window_schema = (
        require_group_topic_window_schema(conn) if 36 in versions else None
    )
    action_commitment_schema = (
        require_action_commitment_schema(conn) if 37 in versions else None
    )
    knowledge_ingestion_schema = require_knowledge_ingestion_schema(conn) if 38 in versions else None
    social_virtual_schema = (
        require_social_virtual_schema(conn) if 24 in versions else None
    )
    proactive_messaging_schema = (
        require_proactive_messaging_schema(conn) if 25 in versions else None
    )
    learning_schema = require_learning_schema(conn) if 26 in versions else None
    network_policy_schema = require_network_policy_schema(conn) if 27 in versions else None
    continuity_kernel_schema = require_continuity_kernel_schema(conn) if 28 in versions else None
    automation_conversation_schema = (
        require_automation_conversation_schema(conn) if 30 in versions else None
    )
    voice_transport_probe_schema = (
        require_voice_transport_probe_schema(conn) if 31 in versions else None
    )
    (
        voice_message_schema,
        voice_input_schema,
        voice_output_schema,
        voice_response_policy_schema,
    ) = require_voice_schemas(conn, versions)
    return registered_assistant_schema_result(
        applied=applied,
        schema=schema,
        values=locals(),
    )


def ensure_assistant_core_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply the registered Assistant Core migrations after fail-closed planning."""

    assistant_core_migration_plan(conn)
    try:
        return apply_migrations(
            conn,
            ASSISTANT_CORE_MIGRATIONS,
            namespace=ASSISTANT_CORE_NAMESPACE,
        )
    except sqlite3.Error as exc:
        raise MigrationError("assistant_core_migration_failed") from exc


def register_after_legacy_bootstrap(conn: sqlite3.Connection) -> list[int]:
    """Commit compatibility DDL before the migration runner opens its transaction."""

    conn.commit()
    return ensure_assistant_core_migrations(conn)
