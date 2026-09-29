#!/usr/bin/env python3
"""Pure routing policy for deciding whether a turn needs a work executor."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping


_WRITE_HINTS = (
    "改", "修改", "修复", "优化", "上线", "部署", "安装", "配置", "写入", "创建", "新增", "重构", "删除",
    "update", "fix", "deploy", "install", "write", "create", "refactor",
)
_LONG_HINTS = ("深度", "详细", "全面", "全部", "整体", "调研", "排查", "优化", "部署", "上线")
_NEW_TASK_HINTS = ("新任务", "另一个任务", "单独开", "另外开", "new task", "separate task")
_TASK_EXECUTION_ACTIONS = frozenset({"start_task", "continue_task", "workspace_task"})
_DELIVERABLE_EXECUTION_ACTIONS = _TASK_EXECUTION_ACTIONS | {"invoke_capability"}
_STRONG_EFFECT_HINTS = (
    "实施", "执行", "落地", "改好", "修好", "弄好", "处理掉", "运行", "提交",
    "implement", "execute", "apply the change", "run it", "fix it", "deploy it",
)
_EXPLICIT_MUTATION_HINTS = (
    "修改", "修复", "优化", "上线", "部署", "安装", "配置", "写入", "创建",
    "新增", "重构", "删除", "迁移到", "接入", "发布到", "应用到",
)
_DIRECT_DELIVERABLE_REQUEST = re.compile(
    r"(?:请|帮我|给我|替我|把|直接|现在|立刻).{0,40}"
    r"(?:做成|整理成|生成|制作|导出|输出|提交|创建|新建).{0,20}"
    r"(?:文档|报告|文件|代码|脚本|表格|图片|视频|方案|配置|artifact|report|document|file)"
)
_ENGLISH_EFFECT_REQUEST = re.compile(
    r"\b(?:please\s+)?(?:implement|execute|deploy|install|create|generate|export|submit|run|fix|update|write)\b"
)
_DIRECT_EXECUTION_REQUEST = re.compile(
    r"(?:照|按|根据).{0,40}(?:办|安排|处理|执行|实施|应用|操作|落地|完成|做)|"
    r"(?:替我|帮我|给我|请你?|麻烦你?).{0,30}"
    r"(?:解决|处理|安排|执行|实施|应用|操作|完成|办|做|弄)|"
    r"(?:把).{0,40}(?:应用|安排|处理|修改|改成|部署|安装|迁移|接入|提交|发布|办|做|弄)"
)
_PRODUCTION_APPLICATION_REQUEST = re.compile(
    r"(?:应用|接入|迁移|放|推|发|改).{0,20}(?:现网|生产|服务器|项目|系统|仓库)"
)
_SIMPLE_DAILY_TOKENS = frozenset({
    "你好", "嗨", "哈喽", "早", "早安", "晚安", "在吗", "还在吗", "人呢",
    "哈哈", "哈哈哈", "笑死", "好耶", "谢谢", "收到", "好", "好的", "嗯", "嗯嗯",
    "这个呢", "这种呢", "是这个", "就是这个", "看看这个", "你看这个",
    "（发送了一项媒体内容）",
})
_ADDRESSED_SIMPLE_DAILY_TOKENS = frozenset({
    "你好", "嗨", "哈喽", "早", "早安", "晚安", "在吗", "还在吗", "人呢",
})
_SOCIAL_ADVICE_REQUEST = re.compile(
    r"^(?:(?:这|那)(?:个|种|样)?(?:情况)?|我|你觉得我)?"
    r"(?:我)?(?:该|应该|可以|要|最好)?(?:怎么|如何)"
    r"(?:追|回复|回|聊|说|安慰|道歉|拒绝|相处|表白|约|开口)"
    r"(?:她|他|这个女孩子|那个女孩子|这个男孩子|那个男孩子|女生|男生|对方|朋友|对象)?"
    r"(?:比较)?(?:自然|合适|好)?[吗呢？?]*$"
)
_VISUAL_REACTION_REQUEST = re.compile(
    r"^(?:你(?:觉得|看)?|帮我看看)?"
    r"(?:这|那)(?:个|张|种|样)?(?:图|图片|照片|截图|表情包|画面)?"
    r"(?:是(?:什么|谁)|什么意思|怎么样|怎么看|怎么回事|好看|可爱|搞笑|像谁)"
    r"(?:吗|呢)?[？?]*$"
)
_ASSISTANT_ADDRESS_PUNCTUATION = frozenset("?？!！~～，,。…")
_ASSISTANT_ADDRESS_SEPARATORS = _ASSISTANT_ADDRESS_PUNCTUATION | {" "}


def message_requests_effectful_work(message: object) -> bool:
    """Conservatively identify an explicit request to make or deliver something.

    This is a safety boundary for deterministic chat-only fast paths, not a
    complete intent classifier.  False positives take the ordinary formal path;
    malformed input and explicit execution/mutation requests fail closed.
    """

    if type(message) is not str:
        return True
    text = " ".join(message.split()).lower()[:12000]
    if not text:
        return True
    if any(hint in text for hint in _EXPLICIT_MUTATION_HINTS):
        return True
    if any(hint in text for hint in _STRONG_EFFECT_HINTS):
        return True
    if _DIRECT_DELIVERABLE_REQUEST.search(text) is not None:
        return True
    if _DIRECT_EXECUTION_REQUEST.search(text) is not None:
        return True
    if _PRODUCTION_APPLICATION_REQUEST.search(text) is not None:
        return True
    return _ENGLISH_EFFECT_REQUEST.search(text) is not None


def message_is_pure_assistant_address(
    message: object,
    *,
    assistant_display_name: object = None,
) -> bool:
    """Match only the active instance display name plus optional punctuation."""

    if type(message) is not str or type(assistant_display_name) is not str:
        return False
    text = " ".join(message.split()).lower()[:500]
    display_name = " ".join(assistant_display_name.split()).lower()
    if not display_name or len(display_name) > 64 or not text.startswith(display_name):
        return False
    suffix = text[len(display_name):]
    return len(suffix) <= 3 and all(
        character in _ASSISTANT_ADDRESS_PUNCTUATION for character in suffix
    )


def _message_is_addressed_simple_daily(
    message: str,
    *,
    assistant_display_name: object = None,
) -> bool:
    """Fully match the active instance name followed by one known greeting."""

    if type(assistant_display_name) is not str:
        return False
    text = " ".join(message.split()).lower()
    display_name = " ".join(assistant_display_name.split()).lower()
    if not display_name or len(display_name) > 64 or not text.startswith(display_name):
        return False
    suffix = text[len(display_name):]
    for separator_count in range(4):
        separators = suffix[:separator_count]
        if len(separators) != separator_count or any(
            character not in _ASSISTANT_ADDRESS_SEPARATORS
            for character in separators
        ):
            continue
        remainder = suffix[separator_count:]
        for token in _ADDRESSED_SIMPLE_DAILY_TOKENS:
            if not remainder.startswith(token):
                continue
            trailing = remainder[len(token):]
            if len(trailing) <= 3 and all(
                character in _ASSISTANT_ADDRESS_PUNCTUATION
                for character in trailing
            ):
                return True
    return False


def message_is_proven_daily_conversation(
    message: object,
    *,
    assistant_display_name: object = None,
) -> bool:
    """Admit only a small positive daily-conversation contract.

    Natural language outside this allowlist is not declared work; it simply
    keeps the ordinary classifier.  This makes the fast path fail closed
    without pretending that an ever-growing list can prove absence of work.
    """

    if type(message) is not str or message_requests_effectful_work(message):
        return False
    text = " ".join(message.split()).lower()[:12000]
    if not text or len(text) > 500:
        return False
    if text in _SIMPLE_DAILY_TOKENS:
        return True
    if message_is_pure_assistant_address(
        message,
        assistant_display_name=assistant_display_name,
    ):
        return True
    if _message_is_addressed_simple_daily(
        text,
        assistant_display_name=assistant_display_name,
    ):
        return True
    return bool(
        _SOCIAL_ADVICE_REQUEST.fullmatch(text)
        or _VISUAL_REACTION_REQUEST.fullmatch(text)
    )


def has_explicit_delegation_contract(mode_decision: Mapping[str, object]) -> bool:
    """Return whether a validated plan already commits the server to execution.

    Task-backed action types are authoritative by themselves.  A capability
    action is task-worthy only when the same plan also requires an Artifact or
    BOTH delivery, so ordinary inline capability reads remain on their owned
    lane and delivery wording alone cannot turn a discussion into a task.
    """

    plan = mode_decision.get("interaction_plan")
    if not isinstance(plan, Mapping):
        return False
    action_types = {
        str(item.get("type") or "").strip()
        for item in plan.get("actions") or []
        if isinstance(item, Mapping)
    }
    if action_types & _TASK_EXECUTION_ACTIONS:
        return True
    delivery = plan.get("delivery")
    delivery_mode = (
        str(delivery.get("mode") or "").strip().upper()
        if isinstance(delivery, Mapping)
        else ""
    )
    return delivery_mode in {"ARTIFACT", "BOTH"} and bool(
        action_types & _DELIVERABLE_EXECUTION_ACTIONS,
    )


def dispatch_sandbox(message: str, intent: str) -> str:
    text = (message or "").lower()
    if intent == "code" and any(hint in text for hint in _WRITE_HINTS):
        return "workspace-write"
    return "workspace-write" if any(hint in text for hint in _WRITE_HINTS) else "read-only"


def dispatch_timeout(
    message: str,
    sandbox: str,
    raw_timeout: int | None = None,
    *,
    work_task_timeout: int,
) -> int:
    if raw_timeout:
        return max(60, min(int(raw_timeout), 900))
    default = work_task_timeout if sandbox == "workspace-write" or any(
        hint in (message or "") for hint in _LONG_HINTS
    ) else 300
    return max(60, min(default, 900))


def should_dispatch_as_task(
    message: str,
    mode_decision: Mapping[str, object],
    force: str = "auto",
    *,
    detect_intent: Callable[[str], str] | None = None,
) -> bool:
    force = (force or "auto").strip().lower()
    if force == "task":
        return True
    if force == "chat":
        return False
    if has_explicit_delegation_contract(mode_decision):
        return True
    if str(mode_decision.get("execution_lane") or "") in {
        "respond", "invoke_capability", "automation.schedule.create", "broker_operation",
    } or mode_decision.get("end_work"):
        return False
    plan = mode_decision.get("interaction_plan") or {}
    if isinstance(plan, Mapping) and any(
        isinstance(item, Mapping)
        and item.get("type") in {"start_task", "continue_task"}
        and bool(item.get("requires_tools"))
        for item in plan.get("actions") or []
    ):
        return True
    mode = str(mode_decision.get("mode") or "daily")
    intent = str(mode_decision.get("intent") or (detect_intent or (lambda _message: "chat"))(message))
    return mode in {"work", "mixed"} and bool(mode_decision.get("need_tools")) and bool(intent)


def new_task_requested(message: str) -> bool:
    text = message or ""
    lowered = text.lower()
    return any(hint in lowered or hint in text for hint in _NEW_TASK_HINTS)


def pending_messages(raw: str | None) -> list[dict]:
    try:
        items = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def active_qq_task(tasks: Mapping[str, dict], lock, source: str, user_id: str) -> dict | None:
    with lock:
        candidates = [
            task for task in tasks.values()
            if task.get("source") == source
            and str(task.get("user_id") or "") == user_id
            and task.get("status") in {"queued", "running"}
        ]
        candidates.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return candidates[0] if candidates else None


__all__ = [
    "dispatch_sandbox",
    "dispatch_timeout",
    "active_qq_task",
    "new_task_requested",
    "pending_messages",
    "has_explicit_delegation_contract",
    "message_is_proven_daily_conversation",
    "message_is_pure_assistant_address",
    "message_requests_effectful_work",
    "should_dispatch_as_task",
]
