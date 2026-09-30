#!/usr/bin/env python3
"""Trusted work-context routing for QQ task execution.

Cross-Phase blocker fix: QQ chat tasks were all bound to the current project
cwd (``_default_cwd()``).  When the current project points at the production
runtime directory (``/opt/agent-stack``), the DeepSeek proxy executor rejects
the cwd (``cwd_not_allowed_for_proxy``), so every real QQ task is blocked.

The routing contract separates:

- Generic Work (no explicit project reference): execute in the generic agent
  execution workspace (``/opt/agent-workspace``), never inherit an unrelated
  current project.
- Project-scoped Work (explicit "current project" / project name / path
  reference): resolve to that project's path; the executor's existing
  workspace boundary decides whether it is allowable (truthful block when the
  project has no legal execution workspace).

cwd is always decided by trusted server-side resolution; a client-supplied
``_qq_cwd`` is never trusted as an arbitrary path under this contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping


WORK_CONTEXT_ROUTING_FLAG = "work_context_routing_v1"
_TRUE_VALUES = {"1", "true", "yes", "on"}

_PROJECT_SCOPE_MARKERS = (
    "当前项目",
    "本项目",
    "这个项目",
    "项目里的",
    "在项目里",
    "在项目中",
    "项目的",
    "项目中",
)


def work_context_routing_enabled(settings: Mapping[str, object]) -> bool:
    """True when the work-context routing contract is enabled."""
    return str(settings.get(WORK_CONTEXT_ROUTING_FLAG) or "").strip().lower() in _TRUE_VALUES


def message_references_project(message: object, project: Mapping[str, object] | None) -> bool:
    """True when the message explicitly references the current project.

    Uses explicit project-scope markers and the project's own name/path, never
    topic keywords (Python/查一下/研究), so Generic Work is not misrouted.
    """
    text = " ".join(str(message or "").split())
    if not text or not project:
        return False
    if any(marker in text for marker in _PROJECT_SCOPE_MARKERS):
        return True
    name = str(project.get("name") or "").strip()
    path = str(project.get("path") or "").strip()
    if len(name) >= 2 and name in text:
        return True
    if path and path in text:
        return True
    return False


def resolve_work_cwd(
    message: object,
    project: Mapping[str, object] | None,
    generic_workspace: object,
) -> Path:
    """Resolve a task cwd through trusted server-side logic only.

    Project-scoped requests resolve to the referenced project path (the
    executor enforces its own workspace boundary, producing a truthful block
    for a project without a legal execution workspace).  All other work
    resolves to the generic execution workspace.
    """
    if message_references_project(message, project) and project:
        path = str(project.get("path") or "").strip()
        if path:
            return Path(path).resolve()
    return Path(generic_workspace).resolve()


__all__ = [
    "WORK_CONTEXT_ROUTING_FLAG",
    "message_references_project",
    "resolve_work_cwd",
    "work_context_routing_enabled",
]
