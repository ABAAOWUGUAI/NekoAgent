"""Fail-closed Unix-socket Root Ops Broker transport.

The broker deliberately has no default executor.  Deployments must inject a
small, allowlisted executor after validating the caller and the normalized
request.  This module never builds shell commands and never invokes a system
process.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import socketserver
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any, Callable

from bridge_ops_broker_contract import WRITE_ACTIONS, OpsBrokerContractError, validate_request


MAX_BROKER_REQUEST = 8192
MAX_BROKER_RESPONSE = 256_000
PEERCRED_STRUCT = "3i"
OpsExecutor = Callable[[dict[str, Any]], dict[str, Any]]


class OpsBrokerTransportError(ValueError):
    """Transport or authorization failure; safe to expose as a stable code."""


def linux_peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    """Read Linux ``pid, uid, gid`` credentials or fail closed."""

    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise OpsBrokerTransportError("broker_peer_unsupported")
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize(PEERCRED_STRUCT))
        return struct.unpack(PEERCRED_STRUCT, raw)
    except (OSError, struct.error) as exc:
        raise OpsBrokerTransportError("broker_peer_unavailable") from exc


def verify_peer_credentials(credentials: tuple[int, int, int], expected_uid: int) -> None:
    if not isinstance(expected_uid, int) or expected_uid < 0:
        raise OpsBrokerTransportError("broker_expected_uid_invalid")
    if len(credentials) != 3 or credentials[1] != expected_uid:
        raise OpsBrokerTransportError("broker_peer_forbidden")


class IdempotencyLedger:
    """Replay ledger; production can persist it across broker restarts."""

    def __init__(self, path: str | None = None) -> None:
        self._entries: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self._path), timeout=5) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS approvals(key TEXT PRIMARY KEY,expires_at TEXT NOT NULL)",
                )

    def reserve(self, key: str, expires_at: str, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError as exc:
            raise OpsBrokerTransportError("broker_idempotency_expiry_invalid") from exc
        with self._lock:
            if self._path:
                with sqlite3.connect(str(self._path), timeout=5, isolation_level=None) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("DELETE FROM approvals WHERE expires_at<=?", (current.isoformat(),))
                    try:
                        conn.execute("INSERT INTO approvals(key,expires_at) VALUES (?,?)", (key, expiry.isoformat()))
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        return False
                    conn.commit()
                    return True
            self._entries = {item: value for item, value in self._entries.items() if value > current}
            if key in self._entries:
                return False
            self._entries[key] = expiry
            return True


def process_request(
    payload: Any,
    *,
    peer_credentials: tuple[int, int, int],
    expected_uid: int,
    executor: OpsExecutor | None = None,
    ledger: IdempotencyLedger | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authorize, validate and dispatch one request without exposing exceptions."""

    try:
        verify_peer_credentials(peer_credentials, expected_uid)
        normalized = validate_request(payload, now=now)
        if normalized["action"] in WRITE_ACTIONS:
            approval = normalized["approval"]
            if not (ledger or IdempotencyLedger()).reserve(
                approval["idempotency_key"], approval["expires_at"], now=now,
            ):
                return {"ok": False, "error": "broker_idempotency_replay"}
        if executor is None:
            return {"ok": False, "error": "ops_executor_unconfigured"}
        result = executor(normalized)
        if not isinstance(result, dict):
            return {"ok": False, "error": "ops_executor_invalid_result"}
        return {"ok": bool(result.get("ok", True)), "data": result}
    except (OpsBrokerTransportError, OpsBrokerContractError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        return {"ok": False, "error": "ops_broker_internal_error"}


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "OpsBrokerServer" = self.server  # type: ignore[assignment]
        try:
            credentials = linux_peer_credentials(self.connection)
        except OpsBrokerTransportError as exc:
            self._reply({"ok": False, "error": str(exc)})
            return
        raw = self.rfile.readline(MAX_BROKER_REQUEST + 1)
        if len(raw) > MAX_BROKER_REQUEST or not raw.endswith(b"\n"):
            self._reply({"ok": False, "error": "broker_request_too_large"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply({"ok": False, "error": "broker_request_invalid_json"})
            return
        result = process_request(
            payload,
            peer_credentials=credentials,
            expected_uid=server.expected_uid,
            executor=server.executor,
            ledger=server.ledger,
        )
        self._reply(result)

    def _reply(self, result: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        self.wfile.flush()


if hasattr(socketserver, "UnixStreamServer"):

    class OpsBrokerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """Linux-only server shell; ``start`` refuses unsupported hosts."""

        daemon_threads = True
        allow_reuse_address = False

        def __init__(self, socket_path: str, *, expected_uid: int, executor: OpsExecutor | None = None) -> None:
            if os.name != "posix":
                raise OpsBrokerTransportError("ops_broker_linux_required")
            if not isinstance(socket_path, str) or not socket_path.startswith("/"):
                raise OpsBrokerTransportError("ops_broker_absolute_socket_required")
            if os.path.exists(socket_path):
                raise OpsBrokerTransportError("ops_broker_socket_exists")
            self.expected_uid = expected_uid
            self.executor = executor
            self.ledger = IdempotencyLedger(os.environ.get("OPS_BROKER_LEDGER_PATH"))
            super().__init__(socket_path, _BrokerHandler)

else:

    class OpsBrokerServer:
        """Platform placeholder that can never accidentally open a TCP socket."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise OpsBrokerTransportError("ops_broker_unix_socket_unsupported")


__all__ = [
    "IdempotencyLedger",
    "MAX_BROKER_REQUEST",
    "MAX_BROKER_RESPONSE",
    "OpsBrokerServer",
    "OpsBrokerTransportError",
    "linux_peer_credentials",
    "process_request",
    "verify_peer_credentials",
]
