#!/usr/bin/env python3
"""Code-defined Capability manifests for the general Agent platform.

The catalog is intentionally fixed.  Database rows may store observations about a
capability, but they must never select an arbitrary Python entry point.  Runtime
handlers are wired by server code (for example by :mod:`bridge_light_executor`).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


HEALTH_STATUSES = frozenset({"unknown", "healthy", "degraded", "unhealthy", "disabled"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
COST_CLASSES = frozenset({"local", "network", "model", "sandbox", "delivery"})
IDEMPOTENCY_POLICIES = frozenset(
    {"idempotent", "read_only", "non_idempotent", "at_least_once"},
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CapabilityHealth:
    """Last known health of one fixed capability adapter."""

    status: str = "unknown"
    checked_at: str | None = None
    message: str = "not_checked"
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        if self.status not in HEALTH_STATUSES:
            raise ValueError("invalid_capability_health_status")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("invalid_capability_health_latency")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "message": self.message,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class CapabilityManifest:
    """Stable, serialisable contract for a server-owned capability."""

    id: str
    version: str
    description: str
    category: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    permissions: tuple[str, ...]
    risk_level: str
    side_effects: tuple[str, ...]
    timeout: float
    idempotency: str
    supports_cancel: bool
    cost_class: str
    health_check: Mapping[str, Any]
    default_health: CapabilityHealth = CapabilityHealth()

    def __post_init__(self) -> None:
        if not self.id or "." not in self.id:
            raise ValueError("invalid_capability_id")
        if not self.version or not self.description or not self.category:
            raise ValueError("incomplete_capability_manifest")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError("invalid_capability_risk_level")
        if self.cost_class not in COST_CLASSES:
            raise ValueError("invalid_capability_cost_class")
        if self.idempotency not in IDEMPOTENCY_POLICIES:
            raise ValueError("invalid_capability_idempotency")
        if self.timeout <= 0:
            raise ValueError("invalid_capability_timeout")
        for schema in (self.input_schema, self.output_schema):
            if schema.get("type") != "object" or "properties" not in schema:
                raise ValueError("invalid_capability_schema")
        if not self.health_check.get("kind"):
            raise ValueError("invalid_capability_health_check")

    @property
    def read_only(self) -> bool:
        return not self.side_effects and self.idempotency in {"read_only", "idempotent"}

    def to_dict(self, health: CapabilityHealth | None = None) -> dict[str, Any]:
        current_health = health or self.default_health
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "category": self.category,
            "input_schema": deepcopy(dict(self.input_schema)),
            "output_schema": deepcopy(dict(self.output_schema)),
            "permissions": list(self.permissions),
            "risk_level": self.risk_level,
            "side_effects": list(self.side_effects),
            "timeout": self.timeout,
            "idempotency": self.idempotency,
            "supports_cancel": self.supports_cancel,
            "cost_class": self.cost_class,
            "health_check": deepcopy(dict(self.health_check)),
            "health": current_health.to_dict(),
            "read_only": self.read_only,
        }


def _object_schema(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_EVIDENCE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["source_name", "source_url", "data_time", "fetched_at", "valid_until"],
    },
}


FIXED_CAPABILITY_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        id="chat.reply",
        version="1.0.0",
        description="Generate a direct conversational reply without executing tools.",
        category="conversation",
        input_schema=_object_schema(
            {
                "message": {"type": "string", "minLength": 1},
                "conversation_id": {"type": "string"},
            },
            required=("message",),
        ),
        output_schema=_object_schema(
            {"reply": {"type": "string"}, "model": {"type": "string"}},
            required=("reply",),
        ),
        permissions=("conversation.read", "model.invoke"),
        risk_level="low",
        side_effects=(),
        timeout=30.0,
        idempotency="non_idempotent",
        supports_cancel=True,
        cost_class="model",
        health_check={"kind": "runtime_role", "target": "conversation_reply"},
    ),
    CapabilityManifest(
        id="codex.sandbox",
        version="1.0.0",
        description="Run code, file, or server work inside an explicitly selected sandbox.",
        category="execution",
        input_schema=_object_schema(
            {
                "goal": {"type": "string", "minLength": 1},
                "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"]},
            },
            required=("goal", "sandbox"),
        ),
        output_schema=_object_schema(
            {
                "run_id": {"type": "string"},
                "status": {"type": "string"},
                "artifacts": {"type": "array"},
            },
            required=("run_id", "status"),
        ),
        permissions=("sandbox.execute", "files.read", "files.write_optional"),
        risk_level="high",
        side_effects=("process", "workspace_optional"),
        timeout=900.0,
        idempotency="non_idempotent",
        supports_cancel=True,
        cost_class="sandbox",
        health_check={"kind": "command", "target": "codex_login_status"},
    ),
    CapabilityManifest(
        id="platform.health.read",
        version="1.0.0",
        description="Read the platform control-plane health summary.",
        category="operations",
        input_schema=_object_schema({}),
        output_schema=_object_schema(
            {"status": {"type": "string"}, "checks": {"type": "array"}},
            required=("status", "checks"),
        ),
        permissions=("platform.health.read",),
        risk_level="low",
        side_effects=(),
        timeout=10.0,
        idempotency="read_only",
        supports_cancel=False,
        cost_class="local",
        health_check={"kind": "self", "target": "bridge"},
        default_health=CapabilityHealth(status="healthy", message="built_in"),
    ),
    CapabilityManifest(
        id="task.status.read",
        version="1.0.0",
        description="Read the status of an existing Goal Run or legacy Task.",
        category="execution",
        input_schema=_object_schema(
            {"run_id": {"type": "string"}, "legacy_task_id": {"type": "string"}},
        ),
        output_schema=_object_schema(
            {"status": {"type": "string"}, "summary": {"type": "string"}},
            required=("status",),
        ),
        permissions=("runs.read",),
        risk_level="low",
        side_effects=(),
        timeout=10.0,
        idempotency="read_only",
        supports_cancel=False,
        cost_class="local",
        health_check={"kind": "repository", "target": "runs"},
    ),
    CapabilityManifest(
        id="clock.current.read",
        version="1.0.0",
        description="Read the current time for one supported timezone.",
        category="realtime",
        input_schema=_object_schema(
            {"timezone": {"type": "string", "default": "Asia/Shanghai"}},
        ),
        output_schema=_object_schema(
            {
                "timezone": {"type": "string"},
                "local_time": {"type": "string", "format": "date-time"},
                "evidence": _EVIDENCE_SCHEMA,
            },
            required=("timezone", "local_time", "evidence"),
        ),
        permissions=("system.clock.read",),
        risk_level="low",
        side_effects=(),
        timeout=1.0,
        idempotency="read_only",
        supports_cancel=False,
        cost_class="local",
        health_check={"kind": "self", "target": "system_clock"},
        default_health=CapabilityHealth(status="healthy", message="built_in"),
    ),
    CapabilityManifest(
        id="weather.forecast.read",
        version="1.0.0",
        description="Read ordinary current conditions and a short forecast from Open-Meteo.",
        category="realtime",
        input_schema=_object_schema(
            {
                "location": {"type": "string", "minLength": 2, "maxLength": 80},
                "forecast_days": {"type": "integer", "minimum": 1, "maximum": 7},
            },
            required=("location", "forecast_days"),
        ),
        output_schema=_object_schema(
            {
                "location": {"type": "object"},
                "current": {"type": "object"},
                "daily": {"type": "array"},
                "evidence": _EVIDENCE_SCHEMA,
            },
            required=("location", "current", "daily", "evidence"),
        ),
        permissions=("network.open_meteo.read",),
        risk_level="low",
        side_effects=(),
        timeout=8.0,
        idempotency="read_only",
        supports_cancel=False,
        cost_class="network",
        health_check={
            "kind": "https_source",
            "targets": ["geocoding-api.open-meteo.com", "api.open-meteo.com"],
        },
    ),
    CapabilityManifest(
        id="github.trending.read",
        version="1.0.0",
        description="Read a GitHub Trending snapshot through a server-injected handler.",
        category="research",
        input_schema=_object_schema(
            {
                "language": {"type": "string"},
                "period": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "topic": {"type": "string", "enum": ["", "ai", "ai-agent"]},
                "output_language": {"type": "string", "enum": ["auto", "zh-CN"]},
                "dedupe_policy": {"type": "string", "enum": ["job_history", "none"]},
                "exclude_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 500,
                },
            },
            required=("period", "limit"),
        ),
        output_schema=_object_schema(
            {"items": {"type": "array"}, "evidence": _EVIDENCE_SCHEMA},
            required=("items", "evidence"),
        ),
        permissions=("network.github.read",),
        risk_level="low",
        side_effects=(),
        timeout=8.0,
        idempotency="read_only",
        supports_cancel=False,
        cost_class="network",
        health_check={"kind": "injected_handler", "target": "github_trending"},
    ),
    CapabilityManifest(
        id="automation.schedule.create",
        version="1.0.0",
        description="Create one validated durable schedule for the authorised Owner.",
        category="automation",
        input_schema=_object_schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "instruction": {"type": "string", "minLength": 1, "maxLength": 4000},
                "action_type": {"type": "string", "enum": ["reminder", "agent"]},
                "schedule_type": {"type": "string", "enum": ["daily"]},
                "time_of_day": {"type": "string"},
                "timezone": {"type": "string"},
            },
            required=("title", "instruction", "action_type", "schedule_type", "time_of_day", "timezone"),
        ),
        output_schema=_object_schema(
            {
                "job_id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "next_due_at": {"type": "string"},
                "receipt_id": {"type": "string"},
            },
            required=("job_id", "enabled", "next_due_at", "receipt_id"),
        ),
        permissions=("automation.schedule.create", "delivery.owner.private"),
        risk_level="medium",
        side_effects=("automation_job.write", "future_delivery.schedule"),
        timeout=3.0,
        idempotency="idempotent",
        supports_cancel=False,
        cost_class="local",
        health_check={"kind": "server_owned", "target": "automation_jobs"},
    ),
    CapabilityManifest(
        id="automation.schedule.update",
        version="1.0.0",
        description="Update one existing durable schedule selected from recent Owner context.",
        category="automation",
        input_schema=_object_schema(
            {
                "target_source": {"type": "string"},
                "changes": {"type": "object"},
            },
            required=("target_source", "changes"),
        ),
        output_schema=_object_schema(
            {
                "job_id": {"type": "string"},
                "updated": {"type": "boolean"},
                "receipt_id": {"type": "string"},
            },
            required=("job_id", "updated", "receipt_id"),
        ),
        permissions=("automation.schedule.update", "delivery.owner.private"),
        risk_level="medium",
        side_effects=("automation_job.write",),
        timeout=3.0,
        idempotency="idempotent",
        supports_cancel=False,
        cost_class="local",
        health_check={"kind": "server_owned", "target": "automation_jobs"},
    ),
    CapabilityManifest(
        id="automation.schedule.disable",
        version="1.0.0",
        description="Disable one existing Owner schedule while retaining its Run and Delivery audit history.",
        category="automation",
        input_schema=_object_schema(
            {
                "target_source": {"type": "string"},
                "reason": {"type": "string", "maxLength": 500},
            },
            required=("target_source",),
        ),
        output_schema=_object_schema(
            {
                "job_id": {"type": "string"},
                "disabled": {"type": "boolean"},
                "audit_retained": {"type": "boolean"},
                "receipt_id": {"type": "string"},
            },
            required=("job_id", "disabled", "audit_retained", "receipt_id"),
        ),
        permissions=("automation.schedule.disable", "delivery.owner.private"),
        risk_level="medium",
        side_effects=("automation_job.disable",),
        timeout=3.0,
        idempotency="idempotent",
        supports_cancel=False,
        cost_class="local",
        health_check={"kind": "server_owned", "target": "automation_jobs"},
    ),
    CapabilityManifest(
        id="automation.schedule.run_now",
        version="1.0.0",
        description="Queue one immediate Owner run without changing the durable schedule.",
        category="automation",
        input_schema=_object_schema(
            {
                "job_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "maxLength": 500},
            },
            required=("job_id",),
        ),
        output_schema=_object_schema(
            {
                "job_id": {"type": "string"},
                "queued": {"type": "boolean"},
                "next_due_at": {"type": "string"},
                "receipt_id": {"type": "string"},
            },
            required=("job_id", "queued", "next_due_at", "receipt_id"),
        ),
        permissions=("automation.schedule.run_now", "delivery.owner.private"),
        risk_level="medium",
        side_effects=("automation_run.queue",),
        timeout=3.0,
        idempotency="idempotent",
        supports_cancel=False,
        cost_class="local",
        health_check={"kind": "server_owned", "target": "automation_jobs"},
    ),
    CapabilityManifest(
        id="meme.discovery.search",
        version="1.0.0",
        description="Discover bounded meme candidates with provenance and hold them for owner review.",
        category="social_assets",
        input_schema=_object_schema(
            {
                "query": {"type": "string", "minLength": 2, "maxLength": 120},
                "provider": {"type": "string", "enum": ["auto", "sample_official", "openverse", "fan_reference"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            required=("query", "provider", "limit"),
        ),
        output_schema=_object_schema(
            {
                "job": {"type": "object"},
                "candidates": {"type": "array"},
                "evidence": _EVIDENCE_SCHEMA,
            },
            required=("job", "candidates", "evidence"),
        ),
        permissions=("network.open_media.read", "artifacts.meme_candidates.write"),
        risk_level="medium",
        side_effects=("local_candidate_artifact",),
        timeout=45.0,
        idempotency="non_idempotent",
        supports_cancel=False,
        cost_class="network",
        health_check={
            "kind": "https_source",
            "targets": ["www.bilibili.com", "api.openverse.org"],
        },
    ),
    CapabilityManifest(
        id="delivery.qq.send",
        version="1.0.0",
        description="Deliver an already produced result through the QQ channel adapter.",
        category="delivery",
        input_schema=_object_schema(
            {
                "delivery_id": {"type": "string"},
                "recipient": {"type": "string"},
                "content": {"type": "string"},
            },
            required=("delivery_id", "recipient", "content"),
        ),
        output_schema=_object_schema(
            {"status": {"type": "string"}, "receipt": {"type": "string"}},
            required=("status",),
        ),
        permissions=("channel.qq.send",),
        risk_level="medium",
        side_effects=("external_message",),
        timeout=30.0,
        idempotency="at_least_once",
        supports_cancel=False,
        cost_class="delivery",
        health_check={"kind": "channel", "target": "qq_send_path"},
    ),
)


_FIXED_BY_ID = {manifest.id: manifest for manifest in FIXED_CAPABILITY_MANIFESTS}
if len(_FIXED_BY_ID) != len(FIXED_CAPABILITY_MANIFESTS):  # pragma: no cover - import guard
    raise RuntimeError("duplicate_fixed_capability_id")


class CapabilityCatalog:
    """Runtime health overlay for the immutable code-defined catalog.

    There is deliberately no ``register`` method.  Adding a capability requires a
    reviewed source change and a process restart.
    """

    def __init__(self, health: Mapping[str, CapabilityHealth] | None = None):
        self._health = {manifest.id: manifest.default_health for manifest in FIXED_CAPABILITY_MANIFESTS}
        for capability_id, value in (health or {}).items():
            if capability_id not in _FIXED_BY_ID:
                raise KeyError("unknown_capability")
            if not isinstance(value, CapabilityHealth):
                raise TypeError("health_must_be_capability_health")
            self._health[capability_id] = value

    def ids(self) -> tuple[str, ...]:
        return tuple(manifest.id for manifest in FIXED_CAPABILITY_MANIFESTS)

    def get(self, capability_id: str) -> dict[str, Any]:
        manifest = _FIXED_BY_ID.get(str(capability_id or ""))
        if manifest is None:
            raise KeyError("unknown_capability")
        return manifest.to_dict(self._health[manifest.id])

    def manifest(self, capability_id: str) -> CapabilityManifest:
        manifest = _FIXED_BY_ID.get(str(capability_id or ""))
        if manifest is None:
            raise KeyError("unknown_capability")
        return manifest

    def health(self, capability_id: str) -> CapabilityHealth:
        if capability_id not in _FIXED_BY_ID:
            raise KeyError("unknown_capability")
        return self._health[capability_id]

    def set_health(
        self,
        capability_id: str,
        status: str,
        *,
        message: str,
        latency_ms: int | None = None,
        checked_at: str | None = None,
    ) -> CapabilityHealth:
        if capability_id not in _FIXED_BY_ID:
            raise KeyError("unknown_capability")
        value = CapabilityHealth(
            status=status,
            checked_at=checked_at or utc_now(),
            message=str(message or "")[:500],
            latency_ms=latency_ms,
        )
        self._health[capability_id] = value
        return value

    def list(self) -> list[dict[str, Any]]:
        return [manifest.to_dict(self._health[manifest.id]) for manifest in FIXED_CAPABILITY_MANIFESTS]


def list_fixed_capabilities() -> list[dict[str, Any]]:
    """Return a defensive JSON-ready snapshot of the fixed catalog."""

    return CapabilityCatalog().list()


def get_fixed_capability(capability_id: str) -> dict[str, Any]:
    return CapabilityCatalog().get(capability_id)


__all__ = [
    "CapabilityCatalog",
    "CapabilityHealth",
    "CapabilityManifest",
    "FIXED_CAPABILITY_MANIFESTS",
    "get_fixed_capability",
    "list_fixed_capabilities",
]
