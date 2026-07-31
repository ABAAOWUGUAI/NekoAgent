"""Normalization helpers for durable legacy Task rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


TASK_TEXT_FIELDS = (
    "executor_provider_id",
    "executor_model_id",
    "executor_model_name",
    "executor_adapter",
    "executor_config_version",
    "executor_profile_sha256",
    "artifact_revision_id",
    "artifact_revision_base_version_id",
)


def task_db_payload(
    task: Mapping[str, object], columns: Iterable[str], *, updated_at: str,
) -> dict:
    payload = {key: task.get(key) for key in columns}
    payload["updated_at"] = str(updated_at)
    payload["ok"] = None if task.get("ok") is None else int(bool(task.get("ok")))
    payload["cancel_requested"] = int(bool(task.get("cancel_requested", False)))
    payload["delivery_attempts"] = int(task.get("delivery_attempts") or 0)
    for key in TASK_TEXT_FIELDS:
        payload[key] = str(payload.get(key) or "")
    if "network_mode" in payload:
        payload["network_mode"] = str(payload.get("network_mode") or "controlled")
    return payload


__all__ = ["TASK_TEXT_FIELDS", "task_db_payload"]
