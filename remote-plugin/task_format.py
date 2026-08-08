"""Plugin task formatting with the A4 truth gate (split from main.py).

Kept in a separate module so ``remote-plugin/main.py`` stays inside its legacy
non-growth budget.  A ``done`` task row that carries a failure marker or
internal runtime/sandbox prose is rendered as a business failure, never as
"已完成".
"""

from __future__ import annotations

from .truth_gate import task_leak_gate, user_safe_failure

STATUS_TEXT = {
    "queued": "排队中",
    "running": "执行中",
    "done": "已完成",
    "failed": "失败",
    "timeout": "超时",
    "cancelled": "已取消",
}


def _compact_output(text: str, reply_max_chars: int) -> str:
    text = (text or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= reply_max_chars:
        return text
    return "...(truncated)\n" + text[-reply_max_chars:]


def _task_leak_gate(text: str) -> bool:
    return task_leak_gate(text)


def display_task_output(task: dict, *, reply_max_chars: int, error_kind_messages: dict) -> str:
    failure = error_kind_messages.get(task.get("error_kind"))
    if failure:
        return _compact_output(failure, reply_max_chars)
    raw = (
        task.get("output", "")
        or task.get("stdout", "")
        or task.get("stderr", "")
        or task.get("error", "")
    )
    if _task_leak_gate(str(raw)):
        return _compact_output(
            user_safe_failure(task.get("error_kind") or "no_business_evidence", error_kind_messages),
            reply_max_chars,
        )
    if task.get("ok") and (task.get("stdout") or "").strip():
        return _compact_output(task.get("stdout", ""), reply_max_chars)
    return _compact_output(raw, reply_max_chars)


def format_task(task: dict, *, reply_max_chars: int, error_kind_messages: dict, include_output: bool = False) -> str:
    task_id = task.get("id", "?")
    status = task.get("status", "?")
    sandbox = task.get("sandbox", "?")
    duration = task.get("duration", "?")
    summary = task.get("summary", "")
    error_kind = task.get("error_kind")
    business_failed = (
        status == "done"
        and (
            bool(error_kind)
            or not task.get("ok")
            or _task_leak_gate(
                str(
                    task.get("output", "")
                    or task.get("stdout", "")
                    or task.get("stderr", "")
                    or task.get("error", "")
                )
            )
        )
    )
    effective_status = "failed" if business_failed else status
    status_text = STATUS_TEXT.get(effective_status, effective_status)
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
        lines.append(display_task_output(task, reply_max_chars=reply_max_chars, error_kind_messages=error_kind_messages))
    return "\n".join(lines)


__all__ = ["display_task_output", "format_task"]
