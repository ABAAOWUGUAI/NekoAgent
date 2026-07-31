#!/usr/bin/env python3
"""Codex subscription-backed text reasoning for the QQ call gateway.

This module intentionally does not accept a Platform API key.  It starts
``codex app-server`` with the service account's saved ChatGPT login, verifies
that the active account type is ``chatgpt``, and exposes streamed text turns.
Local VAD/ASR/TTS and the QQ media adapter live outside this module.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol


SENSITIVE_API_ENV = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
    }
)


class CodexVoiceError(RuntimeError):
    """Fail-closed error raised by the subscription voice backend."""


def codex_subscription_env(
    source: Mapping[str, str] | None = None,
    *,
    home: str | None = None,
) -> dict[str, str]:
    """Return an environment that can only reuse Codex's saved ChatGPT auth."""

    env = dict(source or os.environ)
    for name in SENSITIVE_API_ENV:
        env.pop(name, None)
    if home:
        env["HOME"] = home
    return env


class RpcTransport(Protocol):
    def start(self) -> None: ...

    def request(self, method: str, params: dict, *, timeout: float = 15.0) -> dict: ...

    def notify(self, method: str, params: dict) -> None: ...

    def next_notification(self, *, timeout: float) -> dict: ...

    def close(self) -> None: ...


class JsonLineRpcProcess:
    """Small JSONL RPC client for a private stdio app-server process."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        cwd: str = "/opt/agent-workspace",
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.cwd = str(Path(cwd))
        self.env = dict(env or codex_subscription_env())
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._response_condition = threading.Condition()
        self._responses: dict[int, dict] = {}
        self._notifications: queue.Queue[dict] = queue.Queue(maxsize=1024)
        self._next_id = 1
        self._reader_error = ""

    def start(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            [
                self.executable,
                "app-server",
                "--listen",
                "stdio://",
                "--disable",
                "shell_tool",
                "--disable",
                "apps",
                "--disable",
                "multi_agent",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.cwd,
            env=self.env,
        )
        self._reader = threading.Thread(target=self._read_loop, name="codex-app-server-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message and "method" not in message:
                    with self._response_condition:
                        self._responses[int(message["id"])] = message
                        self._response_condition.notify_all()
                    continue
                try:
                    self._notifications.put_nowait(message)
                except queue.Full:
                    self._reader_error = "codex_notification_queue_full"
                    break
        except OSError as exc:
            self._reader_error = f"codex_reader_failed:{exc}"
        finally:
            with self._response_condition:
                self._response_condition.notify_all()

    def _send(self, message: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise CodexVoiceError("codex_app_server_not_started")
        if self._process.poll() is not None:
            raise CodexVoiceError("codex_app_server_exited")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self._process.stdin.write(payload + "\n")
            self._process.stdin.flush()

    def request(self, method: str, params: dict, *, timeout: float = 15.0) -> dict:
        with self._response_condition:
            request_id = self._next_id
            self._next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + timeout
        with self._response_condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexVoiceError(f"codex_request_timeout:{method}")
                if self._reader_error:
                    raise CodexVoiceError(self._reader_error)
                if self._process is not None and self._process.poll() is not None:
                    raise CodexVoiceError(f"codex_app_server_exited:{method}")
                self._response_condition.wait(min(remaining, 0.25))
            response = self._responses.pop(request_id)
        if response.get("error"):
            message = str((response.get("error") or {}).get("message") or "rpc_error")
            raise CodexVoiceError(f"codex_rpc_error:{method}:{message[:240]}")
        return dict(response.get("result") or {})

    def notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    def next_notification(self, *, timeout: float) -> dict:
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexVoiceError("codex_notification_timeout") from exc

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


@dataclass(frozen=True)
class CodexVoiceTurn:
    text: str
    thread_id: str
    turn_id: str
    status: str


class CodexSubscriptionVoiceSession:
    """Persistent, interruptible text session billed to the ChatGPT plan."""

    DEFAULT_INSTRUCTIONS = (
        "你正在进行电话语音对话。使用自然、简洁、适合朗读的中文回答；"
        "不要输出 Markdown、表格、链接或长列表。除非用户明确要求执行平台任务，"
        "否则不要调用工具。听不清或信息不足时先简短追问。"
    )

    def __init__(
        self,
        transport: RpcTransport,
        *,
        cwd: str = "/opt/agent-workspace",
        model: str | None = None,
        developer_instructions: str | None = None,
        turn_timeout: float = 90.0,
    ) -> None:
        self.transport = transport
        self.cwd = cwd
        self.model = model
        self.developer_instructions = developer_instructions or self.DEFAULT_INSTRUCTIONS
        self.turn_timeout = max(5.0, float(turn_timeout))
        self.thread_id = ""
        self.active_turn_id = ""
        self.account_plan = "unknown"

    def start(self) -> None:
        self.transport.start()
        self.transport.request(
            "initialize",
            {"clientInfo": {"name": "qq_call_gateway", "title": "QQ Call Gateway", "version": "1.0.0"}},
        )
        self.transport.notify("initialized", {})
        account = self.transport.request("account/read", {"refreshToken": False})
        identity = account.get("account") or {}
        if identity.get("type") != "chatgpt":
            raise CodexVoiceError("codex_chatgpt_subscription_login_required")
        self.account_plan = str(identity.get("planType") or "unknown")

        if not self.model:
            catalog = self.transport.request("model/list", {"limit": 100, "includeHidden": False})
            models = list(catalog.get("data") or [])
            selected = next((item for item in models if item.get("isDefault")), None)
            selected = selected or (models[0] if models else None)
            self.model = str((selected or {}).get("model") or (selected or {}).get("id") or "")
            if not self.model:
                raise CodexVoiceError("codex_compatible_model_missing")

        params: dict[str, object] = {
            "cwd": self.cwd,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "personality": "friendly",
            "developerInstructions": self.developer_instructions,
            "threadSource": "qq_call_gateway",
        }
        params["model"] = self.model
        started = self.transport.request("thread/start", params)
        self.thread_id = str((started.get("thread") or {}).get("id") or "")
        if not self.thread_id:
            raise CodexVoiceError("codex_thread_id_missing")

    def ask(self, text: str, *, on_delta: Callable[[str], None] | None = None) -> CodexVoiceTurn:
        prompt = str(text).strip()
        if not prompt:
            raise ValueError("voice_turn_text_required")
        if not self.thread_id:
            raise CodexVoiceError("codex_voice_session_not_started")
        started = self.transport.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "effort": "low",
                "summary": "none",
            },
        )
        self.active_turn_id = str((started.get("turn") or {}).get("id") or "")
        if not self.active_turn_id:
            raise CodexVoiceError("codex_turn_id_missing")

        chunks: list[str] = []
        deadline = time.monotonic() + self.turn_timeout
        status = "unknown"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.interrupt()
                raise CodexVoiceError("codex_voice_turn_timeout")
            try:
                event = self.transport.next_notification(timeout=min(remaining, 2.0))
            except CodexVoiceError as exc:
                if str(exc) == "codex_notification_timeout":
                    continue
                raise
            method = str(event.get("method") or "")
            params = event.get("params") or {}
            if params.get("threadId") not in (None, self.thread_id):
                continue
            if params.get("turnId") not in (None, self.active_turn_id):
                continue
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    chunks.append(delta)
                    if on_delta:
                        on_delta(delta)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "completed")
                break
        turn_id = self.active_turn_id
        self.active_turn_id = ""
        return CodexVoiceTurn("".join(chunks).strip(), self.thread_id, turn_id, status)

    def interrupt(self) -> bool:
        if not self.thread_id or not self.active_turn_id:
            return False
        self.transport.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": self.active_turn_id},
            timeout=10.0,
        )
        self.active_turn_id = ""
        return True

    def close(self) -> None:
        self.transport.close()
        self.thread_id = ""
        self.active_turn_id = ""


__all__ = [
    "CodexSubscriptionVoiceSession",
    "CodexVoiceError",
    "CodexVoiceTurn",
    "JsonLineRpcProcess",
    "codex_subscription_env",
]
