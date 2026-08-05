"""Execution admission and failure semantics for scheduled Agent jobs."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


# Stable terminal-stage identifiers.  They are persisted as telemetry and are
# deliberately independent from exception class names or provider wording.
FAILURE_STAGE_CONTRACT = "contract"
FAILURE_STAGE_CAPABILITY = "capability"
FAILURE_STAGE_EVIDENCE = "evidence"
FAILURE_STAGE_TASK = "task"
FAILURE_STAGE_DELIVERY = "delivery"
FAILURE_STAGE_ACK = "ack"
AUTOMATION_FAILURE_STAGES = (
    FAILURE_STAGE_CONTRACT,
    FAILURE_STAGE_CAPABILITY,
    FAILURE_STAGE_EVIDENCE,
    FAILURE_STAGE_TASK,
    FAILURE_STAGE_DELIVERY,
    FAILURE_STAGE_ACK,
)

_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_KNOWN_ERROR_CODES = frozenset({
    "automation_execution_failed",
    "automation_execution_contract_invalid",
    "automation_execution_contract_needs_clarification",
    "execution_contract_needs_clarification",
    "automation_capability_failed",
    "automation_evidence_or_presentation_missing",
    "automation_preflight_failed",
    "automation_skill_capability_missing",
    "automation_skill_contract_mismatch",
    "automation_skill_not_resolved",
    "capability_not_registered",
    "capability_execution_failed",
    "capability_failed",
    "capability_output_invalid",
    "capability_result_invalid",
    "capability_result_mismatch",
    "delivery_enqueue_failed",
    "delivery_enqueue_unconfirmed",
    "delivery_payload_builder_failed",
    "delivery_payload_invalid",
    "delivery_failed",
    "delivery_ack_timeout",
    "ack_timeout",
    "missing_evidence",
    "evidence_missing",
    "evidence_invalid",
    "task_terminal_failed",
    "temporary_source_error",
    "github_trending_authoritative_source_unavailable",
    "executor_snapshot_missing",
    "executor_model_missing",
    "executor_profile_missing",
    "executor_runtime_not_applied",
    "executor_profile_file_missing",
    "executor_credential_missing",
    "executor_workspace_missing",
    "unsupported_executor_transport",
    "cwd_not_allowed_for_proxy",
    "danger_full_access_not_allowed_for_proxy",
})
_STAGE_MESSAGES = {
    FAILURE_STAGE_CONTRACT: "这项定时任务的执行条件还不完整，本次没有执行。",
    FAILURE_STAGE_CAPABILITY: "这项定时任务暂不支持所需能力，本次没有执行。",
    FAILURE_STAGE_EVIDENCE: "执行结果缺少可验证证据，本次没有发送结果。",
    FAILURE_STAGE_TASK: "执行器本次未返回可用结果，任务仍未结束。",
    FAILURE_STAGE_DELIVERY: "结果生成了，但消息发送未确认，本次没有视为结束。",
    FAILURE_STAGE_ACK: "消息发送状态尚未确认，任务仍未结束。",
}


def _safe_error_code(error: object) -> str:
    """Keep only a short stable token; never expose exception text to users."""

    if isinstance(error, dict):
        raw = error.get("error_code") or error.get("error") or ""
    else:
        raw = error
    token = str(raw or "").strip().split(None, 1)[0].lower()
    # Common exception prefixes are not stable application codes.
    if token in {"runtimeerror", "valueerror", "typeerror", "exception", "error"}:
        return "automation_execution_failed"
    if not _SAFE_ERROR_CODE.fullmatch(token):
        return "automation_execution_failed"
    if token in _KNOWN_ERROR_CODES:
        return token
    if token.startswith("automation_execution_contract"):
        # Contract validator details are internal implementation names.  Keep
        # the stable stage while avoiding persistence of arbitrary suffixes.
        return "automation_execution_contract_invalid"
    return "automation_execution_failed"


def _infer_failure_stage(error_code: str) -> str:
    if error_code.startswith(("execution_contract", "automation_execution_contract", "contract_")):
        return FAILURE_STAGE_CONTRACT
    if error_code.startswith(("capability_", "skill_")):
        return FAILURE_STAGE_CAPABILITY
    if error_code.startswith(("evidence_", "missing_evidence")):
        return FAILURE_STAGE_EVIDENCE
    if error_code.startswith(("delivery_ack", "ack_")):
        return FAILURE_STAGE_ACK
    if error_code.startswith(("delivery_", "outbox_", "enqueue_")):
        return FAILURE_STAGE_DELIVERY
    return FAILURE_STAGE_TASK


def classify_automation_failure(error: object, *, stage: str = "") -> dict:
    """Classify an internal failure into a redacted, stable terminal record."""

    error_code = _safe_error_code(error)
    normalized_stage = str(stage or "").strip().lower()
    if normalized_stage not in AUTOMATION_FAILURE_STAGES:
        normalized_stage = _infer_failure_stage(error_code)
    # Admission and evidence failures are deterministic; transport, task and
    # ACK failures may be retried once by the existing bounded run policy.
    retryable = normalized_stage in {
        FAILURE_STAGE_TASK,
        FAILURE_STAGE_DELIVERY,
        FAILURE_STAGE_ACK,
    }
    if error_code in {
        "executor_snapshot_missing", "executor_model_missing",
        "executor_profile_missing", "executor_runtime_not_applied",
        "executor_profile_file_missing", "executor_credential_missing",
        "executor_workspace_missing", "unsupported_executor_transport",
        "cwd_not_allowed_for_proxy", "danger_full_access_not_allowed_for_proxy",
    }:
        retryable = False
    return {
        "error_code": error_code[:96],
        "stage": normalized_stage,
        "retryable": retryable,
        "user_message": _STAGE_MESSAGES[normalized_stage],
    }


def automation_thread_ref(job: dict) -> str:
    """Give each durable schedule its own ordered delivery lane."""

    return (
        f"qq:automation:{str(job.get('user_id') or '').strip()}:"
        f"{str(job.get('id') or '').strip()}"
    )


def preflight(
    resolve_snapshot: Callable[[], dict],
    workspace_root: Callable[[], object],
    validate_cwd: Callable[[str, str, object], None],
) -> dict:
    try:
        workspace = workspace_root().resolve()
        if not workspace.exists() or not workspace.is_dir():
            return {"ok": False, "error_kind": "executor_workspace_missing"}
        snapshot = resolve_snapshot()
        validate_cwd("read-only", snapshot["adapter"], workspace)
        return {"ok": True, "executor": snapshot["adapter"]}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error_kind": str(exc)[:160] or "automation_preflight_failed"}


@dataclass(frozen=True)
class SkillExecutionContract:
    """The small, auditable contract a scheduled Agent run must satisfy.

    Skill text is guidance only.  The scheduler uses this contract to decide
    whether a run may be dispatched and what evidence is required before a
    delivery can be marked successful.
    """

    skill_ids: tuple[str, ...]
    capability_ids: tuple[str, ...]
    evidence_required: bool = True
    network_required: bool = False

    def to_dict(self) -> dict:
        return {
            "skill_ids": list(self.skill_ids),
            "capability_ids": list(self.capability_ids),
            "evidence_required": self.evidence_required,
            "network_required": self.network_required,
        }


def build_skill_execution_contract(skill_plan: dict) -> SkillExecutionContract:
    selected = tuple(
        str(item.get("id") or "").strip()
        for item in (skill_plan.get("selected_skills") or [])
        if str(item.get("id") or "").strip()
    )
    capabilities = tuple(
        sorted(
            {
                str(item).strip()
                for item in (skill_plan.get("required_capabilities") or [])
                if str(item).strip()
            },
        ),
    )
    return SkillExecutionContract(
        skill_ids=selected,
        capability_ids=capabilities,
        evidence_required=bool(capabilities),
        network_required=any(
            item.startswith(("github.", "network.", "web."))
            for item in capabilities
        ),
    )


def error_user_message(error: object) -> str:
    if isinstance(error, dict) and error.get("user_message"):
        return str(error["user_message"])[:160]
    kind = _safe_error_code(error)
    if kind.startswith("cwd_not_allowed_for_proxy"):
        return "工作区不在受控执行目录内。"
    if kind in {
        "executor_snapshot_missing", "executor_profile_missing",
        "executor_runtime_not_applied", "executor_profile_file_missing",
        "executor_credential_missing", "executor_workspace_missing",
    } or kind.startswith("unsupported_executor_transport"):
        return "模型执行器或工作区尚未就绪。"
    return "执行器返回了暂时性错误。"


def is_permanent_error(error: object) -> bool:
    kind = str(error or "").strip()
    return (
        kind.startswith(("cwd_not_allowed_for_proxy", "unsupported_executor_transport"))
        or kind in {
            "executor_snapshot_missing", "executor_model_missing",
            "executor_profile_missing", "executor_runtime_not_applied",
            "executor_profile_file_missing", "executor_credential_missing",
            "executor_workspace_missing", "danger_full_access_not_allowed_for_proxy",
        }
    )


def notify_failure(enqueue: Callable[..., dict], job: dict, error: object) -> None:
    classified = error if isinstance(error, dict) and error.get("stage") else classify_automation_failure(error)
    title = str(job.get("title") or "定时任务").strip()[:120]
    content = f"定时任务“{title}”本次没有产出可确认结果。{classified['user_message']}"
    enqueue(
        dedupe_key=f"qq:automation-failure:{job.get('id')}:{job.get('run_id')}",
        channel="qq",
        destination=str(job.get("user_id") or ""),
        payload={
            "kind": "automation_failure",
            "automation_job_id": str(job.get("id") or ""),
            "automation_run_id": str(job.get("run_id") or ""),
            "user_id": str(job.get("user_id") or ""),
            "content": content,
            "failure_stage": classified["stage"],
            "error_code": classified["error_code"],
        },
        max_attempts=20,
        thread_ref=automation_thread_ref(job),
        delivery_class="operational",
    )


def run_job(
    job: dict,
    *,
    enqueue: Callable[..., dict],
    create_task: Callable[..., dict],
    workspace_root: Callable[[], object],
    timeout: int,
    source: str,
) -> dict:
    if str(job.get("action_type") or "") == "reminder":
        delivery = enqueue(
            dedupe_key=f"qq:automation:{job['id']}:{job['scheduled_for']}",
            channel="qq",
            destination=str(job.get("user_id") or ""),
            payload={
                "kind": "automation_reminder",
                "automation_job_id": job["id"],
                "automation_run_id": job["run_id"],
                "user_id": job["user_id"],
                "content": str(job.get("instruction") or "").strip(),
            },
            max_attempts=100,
            thread_ref=automation_thread_ref(job),
            delivery_class="operational",
        )
        return {"status": "dispatched", "dispatch": "reminder", "delivery_id": delivery.get("id") or ""}

    prompt = "\n".join((
        "这是由用户预先授权的定时 Agent 工作，不是刚刚收到的聊天消息。",
        f"计划名称：{job.get('title') or job.get('id')}",
        f"计划触发时间（UTC）：{job.get('scheduled_for')}",
        "请完成下面目标并按项目规则验证结果；不要声称用户此刻在线，也不要主动扩大权限。",
        "",
        f"结构化约束：{job.get('parameters_json') or '{}'}",
        str(job.get("instruction") or "").strip(),
    ))
    task = create_task(
        prompt=prompt,
        sandbox="read-only",
        timeout=timeout,
        cwd=workspace_root(),
        source=source,
        user_id=str(job.get("user_id") or ""),
        trace_id=f"automation-{str(job.get('run_id') or '')[:12]}",
        origin_message=f"定时任务：{job.get('title') or job.get('instruction') or ''}",
        intent="automation",
        mode="work",
        automation_run_id=str(job.get("run_id") or ""),
    )
    return {"status": "dispatched", "dispatch": "task", "task_id": task.get("id") or ""}


__all__ = [
    "SkillExecutionContract",
    "AUTOMATION_FAILURE_STAGES",
    "FAILURE_STAGE_ACK",
    "FAILURE_STAGE_CAPABILITY",
    "FAILURE_STAGE_CONTRACT",
    "FAILURE_STAGE_DELIVERY",
    "FAILURE_STAGE_EVIDENCE",
    "FAILURE_STAGE_TASK",
    "automation_thread_ref",
    "build_skill_execution_contract",
    "classify_automation_failure",
    "error_user_message",
    "is_permanent_error",
    "notify_failure",
    "preflight",
    "run_job",
]
