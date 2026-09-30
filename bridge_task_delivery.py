#!/usr/bin/env python3
"""Project final QQ task results into the unified Delivery Outbox."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from bridge_task_expression import normalize_task_failure_projection, task_failure_projection

FINAL_STATUSES = {"done", "failed", "timeout", "cancelled"}
TERMINAL_DELIVERY_STATES = {"none", "sent", "skipped", "failed"}
PRESENTATION_CONTRACT = "task_expression_v1"


def _binding_value(value: object) -> str | None:
    """Return one safe opaque binding, empty for absent, None for malformed."""

    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        return None
    if (
        value != value.strip()
        or len(value) > 200
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return None
    return value


def _valid_https_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253:
        return False
    return all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and re.fullmatch(r"[A-Za-z0-9-]+", label) is not None
        for label in ascii_hostname.split(".")
    )


def _valid_redemption_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if (
        not value
        or value != value.strip()
        or len(value) > 2048
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return ""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    token = parsed.path[len("/r/"):] if parsed.path.startswith("/r/") else ""
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not _valid_https_hostname(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or port == 0
        or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return value


def _bindings_agree(*values: str) -> bool:
    present = {value for value in values if value}
    return len(present) <= 1


def task_artifact_delivery_projection(
    task: Mapping[str, object],
    *,
    goal_id: str = "",
    run_id: str = "",
) -> dict:
    """Return the one canonical, fail-closed Result Page delivery projection."""

    artifact = task.get("artifact") if isinstance(task.get("artifact"), Mapping) else {}
    access = (
        artifact.get("delivery_access")
        if isinstance(artifact.get("delivery_access"), Mapping)
        else {}
    )
    artifact_record = artifact.get("artifact") if isinstance(artifact.get("artifact"), Mapping) else {}
    version_record = artifact.get("version") if isinstance(artifact.get("version"), Mapping) else {}
    raw_bindings = {
        "access_artifact_id": _binding_value(access.get("artifact_id")),
        "record_artifact_id": _binding_value(artifact_record.get("id")),
        "version_artifact_id": _binding_value(version_record.get("artifact_id")),
        "access_version_id": _binding_value(access.get("artifact_version_id")),
        "record_version_id": _binding_value(version_record.get("id")),
        "record_current_version_id": _binding_value(artifact_record.get("current_version_id")),
        "access_goal_id": _binding_value(access.get("source_goal_id")),
        "record_goal_id": _binding_value(artifact_record.get("source_goal_id")),
        "task_goal_id": _binding_value(goal_id or task.get("goal_id")),
        "access_run_id": _binding_value(access.get("source_run_id")),
        "record_run_id": _binding_value(artifact_record.get("source_run_id")),
        "task_run_id": _binding_value(run_id or task.get("run_id")),
        "grant_id": _binding_value(access.get("grant_id")),
    }
    bindings_well_formed = all(value is not None for value in raw_bindings.values())
    bindings = {
        key: value or "" for key, value in raw_bindings.items()
    }
    bindings_consistent = bool(
        bindings_well_formed
        and _bindings_agree(
            bindings["access_artifact_id"],
            bindings["record_artifact_id"],
            bindings["version_artifact_id"],
        )
        and _bindings_agree(
            bindings["access_version_id"],
            bindings["record_version_id"],
            bindings["record_current_version_id"],
        )
        and _bindings_agree(
            bindings["access_goal_id"],
            bindings["record_goal_id"],
        )
        and _bindings_agree(
            bindings["access_run_id"],
            bindings["record_run_id"],
        )
    )
    delivery_fields = {
        "artifact_id": bindings["access_artifact_id"] or bindings["record_artifact_id"],
        "artifact_version_id": bindings["access_version_id"] or bindings["record_version_id"],
        "source_goal_id": bindings["access_goal_id"] or bindings["record_goal_id"] or bindings["task_goal_id"],
        "source_run_id": bindings["access_run_id"] or bindings["record_run_id"] or bindings["task_run_id"],
        "grant_id": bindings["grant_id"],
    }
    redemption_url = _valid_redemption_url(access.get("redemption_url"))
    valid = bool(
        bindings_consistent
        and redemption_url
        and all(
            delivery_fields.get(key)
            for key in (
                "artifact_id",
                "artifact_version_id",
                "source_goal_id",
                "source_run_id",
                "grant_id",
            )
        )
    )
    return {
        "valid": valid,
        "redemption_url": redemption_url,
        "delivery_fields": delivery_fields,
        "artifact_record": dict(artifact_record),
        "version_record": dict(version_record),
        "raw_access_present": bool(access),
    }


def task_has_valid_delivery_access(
    task: Mapping[str, object],
    *,
    goal_id: str = "",
    run_id: str = "",
) -> bool:
    return bool(
        task_artifact_delivery_projection(task, goal_id=goal_id, run_id=run_id)["valid"]
    )


def enqueue_task_result(outbox, task: dict, projection: dict | None, *, sessions, public_task, trim_output, terminal_presentation_factory=None):
    if task.get("status") not in FINAL_STATUSES:
        return None
    if task.get("source") != "qq" or not str(task.get("user_id") or "").strip():
        return None
    if str(task.get("delivery_status") or "pending") in TERMINAL_DELIVERY_STATES:
        return None
    projection = projection or {}
    projected = projection.get("projection") if isinstance(projection.get("projection"), dict) else {}
    goal_id = str(task.get("goal_id") or projected.get("goal_id") or "")
    run_id = str(task.get("run_id") or projected.get("run_id") or "")
    actor_id = str(task.get("user_id") or "").strip()
    user_id = str(task.get("delivery_recipient_id") or actor_id).strip()
    send_session = str(task.get("delivery_session") or "").strip() or sessions.get(user_id, "")
    public_truth = public_task(task, include_output=False)
    visible_task = {
        key: public_truth.get(key)
        for key in ("status", "ok", "error_kind", "cancel_requested")
        if key in public_truth
    }
    canonical_delivery = task_artifact_delivery_projection(
        task,
        goal_id=goal_id,
        run_id=run_id,
    )
    artifact_record = canonical_delivery["artifact_record"]
    delivery_fields = canonical_delivery["delivery_fields"]
    redemption_url = canonical_delivery["redemption_url"]
    has_delivery_access = bool(canonical_delivery["valid"])
    has_artifact_result = has_delivery_access
    terminal_status = str(task.get("status") or "")
    durable_failure = (
        task.get("_failure_projection")
        if isinstance(task.get("_failure_projection"), dict)
        else {}
    )
    failure_task = {
        **task,
        "_has_valid_delivery_access": has_delivery_access,
    }
    failure_projection = (
        normalize_task_failure_projection(
            durable_failure,
            deliverable_available=has_delivery_access,
        )
        if terminal_status in {"failed", "timeout", "cancelled"} and durable_failure
        else task_failure_projection(failure_task)
        if terminal_status in {"failed", "timeout", "cancelled"}
        else {}
    )
    if failure_projection and "error_kind" in visible_task:
        visible_task["error_kind"] = str(
            failure_projection.get("error_kind") or "unknown_failure"
        )
    raw = ""
    if terminal_status == "done" and not has_artifact_result:
        for key in ("stdout", "output"):
            candidate = str(task.get(key) or "").strip()
            if candidate:
                raw = candidate
                break
    presentation_task = {
        **task,
        "_has_valid_delivery_access": has_delivery_access,
        "_artifact_delivery_projection": canonical_delivery,
    }
    if terminal_status != "done" or has_artifact_result:
        presentation_task = {
            **presentation_task,
            "stdout": "",
            "stderr": "",
            "output": "",
            "error": "",
        }
        if failure_projection:
            presentation_task["_failure_projection"] = failure_projection
    presentation = (
        terminal_presentation_factory(presentation_task)
        if terminal_presentation_factory is not None
        else None
    )
    presentation_blocks: list[dict] = []
    presentation_contract = PRESENTATION_CONTRACT
    artifact_delivery_rendered = False
    if isinstance(presentation, Mapping):
        content = str(presentation.get("content") or "")
        raw_blocks = presentation.get("content_blocks")
        if isinstance(raw_blocks, list):
            presentation_blocks = [dict(block) for block in raw_blocks if isinstance(block, Mapping)]
        presentation_contract = str(
            presentation.get("presentation_contract") or PRESENTATION_CONTRACT
        )
        artifact_delivery_rendered = presentation.get("artifact_delivery_rendered") is True
    elif presentation is not None:
        content = str(presentation)
    elif raw:
        content = trim_output(raw)
    else:
        content = {
            "failed": "这件事没办成，出错了。",
            "timeout": "这件事超时了，没能按时完成。",
            "cancelled": "这件事取消了。",
        }.get(terminal_status, "这件事已经结束，但没有可交付的结果。")
    if has_delivery_access:
        delivery_projection = (
            task.get("_delivery_projection")
            if isinstance(task.get("_delivery_projection"), dict)
            else {}
        )
        delivery_mode = str(delivery_projection.get("mode") or "ARTIFACT").upper()
        title = " ".join(str(artifact_record.get("title") or "完整成果").split())[:120]
        sections = [content.rstrip()]
        if delivery_mode == "BOTH":
            points = []
            for raw_point in delivery_projection.get("summary_points") or []:
                point = " ".join(str(raw_point or "").split())[:200]
                if redemption_url and redemption_url in point:
                    point = point.replace(redemption_url, "").strip(" ：:")
                if point and point not in points:
                    points.append(point)
                if len(points) == 3:
                    break
            if points:
                sections.append(
                    "简单说，最值得关注的是：\n"
                    + "\n".join(f"{index}. {point}" for index, point in enumerate(points, 1))
                )
        if not artifact_delivery_rendered:
            sections.append(f"📄 {title}\n在线查看：{redemption_url}")
        content = "\n\n".join(section for section in sections if section)
    if user_id.startswith("group:"):
        thread_ref = f"qq:task-result:{user_id}"
    else:
        thread_ref = f"qq:task-result:{user_id}"
    conversation_ref = user_id
    conversation_channel = "qq"
    if user_id.startswith("group:"):
        conversation_channel = "qq_group"
    payload = {
        "kind": "run_result", "task_id": str(task.get("id") or ""),
        "goal_id": goal_id, "run_id": run_id, "user_id": user_id,
        "actor_id": actor_id, "send_session": send_session,
        "channel": conversation_channel, "conversation_ref": conversation_ref,
        "content": content, "task": visible_task,
    }
    if failure_projection:
        payload["failure"] = failure_projection
    if presentation_blocks:
        payload["content_blocks"] = presentation_blocks
    interactive_terminal = bool(
        str(task.get("delivery_recipient_id") or "").strip()
        and str(task.get("delivery_session") or "").strip()
        and not str(task.get("automation_run_id") or "").strip()
    )
    if not interactive_terminal:
        payload["notification_category"] = (
            "task_completed" if terminal_status == "done" else "task_failed"
        )
    if has_delivery_access:
        payload["artifact_delivery"] = {
            **delivery_fields,
            "actor_id": actor_id,
            "channel": conversation_channel,
            "conversation_ref": conversation_ref,
        }
        payload["redemption_url"] = redemption_url
    if presentation is not None:
        payload["presentation_contract"] = presentation_contract
        payload["terminal_status"] = str(task.get("status") or "")
    return outbox.enqueue(
        dedupe_key=f"qq:task:{task.get('id')}:final:v1",
        channel="qq",
        destination=send_session or user_id,
        payload=payload,
        max_attempts=100,
        thread_ref=thread_ref,
        delivery_class="operational",
    )


__all__ = [
    "FINAL_STATUSES",
    "PRESENTATION_CONTRACT",
    "enqueue_task_result",
    "task_artifact_delivery_projection",
    "task_has_valid_delivery_access",
]
