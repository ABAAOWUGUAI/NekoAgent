#!/usr/bin/env python3
"""Least-privilege HTTP boundary for VM-1B QQ voice transport probes."""

from __future__ import annotations

from bridge_auth import PrincipalKind
from bridge_voice_transport_probe import (
    VoiceTransportProbeError,
    latest_voice_transport_probe,
    record_voice_transport_probe,
)


class VoiceTransportProbeHttpApi:
    PATH = "/qq/voice/transport-probe"

    def __init__(self, assistant_connect, json_response) -> None:
        self._assistant_connect = assistant_connect
        self._json_response = json_response

    def _error(self, request, exc: Exception) -> None:
        if isinstance(exc, VoiceTransportProbeError):
            status = 409 if str(exc) == "voice_transport_probe_disabled" else 400
            self._json_response(request, status, {"ok": False, "error": str(exc)})
            return
        self._json_response(
            request,
            500,
            {"ok": False, "error": "voice_transport_probe_internal_error"},
        )

    def handle_get(self, request, path: str, principal) -> bool:
        if path != self.PATH:
            return False
        if principal not in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}:
            self._json_response(request, 403, {"ok": False, "error": "forbidden"})
            return True
        try:
            with self._assistant_connect() as conn:
                probe = latest_voice_transport_probe(conn)
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 200, {"ok": True, "probe": probe})
        return True

    def handle_post(self, request, path: str, payload: dict, principal) -> bool:
        if path != self.PATH:
            return False
        if principal is not PrincipalKind.QQ_CHANNEL:
            self._json_response(request, 403, {"ok": False, "error": "forbidden"})
            return True
        try:
            with self._assistant_connect() as conn:
                probe = record_voice_transport_probe(conn, payload)
        except Exception as exc:
            self._error(request, exc)
            return True
        self._json_response(request, 201, {"ok": True, "probe": probe})
        return True


__all__ = ["VoiceTransportProbeHttpApi"]
