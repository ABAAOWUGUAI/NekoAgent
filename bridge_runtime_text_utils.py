from __future__ import annotations

import re
from collections.abc import Callable


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(text or ""))


def extract_codex_last_message(text: str, *, strip_ansi_fn: Callable[[str], str] = strip_ansi) -> str:
    clean = strip_ansi_fn(text).replace("\r\n", "\n").strip()
    if not clean:
        return ""
    lines = [line.rstrip() for line in clean.splitlines()]
    token_indexes = [idx for idx, line in enumerate(lines) if line.strip().lower() == "tokens used"]
    end = token_indexes[-1] if token_indexes else len(lines)
    codex_indexes = [idx for idx, line in enumerate(lines[:end]) if line.strip().lower() == "codex"]
    if codex_indexes:
        start = codex_indexes[-1] + 1
        block = [line for line in lines[start:end] if line.strip()]
        if block:
            return "\n".join(block).strip()
    marker = "Reading prompt from stdin..."
    if marker in clean:
        return clean.split(marker, 1)[0].strip()
    return clean


def trim_output(text: str, max_output_chars: int) -> str:
    return text[-max_output_chars:] if len(text) > max_output_chars else text


def codex_failure_diagnosis(
    returncode: int | None,
    output: str,
    *,
    trim_output_fn: Callable[[str], str],
) -> tuple[str, str]:
    if returncode == 0:
        return "", ""
    raw = (output or "").strip()
    lowered = raw.lower()
    markers = {
        "quota": ("insufficient_quota", "usage limit", "rate_limit_exceeded", "rate limit", "too many requests", "quota", "billing", "credit balance", "429"),
        "auth": ("not logged in", "unauthorized", "authentication", "authenticate", "login required", "device auth", "401", "403", "forbidden"),
        "network": ("connection timed out", "timed out", "timeout", "ssl", "tls", "unexpected eof", "connection reset", "proxy", "dns", "network"),
    }
    for kind, words in markers.items():
        if any(word in lowered for word in words):
            break
    else:
        kind = "empty" if not raw else "codex_failed"
    explanations = {
        "quota": "Codex failed: account quota, billing, or rate limit appears to be exhausted.",
        "auth": "Codex failed: authentication appears to be missing or expired.",
        "network": "Codex failed: network or proxy connection failed.",
        "empty": f"Codex failed with exit code {returncode}, but produced no output.",
        "codex_failed": f"Codex failed with exit code {returncode}.",
    }
    message = explanations[kind]
    if raw:
        message += "\n\nRaw Codex output:\n" + raw
    return kind, trim_output_fn(message)


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, raw_value = line.split(":", 1)
                parts = raw_value.strip().split()
                if parts:
                    values[key] = int(parts[0]) * 1024
    except OSError:
        pass
    return values


def sanitize_log_text(text: str) -> str:
    patterns = (
        re.compile(r"(?i)(token|authorization|api[-_ ]?key|secret|password|passwd)(\s*[:=]\s*)([^\s]+)"),
        re.compile(r"(初始密码|密码|密钥)(\s*[:：=]\s*)([^\s]+)"),
    )
    for pattern in patterns:
        text = pattern.sub(r"\1\2[redacted]", text)
    return text


def safe_log_text(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    patterns = (
        re.compile(r"(?i)(token|authorization|api[-_ ]?key|secret|password|passwd)(\s*[:=]\s*)([^\s]+)"),
        re.compile(r"(?i)(webui token)(\s*[:=]\s*)([^\s]+)"),
    )
    for pattern in patterns:
        text = pattern.sub(r"\1\2[redacted]", text)
    return re.sub(r"https://txz\.qq\.com/p\?k=[^\s]+", "[redacted-qq-login-url]", text)


def last_index(text: str, needles: tuple[str, ...]) -> int:
    return max((text.rfind(needle) for needle in needles), default=-1)


def recent_matching_lines(
    text: str,
    needles: tuple[str, ...],
    limit: int = 8,
    *,
    safe_log_text_fn: Callable[[str], str] = safe_log_text,
) -> list[str]:
    lines = []
    for line in safe_log_text_fn(text).splitlines():
        if any(needle in line for needle in needles):
            line = line.strip()
            if len(line) > 240:
                line = line[:237] + "..."
            lines.append(line)
    return lines[-limit:]
