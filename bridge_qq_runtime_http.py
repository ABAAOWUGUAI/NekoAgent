#!/usr/bin/env python3
"""Least-privilege HTTP adapter for Gate C5 channel runtime sync."""

from __future__ import annotations

from bridge_auth import PrincipalKind
from bridge_qq_runtime_service import channel_runtime_config, record_channel_heartbeat
from bridge_voice_transport_probe_http import VoiceTransportProbeHttpApi
from bridge_voice_input_http import VoiceInputHttpApi


class QqRuntimeHttpApi:
    CONFIG_PATH = "/qq/channel/runtime-config"
    HEARTBEAT_PATH = "/qq/channel/heartbeat"

    def __init__(self, assistant_connect, json_response, voice_dispatch=None, bridge_namespace=None) -> None:
        if bridge_namespace is not None:
            from bridge_voice_dispatch import bridge_voice_dispatch
            voice_dispatch = bridge_voice_dispatch(bridge_namespace)
        self._assistant_connect = assistant_connect
        self._json_response = json_response
        self._voice_probe = VoiceTransportProbeHttpApi(assistant_connect, json_response)
        self._voice_input = VoiceInputHttpApi(assistant_connect, json_response, voice_dispatch)

    def _error(self, request, exc: Exception) -> None:
        if isinstance(exc, ValueError):
            self._json_response(
                request,
                400,
                {"ok": False, "error": str(exc) or "qq_runtime_request_invalid"},
            )
        else:
            self._json_response(
                request,
                500,
                {"ok": False, "error": "qq_runtime_internal_error"},
            )

    def handle_get(self, request, path: str, principal) -> bool:
        if path == self._voice_probe.PATH:
            return self._voice_probe.handle_get(request, path, principal)
        if path != self.CONFIG_PATH:
            return False
        if principal is not PrincipalKind.QQ_CHANNEL:
            self._json_response(request, 403, {"ok": False, "error": "forbidden"})
            return True
        try:
            with self._assistant_connect() as conn:
                result = channel_runtime_config(conn)
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(
            request,
            200,
            {"ok": True, "config": result},
            headers={"ETag": f'"{result["etag"]}"'},
        )
        return True

    def handle_post(self, request, path: str, payload: dict, principal) -> bool:
        if path in {self._voice_input.PATH, self._voice_input.INPUT_PATH}:
            return self._voice_input.handle_post(request, path, payload, principal)
        if path == self._voice_probe.PATH:
            return self._voice_probe.handle_post(request, path, payload, principal)
        if path != self.HEARTBEAT_PATH:
            return False
        if principal is not PrincipalKind.QQ_CHANNEL:
            self._json_response(request, 403, {"ok": False, "error": "forbidden"})
            return True
        try:
            with self._assistant_connect() as conn:
                result = record_channel_heartbeat(conn, payload)
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["QqRuntimeHttpApi"]
