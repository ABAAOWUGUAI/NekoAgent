"""Capability-driven execution for durable automation jobs.

This module owns only the boundary from a validated execution contract to the
existing LightExecutor and Delivery Outbox.  It does not create a second task
state machine and it never turns an executor result without evidence into a
delivery.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from bridge_automation_execution_contract import (
    execution_contract_hash,
    normalize_execution_contract,
    validate_json_budget,
)
from bridge_automation_business_gate import evaluate_automation_business_verdict


_FORBIDDEN_TRANSPORT_KEYS = frozenset(
    {
        "private_text",
        "private_body",
        "private_message",
        "private_content",
        "raw_text",
        "raw_message",
        "media",
        "media_url",
        "media_urls",
        "media_data",
        "raw_media",
        "raw_media_url",
        "attachment",
        "attachments",
        "image",
        "image_url",
        "image_data",
        "video",
        "video_url",
        "audio",
        "audio_url",
        "file_bytes",
        "bytes",
        "base64",
    },
)


def _validate_safe_transport_value(value: object) -> None:
    """Reject explicit private/raw-media fields instead of guessing a redaction."""

    validate_json_budget(value, max_depth=16, max_nodes=512)

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                if key in _FORBIDDEN_TRANSPORT_KEYS:
                    raise ValueError("raw_private_or_media_field")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, (bytes, bytearray, memoryview)):
            raise ValueError("raw_private_or_media_value")

    visit(value)


def _failed(error: str, *, stage: str = "capability") -> dict:
    return {"status": "failed", "dispatch": "none", "error": str(error or "capability_failed"), "failure_stage": stage}


def execute_automation_capability(
    job: Mapping[str, object],
    contract: Mapping[str, object],
    *,
    executor: object,
    enqueue: Callable[..., dict],
    build_payload: Callable[[Mapping[str, object], Mapping[str, object], Mapping[str, object]], dict] | None = None,
    argument_overrides: Mapping[str, object] | None = None,
) -> dict:
    """Execute one admitted read capability and enqueue an evidence-backed result."""

    try:
        normalized = normalize_execution_contract(contract)
    except (TypeError, ValueError) as exc:
        return _failed(str(exc) or "execution_contract_invalid", stage="contract")
    if normalized["status"] != "ready":
        return _failed("execution_contract_not_ready", stage="contract")
    capability_id = str(normalized.get("capability_id") or "").strip()
    if not capability_id:
        return _failed("capability_not_registered", stage="capability")
    dispatch_contract = normalized
    if argument_overrides is not None:
        if not isinstance(argument_overrides, Mapping):
            return _failed("execution_arguments_invalid", stage="contract")
        try:
            dispatch_arguments = dict(normalized["arguments"])
            dispatch_arguments.update(dict(argument_overrides))
            dispatch_contract = normalize_execution_contract({**normalized, "arguments": dispatch_arguments})
        except (TypeError, ValueError) as exc:
            return _failed(str(exc) or "execution_arguments_invalid", stage="contract")
    try:
        result = executor.execute_capability(capability_id, dispatch_contract["arguments"])
    except Exception:
        return _failed("capability_execution_failed", stage="capability")
    if not isinstance(result, Mapping):
        return _failed("capability_result_invalid", stage="capability")
    if result.get("status") != "completed" or result.get("fallback"):
        return _failed(str(result.get("reason") or "capability_execution_failed"), stage="capability")
    result_capability = result.get("capability_id")
    if not isinstance(result_capability, str) or not result_capability or result_capability != capability_id:
        return _failed("capability_result_mismatch", stage="capability")
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, Mapping) for item in evidence):
        return _failed("evidence_missing", stage="evidence")
    output = result.get("output")
    if not isinstance(output, (Mapping, list, str, int, float, bool)) and output is not None:
        return _failed("capability_output_invalid", stage="evidence")
    try:
        _validate_safe_transport_value(output)
        _validate_safe_transport_value(evidence)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError):
        return _failed("evidence_invalid", stage="evidence")
    # Enforce the server-owned business verdict before any delivery is enqueued.
    # Only ``github.trending.read`` is bound to a structured verdict; other
    # capabilities keep their existing evidence-based contract here.  A result
    # that fails the business contract (insufficient items, duplicate repo,
    # off-topic for a requested ``topic``) must never be reported as success.
    if capability_id == "github.trending.read":
        verdict = evaluate_automation_business_verdict(
            capability_id,
            result,
            contract_arguments=dispatch_contract.get("arguments"),
        )
        if not verdict.get("passed"):
            return _failed(
                str(verdict.get("error_kind") or "github_trending_business_gate"),
                stage="evidence",
            )
    action_contract_hash = execution_contract_hash(normalized)
    dispatch_contract_hash = execution_contract_hash(dispatch_contract)
    payload = {
        "kind": "automation_capability_result",
        "automation_job_id": str(job.get("id") or ""),
        "automation_run_id": str(job.get("run_id") or ""),
        "user_id": str(job.get("user_id") or ""),
        "capability_id": capability_id,
        "output_kind": dispatch_contract["output_kind"],
        "output": output,
        "evidence": [dict(item) for item in evidence],
        "execution_contract_hash": dispatch_contract_hash,
        "dispatch_execution_contract_hash": dispatch_contract_hash,
        "action_execution_contract_hash": action_contract_hash,
    }
    if build_payload is not None:
        try:
            custom_payload = build_payload(result, dispatch_contract, job)
        except Exception:
            return _failed("delivery_payload_builder_failed", stage="delivery")
        if not isinstance(custom_payload, dict):
            return _failed("delivery_payload_invalid", stage="delivery")
        try:
            _validate_safe_transport_value(custom_payload)
        except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError):
            return _failed("delivery_payload_invalid", stage="delivery")
        payload = {
            **payload,
            **custom_payload,
            "automation_job_id": payload["automation_job_id"],
            "automation_run_id": payload["automation_run_id"],
            "user_id": payload["user_id"],
            "capability_id": capability_id,
            "evidence": payload["evidence"],
            "execution_contract_hash": payload["execution_contract_hash"],
            "dispatch_execution_contract_hash": payload["dispatch_execution_contract_hash"],
            "action_execution_contract_hash": payload["action_execution_contract_hash"],
        }
    # Keep the payload JSON-safe before it reaches the outbox adapter.
    try:
        _validate_safe_transport_value(payload)
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError):
        return _failed("delivery_payload_invalid", stage="delivery")
    try:
        delivery = enqueue(
            dedupe_key=f"qq:automation:{job.get('id')}:{job.get('scheduled_for')}",
            channel="qq",
            destination=str(job.get("user_id") or ""),
            payload=payload,
            max_attempts=100,
            thread_ref=f"qq:automation:{job.get('user_id')}:{job.get('id')}",
            delivery_class="operational",
        )
    except Exception:
        return _failed("delivery_enqueue_failed", stage="delivery")
    delivery_id = str((delivery or {}).get("id") or "") if isinstance(delivery, Mapping) else ""
    if not delivery_id:
        return _failed("delivery_enqueue_unconfirmed", stage="delivery")
    return {
        "status": "dispatched",
        "dispatch": "capability",
        "delivery_id": delivery_id,
        "capability_id": capability_id,
        "payload": payload,
    }


__all__ = ["execute_automation_capability"]
