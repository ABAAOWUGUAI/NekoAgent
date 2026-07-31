#!/usr/bin/env python3
"""Read NapCat's live QQ login state without exposing WebUI credentials."""

from __future__ import annotations

import json
from collections.abc import Callable


_NAPCAT_LOGIN_PROBE = r'''
import hashlib
import json
import urllib.request

config = json.load(open("/app/napcat/config/webui.json", encoding="utf-8"))
password_hash = hashlib.sha256((config["token"] + ".napcat").encode()).hexdigest()

login_request = urllib.request.Request(
    "http://127.0.0.1:6099/api/auth/login",
    data=json.dumps({"hash": password_hash}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(login_request, timeout=6) as response:
    credential = (json.load(response).get("data") or {}).get("Credential")

status_request = urllib.request.Request(
    "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus",
    data=b"{}",
    headers={
        "Authorization": "Bearer " + credential,
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(status_request, timeout=6) as response:
    payload = (json.load(response).get("data") or {})

error = str(payload.get("loginError") or "").lower()
is_login = bool(payload.get("isLogin"))
is_offline = bool(payload.get("isOffline"))
if is_login and not is_offline:
    # NapCat may keep a stale/diagnostic loginError string after a successful
    # login.  The authoritative state is isLogin/isOffline; do not report a
    # healthy account as an actionable login failure.
    error_kind = "none"
elif any(marker in error for marker in ("qrcode", "qr code", "scan")):
    error_kind = "qr_required"
elif any(marker in error for marker in ("offline", "login", "expired", "invalid")):
    error_kind = "login_required"
elif error:
    error_kind = "other_login_error"
else:
    error_kind = "none"

print(json.dumps({
    "checked": True,
    "is_login": is_login,
    "is_offline": is_offline,
    "qrcode_available": bool(payload.get("qrcodeurl")),
    "error_kind": error_kind,
}))
'''


def probe_napcat_login(capture_command: Callable, container: str) -> dict:
    """Return only non-sensitive login facts from the NapCat container."""
    try:
        ok, output = capture_command(
            ["docker", "exec", container, "python3", "-c", _NAPCAT_LOGIN_PROBE],
            timeout=16,
        )
    except Exception:
        return {"checked": False, "error_kind": "probe_failed"}
    if not ok:
        return {"checked": False, "error_kind": "probe_failed"}
    try:
        line = next(item for item in reversed(output.splitlines()) if item.strip())
        payload = json.loads(line)
    except (StopIteration, TypeError, ValueError, json.JSONDecodeError):
        return {"checked": False, "error_kind": "invalid_probe_output"}
    if payload.get("checked") is not True or not isinstance(payload.get("is_login"), bool):
        return {"checked": False, "error_kind": "invalid_probe_output"}
    return {
        "checked": True,
        "is_login": payload["is_login"],
        "is_offline": bool(payload.get("is_offline")),
        "qrcode_available": bool(payload.get("qrcode_available")),
        "error_kind": str(payload.get("error_kind") or "none"),
    }


__all__ = ["probe_napcat_login"]
