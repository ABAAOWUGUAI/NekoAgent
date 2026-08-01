#!/usr/bin/env python3
"""Least-privilege HTTP boundary for VM-1C controlled voice fetch."""

from bridge_auth import PrincipalKind
from bridge_voice_input import VoiceInputError, fetch_owner_voice, process_owner_voice


class VoiceInputHttpApi:
    PATH = "/qq/voice/fetch"
    INPUT_PATH = "/qq/voice/input"

    def __init__(self, assistant_connect, json_response, dispatch_voice=None):
        self._assistant_connect = assistant_connect
        self._json_response = json_response
        self._dispatch_voice = dispatch_voice

    def handle_post(self, request, path, payload, principal):
        if path not in {self.PATH, self.INPUT_PATH}:
            return False
        if principal is not PrincipalKind.QQ_CHANNEL:
            self._json_response(request, 403, {"ok": False, "error": "forbidden"})
            return True
        try:
            with self._assistant_connect() as conn:
                receipt = process_owner_voice(conn, payload) if path == self.INPUT_PATH else fetch_owner_voice(conn, payload)
            if path == self.INPUT_PATH:
                if not receipt.get("dispatch_required"):
                    self._json_response(
                        request,
                        200,
                        {
                            "ok": True,
                            "deduplicated": True,
                            "delivery_queued": bool(receipt.get("delivery_queued")),
                            "processing": bool(receipt.get("processing")),
                        },
                    )
                    return True
                if not self._dispatch_voice:
                    raise VoiceInputError("voice_dispatch_unavailable")
                result = self._dispatch_voice(request, payload, receipt)
                self._json_response(request, 200 if result.get("ok") else 500, result)
                return True
            public = {key: receipt.get(key) for key in ("fetch_status", "detected_media_type", "size_bytes", "source_deleted", "deduplicated")}
            self._json_response(request, 201, {"ok": True, "receipt": public})
        except VoiceInputError as exc:
            status = 409 if str(exc) in {"voice_input_fetch_disabled", "voice_input_disabled"} else 400
            self._json_response(request, status, {"ok": False, "error": str(exc)})
        except Exception:
            self._json_response(request, 500, {"ok": False, "error": "voice_input_internal_error"})
        return True


__all__ = ["VoiceInputHttpApi"]
