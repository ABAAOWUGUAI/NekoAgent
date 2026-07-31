#!/usr/bin/env python3
from __future__ import annotations


def build_system_framework(settings: dict, audit: dict | None = None) -> dict:
    """返回面向通用 Agent 平台的产品闭环摘要。

    保留控制台已经使用的 runtime/loops/dimensions 响应结构，避免产品定位调整
    破坏现有前端；QQ 和 Codex 只作为当前 Adapter/Executor 展示。
    """

    chat_provider = settings.get("chat_provider") or "codex"
    preset = settings.get("chat_provider_preset") or (
        "codex" if chat_provider == "codex" else "custom"
    )
    model = settings.get("chat_model") or ""
    audit_level = (audit or {}).get("level") or "unknown"
    findings = (audit or {}).get("findings") or []
    critical_areas = {
        item.get("area") for item in findings if item.get("severity") == "critical"
    }

    return {
        "ok": True,
        "product": "general_personal_agent_platform",
        "audit_level": audit_level,
        "runtime": {
            "platform_mode": "single_owner_self_hosted",
            "chat_provider": chat_provider,
            "chat_provider_preset": preset,
            "chat_model": model,
            "work_provider": "codex",
            "primary_channel": "qq",
            "result_push": "channel_adapter",
        },
        "loops": [
            {
                "key": "understand_goal",
                "label": "理解目标",
                "owner": "conversation + goal services",
                "entry": "任意 Channel 输入",
                "provider": preset if chat_provider != "codex" else "codex",
                "success": "识别新目标、追问、纠正、取消或话题切换。",
                "risk": "只按单轮关键词分类会丢失目标版本和上下文。",
            },
            {
                "key": "plan_and_route",
                "label": "规划与路由",
                "owner": "strategy router + policy",
                "entry": "Goal 当前版本",
                "provider": "direct / grounded / action / workflow / sandbox",
                "success": "按复杂度、时效、风险和成本选择最轻可用路径。",
                "risk": "把表达风格等同执行路径会造成不必要的完整 Agent 任务。",
            },
            {
                "key": "execute_and_validate",
                "label": "执行与验证",
                "owner": "run orchestrator + capabilities",
                "entry": "结构化 Run / Step",
                "provider": "registered capabilities + codex sandbox",
                "success": "执行可观察、可取消、可恢复，并生成 Artifact 或 Evidence。",
                "risk": "没有能力契约和检查点时，失败重试可能重复有副作用动作。",
            },
            {
                "key": "deliver_and_continue",
                "label": "交付与继续",
                "owner": "result validator + delivery adapter",
                "entry": "验证后的结果",
                "provider": "qq adapter（当前）/ 其他 Channel（未来）",
                "success": "结果可靠送达，用户可以继续追问、纠正或发起下一次 Run。",
                "risk": "执行成功、送达成功和 Goal 完成若混为一体会产生假成功。",
            },
        ],
        "dimensions": [
            {
                "dimension": "产品中心",
                "problem": "当前以 QQ 消息和 task 状态代替用户目标。",
                "fix": "以 Goal 为中心关联 Conversation、Run、Artifact、Evidence 和 Delivery。",
                "priority": "P0",
            },
            {
                "dimension": "能力扩展",
                "problem": "新场景容易继续增加主路由条件和专用页面。",
                "fix": "建立 Capability Manifest，分离 Model、Tool、Skill、Workflow 和 Channel。",
                "priority": "P0",
            },
            {
                "dimension": "控制台",
                "problem": "一级页面按后端技术模块平铺，功能边界重叠。",
                "fix": "按工作台、工作空间、Agent、渠道和系统五个产品域组织。",
                "priority": "P1",
            },
            {
                "dimension": "可靠性",
                "problem": "执行、审批和送达状态仍紧密耦合。",
                "fix": "后续引入 Run/Step checkpoint、Approval 和独立 Delivery Outbox。",
                "priority": "P0" if "task" in critical_areas or "qq" in critical_areas else "P1",
            },
            {
                "dimension": "基础设施",
                "problem": "代理、容器和 QQ 健康信息分散。",
                "fix": "继续由系统体检统一聚合，但不让基础设施对象进入核心领域。",
                "priority": "P0" if "proxy" in critical_areas else "P1",
            },
        ],
    }
