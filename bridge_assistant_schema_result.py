"""Pure assembly for registered Assistant Core schema audits."""

from __future__ import annotations

from typing import Mapping

from bridge_qq_quality_receipt_schema import require_qq_quality_receipt_schema


REGISTERED_SCHEMA_FIELDS = (
    "identity_schema",
    "conversation_memory_schema",
    "interaction_plan_schema",
    "relationship_proactive_schema",
    "qq_access_schema",
    "qq_object_schema",
    "reliability_schema",
    "qq_runtime_schema",
    "provider_secret_schema",
    "project_lifecycle_schema",
    "assistant_knowledge_schema",
    "assistant_continuity_schema",
    "living_wiki_schema",
    "executor_profile_schema",
    "executor_verification_schema",
    "qq_quality_receipt_schema",
    "conversation_participation_schema",
    "conversation_participation_routing_schema",
    "group_participation_schema",
    "group_topic_window_schema",
    "group_topic_delivery_schema",
    "group_research_schema",
    "social_virtual_schema",
    "proactive_messaging_schema",
    "learning_schema",
    "network_policy_schema",
    "continuity_kernel_schema",
    "automation_conversation_schema",
    "voice_transport_probe_schema",
    "voice_message_schema",
    "voice_input_schema",
    "voice_output_schema",
    "voice_response_policy_schema",
    "behavior_observation_schema",
    "assistant_affect_shadow_schema",
    "behavior_benchmark_registry_schema",
    "behavior_candidate_registry_schema",
    "behavior_assistant_isolation_schema",
    "behavior_owner_authorization_schema",
    "behavior_paired_shadow_cutover_schema",
    "continuous_private_conversation_schema",
    "response_cycle_recovery_schema",
    "response_cycle_successor_schema",
    "conversation_visual_observation_schema",
    "group_response_commitment_schema",
    "response_assessment_schema",
)


def registered_assistant_schema_result(
    *,
    applied: list[dict],
    schema: dict,
    values: Mapping[str, object],
) -> dict:
    """Build the stable registered-schema result and project optional schemas."""

    projected = {
        field: values[field]
        for field in REGISTERED_SCHEMA_FIELDS
        if field != "qq_quality_receipt_schema"
    }
    projected["qq_quality_receipt_schema"] = (
        require_qq_quality_receipt_schema(values["conn"])
        if 41 in values["versions"] else None
    )

    return {
        "registered": True,
        "applied": applied,
        "schema": schema,
        **projected,
    }
