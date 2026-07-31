#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from bridge_proxy_environment import command_environment


OK_HTTP_CODES = {"200", "204", "301", "302", "401", "403"}


def _command_env() -> dict[str, str]:
    env = command_environment(os.environ, "", "")
    env["CODEX_HOME"] = os.environ.get(
        "CODEX_EXECUTOR_PROFILE_DIR",
        "/var/lib/agent-bridge/codex-profiles",
    )
    return env


def curl_status(
    url: str,
    *,
    proxy: str | None = None,
    timeout: int = 10,
    mode: str = "default",
) -> dict:
    started = time.monotonic()
    command = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{time_total} %{errormsg}",
        "--connect-timeout",
        "5",
        "--max-time",
        str(timeout),
    ]
    if mode == "no_alpn":
        command.append("--no-alpn")
    elif mode == "tls12":
        command.extend(["--tlsv1.2", "--tls-max", "1.2"])
    if proxy:
        command.extend(["--proxy", proxy])
    else:
        command.extend(["--noproxy", "*"])
    command.append(url)
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout + 4,
            env=_command_env(),
        )
        output = (completed.stdout or "").strip()
        parts = output.split(" ", 2)
        code = parts[0] if parts else "000"
        elapsed = parts[1] if len(parts) > 1 else ""
        error = parts[2] if len(parts) > 2 else (completed.stderr or "")
        ok = completed.returncode == 0 and code in OK_HTTP_CODES
        return {
            "client": f"curl:{mode}",
            "ok": ok,
            "http_code": code,
            "returncode": completed.returncode,
            "duration": elapsed or round(time.monotonic() - started, 2),
            "error": error.strip()[:240],
        }
    except Exception as exc:
        return {
            "client": f"curl:{mode}",
            "ok": False,
            "http_code": "000",
            "returncode": -1,
            "duration": round(time.monotonic() - started, 2),
            "error": str(exc)[:240],
        }


def urllib_status(url: str, *, proxy: str | None = None, timeout: int = 10) -> dict:
    started = time.monotonic()
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "codex-qq-bridge-probe/1.0"})
    try:
        with opener.open(request, timeout=max(5, timeout)) as response:
            code = str(response.status)
        ok = code in OK_HTTP_CODES
        error = ""
    except urllib.error.HTTPError as exc:
        code = str(exc.code)
        ok = code in OK_HTTP_CODES
        try:
            error = exc.read(240).decode("utf-8", "replace")
        except Exception:
            error = str(exc)
    except Exception as exc:
        code = "000"
        ok = False
        error = str(exc)
    return {
        "client": "python:urllib",
        "ok": ok,
        "http_code": code,
        "returncode": 0 if ok else -1,
        "duration": round(time.monotonic() - started, 2),
        "error": error[:240],
    }


def target_probe(target: dict, *, proxy: str | None = None, timeout: int = 12) -> dict:
    probes = [
        curl_status(str(target["url"]), proxy=proxy, timeout=timeout, mode="default"),
        curl_status(str(target["url"]), proxy=proxy, timeout=timeout, mode="no_alpn"),
        urllib_status(str(target["url"]), proxy=proxy, timeout=timeout),
    ]
    ok = any(item.get("ok") for item in probes)
    best = next((item for item in probes if item.get("ok")), probes[0])
    return {
        "name": target["name"],
        "label": target["label"],
        "url": target["url"],
        "required": bool(target.get("required")),
        "ok": ok,
        "http_code": best.get("http_code", "000"),
        "duration": best.get("duration", 0),
        "client": best.get("client", ""),
        "error": "" if ok else str(best.get("error") or "")[:240],
        "probes": probes,
    }


def fast_target_probe(target: dict, *, proxy: str | None = None, timeout: int = 8) -> dict:
    """Probe one target with fallbacks, stopping as soon as a client succeeds.

    Interactive diagnostics intentionally keep :func:`target_probe` exhaustive
    so operators can compare all clients.  The system overview only needs a
    current reachability answer; running all three clients for every target made
    a manual health check take several seconds even when the first probe worked.
    """

    probes = [curl_status(str(target["url"]), proxy=proxy, timeout=timeout, mode="default")]
    if not probes[-1].get("ok"):
        probes.append(curl_status(str(target["url"]), proxy=proxy, timeout=timeout, mode="no_alpn"))
    if not probes[-1].get("ok"):
        probes.append(urllib_status(str(target["url"]), proxy=proxy, timeout=timeout))
    ok = any(item.get("ok") for item in probes)
    best = next((item for item in probes if item.get("ok")), probes[0])
    return {
        "name": target["name"],
        "label": target["label"],
        "url": target["url"],
        "required": bool(target.get("required")),
        "ok": ok,
        "http_code": best.get("http_code", "000"),
        "duration": best.get("duration", 0),
        "client": best.get("client", ""),
        "error": "" if ok else str(best.get("error") or "")[:240],
        "probes": probes,
    }


def targets_probe(targets: tuple[dict, ...] | list[dict], *, proxy: str | None = None, timeout: int = 12) -> list[dict]:
    return [target_probe(target, proxy=proxy, timeout=timeout) for target in targets]


def codex_proxy_smoke(*, proxy: str, timeout: int = 75) -> dict:
    started = time.monotonic()
    env = _command_env()
    env.update(
        {
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "https_proxy": proxy,
            "http_proxy": proxy,
            "ALL_PROXY": "socks5h://127.0.0.1:7891",
            "all_proxy": "socks5h://127.0.0.1:7891",
        },
    )
    try:
        completed = subprocess.run(
            [
                "timeout",
                str(max(15, min(timeout, 120))),
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "Reply with exactly: OK",
            ],
            text=True,
            capture_output=True,
            timeout=max(20, min(timeout + 10, 140)),
            env=env,
            cwd="/opt/agent-stack",
        )
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        ok = completed.returncode == 0 and "OK" in output
        return {
            "ok": ok,
            "returncode": completed.returncode,
            "duration": round(time.monotonic() - started, 2),
            "output_tail": output[-1200:],
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": -1,
            "duration": round(time.monotonic() - started, 2),
            "output_tail": str(exc)[:1200],
        }


def compact_target_results(results: list[dict]) -> list[dict]:
    compact = []
    for item in results:
        compact.append(
            {
                "name": item.get("name"),
                "ok": item.get("ok"),
                "client": item.get("client"),
                "http_code": item.get("http_code"),
                "required": item.get("required"),
                "error": item.get("error", ""),
            },
        )
    return compact


def dumps(data: object) -> str:
    return json.dumps(data, ensure_ascii=False)
