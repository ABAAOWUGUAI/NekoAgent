"""Execution admission and failure semantics for scheduled Agent jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


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
    kind = str(error or "").strip()
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
    title = str(job.get("title") or "定时任务").strip()[:120]
    content = f"定时任务“{title}”本次未执行成功，任务没有生成结果。{error_user_message(error)}"
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
    "automation_thread_ref",
    "build_skill_execution_contract",
    "error_user_message",
    "is_permanent_error",
    "notify_failure",
    "preflight",
    "run_job",
]
