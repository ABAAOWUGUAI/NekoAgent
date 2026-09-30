#!/usr/bin/env python3
"""Private Unix-socket service for BE-4 evaluation.

The service owns its Vault, provider credential file and Ed25519 private key.
It never opens the Assistant database and never exposes a prompt, private
case, Gold, Rubric, model output, credential or signing key through its RPC.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import socketserver
import stat
import threading
import time
import urllib.error
import urllib.request

from bridge_behavior_private_evaluator_runtime import (
    PrivateEvaluatorRuntime,
    PrivateEvaluatorRuntimeError,
    _regular_private_path,
)
from bridge_model_adapters import parse_model_response, prepare_model_request


_SERVICE_CONFIG_FIELDS = frozenset({"schema_version", "runtime", "model"})
_MODEL_FIELDS = frozenset(
    {
        "base_url", "model", "api_key_path", "timeout_seconds",
        "input_microunits_per_token", "output_microunits_per_token",
    }
)
_MAX_REQUEST_BYTES = 65536


class PrivateEvaluatorServiceError(RuntimeError):
    """The isolated evaluator service cannot safely process a request."""


def _integer(value: object, *, error: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise PrivateEvaluatorServiceError(error)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PrivateEvaluatorServiceError(error) from exc
    if result < minimum or result > maximum or str(result) != str(value).strip():
        raise PrivateEvaluatorServiceError(error)
    return result


def _api_key(value: object) -> str:
    path = _regular_private_path(value, error="private_evaluator_api_key_invalid")
    try:
        result = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PrivateEvaluatorServiceError("private_evaluator_api_key_invalid") from exc
    if not result or "\x00" in result or len(result.encode("utf-8")) > 65536:
        raise PrivateEvaluatorServiceError("private_evaluator_api_key_invalid")
    return result


def _model_runner(config: Mapping[str, object]):
    if not isinstance(config, Mapping) or set(config) != _MODEL_FIELDS:
        raise PrivateEvaluatorServiceError("private_evaluator_model_config_invalid")
    base_url = str(config.get("base_url") or "").strip()
    model = str(config.get("model") or "").strip()
    if not base_url.startswith("https://") or not model:
        raise PrivateEvaluatorServiceError("private_evaluator_model_config_invalid")
    timeout = _integer(config.get("timeout_seconds"), error="private_evaluator_model_config_invalid", minimum=10, maximum=300)
    input_rate = _integer(config.get("input_microunits_per_token"), error="private_evaluator_model_config_invalid", minimum=1, maximum=10**9)
    output_rate = _integer(config.get("output_microunits_per_token"), error="private_evaluator_model_config_invalid", minimum=1, maximum=10**9)
    api_key_path = config.get("api_key_path")

    def run(kind: str, prompt: str, identity: Mapping[str, str]) -> Mapping[str, object]:
        del identity
        settings = {
            "model_transport": "openai_chat_completions",
            "chat_base_url": base_url,
            "chat_api_key": _api_key(api_key_path),
            "chat_model": model,
            "chat_temperature": 0.0,
            "chat_max_tokens": 1200 if kind == "judge" else 900,
            "model_role": "behavior_private_evaluator",
        }
        try:
            spec = prepare_model_request(settings, [{"role": "user", "content": prompt}])
            request = urllib.request.Request(
                str(spec["url"]),
                data=json.dumps(spec["payload"], ensure_ascii=False).encode("utf-8"),
                headers=dict(spec["headers"]),
                method="POST",
            )
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise PrivateEvaluatorServiceError("private_evaluator_model_response_invalid")
            data = json.loads(raw.decode("utf-8"))
            output, usage = parse_model_response(str(spec["transport"]), data)
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
            if not output or prompt_tokens < 0 or output_tokens < 1:
                raise PrivateEvaluatorServiceError("private_evaluator_model_response_invalid")
            return {
                "ok": True,
                "output": output,
                "cost_microunits": prompt_tokens * input_rate + output_tokens * output_rate,
                "latency_ms": max(1, round((time.monotonic() - started) * 1000)),
                "output_tokens": output_tokens,
            }
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            raise PrivateEvaluatorServiceError("private_evaluator_model_call_failed") from exc

    return run


def load_private_evaluator_runtime(config_path: Path | str) -> PrivateEvaluatorRuntime:
    path = _regular_private_path(config_path, error="private_evaluator_service_config_invalid")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrivateEvaluatorServiceError("private_evaluator_service_config_invalid") from exc
    if not isinstance(config, dict) or set(config) != _SERVICE_CONFIG_FIELDS or config.get("schema_version") != 1:
        raise PrivateEvaluatorServiceError("private_evaluator_service_config_invalid")
    try:
        return PrivateEvaluatorRuntime(config["runtime"], model_runner=_model_runner(config["model"]))
    except (PrivateEvaluatorRuntimeError, PrivateEvaluatorServiceError) as exc:
        raise PrivateEvaluatorServiceError("private_evaluator_service_config_invalid") from exc


class PrivateEvaluatorService:
    """Strict body-free RPC dispatch around one private runtime."""

    def __init__(self, runtime: PrivateEvaluatorRuntime) -> None:
        if not isinstance(runtime, PrivateEvaluatorRuntime):
            raise PrivateEvaluatorServiceError("private_evaluator_runtime_invalid")
        self._runtime = runtime

    def handle(self, payload: object) -> dict[str, object]:
        if type(payload) is not dict or type(payload.get("op")) is not str:
            raise PrivateEvaluatorServiceError("private_evaluator_request_invalid")
        operation = payload["op"]
        expected = {
            "reference_plan": {"op", "assistant_id"},
            "run_reference": {"op", "assistant_id", "run_ref", "completed_at"},
            "candidate_plan": {"op", "assistant_id", "candidate", "run_ref"},
            "run_candidate": {"op", "assistant_id", "candidate", "owner_authorization_ref", "run_ref", "completed_at"},
        }
        if operation not in expected or set(payload) != expected[operation]:
            raise PrivateEvaluatorServiceError("private_evaluator_request_invalid")
        if operation == "reference_plan":
            return self._runtime.reference_plan(payload["assistant_id"])
        if operation == "run_reference":
            return self._runtime.run_reference(payload["assistant_id"], payload["run_ref"], payload["completed_at"])
        if operation == "candidate_plan":
            return self._runtime.candidate_plan(payload["assistant_id"], payload["candidate"], payload["run_ref"])
        return self._runtime.run_candidate(
            payload["assistant_id"], payload["candidate"], payload["owner_authorization_ref"], payload["run_ref"], payload["completed_at"],
        )


def serve_unix_socket(socket_path: Path | str, service: PrivateEvaluatorService, *, stop_event: threading.Event | None = None) -> None:
    """Serve one private evaluator socket; callers own service supervision."""

    if not hasattr(socketserver, "UnixStreamServer"):
        raise PrivateEvaluatorServiceError("private_evaluator_unix_socket_unavailable")
    path = Path(socket_path)
    if not path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise PrivateEvaluatorServiceError("private_evaluator_socket_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o750)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISSOCK(info.st_mode):
            path.unlink()
        else:
            raise PrivateEvaluatorServiceError("private_evaluator_socket_invalid")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
                if not raw or len(raw) > _MAX_REQUEST_BYTES:
                    raise PrivateEvaluatorServiceError("private_evaluator_request_invalid")
                value = json.loads(raw.decode("utf-8"))
                result = service.handle(value)
                response = {"ok": True, "result": result}
            except Exception:
                response = {"ok": False, "error": "private_evaluator_request_rejected"}
            self.wfile.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n")

    class Server(socketserver.UnixStreamServer):
        allow_reuse_address = False

    with Server(str(path), Handler) as server:
        if os.name != "nt":
            os.chmod(path, 0o660)
        server.timeout = 0.25
        while stop_event is None or not stop_event.is_set():
            server.handle_request()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "PrivateEvaluatorService",
    "PrivateEvaluatorServiceError",
    "load_private_evaluator_runtime",
    "serve_unix_socket",
]
