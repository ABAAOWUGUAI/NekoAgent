import asyncio
import json
import os
import re
import time
import urllib.parse
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from .qq_access import actor_headers,event_access_allowed,last_access_decision
from .bridge_client import BridgeClient,public_failure_message
from .delivery_worker import deliver_claimed_record
from .participation_feedback import group_event_requires_failure_notice
from .participation_metadata import (
    event_addresses_assistant,
    event_external_message_id,
    event_is_structural_group,
    event_participation_metadata,
    event_visual_media_payloads,
)
from .runtime_config import ChannelRuntimeClient, ChannelRuntimeState
from .voice_transport_probe import handle_owner_private_voice_transport_probe
from .voice_input_fetch import handle_owner_private_voice
from .voice_media import fetch_delivery_voice


BRIDGE_URL = os.environ.get("ASSISTANT_PLATFORM_BRIDGE_URL", "").rstrip("/")
TOKEN_PATH = os.environ.get(
    "ASSISTANT_PLATFORM_CHANNEL_TOKEN_PATH",
    "/agent-stack/codex-qq-bridge/qq-token",
)
DELIVERY_LONG_POLL_SECONDS = min(
    25,
    max(1, int(os.environ.get("ASSISTANT_PLATFORM_DELIVERY_LONG_POLL", "25"))),
)
DELIVERY_CLAIM_REPROBE_SECONDS = max(
    30,
    int(os.environ.get("ASSISTANT_PLATFORM_DELIVERY_CLAIM_REPROBE", "60")),
)
RUNTIME_STATE = ChannelRuntimeState()
STATUS_WORDS = {
    "status",
    "st",
    "stat",
    "stautus",
    "\u72b6\u6001",
    "\u767b\u5f55\u72b6\u6001",
    "codex\u72b6\u6001",
    "codex \u72b6\u6001",
}
HELP_WORDS = {"help", "h", "?", "\u5e2e\u52a9", "\u8bf4\u660e", "\u83dc\u5355"}
CODE_PREFIXES = (
    "code ",
    "code:",
    "code\uff1a",
    "\u5f00\u53d1 ",
    "\u5f00\u53d1:",
    "\u5f00\u53d1\uff1a",
    "\u5199\u4ee3\u7801",
    "\u6539\u4ee3\u7801",
    "\u4fee\u6539\u4ee3\u7801",
    "\u505a\u4ee3\u7801",
    "\u5b9e\u73b0 ",
)
ASK_PREFIXES = ("ask ", "ask:", "ask\uff1a", "\u95ee ", "\u95ee:", "\u95ee\uff1a")
HEALTH_WORDS = {
    "health",
    "server",
    "server status",
    "\u670d\u52a1\u5668",
    "\u6b63\u5e38",
    "\u5065\u5eb7",
    "\u8d44\u6e90",
    "\u5360\u7528",
    "\u5185\u5b58",
    "\u78c1\u76d8",
    "\u5bb9\u5668",
    "\u8d1f\u8f7d",
    "\u5361",
    "docker",
    "cpu",
}
HEALTH_HINT_WORDS = {
    "health",
    "server status",
    "\u5065\u5eb7",
    "\u8d44\u6e90",
    "\u5360\u7528",
    "\u5185\u5b58",
    "\u78c1\u76d8",
    "\u5bb9\u5668",
    "\u8d1f\u8f7d",
    "\u5361",
    "docker",
    "cpu",
}
TASK_WORDS = {
    "tasks",
    "task",
    "jobs",
    "history",
    "\u4efb\u52a1",
    "\u5386\u53f2",
    "\u5217\u8868",
}
TASK_STATS_WORDS = {
    "task stats",
    "tasks stats",
    "stats",
    "statistics",
    "\u4efb\u52a1\u7edf\u8ba1",
    "\u7edf\u8ba1\u4efb\u52a1",
}
RESULT_PREFIXES = (
    "result ",
    "result:",
    "result\uff1a",
    "task ",
    "task:",
    "task\uff1a",
    "\u7ed3\u679c ",
    "\u7ed3\u679c:",
    "\u7ed3\u679c\uff1a",
    "\u4efb\u52a1 ",
    "\u4efb\u52a1:",
    "\u4efb\u52a1\uff1a",
)
CANCEL_PREFIXES = (
    "cancel ",
    "cancel:",
    "cancel\uff1a",
    "stop ",
    "stop:",
    "stop\uff1a",
    "\u53d6\u6d88 ",
    "\u53d6\u6d88:",
    "\u53d6\u6d88\uff1a",
    "\u505c\u6b62 ",
    "\u505c\u6b62:",
    "\u505c\u6b62\uff1a",
)
RETRY_PREFIXES = (
    "retry ",
    "retry:",
    "retry\uff1a",
    "rerun ",
    "rerun:",
    "rerun\uff1a",
    "\u91cd\u8bd5 ",
    "\u91cd\u8bd5:",
    "\u91cd\u8bd5\uff1a",
    "\u91cd\u8dd1 ",
    "\u91cd\u8dd1:",
    "\u91cd\u8dd1\uff1a",
    "\u518d\u8dd1 ",
)
TASK_ID_RE = re.compile(r"^[0-9a-fA-F]{8}$")
TASK_STATUS_ALIASES = {
    "failed": ("failed", "fail", "\u5931\u8d25", "\u62a5\u9519", "\u9519\u8bef"),
    "timeout": ("timeout", "timed out", "\u8d85\u65f6"),
    "running": ("running", "run", "\u8fd0\u884c", "\u6267\u884c\u4e2d", "\u5904\u7406\u4e2d"),
    "queued": ("queued", "queue", "\u6392\u961f", "\u7b49\u5f85"),
    "done": ("done", "success", "succeeded", "\u5b8c\u6210", "\u6210\u529f"),
    "cancelled": ("cancelled", "canceled", "\u53d6\u6d88", "\u505c\u6b62"),
}
TRENDING_WORDS = {
    "github trending",
    "trending",
    "grehub",
    "github\u70ed\u95e8",
    "github \u70ed\u95e8",
    "\u70ed\u95e8\u699c",
    "\u70ed\u95e8\u9879\u76ee",
    "\u70ed\u95e8\u4ed3\u5e93",
    "\u9879\u76ee\u699c",
}
PROJECT_WORDS = {"project", "projects", "\u9879\u76ee", "\u9879\u76ee\u5217\u8868", "\u5f53\u524d\u9879\u76ee"}
PROJECT_SWITCH_PREFIXES = (
    "switch project ",
    "project ",
    "\u5207\u6362\u9879\u76ee ",
    "\u4f7f\u7528\u9879\u76ee ",
)
PROJECT_CREATE_PREFIXES = (
    "new project ",
    "create project ",
    "\u65b0\u9879\u76ee ",
    "\u521b\u5efa\u9879\u76ee ",
)
MEMORY_WORDS = {"memory", "memories", "\u8bb0\u5fc6", "\u8bb0\u5fc6\u5217\u8868"}
REMEMBER_PREFIXES = (
    "remember ",
    "remember:",
    "\u8bb0\u4f4f ",
    "\u8bb0\u4f4f:",
    "\u8bb0\u4f4f\uff1a",
    "\u8bf7\u8bb0\u4f4f ",
    "\u4f60\u8981\u8bb0\u4f4f ",
)
FORGET_PREFIXES = (
    "forget ",
    "forget:",
    "\u5fd8\u8bb0 ",
    "\u5fd8\u8bb0:",
    "\u5220\u9664\u8bb0\u5fc6 ",
)
PERSONA_WORDS = {"persona", "\u4eba\u8bbe", "\u89d2\u8272"}
PERSONA_PREFIXES = (
    "persona ",
    "persona:",
    "\u4eba\u8bbe ",
    "\u4eba\u8bbe:",
    "\u4eba\u8bbe\uff1a",
    "\u89d2\u8272 ",
)
RELATION_PREFIXES = (
    "relationship ",
    "\u5173\u7cfb ",
    "\u6a21\u5f0f ",
)


def _read_token() -> str:
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


BRIDGE_CLIENT = BridgeClient(
    BRIDGE_URL,
    _read_token,
    actor_headers,
    long_poll_seconds=DELIVERY_LONG_POLL_SECONDS,
)


def _bridge_request(method: str, path: str, payload: dict | None = None) -> dict:
    return BRIDGE_CLIENT.request(method, path, payload)


async def _call_bridge(method: str, path: str, payload: dict | None = None) -> dict:
    return await BRIDGE_CLIENT.call(method, path, payload)


def _clip_audit_text(text: object, limit: int = 700) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


async def _audit_event(
    event: AstrMessageEvent,
    trace_id: str,
    stage: str,
    *,
    action: str = "",
    status: str = "",
    message: str = "",
    detail: str = "",
    task_id: str = "",
) -> None:
    payload = {
        "trace_id": trace_id,
        "user_id": _sender_id(event),
        "stage": stage,
        "action": action,
        "status": status,
        "task_id": task_id,
        "message": _clip_audit_text(message),
        "detail": _clip_audit_text(detail, 1600),
        "session": _event_session(event),
    }
    try:
        result = await _call_bridge("POST", "/qq/events", payload)
        if not result.get("ok"):
            logger.warning("codex_agent audit failed stage=%s error=%s", stage, result.get("error"))
    except Exception:
        logger.exception("codex_agent audit exception stage=%s", stage)


def _compact_output(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "(empty)"
    max_chars = RUNTIME_STATE.reply_max_chars
    if len(text) <= max_chars:
        return text
    return "...(truncated)\n" + text[-max_chars:]


def _bridge_http_status(result: dict) -> int:
    try:
        status = int(result.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status:
        return status
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(result.get("error") or ""), re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _claimed_delivery_records(result: dict) -> list[dict]:
    for key in ("deliveries", "items"):
        records = result.get(key)
        if isinstance(records, list):
            return [item for item in records if isinstance(item, dict)]
    record = result.get("delivery")
    return [record] if isinstance(record, dict) else []


ERROR_KIND_MESSAGES = {
    "quota": (
        "AI 请求失败：当前账号或 Provider 可能没有可用额度，"
        "或触发了使用频率限制。请检查 ChatGPT/Codex、OpenAI-compatible Provider "
        "额度、计费或稍后重试。"
    ),
    "auth": "AI 请求失败：登录态或 API Key 可能失效。Codex 请重新执行 codex login；独立 Provider 请检查 API Key。",
    "invalid_model": "AI 请求失败：当前 Provider 不支持配置的模型标识，请在模型管理中核对并测试当前角色绑定。",
    "rate_limit": "AI 请求失败：Provider 当前请求过于频繁，请稍后重试。",
    "timeout": "AI 请求超时：Provider 在限定时间内没有返回，请稍后重试或检查该连接的响应速度。",
    "network": "AI 请求失败：网络或代理连接异常，请检查 mihomo/FlowerCloud 代理和 Provider Base URL。",
    "provider_config": "AI 请求失败：聊天 Provider 配置不完整，请在管理后台配置 Base URL、模型和 API Key。",
    "cwd_not_allowed_for_proxy": (
        "这条消息被识别为工作请求，但当前执行器只允许受控工作区。"
        "系统没有执行；请在控制台修正工作执行器的工作区配置后再试。"
    ),
    "danger_full_access_not_allowed_for_proxy": (
        "当前工作执行器不允许此权限级别，系统没有执行。"
    ),
    "work_executor_binding_missing": "工作执行器尚未配置，系统没有执行。",
    "http": "AI 请求失败：Provider 返回 HTTP 错误，详细信息如下。",
    "upstream": "AI 请求失败：Provider 上游服务异常，请稍后重试或切换模型。",
    "parse": "AI 请求失败：Provider 响应无法解析，可能不是 OpenAI-compatible 格式。",
    "empty": "AI 进程或 Provider 没有返回任何可发送内容。",
    "codex_failed": "Codex \u6267\u884c\u5931\u8d25\uff0c\u8be6\u7ec6\u4fe1\u606f\u5982\u4e0b\u3002",
    "service_restart": "任务执行期间 bridge 发生重启。为避免重复写入，系统没有自动重跑，请检查部分修改后手动重试。",
}


def _retry_error_text(error: str) -> str:
    if error == "task_prompt_unavailable":
        return "\u8fd9\u6761\u5386\u53f2\u4efb\u52a1\u6ca1\u6709\u4fdd\u5b58\u539f\u59cb prompt\uff0c\u65e0\u6cd5\u91cd\u8bd5\u3002"
    if error.startswith("task_not_retryable:"):
        status = error.split(":", 1)[1] or "unknown"
        return f"\u4efb\u52a1\u5f53\u524d\u72b6\u6001\u662f {status}\uff0c\u53ea\u6709 failed/timeout/cancelled \u4efb\u52a1\u5141\u8bb8\u91cd\u8bd5\u3002"
    if error == "task_not_found":
        return "\u4efb\u52a1\u4e0d\u5b58\u5728\u6216\u5df2\u7ecf\u88ab\u6e05\u7406\u3002"
    if error == "invalid_sandbox":
        return "\u539f\u4efb\u52a1\u7684 sandbox \u914d\u7f6e\u4e0d\u5408\u6cd5\uff0c\u65e0\u6cd5\u91cd\u8bd5\u3002"
    return error or "task_retry_failed"


def _failure_output(payload: dict) -> str | None:
    kind = payload.get("error_kind")
    message = ERROR_KIND_MESSAGES.get(kind)
    if not message:
        return None
    return _compact_output(message)


def _display_text(result: dict) -> str:
    failure = _failure_output(result)
    if failure:
        return failure
    if result.get("ok") and (result.get("stdout") or "").strip():
        return _compact_output(result.get("stdout", ""))
    return _compact_output(
        result.get("output", "")
        or result.get("stderr", "")
        or result.get("error", "")
    )


def _display_task_output(task: dict) -> str:
    failure = _failure_output(task)
    if failure:
        return failure
    if task.get("ok") and (task.get("stdout") or "").strip():
        return _compact_output(task.get("stdout", ""))
    return _compact_output(
        task.get("output", "")
        or task.get("stdout", "")
        or task.get("stderr", "")
        or task.get("error", "")
    )


def _format_task(task: dict, include_output: bool = False) -> str:
    task_id = task.get("id", "?")
    status = task.get("status", "?")
    sandbox = task.get("sandbox", "?")
    duration = task.get("duration", "?")
    returncode = task.get("returncode", "?")
    summary = task.get("summary", "")
    status_text = {
        "queued": "排队中",
        "running": "执行中",
        "done": "已完成",
        "failed": "失败",
        "timeout": "超时",
        "cancelled": "已取消",
    }.get(status, status)
    lines = [f"任务 #{task_id}", f"- 状态：{status_text}", f"- 权限：{sandbox}"]
    if duration not in (None, "?"):
        lines.append(f"- 耗时：{duration}s")
    if summary:
        lines.append(f"- 内容：{summary}")
    pending_count = int(task.get("pending_message_count") or 0)
    if pending_count:
        lines.append(f"- 执行期间收到补充：{pending_count} 条（已保留在任务记录）")
    if include_output:
        lines.append("")
        lines.append(_display_task_output(task))
    return "\n".join(lines)


def _format_task_list(tasks: list[dict]) -> str:
    if not tasks:
        return "\u6682\u65e0\u4efb\u52a1\u3002"
    lines = ["Recent tasks:"]
    for task in tasks:
        task_id = task.get("id", "?")
        status = task.get("status", "?")
        sandbox = task.get("sandbox", "?")
        summary = task.get("summary", "")
        duration = task.get("duration")
        error_kind = task.get("error_kind")
        duration_text = f", {duration}s" if duration is not None else ""
        error_text = f", {error_kind}" if error_kind else ""
        lines.append(f"- #{task_id} {status} [{sandbox}{duration_text}{error_text}] {summary}")
    lines.append(
        "\u53d1\u9001 '\u7ed3\u679c <task_id>' \u67e5\u770b\u8be6\u60c5\uff0c"
        "'\u53d6\u6d88 <task_id>' \u505c\u6b62\u4efb\u52a1\uff0c"
        "'\u91cd\u8bd5 <task_id>' \u91cd\u65b0\u6267\u884c\u4efb\u52a1\u3002"
    )
    return "\n".join(lines)


def _format_task_stats(result: dict) -> str:
    counts = result.get("counts") or {}
    order = ("queued", "running", "done", "failed", "timeout", "cancelled")
    lines = [
        "Task stats:",
        f"- total: {result.get('total', 0)}",
        f"- active: {result.get('active', 0)}",
    ]
    for status in order:
        lines.append(f"- {status}: {counts.get(status, 0)}")
    return "\n".join(lines)


def _format_projects(result: dict) -> str:
    current = result.get("current") or result.get("project") or {}
    projects = result.get("projects") or ([current] if current else [])
    lines = ["项目:"]
    if current:
        lines.append(f"- 当前: {current.get('name', '?')} ({current.get('path', '?')})")
    if projects:
        lines.append("- 列表:")
        for item in projects[:12]:
            marker = "*" if current and item.get("id") == current.get("id") else "-"
            lines.append(f"  {marker} {item.get('name', '?')} [{item.get('id', '?')}] {item.get('path', '?')}")
    else:
        lines.append("- 暂无项目。")
    lines.append("用法: 切换项目 <名称或ID> / 新项目 <名称>")
    return "\n".join(lines)


def _format_memories(result: dict) -> str:
    memories = result.get("memories") or []
    if not memories:
        return "暂无长期记忆。"
    lines = ["长期记忆:"]
    for item in memories[:20]:
        lines.append(f"- {item.get('id', '?')}: {item.get('content', '')}")
    lines.append("用法: 记住 <内容> / 忘记 <memory_id>")
    return "\n".join(lines)


def _format_persona(result: dict) -> str:
    settings = result.get("settings") or {}
    return "\n".join(
        [
            "当前人设:",
            f"- 名字: {settings.get('display_name', '?')}",
            f"- 关系: {settings.get('relationship', '?')}",
            f"- 人设: {settings.get('persona', '?')}",
            f"- 风格: {settings.get('style', '?')}",
            "用法: 人设 <描述> / 模式 朋友|恋人|工作助手",
        ],
    )


def _sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id())
    except Exception:
        return ""


def _sender_name(event: AstrMessageEvent) -> str:
    for method_name in ("get_sender_name", "get_sender_nickname"):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                value = method()
                if value:
                    return str(value)
            except Exception:
                pass
    return _sender_id(event)


def _group_id(event: AstrMessageEvent) -> str:
    method = getattr(event, "get_group_id", None)
    if callable(method):
        try:
            value = method()
            if value:
                return str(value)
        except Exception:
            pass
    message_obj = getattr(event, "message_obj", None)
    value = getattr(message_obj, "group_id", "")
    if value:
        return str(value)
    group = getattr(message_obj, "group", None)
    value = getattr(group, "group_id", "") or getattr(group, "id", "")
    if value:
        return str(value)
    try:
        message_type = getattr(event.get_message_type(), "name", "")
    except Exception:
        message_type = ""
    if message_type == "GROUP_MESSAGE":
        value = getattr(event, "session_id", "")
        if value:
            return str(value)
    return ""


def _group_name(event: AstrMessageEvent) -> str:
    for method_name in ("get_group_name", "get_group_display_name"):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                value = method()
                if value:
                    return str(value)
            except Exception:
                pass
    return ""


def _event_session(event: AstrMessageEvent) -> str:
    for name in ("unified_msg_origin", "session"):
        value = getattr(event, name, "")
        if value:
            return str(value)
    for method in ("get_session_id", "get_unified_msg_origin"):
        func = getattr(event, method, None)
        if callable(func):
            try:
                value = func()
                if value:
                    return str(value)
            except Exception:
                pass
    return ""


def _meme_url(meme: dict | None) -> str:
    if not isinstance(meme, dict):
        return ""
    url = str(meme.get("public_url") or "").strip()
    if not url:
        return ""
    if url.startswith("/"):
        return BRIDGE_URL + url
    return url


def _message_components(
    text: str,
    meme: dict | None = None,
    voice_path: str = "",
) -> list:
    """Build components for ``event.chain_result``.

    AstrBot's direct ``send_message`` API accepts ``MessageChain`` while
    ``AstrMessageEvent.chain_result`` accepts the underlying component list.
    Keeping those contracts separate avoids nesting a MessageChain inside a
    MessageEventResult, which current AstrBot cannot decorate or send.
    """
    parts = [Plain(text)] if text else []
    url = _meme_url(meme)
    if url:
        try:
            parts.append(Image.fromURL(url))
        except Exception:
            logger.exception("create meme image component failed url=%s", url)
    if voice_path:
        parts.append(Record(file=voice_path, url=voice_path))
    return parts


def _deny_text(_) -> str:
    return "Permission denied."


async def _event_access_allowed(event: AstrMessageEvent, action: str) -> bool:
    if not RUNTIME_STATE.ready:
        return False
    return await event_access_allowed(event, action, call_bridge=_call_bridge,
        group_id_of=_group_id, sender_id_of=_sender_id,
        message_id_of=event_external_message_id,
        logger=logger)


def _strip_private_command_prefix(text: str) -> str | None:
    text = (text or "").strip()
    lowered = text.lower()
    for prefix in RUNTIME_STATE.command_prefixes:
        if lowered == prefix:
            return ""
        if lowered.startswith(prefix + " "):
            return text[len(prefix) :].strip()
    if text.startswith("/"):
        return None
    return text


def _strip_text_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix.lower()):
            return text[len(prefix) :].strip()
    return None


def _strip_id_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
    return _strip_text_prefix(text.strip(), prefixes)


def _looks_like_health(text: str) -> bool:
    lowered = text.lower()
    if lowered in HEALTH_WORDS:
        return True
    has_server = "\u670d\u52a1\u5668" in text or "server" in lowered
    if has_server:
        return True
    return any(word in lowered or word in text for word in HEALTH_HINT_WORDS)


def _looks_like_health_work_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        word in lowered or word in text
        for word in (
            "总结",
            "分析",
            "建议",
            "详细",
            "排查",
            "诊断",
            "为什么",
            "优化",
            "报告",
            "summary",
            "analyze",
            "analysis",
            "diagnose",
            "recommend",
        )
    )


def _looks_like_trending(text: str) -> bool:
    # Natural-language sentences may combine a lookup with scheduling, memory,
    # delivery, or other actions.  Only exact shortcut utterances belong to the
    # legacy one-shot endpoint; compositional requests must reach Bridge intact.
    lowered = (text or "").strip().lower().strip("，,。.!！?？：:;； ")
    return lowered in TRENDING_WORDS


def _parse_task_query(text: str) -> tuple[str | None, int]:
    text = (text or "").strip()
    lowered = text.lower()
    status = None
    for candidate, aliases in TASK_STATUS_ALIASES.items():
        if any(alias in lowered or alias in text for alias in aliases):
            status = candidate
            break
    limit = 10
    match = re.search(r"\b(\d{1,2})\b", lowered)
    if match:
        limit = max(1, min(int(match.group(1)), 50))
    return status, limit


def _looks_like_task_list(text: str) -> bool:
    lowered = (text or "").lower()
    if lowered in TASK_WORDS:
        return True
    has_task_word = "\u4efb\u52a1" in text or "task" in lowered or "job" in lowered
    if not has_task_word:
        return False
    if any(word in text for word in ("\u5386\u53f2", "\u5217\u8868", "\u6700\u8fd1")):
        return True
    return any(
        alias in lowered or alias in text
        for aliases in TASK_STATUS_ALIASES.values()
        for alias in aliases
    )


def _task_query_path(query: str) -> str:
    status, limit = _parse_task_query(query)
    params = {"limit": str(limit)}
    if status:
        params["status"] = status
    return "/tasks?" + urllib.parse.urlencode(params)


def _route_private_text(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    lowered = text.lower()
    if TASK_ID_RE.fullmatch(text):
        return "result", text
    if lowered in STATUS_WORDS:
        return "status", ""
    if lowered in HELP_WORDS:
        return "help", ""
    if lowered in PROJECT_WORDS:
        return "projects", ""
    if lowered in MEMORY_WORDS:
        return "memories", ""
    if lowered in PERSONA_WORDS:
        return "persona", ""
    if lowered in TASK_STATS_WORDS:
        return "task_stats", ""
    if _looks_like_task_list(text):
        return "tasks", text
    result_id = _strip_id_prefix(text, RESULT_PREFIXES)
    if result_id:
        return "result", result_id
    cancel_id = _strip_id_prefix(text, CANCEL_PREFIXES)
    if cancel_id:
        return "cancel", cancel_id
    retry_id = _strip_id_prefix(text, RETRY_PREFIXES)
    if retry_id:
        return "retry", retry_id
    project_name = _strip_text_prefix(text, PROJECT_CREATE_PREFIXES)
    if project_name is not None:
        return "project_create", project_name
    project_id = _strip_text_prefix(text, PROJECT_SWITCH_PREFIXES)
    if project_id is not None:
        return "project_switch", project_id
    memory_text = _strip_text_prefix(text, REMEMBER_PREFIXES)
    if memory_text is not None:
        return "remember", memory_text
    memory_id = _strip_text_prefix(text, FORGET_PREFIXES)
    if memory_id is not None:
        return "forget", memory_id
    persona_text = _strip_text_prefix(text, PERSONA_PREFIXES)
    if persona_text is not None:
        return "persona_set", persona_text
    relation_text = _strip_text_prefix(text, RELATION_PREFIXES)
    if relation_text is not None:
        return "relationship_set", relation_text
    if _looks_like_trending(text):
        return "github_trending", ""
    if _looks_like_health(text):
        if _looks_like_health_work_request(text):
            return "chat", text
        return "health", ""

    code_prompt = _strip_text_prefix(text, CODE_PREFIXES)
    if code_prompt is not None:
        return "code", code_prompt or text

    ask_prompt = _strip_text_prefix(text, ASK_PREFIXES)
    if ask_prompt is not None:
        return "ask", ask_prompt or text

    return "chat", text


class CodexAgentPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._delivery_task = None
        self._runtime_task = None
        self._runtime_client = ChannelRuntimeClient(
            self.context, _call_bridge, logger, RUNTIME_STATE,
        )
        self._delivery_lease_owner = f"astrbot-{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self._delivery_claim_unavailable_until = 0.0
        try:
            self._runtime_task = asyncio.create_task(self._runtime_client.run())
            self._delivery_task = asyncio.create_task(self._delivery_loop())
        except RuntimeError:
            self._runtime_task = None
            self._delivery_task = None

    async def _deliver_claimed_record(self, delivery: dict) -> None:
        await deliver_claimed_record(
            delivery,
            call_bridge=_call_bridge,
            context=self.context,
            logger=logger,
            format_task=_format_task,
            compact_output=_compact_output,
            message_components=_message_components,
            fetch_voice_media=lambda delivery, lease_token: fetch_delivery_voice(
                delivery,
                lease_token,
                BRIDGE_CLIENT.fetch_bytes,
            ),
        )

    async def _delivery_loop(self):
        await asyncio.sleep(8)
        while True:
            try:
                loop = asyncio.get_running_loop()
                if loop.time() >= self._delivery_claim_unavailable_until:
                    result = await _call_bridge(
                        "POST",
                        "/deliveries/claim",
                        {
                            "lease_owner": self._delivery_lease_owner,
                            "wait_seconds": DELIVERY_LONG_POLL_SECONDS,
                            # Claim one at a time so a later QQ send cannot lose
                            # its lease while earlier records are being sent.
                            "limit": 1,
                            "channel": "qq",
                        },
                    )
                    status = _bridge_http_status(result)
                    if status in {404, 405}:
                        self._delivery_claim_unavailable_until = (
                            loop.time() + DELIVERY_CLAIM_REPROBE_SECONDS
                        )
                        logger.error(
                            "delivery claim endpoint unavailable status=%s; direct legacy delivery is disabled",
                            status,
                        )
                    elif not result.get("ok"):
                        logger.warning("delivery claim failed error=%s", result.get("error"))
                        await asyncio.sleep(5)
                        continue
                    else:
                        deliveries = _claimed_delivery_records(result)
                        for delivery in deliveries:
                            await self._deliver_claimed_record(delivery)
                        # A conforming empty claim has already waited up to 25s.
                        # A tiny yield also protects against a misconfigured
                        # server returning empty results immediately.
                        if not deliveries:
                            await asyncio.sleep(0.2)
                        continue

                await asyncio.sleep(max(5, RUNTIME_STATE.delivery_poll_seconds))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("codex delivery loop failed")
                await asyncio.sleep(5)

    async def _forward_with_audit(self, event: AstrMessageEvent, trace_id: str, action: str, generator):
        count = 0
        try:
            async for result in generator:
                count += 1
                await _audit_event(
                    event,
                    trace_id,
                    "reply_ready",
                    action=action,
                    status="ok",
                    detail=f"reply_index={count}",
                )
                yield result
            await _audit_event(
                event,
                trace_id,
                "reply_complete",
                action=action,
                status="ok" if count else "empty",
                detail=f"reply_count={count}",
            )
        except Exception as exc:
            await _audit_event(
                event,
                trace_id,
                "error",
                action=action,
                status="exception",
                detail=str(exc),
            )
            raise

    @filter.command_group("codex")
    def codex(self):
        """Codex command group."""
        pass

    @codex.command("help")
    async def help(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "help"):
            yield event.plain_result(_deny_text(event))
            return
        yield event.plain_result(
            "\n".join(
                [
                    "Codex QQ \u547d\u4ee4:",
                    "status / \u72b6\u6001 - \u67e5\u770b Codex \u767b\u5f55\u72b6\u6001",
                    "health / \u670d\u52a1\u5668 - \u5feb\u901f\u68c0\u67e5\u670d\u52a1\u5668",
                    "trending / GitHub \u70ed\u95e8 - \u5feb\u901f\u67e5 GitHub Trending",
                    "\u666e\u901a\u79c1\u804a - \u81ea\u52a8\u5206\u6d41\uff1a\u95f2\u804a\u5373\u65f6\u56de\u590d\uff0c\u5de5\u4f5c\u8fdb\u4efb\u52a1\uff0c\u5b8c\u6210\u540e\u63a8\u56de QQ",
                    "\u5f00\u53d1 <task> or code <task> - \u8fdb\u5165 Codex \u9879\u76ee\u4efb\u52a1",
                    "\u9879\u76ee / \u5207\u6362\u9879\u76ee <id> / \u65b0\u9879\u76ee <name> - \u7ba1\u7406\u5f53\u524d\u9879\u76ee",
                    "\u8bb0\u5fc6 / \u8bb0\u4f4f <content> / \u5fd8\u8bb0 <id> - \u7ba1\u7406\u957f\u671f\u8bb0\u5fc6",
                    "\u4eba\u8bbe / \u4eba\u8bbe <description> / \u6a21\u5f0f <relationship> - \u81ea\u5b9a\u4e49\u52a9\u624b\u89d2\u8272",
                    "\u4efb\u52a1 - \u67e5\u770b\u6700\u8fd1\u4efb\u52a1",
                    "\u4efb\u52a1\u7edf\u8ba1 - \u67e5\u770b\u6309\u72b6\u6001\u6c47\u603b\u7684\u4efb\u52a1\u6570",
                    "\u5931\u8d25\u4efb\u52a1 / \u8fd0\u884c\u4efb\u52a1 - \u6309\u72b6\u6001\u8fc7\u6ee4\u4efb\u52a1",
                    "\u7ed3\u679c <task_id> - \u67e5\u770b\u4efb\u52a1\u7ed3\u679c",
                    "\u53d6\u6d88 <task_id> - \u505c\u6b62\u4efb\u52a1",
                    "\u91cd\u8bd5 <task_id> - \u91cd\u65b0\u6267\u884c\u539f\u4efb\u52a1",
                    "/c status, /c health, /c tasks, /c result <id>, /c cancel <id>, /c retry <id>",
                ],
            ),
        )

    @codex.command("status", alias={"st", "stat", "stautus", "\u72b6\u6001"})
    async def status(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "status"):
            yield event.plain_result(_deny_text(event))
            return
        try:
            result = await _call_bridge("GET", "/status")
            output = _compact_output(result.get("output", "") or result.get("error", ""))
            yield event.plain_result(f"Codex status:\n{output}")
        except Exception as exc:
            logger.exception("codex status failed")
            yield event.plain_result(f"Codex status failed: {exc}")

    @codex.command(
        "health",
        alias={"server", "\u670d\u52a1\u5668", "\u5065\u5eb7", "\u68c0\u67e5"},
    )
    async def health(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "health"):
            yield event.plain_result(_deny_text(event))
            return
        try:
            result = await _call_bridge("GET", "/server/status")
            output = _compact_output(result.get("output", "") or result.get("error", ""))
            yield event.plain_result(output)
        except Exception as exc:
            logger.exception("server health failed")
            yield event.plain_result(f"Server health failed: {exc}")

    @codex.command(
        "trending",
        alias={"github", "github-trending", "\u70ed\u95e8", "\u70ed\u95e8\u699c"},
    )
    async def github_trending(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "github_trending"):
            yield event.plain_result(_deny_text(event))
            return
        try:
            result = await _call_bridge("GET", "/github/trending")
            if result.get("ok"):
                output = _compact_output(result.get("output", "") or result.get("error", ""))
                yield event.plain_result(output)
                return

            fallback_prompt = (
                "Find recent GitHub Trending repositories and list 10 notable projects. "
                "For each repository, include name, main language, why it is trending, "
                "and what our QQ-controlled server AI Agent project can reuse from it. "
                "Reply in concise Chinese."
            )
            async for item in self._run_codex(event, fallback_prompt, sandbox="read-only"):
                yield item
        except Exception as exc:
            logger.exception("github trending failed")
            yield event.plain_result(f"GitHub trending failed: {exc}")

    @codex.command("tasks", alias={"task", "jobs", "history", "\u4efb\u52a1", "\u5386\u53f2"})
    async def tasks(self, event: AstrMessageEvent, query: GreedyStr = ""):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "tasks"):
            yield event.plain_result(_deny_text(event))
            return
        try:
            result = await _call_bridge("GET", _task_query_path(str(query or "")))
            yield event.plain_result(_format_task_list(result.get("tasks", [])))
        except Exception as exc:
            logger.exception("task list failed")
            yield event.plain_result(f"Task list failed: {exc}")

    @codex.command("stats", alias={"statistics", "\u4efb\u52a1\u7edf\u8ba1", "\u7edf\u8ba1"})
    async def task_stats(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "task_stats"):
            yield event.plain_result(_deny_text(event))
            return
        try:
            result = await _call_bridge("GET", "/tasks/stats")
            yield event.plain_result(_format_task_stats(result))
        except Exception as exc:
            logger.exception("task stats failed")
            yield event.plain_result(f"Task stats failed: {exc}")

    @codex.command("projects", alias={"project", "\u9879\u76ee", "\u9879\u76ee\u5217\u8868"})
    async def projects(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "projects"):
            yield event.plain_result(_deny_text(event))
            return
        result = await _call_bridge("GET", "/projects")
        yield event.plain_result(_format_projects(result))

    @codex.command("use-project", alias={"switch-project", "\u5207\u6362\u9879\u76ee", "\u4f7f\u7528\u9879\u76ee"})
    async def switch_project(self, event: AstrMessageEvent, identifier: GreedyStr):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "project_switch"):
            yield event.plain_result(_deny_text(event))
            return
        result = await _call_bridge(
            "POST",
            "/projects/current",
            {"id": str(identifier or "").strip()},
        )
        if not result.get("ok"):
            yield event.plain_result(f"切换项目失败: {_display_text(result)}")
            return
        yield event.plain_result("已切换项目:\n" + _format_projects(result))

    @codex.command("new-project", alias={"create-project", "\u65b0\u9879\u76ee", "\u521b\u5efa\u9879\u76ee"})
    async def new_project(self, event: AstrMessageEvent, name: GreedyStr):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "project_create"):
            yield event.plain_result(_deny_text(event))
            return
        result = await _call_bridge("POST", "/projects", {"name": str(name or "").strip()})
        if not result.get("ok"):
            yield event.plain_result(f"创建项目失败: {_display_text(result)}")
            return
        yield event.plain_result("项目已创建并切换:\n" + _format_projects(result))

    @codex.command("memories", alias={"memory", "\u8bb0\u5fc6", "\u8bb0\u5fc6\u5217\u8868"})
    async def memories(self, event: AstrMessageEvent, query: GreedyStr = ""):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "memories"):
            yield event.plain_result(_deny_text(event))
            return
        params = {"user_id": _sender_id(event), "limit": "20"}
        if str(query or "").strip():
            params["q"] = str(query or "").strip()
        result = await _call_bridge("GET", "/assistant/memories?" + urllib.parse.urlencode(params))
        yield event.plain_result(_format_memories(result))

    @codex.command("remember", alias={"\u8bb0\u4f4f"})
    async def remember(self, event: AstrMessageEvent, content: GreedyStr):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "remember"):
            yield event.plain_result(_deny_text(event))
            return
        result = await _call_bridge(
            "POST",
            "/assistant/memories",
            {"user_id": _sender_id(event), "content": str(content or "").strip(), "source": "qq-manual"},
        )
        if not result.get("ok"):
            yield event.plain_result(f"记忆写入失败: {_display_text(result)}")
            return
        memory = result.get("memory") or {}
        yield event.plain_result(f"已记住: {memory.get('content', '')}\nID: {memory.get('id', '?')}")

    @codex.command("forget", alias={"\u5fd8\u8bb0", "\u5220\u9664\u8bb0\u5fc6"})
    async def forget(self, event: AstrMessageEvent, memory_id: str):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "forget"):
            yield event.plain_result(_deny_text(event))
            return
        result = await _call_bridge(
            "POST",
            "/assistant/memories/delete",
            {"user_id": _sender_id(event), "id": (memory_id or "").strip()},
        )
        yield event.plain_result("已删除记忆。" if result.get("deleted") else "没有找到这条记忆。")

    @codex.command("persona", alias={"\u4eba\u8bbe", "\u89d2\u8272"})
    async def persona(self, event: AstrMessageEvent, content: GreedyStr = ""):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "persona"):
            yield event.plain_result(_deny_text(event))
            return
        text = str(content or "").strip()
        if text:
            result = await _call_bridge("POST", "/assistant/settings", {"persona": text})
        else:
            result = await _call_bridge("GET", "/assistant/settings")
        yield event.plain_result(_format_persona(result))

    @codex.command("relationship", alias={"\u5173\u7cfb", "\u6a21\u5f0f"})
    async def relationship(self, event: AstrMessageEvent, content: GreedyStr):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "relationship_set"):
            yield event.plain_result(_deny_text(event))
            return
        result = await _call_bridge(
            "POST",
            "/assistant/settings",
            {"relationship": str(content or "").strip()},
        )
        yield event.plain_result(_format_persona(result))

    @codex.command("result", alias={"r", "\u7ed3\u679c"})
    async def result(self, event: AstrMessageEvent, task_id: str):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "result"):
            yield event.plain_result(_deny_text(event))
            return
        try:
            result = await _call_bridge("GET", f"/tasks/{task_id.strip()}")
            task = result.get("task")
            if not task:
                yield event.plain_result(result.get("error", "task_not_found"))
                return
            yield event.plain_result(_format_task(task, include_output=True))
        except Exception as exc:
            logger.exception("task result failed")
            yield event.plain_result(f"Task result failed: {exc}")

    @codex.command("cancel", alias={"stop", "\u53d6\u6d88", "\u505c\u6b62"})
    async def cancel(self, event: AstrMessageEvent, task_id: str):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "cancel"):
            yield event.plain_result(_deny_text(event))
            return
        try:
            result = await _call_bridge("POST", f"/tasks/{task_id.strip()}/cancel")
            task = result.get("task")
            if not task:
                yield event.plain_result(result.get("error", "task_not_found"))
                return
            yield event.plain_result(_format_task(task, include_output=False))
        except Exception as exc:
            logger.exception("task cancel failed")
            yield event.plain_result(f"Task cancel failed: {exc}")

    @codex.command("retry", alias={"rerun", "\u91cd\u8bd5", "\u91cd\u8dd1"})
    async def retry(self, event: AstrMessageEvent, task_id: str):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "retry"):
            yield event.plain_result(_deny_text(event))
            return
        task_id = (task_id or "").strip()
        try:
            result = await _call_bridge("POST", f"/tasks/{task_id}/retry")
            task = result.get("task")
            if not task:
                yield event.plain_result(_retry_error_text(result.get("error", "task_retry_failed")))
                return
            yield event.plain_result(
                f"已开始重试任务 #{task.get('id', '?')}，最终结果将由可靠投递队列发送。",
            )
        except Exception as exc:
            logger.exception("task retry failed")
            yield event.plain_result(f"Task retry failed: {exc}")

    @codex.command("ask", alias={"q", "\u95ee"})
    async def ask(self, event: AstrMessageEvent, prompt: GreedyStr):
        """Run Codex in read-only mode."""
        async for result in self._run_codex(event, prompt, sandbox="read-only"):
            yield result

    @codex.command(
        "code",
        alias={"dev", "\u5f00\u53d1", "\u5199\u4ee3\u7801", "\u6539\u4ee3\u7801"},
    )
    async def code(self, event: AstrMessageEvent, prompt: GreedyStr):
        """Run Codex with workspace-write access."""
        async for result in self._run_codex(event, prompt, sandbox="workspace-write"):
            yield result

    async def assistant_chat(
        self,
        event: AstrMessageEvent,
        prompt: str,
        trace_id: str = "",
        participation_metadata: dict | None = None,
        visual_media: list[dict] | None = None,
    ):
        event.should_call_llm(True)
        if not await _event_access_allowed(event, "chat"):
            yield event.plain_result(_deny_text(event))
            return
        prompt = (prompt or "").strip()
        if not prompt:
            async for result in self.help(event):
                yield result
            return
        try:
            try:
                await event.send_typing()
            except Exception:
                pass
            bridge_payload = {
                "user_id": _sender_id(event),
                "session": _event_session(event),
                "message": prompt,
                "requested_action": "chat",
                "timeout": 180,
                "trace_id": trace_id,
                **(participation_metadata or {}),
            }
            if visual_media:
                bridge_payload["visual_media"] = visual_media
            result = await _call_bridge(
                "POST",
                "/assistant/dispatch",
                bridge_payload,
            )
            if not result.get("ok"):
                if result.get("delivery_queued"):
                    return
                message = _failure_output(result) or public_failure_message(result)
                yield event.plain_result(message)
                return
            if result.get("dispatch") in {"task", "task_append"}:
                task = result.get("task") or {}
                task_id = str(task.get("id") or "")
                if trace_id:
                    await _audit_event(
                        event,
                        trace_id,
                        "task_created" if result.get("dispatch") == "task" else "task_append",
                        action="dispatch",
                        status=str(task.get("status") or "ok"),
                        task_id=task_id,
                        detail=(
                            f"mode={result.get('mode') or ''}; "
                            f"intent={result.get('intent') or ''}; "
                            f"delivery={task.get('delivery_status') or ''}"
                        ),
                    )
                if result.get("delivery_queued"):
                    return
                yield event.plain_result(_compact_output(result.get("reply") or "已转入后台任务。"))
                return
            if result.get("delivery_queued"):
                return
            reply_text = _compact_output(result.get("reply") or result.get("output") or "")
            meme = result.get("meme") if isinstance(result, dict) else None
            if meme:
                selection_id = str(meme.get("selection_id") or "").strip()
                try:
                    yield event.chain_result(_message_components(reply_text, meme))
                except Exception as exc:
                    if selection_id:
                        await _call_bridge(
                            "POST",
                            "/assistant/memes/mark",
                            {"selection_id": selection_id, "status": "failed", "error": str(exc)},
                        )
                    raise
                else:
                    if selection_id:
                        await _call_bridge(
                            "POST",
                            "/assistant/memes/mark",
                            {"selection_id": selection_id, "status": "sent"},
                        )
            else:
                yield event.plain_result(reply_text)
        except Exception as exc:
            logger.warning("assistant chat failed error=%s", type(exc).__name__)
            yield event.plain_result(public_failure_message({"error_kind": "internal"}))

    # Receive every mapped Adapter message type without consulting rendered
    # text.  Real QQ At-only chains can bypass RegexFilter even when the same
    # conversation's At+text messages activate it.  Structural group facts
    # below decide ownership; private messages continue to their own handler.
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
        | filter.EventMessageType.OTHER_MESSAGE,
        priority=1000,
    )
    async def group_auto_codex(self, event: AstrMessageEvent):
        try:
            message_type = getattr(event.get_message_type(), "name", "")
        except Exception:
            message_type = ""
        group_id = _group_id(event)
        if not event_is_structural_group(event, group_id=group_id):
            return
        logger.info(
            "codex_agent group entry message_type=%s structural_group=1",
            message_type or "unknown",
        )
        # This adapter is the sole owner of the assistant's group-conversation
        # policy.  Suppress AstrBot's default Agent before any text/mention/
        # access early return; other plugin handlers may still run unless this
        # handler explicitly stops the event below.
        event.should_call_llm(True)
        started = time.monotonic()
        raw_text = (event.get_message_str() or "").strip()
        participation_metadata = event_participation_metadata(event, bot_id=RUNTIME_STATE.actual_bot_id)
        if raw_text.startswith("/"):
            return
        trace_id = uuid.uuid4().hex[:12]
        is_mention = event_addresses_assistant(event, participation_metadata)
        if not raw_text and not is_mention and not participation_metadata["attachments"]:
            logger.debug("codex_agent group silent group=%s reason=empty_non_mention", group_id or "unknown")
            return

        event.stop_event()
        if not group_id:
            logger.warning("codex_agent group rejected reason=missing_group_id")
            if is_mention:
                yield event.plain_result("群聊消息已收到，但当前适配器没有提供群会话标识，请在管理后台查看 QQ 连接诊断。")
            return
        if not await _event_access_allowed(event, "group_message"):
            access = last_access_decision()
            if is_mention and access.get("reason") == "group_not_allowlisted":
                if access.get("role") in {"super_admin", "admin"}:
                    yield event.plain_result(
                        f"当前群尚未加入白名单（群号 {group_id}）。"
                        f"请私聊我发送：开放QQ群 {group_id}"
                    )
                else:
                    yield event.plain_result("当前群尚未获得管理员授权。")
            return
        try:
            visual_media = (
                await event_visual_media_payloads(event)
                if participation_metadata["attachments"]
                else []
            )
            bridge_payload = {
                "group_id": group_id,
                "group_name": _group_name(event),
                "session": _event_session(event),
                "sender_id": _sender_id(event),
                "sender_name": _sender_name(event),
                "message": raw_text,
                "is_mention": is_mention,
                "trace_id": trace_id,
                "timeout": 120,
                **participation_metadata,
            }
            if visual_media:
                bridge_payload["visual_media"] = visual_media
            result = await _call_bridge(
                "POST",
                "/assistant/group/dispatch",
                bridge_payload,
            )
            logger.info(
                "codex_agent group dispatch trace=%s addressed=%s components=%s self_source=%s elapsed_ms=%s queued=%s",
                trace_id,
                int(is_mention),
                len(participation_metadata["message_components"]),
                participation_metadata.get("self_id_source") or "missing",
                round((time.monotonic() - started) * 1000),
                int(bool(result.get("delivery_queued"))),
            )
        except Exception as exc:
            logger.warning(
                "codex_agent group bridge failed trace=%s elapsed_ms=%s error=%s",
                trace_id,
                round((time.monotonic() - started) * 1000),
                type(exc).__name__,
            )
            if group_event_requires_failure_notice(
                is_mention=is_mention,
                participation_metadata=participation_metadata,
            ):
                yield event.plain_result("群聊助手暂时无法连接到 Bridge，请稍后重试。")
            return
        if not result.get("ok"):
            logger.warning(
                "codex_agent group model failed group=%s kind=%s error=%s",
                group_id,
                result.get("error_kind") or "unknown",
                result.get("error") or "group_dispatch_failed",
            )
            if result.get("delivery_queued"):
                return
            if group_event_requires_failure_notice(
                is_mention=is_mention,
                participation_metadata=participation_metadata,
                result=result,
            ):
                yield event.plain_result(
                    _failure_output(result)
                    or "群聊助手本次请求失败，请稍后重试或在管理后台查看模型连接状态。",
                )
            return
        if not result.get("should_reply"):
            logger.debug(
                "codex_agent group silent group=%s reason=%s",
                group_id,
                result.get("reason") or result.get("error") or "policy",
            )
            return
        if result.get("delivery_queued"):
            return
        reply_text = _compact_output(result.get("reply") or result.get("output") or "")
        if not reply_text:
            return
        meme = result.get("meme") if isinstance(result.get("meme"), dict) else None
        selection_id = str((meme or {}).get("selection_id") or "").strip()
        try:
            if meme:
                yield event.chain_result(_message_components(reply_text, meme))
            else:
                yield event.plain_result(reply_text)
        except Exception as exc:
            if selection_id:
                await _call_bridge(
                    "POST",
                    "/assistant/memes/mark",
                    {"selection_id": selection_id, "status": "failed", "error": str(exc)},
                )
            raise
        else:
            if selection_id:
                await _call_bridge(
                    "POST",
                    "/assistant/memes/mark",
                    {"selection_id": selection_id, "status": "sent"},
                )

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-100)
    async def private_auto_codex(self, event: AstrMessageEvent):
        if not self._runtime_task or self._runtime_task.done():
            self._runtime_task = asyncio.create_task(self._runtime_client.run())
        if not self._delivery_task or self._delivery_task.done():
            self._delivery_task = asyncio.create_task(self._delivery_loop())

        if not RUNTIME_STATE.ready or not RUNTIME_STATE.auto_private_chat:
            return

        sender = _sender_id(event)
        raw_text = (event.get_message_str() or "").strip()
        participation_metadata = event_participation_metadata(event, bot_id=RUNTIME_STATE.actual_bot_id)
        if not raw_text and not participation_metadata["attachments"]:
            return
        trace_id = uuid.uuid4().hex[:12]
        text = _strip_private_command_prefix(raw_text or "（发送了一项媒体内容）")
        if text is None:
            logger.info("codex_agent private ignored sender=%s reason=unknown_slash_command", sender)
            return
        action, prompt = ("help", "") if not text else _route_private_text(text)
        if not await _event_access_allowed(event, action):
            event.should_call_llm(True)
            event.stop_event()
            return

        if RUNTIME_STATE.voice_transport_probe_enabled:
            handled, probe_reply = await handle_owner_private_voice_transport_probe(
                event,
                external_message_id=event_external_message_id(event),
                call_bridge=_call_bridge,
                logger=logger,
            )
            if handled:
                event.should_call_llm(True)
                event.stop_event()
                yield event.plain_result(probe_reply)
                return

        if RUNTIME_STATE.voice_input_enabled or RUNTIME_STATE.voice_input_fetch_enabled:
            handled, voice_reply = await handle_owner_private_voice(
                event,
                input_enabled=RUNTIME_STATE.voice_input_enabled,
                external_message_id=event_external_message_id(event),
                session=_event_session(event),
                call_bridge=_call_bridge,
                logger=logger,
            )
            if handled:
                event.should_call_llm(True)
                event.stop_event()
                if voice_reply:
                    yield event.plain_result(voice_reply)
                return

        visual_media = (
            await event_visual_media_payloads(event)
            if participation_metadata["attachments"]
            else []
        )

        logger.info("codex_agent private received sender=%s chars=%s", sender, len(raw_text))
        await _audit_event(event, trace_id, "received", status="ok", message=raw_text)

        event.should_call_llm(True)
        event.stop_event()
        if not text:
            await _audit_event(event, trace_id, "route", action="help", status="empty_text")
            async for result in self._forward_with_audit(event, trace_id, "help", self.help(event)):
                yield result
            return

        logger.info(
            "codex_agent private route sender=%s action=%s prompt_chars=%s",
            sender,
            action,
            len(prompt or ""),
        )
        await _audit_event(event, trace_id, "route", action=action, status="ok", message=prompt)
        if action == "status":
            async for result in self._forward_with_audit(event, trace_id, action, self.status(event)):
                yield result
            return
        if action == "health":
            async for result in self._forward_with_audit(event, trace_id, action, self.health(event)):
                yield result
            return
        if action == "github_trending":
            async for result in self._forward_with_audit(event, trace_id, action, self.github_trending(event)):
                yield result
            return
        if action == "help":
            async for result in self._forward_with_audit(event, trace_id, action, self.help(event)):
                yield result
            return
        if action == "tasks":
            async for result in self._forward_with_audit(event, trace_id, action, self.tasks(event, prompt)):
                yield result
            return
        if action == "task_stats":
            async for result in self._forward_with_audit(event, trace_id, action, self.task_stats(event)):
                yield result
            return
        if action == "result":
            async for result in self._forward_with_audit(event, trace_id, action, self.result(event, prompt)):
                yield result
            return
        if action == "cancel":
            async for result in self._forward_with_audit(event, trace_id, action, self.cancel(event, prompt)):
                yield result
            return
        if action == "retry":
            async for result in self._forward_with_audit(event, trace_id, action, self.retry(event, prompt)):
                yield result
            return
        if action == "projects":
            async for result in self._forward_with_audit(event, trace_id, action, self.projects(event)):
                yield result
            return
        if action == "project_switch":
            async for result in self._forward_with_audit(event, trace_id, action, self.switch_project(event, prompt)):
                yield result
            return
        if action == "project_create":
            async for result in self._forward_with_audit(event, trace_id, action, self.new_project(event, prompt)):
                yield result
            return
        if action == "memories":
            async for result in self._forward_with_audit(event, trace_id, action, self.memories(event, "")):
                yield result
            return
        if action == "remember":
            async for result in self._forward_with_audit(event, trace_id, action, self.remember(event, prompt)):
                yield result
            return
        if action == "forget":
            async for result in self._forward_with_audit(event, trace_id, action, self.forget(event, prompt)):
                yield result
            return
        if action == "persona":
            async for result in self._forward_with_audit(event, trace_id, action, self.persona(event, "")):
                yield result
            return
        if action == "persona_set":
            async for result in self._forward_with_audit(event, trace_id, action, self.persona(event, prompt)):
                yield result
            return
        if action == "relationship_set":
            async for result in self._forward_with_audit(event, trace_id, action, self.relationship(event, prompt)):
                yield result
            return
        if action == "chat":
            async for result in self._forward_with_audit(
                event,
                trace_id,
                action,
                self.assistant_chat(
                    event,
                    prompt,
                    trace_id=trace_id,
                    participation_metadata=participation_metadata,
                    visual_media=visual_media,
                ),
            ):
                yield result
            return

        sandbox = "workspace-write" if action == "code" else "read-only"
        async for result in self._forward_with_audit(
            event,
            trace_id,
            action,
            self._run_codex(event, prompt, sandbox=sandbox, trace_id=trace_id),
        ):
            yield result

    async def _run_codex(self, event: AstrMessageEvent, prompt: str, sandbox: str, trace_id: str = ""):
        event.should_call_llm(True)
        if not await _event_access_allowed(
            event,
            "code" if sandbox == "workspace-write" else "ask",
        ):
            yield event.plain_result(_deny_text(event))
            return

        prompt = (prompt or "").strip()
        if not prompt:
            if trace_id:
                await _audit_event(event, trace_id, "error", action="codex", status="empty_prompt")
            yield event.plain_result("Please provide a prompt, for example: /c ask hello")
            return

        try:
            try:
                await event.send_typing()
            except Exception:
                pass

            created = await _call_bridge(
                "POST",
                "/tasks",
                {
                    "prompt": prompt,
                    "sandbox": sandbox,
                    "timeout": 600 if sandbox == "workspace-write" else 180,
                    "source": "qq",
                    "user_id": _sender_id(event),
                    "trace_id": trace_id,
                    "origin_message": prompt,
                    "intent": "code" if sandbox == "workspace-write" else "analysis",
                    "mode": "work",
                },
            )
            if not created.get("ok"):
                if trace_id:
                    await _audit_event(
                        event,
                        trace_id,
                        "error",
                        action="codex",
                        status=str(created.get("error_kind") or created.get("error") or "create_failed"),
                        detail=_display_text(created),
                    )
                yield event.plain_result(f"Codex task create failed: {_display_text(created)}")
                return

            task = created.get("task", {})
            if trace_id:
                await _audit_event(
                    event,
                    trace_id,
                    "task_created",
                    action="codex",
                    status=str(task.get("status") or "created"),
                    task_id=str(task.get("id") or ""),
                    detail=f"sandbox={task.get('sandbox', sandbox)}",
                )
            logger.info(
                "codex_agent task created sender=%s task_id=%s sandbox=%s",
                _sender_id(event),
                task.get("id", "?"),
                task.get("sandbox", sandbox),
            )
            yield event.plain_result(
                f"已开始任务 #{task.get('id', '?')} ({task.get('sandbox', sandbox)})，"
                "最终结果将由可靠投递队列发送。",
            )
        except Exception as exc:
            logger.exception("codex command failed")
            if trace_id:
                await _audit_event(event, trace_id, "error", action="codex", status="exception", detail=str(exc))
            yield event.plain_result(f"Codex call failed: {exc}")

    async def _mark_task_delivery(self, task_id: str, status: str, error: str = ""):
        if not task_id:
            return
        try:
            await _call_bridge(
                "POST",
                f"/tasks/{task_id}/delivery",
                {"delivery_status": status, "delivery_error": error},
            )
        except Exception:
            logger.exception("task delivery status update failed")

    async def terminate(self):
        if self._runtime_task:
            self._runtime_task.cancel()
        if self._delivery_task:
            self._delivery_task.cancel()
        logger.info("codex_agent plugin terminated")
