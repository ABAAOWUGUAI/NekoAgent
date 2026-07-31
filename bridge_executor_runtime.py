"""Safe runtime argument and workspace policy for Codex executors."""

from __future__ import annotations

import os
from pathlib import Path

from bridge_executor_profiles import executor_workspace_root, read_executor_credential
from bridge_proxy_environment import apply_proxy_environment


_SENSITIVE_ENV = (
    "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY",
    "CODEX_API_KEY", "CODEX_PROXY_ACCESS_KEY",
)


def codex_exec_env(
    adapter: str, profile: dict | None, http_proxy: str, socks_proxy: str,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in _SENSITIVE_ENV:
        env.pop(key, None)
    env.update({
        "HOME": os.environ.get("CODEX_EXECUTOR_HOME", "/var/lib/agent-bridge"),
        "CODEX_HOME": os.environ.get(
            "CODEX_EXECUTOR_PROFILE_DIR",
            "/var/lib/agent-bridge/codex-profiles",
        ),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    })
    apply_proxy_environment(env, http_proxy, socks_proxy)
    if adapter in {"codex_custom_provider", "deepseek_proxy"}:
        source = str((profile or {}).get("credential_source") or "proxy_access_key")
        access_key = read_executor_credential(source)
        if not access_key:
            raise RuntimeError("executor_credential_missing")
        env["CODEX_PROXY_ACCESS_KEY"] = access_key
    return env


def validate_executor_sandbox_and_cwd(sandbox: str, adapter: str, cwd: Path) -> None:
    if adapter not in {"codex_custom_provider", "deepseek_proxy"}:
        return
    if sandbox == "danger-full-access":
        raise ValueError("danger_full_access_not_allowed_for_proxy")
    real = cwd.resolve(strict=True)
    root = executor_workspace_root().resolve(strict=True)
    if real != root and not real.is_relative_to(root):
        raise ValueError(f"cwd_not_allowed_for_proxy:{real}")


def codex_exec_args(task: dict, profile: dict | None = None) -> list[str]:
    adapter = task.get("executor_adapter") or ""
    if not adapter:
        raise RuntimeError("executor_adapter_missing")
    args = ["codex", "exec", "--skip-git-repo-check"]
    model = str(task.get("executor_model_name") or "").strip()
    if adapter in {"codex_custom_provider", "deepseek_proxy"}:
        profile_name = str((profile or {}).get("profile_name") or "").strip()
        if not profile_name:
            raise RuntimeError("executor_profile_missing")
        if not model:
            raise RuntimeError("executor_model_missing")
        args.extend(["--profile", profile_name, "--json", "--ephemeral", "--model", model])
    elif adapter == "codex_login":
        if model:
            args.extend(["--model", model])
    else:
        raise RuntimeError(f"unknown_executor_adapter:{adapter}")
    args.extend(["--sandbox", task["sandbox"]])
    if str(task.get("network_mode") or "controlled") == "search":
        if adapter != "codex_login":
            raise RuntimeError("executor_web_search_unsupported")
        # Codex Web Search is a model tool. It does not grant arbitrary shell
        # egress and therefore preserves the selected file/process sandbox.
        args.append("--search")
    return args
