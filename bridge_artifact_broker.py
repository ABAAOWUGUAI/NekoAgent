#!/usr/bin/env python3
"""Narrow Unix-socket authorization broker for the isolated preview process."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import sys
import threading
from pathlib import Path
from typing import Callable

from bridge_artifact_repository import ArtifactError, ArtifactRepository
from bridge_artifact_service import normalize_relative_path


MAX_BROKER_REQUEST = 8192


def _broker_error(exc: Exception) -> str:
    if isinstance(exc, ArtifactError):
        return str(exc) or "artifact_broker_error"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "broker_request_invalid"
    return "broker_internal_error"


def broker_security_supported() -> bool:
    return bool(
        sys.platform.startswith("linux")
        and hasattr(socket, "AF_UNIX")
        and hasattr(socket, "SO_PEERCRED")
        and hasattr(os, "getuid")
    )


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not server.peer_allowed(self.request):  # type: ignore[attr-defined]
            self._reply({"ok": False, "error": "broker_peer_forbidden"})
            return
        raw = self.rfile.readline(MAX_BROKER_REQUEST + 1)
        if len(raw) > MAX_BROKER_REQUEST or not raw.endswith(b"\n"):
            self._reply({"ok": False, "error": "broker_request_too_large"})
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("broker_json_object_required")
            result = server.dispatch(request)  # type: ignore[attr-defined]
        except Exception as exc:
            result = {"ok": False, "error": _broker_error(exc)}
        self._reply(result)

    def _reply(self, payload: dict) -> None:
        self.wfile.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n")


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    address_family = getattr(socket, "AF_UNIX", socket.AF_INET)

    def __init__(self, socket_path: str, connect: Callable, allowed_uid: int) -> None:
        self.connect = connect
        self.allowed_uid = int(allowed_uid)
        self.feature_enabled = lambda: True
        super().__init__(socket_path, _BrokerHandler)

    def peer_allowed(self, connection: socket.socket) -> bool:
        if not broker_security_supported():
            return False
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if not isinstance(credentials, bytes) or len(credentials) != 12:
                return False
            _, uid, _ = struct.unpack("3i", credentials)
        except (OSError, TypeError, struct.error):
            return False
        return uid == self.allowed_uid

    def dispatch(self, payload: dict) -> dict:
        operation = str(payload.get("operation") or "")
        if operation != "health" and not self.feature_enabled():
            raise ArtifactError("artifact_preview_disabled")
        with self.connect() as conn:
            repo = ArtifactRepository(conn, validate_schema=False)
            if operation == "health":
                return {
                    "ok": True,
                    "service": "artifact-authorization-broker",
                    "security": "linux_so_peercred",
                }
            if operation == "challenge":
                return {"ok": True, **repo.issue_challenge(str(payload.get("token") or ""))}
            if operation == "activate":
                return {
                    "ok": True,
                    **repo.activate(
                        str(payload.get("token") or ""),
                        str(payload.get("challenge") or ""),
                    ),
                }
            if operation == "authorize":
                publication_id = str(payload.get("publication_id") or "")
                authorization = repo.authorize(str(payload.get("session") or ""), publication_id)
                requested = str(payload.get("path") or "") or authorization["entrypoint_path"]
                relative = normalize_relative_path(requested)
                row = conn.execute(
                    """
                    SELECT relative_path,storage_name,media_type,size_bytes,sha256
                    FROM artifact_version_files WHERE version_id=? AND relative_path=?
                    """,
                    (authorization["version_id"], relative),
                ).fetchone()
                if not row:
                    raise ArtifactError("artifact_file_not_found")
                return {
                    "ok": True,
                    **authorization,
                    "relative_path": str(row["relative_path"]),
                    "storage_name": str(row["storage_name"]),
                    "media_type": str(row["media_type"]),
                    "size_bytes": int(row["size_bytes"]),
                    "sha256": str(row["sha256"]),
                }
        raise ArtifactError("broker_operation_invalid")


class ArtifactAuthorizationBroker:
    def __init__(
        self,
        connect: Callable,
        socket_path: Path,
        *,
        allowed_uid: int,
        socket_gid: int | None = None,
        feature_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.connect = connect
        self.socket_path = Path(socket_path)
        self.allowed_uid = int(allowed_uid)
        self.socket_gid = socket_gid
        self.feature_enabled = feature_enabled or (lambda: True)
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server:
            return
        if not broker_security_supported():
            raise ArtifactError("artifact_broker_linux_peercred_required")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = _ThreadingUnixServer(str(self.socket_path), self.connect, self.allowed_uid)
        self._server.feature_enabled = self.feature_enabled
        os.chmod(self.socket_path, 0o660)
        if self.socket_gid is not None and hasattr(os, "chown"):
            os.chown(self.socket_path, -1, int(self.socket_gid))
        self._thread = threading.Thread(target=self._server.serve_forever, name="artifact-auth-broker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._server = None
        self._thread = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def health(self) -> dict:
        ready = bool(
            self._server
            and self._thread
            and self._thread.is_alive()
            and self.socket_path.exists()
        )
        return {
            "ok": ready,
            "service": "artifact-authorization-broker",
            "security": "linux_so_peercred" if broker_security_supported() else "unsupported",
        }


class ArtifactBrokerClient:
    def __init__(self, socket_path: Path, *, timeout: float = 3.0) -> None:
        self.socket_path = str(socket_path)
        self.timeout = float(timeout)

    def request(self, operation: str, **payload) -> dict:
        message = json.dumps({"operation": operation, **payload}, ensure_ascii=True, separators=(",", ":"))
        if len(message.encode("utf-8")) > MAX_BROKER_REQUEST:
            raise ArtifactError("broker_request_too_large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(message.encode("utf-8") + b"\n")
            chunks = bytearray()
            while not chunks.endswith(b"\n") and len(chunks) <= MAX_BROKER_REQUEST:
                block = client.recv(4096)
                if not block:
                    break
                chunks.extend(block)
        if len(chunks) > MAX_BROKER_REQUEST:
            raise ArtifactError("broker_response_too_large")
        try:
            result = json.loads(bytes(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactError("broker_response_invalid") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise ArtifactError(str(result.get("error") if isinstance(result, dict) else "broker_failed"))
        return result


__all__ = [
    "ArtifactAuthorizationBroker", "ArtifactBrokerClient", "MAX_BROKER_REQUEST",
    "broker_security_supported",
]
