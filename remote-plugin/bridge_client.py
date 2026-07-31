"""Bounded and fail-fast HTTP client for the QQ Adapter -> Bridge boundary."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RequestPolicy:
    name: str
    timeout_seconds: float
    long_running: bool = False


QUICK = RequestPolicy("quick", 12)
STANDARD = RequestPolicy("standard", 45)
INTERACTIVE = RequestPolicy("interactive", 195, True)

BRIDGE_FAILURE_MESSAGES = {
    "transport_error": "当前助手刚才没能连接到处理服务，这条消息没有处理完成，请稍后再发一次。",
    "bridge_timeout": "当前助手刚才处理得太久超时了，这条消息没有处理完成，请稍后再发一次。",
    "circuit_open": "当前助手的处理服务正在恢复中，这条消息没有处理完成，请稍后再发一次。",
    "bridge_http_5xx": "当前助手刚才处理消息时遇到了服务故障，请稍后再发一次。",
    "internal": "当前助手刚才处理消息时遇到了服务故障，请稍后再发一次。",
}


def public_failure_message(result: dict) -> str:
    """Return a user-safe message without echoing transport or server details."""
    kind = str(result.get("error_kind") or result.get("error") or "").strip()
    return BRIDGE_FAILURE_MESSAGES.get(
        kind,
        "当前助手刚才没有处理完这条消息，请稍后再发一次。",
    )


def policy_for(method: str, path: str, *, long_poll_seconds: int = 25) -> RequestPolicy:
    route = str(path or "").split("?", 1)[0]
    if route == "/deliveries/claim":
        return RequestPolicy("long_poll", min(90, max(20, int(long_poll_seconds) + 15)), True)
    if route in {"/assistant/dispatch", "/assistant/group/dispatch"}:
        return INTERACTIVE
    if route in {
        "/status", "/server/status", "/tasks/stats", "/qq/events",
        "/qq/channel/runtime-config", "/qq/channel/heartbeat",
    } or route.endswith(("/ack", "/retry", "/delivery")):
        return QUICK
    return STANDARD


class BridgeClient:
    """HTTP transport with concurrency limits and a small circuit breaker.

    Mutating requests are never retried here.  The caller owns idempotency.
    """

    def __init__(
        self,
        base_url: str,
        token_reader: Callable[[], str],
        actor_headers: Callable[[], dict],
        *,
        long_poll_seconds: int = 25,
        max_concurrency: int = 8,
        max_long_concurrency: int = 2,
        failure_threshold: int = 5,
        cooldown_seconds: float = 15,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token_reader = token_reader
        self.actor_headers = actor_headers
        self.long_poll_seconds = max(1, int(long_poll_seconds))
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.clock = clock
        self._all_slots = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._long_slots = asyncio.Semaphore(max(1, int(max_long_concurrency)))
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._half_open_inflight = False
        self._last_error_kind = ""

    def _before_request(self) -> bool:
        now = self.clock()
        with self._lock:
            if self._open_until > now:
                return False
            if self._open_until:
                if self._half_open_inflight:
                    return False
                self._half_open_inflight = True
            return True

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._half_open_inflight = False
            self._last_error_kind = ""

    def _record_failure(self, error_kind: str) -> None:
        now = self.clock()
        with self._lock:
            self._consecutive_failures += 1
            self._last_error_kind = str(error_kind or "transport_error")[:80]
            if self._half_open_inflight or self._consecutive_failures >= self.failure_threshold:
                self._open_until = now + self.cooldown_seconds
            self._half_open_inflight = False

    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            remaining = max(0.0, self._open_until - now)
            return {
                "state": "open" if remaining > 0 else "closed",
                "consecutive_failures": self._consecutive_failures,
                "cooldown_remaining_seconds": round(remaining, 3),
                "last_error_kind": self._last_error_kind,
            }

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self._before_request():
            return {"ok": False, "error": "circuit_open", "error_kind": "circuit_open"}
        policy = policy_for(method, path, long_poll_seconds=self.long_poll_seconds)
        data = None
        headers = {"X-Channel-Token": self.token_reader(), **self.actor_headers()}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=policy.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
            self._record_success()
            return result if isinstance(result, dict) else {"ok": False, "error": "invalid_bridge_response"}
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                self._record_failure("bridge_http_5xx")
            else:
                self._record_success()
            try:
                body = json.loads(exc.read().decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("invalid_error_body")
                body.setdefault("ok", False)
                body.setdefault("status", exc.code)
                return body
            except Exception:
                return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}"}
        except (TimeoutError, socket.timeout):
            self._record_failure("bridge_timeout")
            return {"ok": False, "error": "bridge_timeout", "error_kind": "bridge_timeout"}
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            self._record_failure("transport_error")
            return {"ok": False, "error": "transport_error", "error_kind": "transport_error"}
        except Exception:
            self._record_failure("transport_error")
            return {"ok": False, "error": "transport_error", "error_kind": "transport_error"}

    async def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        policy = policy_for(method, path, long_poll_seconds=self.long_poll_seconds)
        async with self._all_slots:
            if policy.long_running:
                async with self._long_slots:
                    return await asyncio.to_thread(self.request, method, path, payload)
            return await asyncio.to_thread(self.request, method, path, payload)


__all__ = ["BridgeClient", "RequestPolicy", "policy_for", "public_failure_message"]
