from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bridge_automation_execution_contract import derive_execution_contract, normalize_execution_contract


def prepare_automation_create_contract(
    action: dict,
    *,
    preflight: Callable[[dict], dict] | None,
    receipt: Callable[..., dict],
    clip: Callable[[Any, int], str],
) -> tuple[dict | None, dict | None]:
    contract = normalize_execution_contract(
        derive_execution_contract(
            str(action.get("instruction") or ""),
            action.get("parameters") if isinstance(action.get("parameters"), dict) else {},
            action_type=str(action.get("job_action_type") or "reminder"),
        ),
    )
    if contract.get("status") != "ready":
        missing_labels = {
            "location": "地点",
            "action_type": "动作类型",
            "execution_contract": "执行契约",
        }
        missing = ", ".join(
            missing_labels.get(str(item), str(item))
            for item in contract.get("missing_inputs") or []
        )
        return None, {
            "ok": True,
            "dispatch": "automation_clarification",
            "reply": f"我可以建立这个定时任务，但还缺少必要信息：{missing or '执行条件'}。补充后我再写入任务。",
            "action_receipts": [
                receipt(
                    "automation.schedule.create",
                    "blocked",
                    reason="execution_contract_needs_clarification",
                    missing_inputs=list(contract.get("missing_inputs") or []),
                ),
            ],
        }
    if preflight is not None and str(action.get("job_action_type") or "") == "agent":
        check = dict(preflight(action) or {})
        if not check.get("ok"):
            reason = clip(
                check.get("error_kind") or check.get("error") or "automation_preflight_failed",
                160,
            )
            return None, {
                "ok": True,
                "dispatch": "automation_unavailable",
                "reply": "定时 Agent 未创建：执行环境未就绪，任务没有写入。请先完成模型或工作区配置。",
                "action_receipts": [receipt("automation.schedule.create", "blocked", reason=reason)],
            }
    return contract, None
