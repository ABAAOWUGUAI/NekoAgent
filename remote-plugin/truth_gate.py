"""Plugin-side internal-prose truth helpers (A4).

Kept in a separate module so ``remote-plugin/main.py`` stays inside its legacy
non-growth budget.  A task row may still say ``done`` from a completed turn;
these helpers decide whether the body is internal runtime/tooling/sandbox
prose that must never reach the user, and produce a stable, sanitised Chinese
failure instead of echoing it.
"""

from __future__ import annotations

# Server-owned closed-set internal-prose signatures.  Matching is deliberately
# conservative: only prose that reads as an internal runtime/tooling note is
# blocked, never a normal user-facing sentence that happens to mention a word.
_INTERNAL_PROSE_PATTERNS = (
    "bubblewrap",
    "restrictnamespaces",
    "no permissions to create",
    "namespace",
    "looking at my function list",
    "my available tools",
    "tool_search",
    "given these limitations",
    "execution limited",
    "the sandbox's command runner",
)


def task_leak_gate(text: str) -> bool:
    """Return True when ``text`` looks like internal runtime/tooling prose."""
    value = (text or "").lower()
    if not value:
        return True
    return any(pattern in value for pattern in _INTERNAL_PROSE_PATTERNS)


DEFAULT_SAFE_FAILURE = (
    "这次执行没有返回可验证的业务结果，系统没有把它标记为完成。"
    "请稍后重试，或在控制台查看该任务的详细错误。"
)


def user_safe_failure(kind: str, error_kind_messages: dict | None = None) -> str:
    """Return a stable Chinese failure for a known error_kind, else default."""
    if isinstance(error_kind_messages, dict):
        message = error_kind_messages.get(kind)
        if message:
            return message
    return DEFAULT_SAFE_FAILURE


__all__ = [
    "DEFAULT_SAFE_FAILURE",
    "task_leak_gate",
    "user_safe_failure",
]
