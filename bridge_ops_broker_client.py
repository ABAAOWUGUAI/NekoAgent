"""Bridge-side client for the restricted Unix Ops Broker."""

from __future__ import annotations

import json
import os
import socket
from typing import Any

from bridge_ops_broker import MAX_BROKER_REQUEST, MAX_BROKER_RESPONSE


class OpsBrokerClientError(RuntimeError):
    pass


class OpsBrokerClient:
    def __init__(self, socket_path: str | None = None, *, timeout: float = 8.0) -> None:
        self.socket_path = socket_path or os.environ.get("OPS_BROKER_SOCKET", "/run/agent-bridge/ops.sock")
        self.timeout = max(0.5, min(float(timeout), 30.0))

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(message) > MAX_BROKER_REQUEST:
            raise OpsBrokerClientError("broker_request_too_large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(message)
                chunks = bytearray()
                while len(chunks) <= MAX_BROKER_RESPONSE:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            raise OpsBrokerClientError("broker_unreachable") from exc
        if len(chunks) > MAX_BROKER_RESPONSE or b"\n" not in chunks:
            raise OpsBrokerClientError("broker_response_invalid")
        try:
            result = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpsBrokerClientError("broker_response_invalid") from exc
        if not isinstance(result, dict):
            raise OpsBrokerClientError("broker_response_invalid")
        return result


__all__ = ["OpsBrokerClient", "OpsBrokerClientError"]
