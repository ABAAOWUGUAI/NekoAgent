#!/usr/bin/env python3
"""Server-owned execution contracts for durable natural-language work.

The natural-language parser may preserve business constraints, but this module
is the only place that turns those constraints into a bounded lightweight
Capability contract.  Conversation history and Skill metadata are deliberately
not inputs to contract derivation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from bridge_capabilities import CapabilityCatalog


EXECUTION_CONTRACT_SCHEMA_VERSION = 1
_CAPABILITY_CATALOG = CapabilityCatalog()
_ALLOWED_STATUS = {"ready", "needs_clarification", "unsupported"}
_ALLOWED_OUTPUT_KINDS = {
    "clock",
    "weather_forecast",
    "github_trending",
    "reminder",
    "agent_task",
}
_CAPABILITY_OUTPUTS = {
    "clock.current.read": ("clock", False),
    "weather.forecast.read": ("weather_forecast", True),
    "github.trending.read": ("github_trending", True),
}
_CONTRACT_FIELDS = {
    "schema_version",
    "capability_id",
    "arguments",
    "status",
    "missing_inputs",
    "network_required",
    "output_kind",
}


def _text(value: object, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _bounded_int(value: object, *, default: int, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except OverflowError as exc:
        raise ValueError("automation_execution_contract_integer_overflow") from exc
    except (TypeError, ValueError):
        number = default
    return max(lower, min(number, upper))


def _utf8_size(value: str, *, error: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(error) from exc


def validate_json_budget(value: object, *, max_depth: int = 16, max_nodes: int = 256) -> None:
    active: set[int] = set()
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("automation_execution_contract_arguments_too_deep")
        if isinstance(item, Mapping):
            marker = id(item)
            if marker in active:
                raise ValueError("automation_execution_contract_arguments_cycle")
            active.add(marker)
            try:
                for key, child in item.items():
                    if not isinstance(key, str) or _utf8_size(
                        key, error="automation_execution_contract_argument_encoding_invalid"
                    ) > 160:
                        raise ValueError("automation_execution_contract_argument_key_invalid")
                    visit(child, depth + 1)
            finally:
                active.remove(marker)
        elif isinstance(item, list):
            marker = id(item)
            if marker in active:
                raise ValueError("automation_execution_contract_arguments_cycle")
            active.add(marker)
            try:
                for child in item:
                    visit(child, depth + 1)
            finally:
                active.remove(marker)
        elif isinstance(item, str):
            if _utf8_size(item, error="automation_execution_contract_argument_encoding_invalid") > 4096:
                raise ValueError("automation_execution_contract_argument_string_too_large")
        elif item is None or isinstance(item, (bool, int)):
            return
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("automation_execution_contract_argument_number_invalid")
        else:
            raise ValueError("automation_execution_contract_argument_type")

    visit(value, 0)


def _base(*, capability_id: str | None, output_kind: str, network_required: bool) -> dict:
    return {
        "schema_version": EXECUTION_CONTRACT_SCHEMA_VERSION,
        "capability_id": capability_id,
        "arguments": {},
        "status": "ready",
        "missing_inputs": [],
        "network_required": bool(network_required),
        "output_kind": output_kind,
    }


def _validate_arguments(capability_id: str, arguments: Mapping[str, object]) -> None:
    """Validate bounded arguments against the code-defined capability schema."""

    try:
        manifest = _CAPABILITY_CATALOG.manifest(capability_id)
    except KeyError as exc:
        raise ValueError("automation_execution_contract_capability_invalid") from exc
    schema = manifest.input_schema if isinstance(manifest.input_schema, Mapping) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    if schema.get("additionalProperties") is False and set(arguments) - set(properties):
        raise ValueError("automation_execution_contract_arguments_unknown")
    for name in schema.get("required") or []:
        if name not in arguments:
            raise ValueError("automation_execution_contract_argument_required")
    for name, value in arguments.items():
        rule = properties.get(name)
        if not isinstance(rule, Mapping):
            continue
        value_type = str(rule.get("type") or "")
        if value_type == "string":
            if not isinstance(value, str):
                raise ValueError("automation_execution_contract_argument_type")
            if "minLength" in rule and len(value) < int(rule["minLength"]):
                raise ValueError("automation_execution_contract_argument_min_length")
            if "maxLength" in rule and len(value) > int(rule["maxLength"]):
                raise ValueError("automation_execution_contract_argument_max_length")
        elif value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("automation_execution_contract_argument_type")
            if "minimum" in rule and value < int(rule["minimum"]):
                raise ValueError("automation_execution_contract_argument_minimum")
            if "maximum" in rule and value > int(rule["maximum"]):
                raise ValueError("automation_execution_contract_argument_maximum")
        elif value_type == "array":
            if not isinstance(value, list):
                raise ValueError("automation_execution_contract_argument_type")
            if "maxItems" in rule and len(value) > int(rule["maxItems"]):
                raise ValueError("automation_execution_contract_argument_max_items")
            item_rule = rule.get("items")
            if isinstance(item_rule, Mapping) and item_rule.get("type") == "string":
                if any(not isinstance(item, str) for item in value):
                    raise ValueError("automation_execution_contract_argument_item_type")
        enum = rule.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise ValueError("automation_execution_contract_argument_enum")
    if capability_id == "clock.current.read" and "timezone" in arguments:
        try:
            ZoneInfo(str(arguments["timezone"]))
        except Exception as exc:
            if str(arguments["timezone"]) not in {"UTC", "Etc/UTC", "Asia/Shanghai"}:
                raise ValueError("automation_execution_contract_timezone_invalid") from exc


def _finalize(contract: dict) -> dict:
    """Normalize every derived contract before it can cross a write boundary."""

    return normalize_execution_contract(contract)


def derive_execution_contract(
    instruction: str,
    parameters: Mapping[str, object] | None,
    *,
    action_type: str = "agent",
    default_location: str = "",
) -> dict:
    """Derive a bounded server contract from structured job facts.

    ``parameters`` comes from the deterministic automation parser.  The free
    text instruction is only a compatibility hint for legacy rows; it never
    introduces an arbitrary capability.  A default location is accepted only
    when the caller has already loaded it from an explicit Owner setting.
    """

    facts = dict(parameters or {})
    action = _text(action_type, 32).lower()
    text = _text(instruction, 4000).lower()
    if action == "reminder":
        return _base(capability_id=None, output_kind="reminder", network_required=False)
    if action not in {"agent", "automation"}:
        contract = _base(capability_id=None, output_kind="agent_task", network_required=False)
        contract.update({"status": "unsupported", "missing_inputs": ["action_type"]})
        return contract

    topic = _text(facts.get("topic"), 40).lower()
    source = _text(facts.get("source"), 40).lower()
    structured_topic = topic if topic in {"weather", "clock"} else ""
    if structured_topic and source == "github":
        raise ValueError("automation_execution_contract_structured_conflict")
    if structured_topic == "weather" or (
        not structured_topic and source != "github" and ("weather" in text or "天气" in instruction)
    ):
        contract = _base(
            capability_id="weather.forecast.read",
            output_kind="weather_forecast",
            network_required=True,
        )
        location = _text(facts.get("location"), 80) or _text(default_location, 80)
        if not location:
            contract.update({"status": "needs_clarification", "missing_inputs": ["location"]})
            return contract
        horizon = facts.get("forecast_horizon_hours")
        try:
            forecast_days = max(1, min(7, math.ceil(int(horizon) / 24))) if horizon else 1
        except (TypeError, ValueError, OverflowError):
            forecast_days = 1
        contract["arguments"] = {"location": location, "forecast_days": forecast_days}
        return _finalize(contract)

    if source == "github" or (not structured_topic and "github" in text):
        contract = _base(
            capability_id="github.trending.read",
            output_kind="github_trending",
            network_required=True,
        )
        topic_value = _text(facts.get("topic"), 40).lower()
        topic_map = {"ai_agent": "ai-agent", "ai-agent": "ai-agent", "ai": "ai"}
        contract["arguments"] = {
            "period": _text(facts.get("period"), 16).lower() or "daily",
            "limit": _bounded_int(facts.get("item_limit"), default=10, lower=1, upper=20),
            "topic": topic_map.get(topic_value, ""),
            "output_language": _text(facts.get("output_language"), 16) or "auto",
        }
        return _finalize(contract)

    if structured_topic == "clock" or (
        not structured_topic and ("current time" in text or "现在几点" in instruction)
    ):
        contract = _base(capability_id="clock.current.read", output_kind="clock", network_required=False)
        contract["arguments"] = {"timezone": _text(facts.get("timezone"), 64) or "Asia/Shanghai"}
        return _finalize(contract)

    return _finalize(_base(capability_id=None, output_kind="agent_task", network_required=False))


def normalize_execution_contract(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("automation_execution_contract_invalid")
    if set(value) != _CONTRACT_FIELDS:
        raise ValueError("automation_execution_contract_fields_invalid")
    try:
        version = int(value.get("schema_version") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("automation_execution_contract_version_invalid") from exc
    if version != EXECUTION_CONTRACT_SCHEMA_VERSION:
        raise ValueError("automation_execution_contract_version_unsupported")
    capability = value.get("capability_id")
    if capability is not None:
        capability = _text(capability, 80)
        try:
            _CAPABILITY_CATALOG.manifest(capability)
        except KeyError as exc:
            raise ValueError("automation_execution_contract_capability_invalid") from exc
    status = _text(value.get("status"), 32).lower()
    if status not in _ALLOWED_STATUS:
        raise ValueError("automation_execution_contract_status_invalid")
    output_kind = _text(value.get("output_kind"), 40)
    if output_kind not in _ALLOWED_OUTPUT_KINDS:
        raise ValueError("automation_execution_contract_output_invalid")
    arguments = value.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError("automation_execution_contract_arguments_invalid")
    validate_json_budget(arguments)
    try:
        arguments_json = json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError("automation_execution_contract_arguments_not_json") from exc
    if len(arguments_json.encode("utf-8")) > 8192:
        raise ValueError("automation_execution_contract_arguments_too_large")
    missing = value.get("missing_inputs")
    if (
        not isinstance(missing, list)
        or len(missing) > 16
        or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in missing)
        or sum(
            _utf8_size(item, error="automation_execution_contract_missing_input_encoding_invalid")
            for item in missing if isinstance(item, str)
        ) > 2048
    ):
        raise ValueError("automation_execution_contract_missing_inputs_invalid")
    if not isinstance(value.get("network_required"), bool):
        raise ValueError("automation_execution_contract_network_flag_invalid")
    normalized = {
        "schema_version": version,
        "capability_id": _text(capability, 80) if capability is not None else None,
        "arguments": dict(arguments),
        "status": status,
        "missing_inputs": [item.strip() for item in missing],
        "network_required": value["network_required"],
        "output_kind": output_kind,
    }
    if capability is None and normalized["arguments"]:
        raise ValueError("automation_execution_contract_arguments_without_capability")
    if capability is not None:
        expected = _CAPABILITY_OUTPUTS.get(capability)
        if expected is None:
            raise ValueError("automation_execution_contract_capability_unsupported")
        if (output_kind, bool(normalized["network_required"])) != expected:
            raise ValueError("automation_execution_contract_capability_shape_mismatch")
    if capability is not None and (status == "ready" or arguments):
        _validate_arguments(capability, normalized["arguments"])
    if status == "ready" and missing:
        raise ValueError("automation_execution_contract_ready_with_missing_inputs")
    if status == "needs_clarification" and not missing:
        raise ValueError("automation_execution_contract_clarification_without_missing_inputs")
    return normalized


def execution_contract_hash(contract: Mapping[str, object]) -> str:
    normalized = normalize_execution_contract(contract)
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "EXECUTION_CONTRACT_SCHEMA_VERSION",
    "derive_execution_contract",
    "execution_contract_hash",
    "normalize_execution_contract",
    "validate_json_budget",
]
