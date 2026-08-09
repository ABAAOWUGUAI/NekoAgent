"""Pure assembly for registered Assistant Core schema audits."""

from __future__ import annotations

from typing import Mapping


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
    "conversation_participation_schema",
    "conversation_participation_routing_schema",
    "group_participation_schema",
    "group_topic_window_schema",
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
)


def registered_assistant_schema_result(
    *,
    applied: list[dict],
    schema: dict,
    values: Mapping[str, object],
) -> dict:
    """Build the stable registered-schema result without inspecting a database."""

    return {
        "registered": True,
        "applied": applied,
        "schema": schema,
        **{field: values[field] for field in REGISTERED_SCHEMA_FIELDS},
    }
