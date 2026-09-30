#!/usr/bin/env python3
"""C1.1 loopback-only public Admin security gateway.

The browser authenticates only to this process.  The fixed loopback Bridge
upstream receives a separate service principal and independently re-applies
the canonical route policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
import gzip
import hashlib
import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from admin_console import ADMIN_ASSET_VERSION, ADMIN_HTML, admin_asset
from admin_gateway_security import (
    GatewayStateStore,
    PasswordHashStore,
    PasswordVerifier,
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    session_cookie,
)
from bridge_auth import read_secret
from bridge_public_admin_policy import PolicyError, PolicyMatch, match_public_admin_request


GATEWAY_ADMIN_HTML = ADMIN_HTML.replace(
    "</head>", '<meta name="admin-auth-mode" content="gateway"></head>', 1,
).replace(
    '<label for="v4Token">访问凭证</label>', '<label for="v4Token">公共管理密码</label>', 1,
)
DEFAULT_APPEARANCE = {
    "theme": "dark",
    "density": "comfortable",
    "sample_background_enabled": True,
    "sample_background_url": "/admin/assets/sample-background.jpg",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Strict-Transport-Security": "max-age=0; includeSubDomains",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_REQUEST_HEADER_ALLOWLIST = frozenset({"accept", "content-type", "idempotency-key", "if-none-match", "x-request-id"})
_RESPONSE_HEADER_ALLOWLIST = frozenset({"content-type", "content-disposition", "etag", "cache-control", "x-request-id"})
_REQUEST_ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}")
_PROXY_ENVIRONMENT = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


class GatewayConfigurationError(RuntimeError):
    pass


def _static_encoding(header: str) -> str | None:
    qualities: dict[str, float] = {}
    for member in header.split(","):
        parts = [part.strip().lower() for part in member.split(";")]
        name = parts[0]
        if not name:
            continue
        quality = 1.0
        if len(parts) > 1:
            if len(parts) != 2 or not re.fullmatch(r"q=(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", parts[1]):
                quality = 0.0
            else:
                quality = float(parts[1][2:])
        qualities[name] = min(qualities.get(name, quality), quality)
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))
    identity_quality = qualities.get("identity", 0.0 if qualities.get("*") == 0.0 else 1.0)
    if gzip_quality > 0 and gzip_quality >= identity_quality:
        return "gzip"
    return "identity" if identity_quality > 0 else None


@dataclass(frozen=True)
class GatewayConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 18779
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 18777
    expected_origin: str = ""
    password_hash_path: Path = Path("/run/credentials/agent-admin-gateway.service/admin-password")
    gateway_token_path: Path = Path("/run/credentials/agent-admin-gateway.service/bridge-token")
    state_db_path: Path = Path("/var/lib/agent-admin-gateway/state.sqlite3")
    background_path: Path = Path("/opt/agent-stack/codex-qq-bridge/assets/sample-background.jpg")
    session_idle_seconds: int = 1800
    session_absolute_seconds: int = 43200
    login_max_failures: int = 8
    login_window_seconds: int = 300
    maximum_concurrent_scrypt: int = 2
    upstream_timeout_seconds: float = 20.0
    test_mode: bool = False

    def validate(self) -> None:
        try:
            listen_ip = ipaddress.ip_address(self.listen_host)
            bridge_ip = ipaddress.ip_address(self.bridge_host)
        except ValueError as exc:
            raise GatewayConfigurationError("numeric_loopback_required") from exc
        if not listen_ip.is_loopback or not bridge_ip.is_loopback:
            raise GatewayConfigurationError("loopback_required")
        if not self.test_mode and (
            self.listen_host != "127.0.0.1" or self.listen_port != 18779
            or self.bridge_host != "127.0.0.1" or self.bridge_port != 18777
        ):
            raise GatewayConfigurationError("fixed_endpoint_required")
        parsed = urlsplit(self.expected_origin)
        test_http_loopback = False
        if self.test_mode and parsed.scheme == "http" and parsed.hostname:
            try:
                test_http_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                test_http_loopback = parsed.hostname == "localhost"
        if (
            parsed.scheme != "https" and not test_http_loopback
            or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username
        ):
            raise GatewayConfigurationError("expected_origin_invalid")
        if not 1 <= int(self.session_idle_seconds) <= 3600:
            raise GatewayConfigurationError("session_idle_invalid")
        if not 1 <= int(self.session_absolute_seconds) <= 86400:
            raise GatewayConfigurationError("session_absolute_invalid")

    @property
    def expected_host(self) -> str:
        return urlsplit(self.expected_origin).netloc


class GatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, config: GatewayConfig):
        config.validate()
        token = read_secret(config.gateway_token_path)
        if len(token) < 32 or len(token) > 512 or any(ord(character) < 33 or ord(character) > 126 for character in token):
            raise GatewayConfigurationError("gateway_service_token_invalid")
        self.config = config
        self.gateway_token = token
        self.state_store = GatewayStateStore(
            config.state_db_path,
            login_max_failures=config.login_max_failures,
            login_window_seconds=config.login_window_seconds,
        )
        self.password_store = PasswordHashStore(config.password_hash_path)
        self.password_verifier = PasswordVerifier(config.maximum_concurrent_scrypt)
        super().__init__((config.listen_host, config.listen_port), GatewayHandler)

    def handle_error(self, _request, _client_address) -> None:
        print("public Admin Gateway request failed", file=sys.stderr)


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15
    server: GatewayServer

    def log_message(self, _format: str, *_args) -> None:
        # Do not let public request targets or headers enter application logs.
        return

    def _headers(self, *, content_type: str, length: int, cache_control: str, extra: dict[str, str] | None = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _bytes(
        self, status: int, payload: bytes, content_type: str,
        *, cache_control: str = "no-store", extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._headers(content_type=content_type, length=len(payload), cache_control=cache_control, extra=extra)
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, payload: dict, *, extra: dict[str, str] | None = None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._bytes(status, encoded, "application/json; charset=utf-8", extra=extra)

    def _public_static_bytes(self, payload: bytes, content_type: str, *, cache_control: str = "no-store") -> None:
        encoding = _static_encoding(",".join(self.headers.get_all("Accept-Encoding", [])))
        headers = {"Vary": "Accept-Encoding"}
        if encoding is None:
            self._bytes(406, b"", "text/plain; charset=utf-8", extra=headers)
            return
        if encoding == "gzip":
            payload = gzip.compress(payload, compresslevel=6, mtime=0)
            headers["Content-Encoding"] = "gzip"
        headers["ETag"] = '"' + hashlib.sha256(payload).hexdigest() + '"'
        self._bytes(200, payload, content_type, cache_control=cache_control, extra=headers)

    def _host_valid(self) -> bool:
        values = self.headers.get_all("Host", [])
        return len(values) == 1 and values[0].strip().lower() == self.server.config.expected_host.lower()

    def _policy(self) -> PolicyMatch | None:
        if not self._host_valid():
            self._json(400, {"ok": False, "error": "host_rejected"})
            return None
        try:
            return match_public_admin_request(self.command, self.path, browser=True)
        except PolicyError as exc:
            error = str(exc)
            status = 404 if error == "public_admin_route_denied" else 400
            self._json(status, {"ok": False, "error": "request_rejected"})
            return None

    def _session_token(self) -> str:
        cookie_values = self.headers.get_all("Cookie", [])
        if len(cookie_values) != 1:
            return ""
        pieces = [piece.partition("=")[0].strip() for piece in cookie_values[0].split(";")]
        if pieces.count(SESSION_COOKIE_NAME) != 1:
            return ""
        try:
            parsed = SimpleCookie()
            parsed.load(cookie_values[0])
            morsel = parsed.get(SESSION_COOKIE_NAME)
            return morsel.value if morsel is not None else ""
        except CookieError:
            return ""

    def _authenticated_session(self, *, touch: bool = True) -> str:
        token = self._session_token()
        return token if self.server.state_store.validate_session(token, touch=touch) is not None else ""

    def _origin_valid(self) -> bool:
        values = self.headers.get_all("Origin", [])
        return len(values) == 1 and values[0] == self.server.config.expected_origin

    def _read_json(self, limit: int) -> dict | None:
        if not self._origin_valid():
            self._json(403, {"ok": False, "error": "origin_rejected"})
            return None
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"ok": False, "error": "json_required"})
            return None
        if self.headers.get("Transfer-Encoding") is not None:
            self._json(400, {"ok": False, "error": "transfer_encoding_rejected"})
            return None
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if len(lengths) == 1 else -1
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self._json(413 if length > limit else 400, {"ok": False, "error": "body_length_rejected"})
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "json_invalid"})
            return None
        if not isinstance(value, dict):
            self._json(400, {"ok": False, "error": "json_object_required"})
            return None
        return value

    def _csrf_valid(self, session: str) -> bool:
        values = self.headers.get_all("X-CSRF-Token", [])
        return len(values) == 1 and self.server.state_store.validate_csrf(session, values[0])

    def _proxy(self, match: PolicyMatch, body: bytes | None = None) -> None:
        headers = {
            name: value for name, value in self.headers.items()
            if name.lower() in _REQUEST_HEADER_ALLOWLIST
        }
        headers["Host"] = f"{self.server.config.bridge_host}:{self.server.config.bridge_port}"
        headers["X-Admin-Gateway-Token"] = self.server.gateway_token
        headers["Connection"] = "close"
        request_id = str(self.headers.get("X-Request-ID") or "").strip()
        for name in tuple(headers):
            if name.lower() == "x-request-id":
                headers.pop(name, None)
        if _REQUEST_ID_PATTERN.fullmatch(request_id):
            headers["X-Request-ID"] = request_id
        else:
            request_id = ""
        if body is not None:
            headers["Content-Length"] = str(len(body))
            headers["Content-Type"] = "application/json"
        started = time.monotonic()
        connection = http.client.HTTPConnection(
            self.server.config.bridge_host,
            self.server.config.bridge_port,
            timeout=135.0 if match.canonical_path == "/proxy/consumers/aiclient2api/test" else self.server.config.upstream_timeout_seconds,
        )
        try:
            connection.request(self.command, match.canonical_target, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise OSError("upstream_response_too_large")
            response_headers = {
                name: value for name, value in response.getheaders()
                if name.lower() in _RESPONSE_HEADER_ALLOWLIST and "\r" not in value and "\n" not in value
            }
            if request_id:
                response_headers["X-Request-ID"] = request_id
            content_type = response_headers.pop("Content-Type", "application/json; charset=utf-8")
            cache_control = response_headers.pop("Cache-Control", "no-store")
            self._bytes(response.status, payload, content_type, cache_control=cache_control, extra=response_headers)
            self.server.state_store.audit(
                "proxy", "success", f"{match.rule.method} {match.rule.template}",
                {"status": response.status, "duration_ms": int((time.monotonic() - started) * 1000), "request_id": request_id},
            )
        except (OSError, http.client.HTTPException):
            self.server.state_store.audit("proxy", "unavailable", f"{match.rule.method} {match.rule.template}")
            self._json(502, {"ok": False, "error": "admin_upstream_unavailable", "request_id": request_id})
        finally:
            connection.close()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        match = self._policy()
        if match is None:
            return
        path = match.canonical_path
        if path == "/":
            self.send_response(302)
            self._headers(content_type="text/plain; charset=utf-8", length=0, cache_control="no-store", extra={"Location": "/admin"})
            return
        if path in {"/admin", "/admin/"}:
            self._public_static_bytes(GATEWAY_ADMIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith("/admin/static/"):
            asset = admin_asset(path.rsplit("/", 1)[-1])
            if asset is None:
                self._json(404, {"ok": False, "error": "asset_not_found"})
                return
            payload, content_type, etag = asset
            self._public_static_bytes(payload, content_type, cache_control="public, max-age=0, immutable")
            return
        if path == "/admin/assets/sample-background.jpg":
            if not self._authenticated_session():
                self._json(401, {"ok": False, "error": "authentication_required"})
                return
            try:
                payload = self.server.config.background_path.read_bytes()
            except OSError:
                self._json(404, {"ok": False, "error": "background_not_found"})
                return
            self._bytes(200, payload, "image/jpeg", cache_control="public, max-age=86400")
            return
        if path == "/admin/bootstrap":
            session = self._authenticated_session()
            if not session:
                self._json(200, {"ok": True, "authenticated": False, "appearance": DEFAULT_APPEARANCE})
                return
            csrf = self.server.state_store.csrf_token(session, csrf_key=self.server.gateway_token)
            appearance = self._bridge_appearance()
            self._json(200, {
                "ok": True, "authenticated": True, "csrf_token": csrf,
                "appearance": appearance, "version": ADMIN_ASSET_VERSION,
            })
            return
        session = self._authenticated_session()
        if not session:
            self._json(401, {"ok": False, "error": "authentication_required"})
            return
        self._proxy(match)

    def _bridge_appearance(self) -> dict:
        connection = http.client.HTTPConnection(
            self.server.config.bridge_host, self.server.config.bridge_port,
            timeout=min(3.0, self.server.config.upstream_timeout_seconds),
        )
        try:
            connection.request("GET", "/admin/appearance", headers={
                "Host": f"{self.server.config.bridge_host}:{self.server.config.bridge_port}",
                "X-Admin-Gateway-Token": self.server.gateway_token,
                "Connection": "close",
            })
            response = connection.getresponse()
            payload = json.loads(response.read(65537).decode("utf-8"))
            appearance = payload.get("appearance") if response.status == 200 and isinstance(payload, dict) else None
            if isinstance(appearance, dict):
                return appearance
        except (OSError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        return dict(DEFAULT_APPEARANCE)

    def do_POST(self) -> None:
        match = self._policy()
        if match is None:
            return
        payload = self._read_json(match.rule.body_limit)
        if payload is None:
            return
        path = match.canonical_path
        if path == "/admin/login":
            self._login(payload)
            return
        session = self._authenticated_session()
        if not session:
            self._json(401, {"ok": False, "error": "authentication_required"})
            return
        if not self._csrf_valid(session):
            self.server.state_store.audit("csrf", "denied", f"{self.command} {match.rule.template}")
            self._json(403, {"ok": False, "error": "csrf_rejected"})
            return
        if path == "/admin/logout":
            self.server.state_store.revoke_session(session)
            self.server.state_store.audit("logout", "success", "POST /admin/logout")
            self._json(200, {"ok": True}, extra={"Set-Cookie": clear_session_cookie()})
            return
        supplied_build = str(self.headers.get("X-Admin-Build") or "").strip()
        if supplied_build != ADMIN_ASSET_VERSION:
            request_id = str(self.headers.get("X-Request-ID") or "").strip()
            self.server.state_store.audit(
                "console_build",
                "denied",
                f"{self.command} {match.rule.template}",
                {"request_id": request_id if _REQUEST_ID_PATTERN.fullmatch(request_id) else ""},
            )
            self._json(409, {
                "ok": False,
                "error": "console_update_required",
                "version": ADMIN_ASSET_VERSION,
            })
            return
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._proxy(match, encoded)

    do_PATCH = do_POST

    def _login(self, payload: dict) -> None:
        retry_after = self.server.state_store.login_retry_after()
        if retry_after:
            self.server.state_store.audit("login", "rate_limited", "POST /admin/login", {"retry_after": retry_after})
            self._json(429, {"ok": False, "error": "too_many_login_attempts"}, extra={"Retry-After": str(retry_after)})
            return
        if str(payload.get("build") or "") != ADMIN_ASSET_VERSION:
            self._json(409, {"ok": False, "error": "console_update_required", "version": ADMIN_ASSET_VERSION})
            return
        password = payload.get("password")
        if not isinstance(password, str):
            self.server.state_store.record_login_failure()
            self._json(401, {"ok": False, "error": "invalid_credentials"})
            return
        verified = self.server.password_verifier.verify(self.server.password_store, password)
        if verified is None:
            self.server.state_store.audit("login", "busy", "POST /admin/login")
            self._json(429, {"ok": False, "error": "login_capacity_busy"}, extra={"Retry-After": "1"})
            return
        if not verified:
            self.server.state_store.record_login_failure()
            self.server.state_store.audit("login", "denied", "POST /admin/login")
            self._json(401, {"ok": False, "error": "invalid_credentials"})
            return
        self.server.state_store.clear_login_failures()
        session, csrf = self.server.state_store.create_session(
            idle_seconds=self.server.config.session_idle_seconds,
            absolute_seconds=self.server.config.session_absolute_seconds,
            csrf_key=self.server.gateway_token,
        )
        self.server.state_store.audit("login", "success", "POST /admin/login")
        self._json(
            200,
            {"ok": True, "authenticated": True, "csrf_token": csrf, "version": ADMIN_ASSET_VERSION},
            extra={"Set-Cookie": session_cookie(session, max_age=self.server.config.session_absolute_seconds)},
        )

    def _method_not_allowed(self) -> None:
        self._json(405, {"ok": False, "error": "method_not_allowed"}, extra={"Allow": "GET, HEAD, POST, PATCH"})

    do_OPTIONS = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_CONNECT = _method_not_allowed
    do_TRACE = _method_not_allowed


def create_server(config: GatewayConfig) -> GatewayServer:
    return GatewayServer(config)


def _config_from_environment() -> GatewayConfig:
    credential_directory = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/run/credentials/agent-admin-gateway.service"))
    return GatewayConfig(
        listen_host=os.environ.get("LISTEN_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("LISTEN_PORT", "18779")),
        bridge_host=os.environ.get("BRIDGE_HOST", "127.0.0.1"),
        bridge_port=int(os.environ.get("BRIDGE_PORT", "18777")),
        expected_origin=os.environ.get("EXPECTED_ORIGIN", ""),
        password_hash_path=Path(os.environ.get("ADMIN_PASSWORD_HASH_PATH", str(credential_directory / "admin-password"))),
        gateway_token_path=Path(os.environ.get("ADMIN_GATEWAY_TOKEN_PATH", str(credential_directory / "bridge-token"))),
        state_db_path=Path(os.environ.get("GATEWAY_STATE_DB", "/var/lib/agent-admin-gateway/state.sqlite3")),
        background_path=Path(os.environ.get(
            "SAMPLE_BACKGROUND_ASSET_PATH", "/opt/agent-stack/codex-qq-bridge/assets/sample-background.jpg",
        )),
    )


def _clear_proxy_environment() -> None:
    for name in _PROXY_ENVIRONMENT:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def main() -> int:
    _clear_proxy_environment()
    try:
        server = create_server(_config_from_environment())
    except (GatewayConfigurationError, OSError, ValueError):
        print("public Admin Gateway configuration invalid", file=sys.stderr)
        return 2
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
