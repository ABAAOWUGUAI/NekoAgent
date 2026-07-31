#!/usr/bin/env python3
"""Apply one authoritative proxy configuration to child-process environments."""

from __future__ import annotations

from collections.abc import Mapping


def runtime_home(source: Mapping[str, str]) -> str:
    """Return the deployment-owned home for every non-root child process."""

    return str(
        source.get("AGENT_RUNTIME_HOME")
        or source.get("CODEX_EXECUTOR_HOME")
        or "/var/lib/agent-bridge"
    ).strip() or "/var/lib/agent-bridge"


def apply_proxy_environment(env: dict[str, str], http_proxy: str, socks_proxy: str = "") -> dict[str, str]:
    http_proxy = str(http_proxy or "").strip()
    socks_proxy = str(socks_proxy or "").strip()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if http_proxy:
            env[key] = http_proxy
        else:
            env.pop(key, None)
    for key in ("ALL_PROXY", "all_proxy"):
        if socks_proxy:
            env[key] = socks_proxy
        else:
            env.pop(key, None)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    return env


def command_environment(source: Mapping[str, str], http_proxy: str, socks_proxy: str = "") -> dict[str, str]:
    env = dict(source)
    env.update({
        "HOME": runtime_home(source),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    })
    return apply_proxy_environment(env, http_proxy, socks_proxy)


def direct_command_environment(source: Mapping[str, str]) -> dict[str, str]:
    env = dict(source)
    env["HOME"] = runtime_home(source)
    for key in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
        "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    ):
        env.pop(key, None)
    return env


__all__ = [
    "apply_proxy_environment", "command_environment", "direct_command_environment", "runtime_home",
]
