#!/usr/bin/env python3
"""Bridge-side body-free Unix client for the separately operated BE-4 evaluator."""

from __future__ import annotations

from collections.abc import Mapping
import base64
import json
import os
from pathlib import Path
import socket

try:  # The Bridge must fail closed if it cannot verify evaluator receipts.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # pragma: no cover - exercised by dependency preflight.
    Ed25519PublicKey = None  # type: ignore[assignment,misc]


_PUBLIC_CONFIG_FIELDS = frozenset(
    {"schema_version", "socket_path", "timeout_seconds", "evaluator_ref", "signing_key_ref", "public_key_base64"}
)
_MAX_RESPONSE_BYTES = 1024 * 1024


class PrivateEvaluatorClientError(RuntimeError):
    """The Bridge could not safely call or verify the private evaluator."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(char.isspace() for char in text):
        raise PrivateEvaluatorClientError(error)
    return text


def _seconds(value: object) -> int:
    if isinstance(value, bool):
        raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid") from exc
    if result < 5 or result > 1800 or str(result) != str(value).strip():
        raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid")
    return result


def _config_path() -> Path:
    configured = str(os.environ.get("BE_PRIVATE_EVALUATOR_PUBLIC_CONFIG") or "").strip()
    if not configured:
        raise PrivateEvaluatorClientError("private_evaluator_not_configured")
    path = Path(configured).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid")
    return path


class PrivateEvaluatorClient:
    """A public-key-only client.  It cannot read a Vault, model credential or private key."""

    def __init__(self, config: Mapping[str, object]) -> None:
        if Ed25519PublicKey is None:
            raise PrivateEvaluatorClientError("private_evaluator_crypto_unavailable")
        if not isinstance(config, Mapping) or set(config) != _PUBLIC_CONFIG_FIELDS or config.get("schema_version") != 1:
            raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid")
        path = Path(str(config.get("socket_path") or ""))
        if not path.is_absolute() or ".." in path.parts:
            raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid")
        self._socket_path = str(path)
        self._timeout = _seconds(config.get("timeout_seconds"))
        self.evaluator_ref = _ref(config.get("evaluator_ref"), error="private_evaluator_public_config_invalid")
        self.signing_key_ref = _ref(config.get("signing_key_ref"), error="private_evaluator_public_config_invalid")
        try:
            encoded = str(config.get("public_key_base64") or "")
            raw = base64.urlsafe_b64decode(encoded + "==")
            self.verification_key = Ed25519PublicKey.from_public_bytes(raw)
        except Exception as exc:
            raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid") from exc

    @classmethod
    def from_environment(cls) -> "PrivateEvaluatorClient":
        path = _config_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrivateEvaluatorClientError("private_evaluator_public_config_invalid") from exc
        return cls(value)

    def _call(self, payload: Mapping[str, object]) -> dict[str, object]:
        raw = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if not hasattr(socket, "AF_UNIX"):
            raise PrivateEvaluatorClientError("private_evaluator_unix_socket_unavailable")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout)
                client.connect(self._socket_path)
                client.sendall(raw)
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise PrivateEvaluatorClientError("private_evaluator_response_invalid")
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            raise PrivateEvaluatorClientError("private_evaluator_unavailable") from exc
        try:
            response = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PrivateEvaluatorClientError("private_evaluator_response_invalid") from exc
        if type(response) is not dict or set(response) not in ({"ok", "result"}, {"ok", "error"}):
            raise PrivateEvaluatorClientError("private_evaluator_response_invalid")
        if response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise PrivateEvaluatorClientError("private_evaluator_rejected")
        result = dict(response["result"])
        if result.get("evaluator_ref") != self.evaluator_ref or result.get("signing_key_ref") != self.signing_key_ref:
            raise PrivateEvaluatorClientError("private_evaluator_identity_mismatch")
        return result

    def reference_plan(self, assistant_id: object) -> dict[str, object]:
        return self._call({"op": "reference_plan", "assistant_id": _ref(assistant_id, error="private_evaluator_assistant_invalid")})

    def run_reference(self, assistant_id: object, run_ref: object, completed_at: object) -> dict[str, object]:
        return self._call({
            "op": "run_reference", "assistant_id": _ref(assistant_id, error="private_evaluator_assistant_invalid"),
            "run_ref": _ref(run_ref, error="private_evaluator_run_ref_invalid"), "completed_at": _ref(completed_at, error="private_evaluator_time_invalid"),
        })

    def candidate_plan(self, assistant_id: object, candidate: Mapping[str, object], run_ref: object) -> dict[str, object]:
        return self._call({
            "op": "candidate_plan", "assistant_id": _ref(assistant_id, error="private_evaluator_assistant_invalid"),
            "candidate": dict(candidate), "run_ref": _ref(run_ref, error="private_evaluator_run_ref_invalid"),
        })

    def run_candidate(
        self, assistant_id: object, candidate: Mapping[str, object], owner_authorization_ref: object, run_ref: object, completed_at: object,
    ) -> dict[str, object]:
        return self._call({
            "op": "run_candidate", "assistant_id": _ref(assistant_id, error="private_evaluator_assistant_invalid"),
            "candidate": dict(candidate), "owner_authorization_ref": _ref(owner_authorization_ref, error="private_evaluator_authorization_invalid"),
            "run_ref": _ref(run_ref, error="private_evaluator_run_ref_invalid"), "completed_at": _ref(completed_at, error="private_evaluator_time_invalid"),
        })


__all__ = ["PrivateEvaluatorClient", "PrivateEvaluatorClientError"]
