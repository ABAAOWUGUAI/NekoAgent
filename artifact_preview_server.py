#!/usr/bin/env python3
"""Isolated, secret-free static Artifact preview HTTP service."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sys
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from bridge_artifact_broker import ArtifactBrokerClient
from bridge_artifact_repository import ArtifactError
from bridge_artifact_service import (
    INTERNAL_MANIFEST,
    attachment_response_headers,
    build_download_payload,
    normalize_relative_path,
)


SESSION_COOKIE = "artifact_preview_session"
IDENTIFIER = re.compile(r"^[a-zA-Z0-9-]{8,120}$")
TOKEN = re.compile(r"^[a-zA-Z0-9_-]{32,200}$")
MAX_ACTIVATION_BODY = 4096

COMMON_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "accelerometer=(), camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}
ACTIVATION_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
SHELL_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
def _content_csp(expected_host: str) -> str:
    # The iframe is deliberately opaque (no allow-same-origin). Explicitly naming
    # the validated preview host keeps local assets usable without granting access
    # to arbitrary network origins or weakening the sandbox.
    host = _host_name(expected_host)
    if not host:
        raise ArtifactError("artifact_preview_host_required")
    source = f"https://{host}"
    return (
        "sandbox allow-scripts; default-src 'none'; "
        f"script-src {source}; style-src {source} 'unsafe-inline'; "
        f"img-src {source} data:; font-src {source}; media-src {source} data:; "
        "connect-src 'none'; object-src 'none'; frame-src 'none'; child-src 'none'; "
        "worker-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self'; navigate-to 'none'"
    )


def _quoted_path(value: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in PurePosixPath(value).parts)


def _host_name(value: str) -> str:
    try:
        return str(urllib.parse.urlsplit("//" + str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


class ArtifactPreviewApplication:
    def __init__(self, broker, storage_root: Path, expected_host: str) -> None:
        self.broker = broker
        self.storage_root = Path(storage_root).resolve()
        self.published_root = (self.storage_root / "published").resolve()
        self.expected_host = _host_name(expected_host)
        if not self.expected_host:
            raise ArtifactError("artifact_preview_host_required")

    def validate_host(self, value: str) -> bool:
        return secrets.compare_digest(_host_name(value), self.expected_host)

    def read_authorized_file(self, authorization: dict) -> bytes:
        return self._read_verified_file(
            str(authorization.get("storage_key") or ""),
            authorization,
        )

    def _read_verified_file(self, storage_key: str, record: dict) -> bytes:
        storage_key = str(storage_key or "")
        if not re.fullmatch(r"[a-z2-7]{20,80}", storage_key):
            raise ArtifactError("artifact_storage_key_invalid")
        storage_name = normalize_relative_path(record.get("storage_name"))
        storage_root = self.published_root / storage_key
        if storage_root.is_symlink() or not storage_root.is_dir():
            raise ArtifactError("artifact_storage_invalid")
        root = storage_root.resolve()
        path = (root / Path(*PurePosixPath(storage_name).parts)).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ArtifactError("artifact_path_escape") from exc
        if path.is_symlink() or not path.is_file():
            raise ArtifactError("artifact_file_not_found")
        payload = path.read_bytes()
        expected_size = int(record.get("size_bytes") or -1)
        expected_hash = str(record.get("sha256") or "")
        if len(payload) != expected_size or not secrets.compare_digest(
            hashlib.sha256(payload).hexdigest(), expected_hash,
        ):
            raise ArtifactError("artifact_file_integrity_failed")
        return payload

    def download_payload(self, authorization: dict) -> tuple[bytes, str, str]:
        storage_key = str(authorization.get("storage_key") or "")
        version_id = str(authorization.get("version_id") or "")
        manifest_hash = str(authorization.get("manifest_sha256") or "")
        if (
            not re.fullmatch(r"[a-z2-7]{20,80}", storage_key)
            or not re.fullmatch(r"[a-zA-Z0-9-]{8,120}", version_id)
            or not re.fullmatch(r"[a-f0-9]{64}", manifest_hash)
        ):
            raise ArtifactError("artifact_delivery_not_available")
        storage_root = self.published_root / storage_key
        if storage_root.is_symlink() or not storage_root.is_dir():
            raise ArtifactError("artifact_delivery_not_available")
        root = storage_root.resolve()
        manifest_path = root / INTERNAL_MANIFEST
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ArtifactError("artifact_delivery_not_available")
        manifest_bytes = manifest_path.read_bytes()
        if not secrets.compare_digest(hashlib.sha256(manifest_bytes).hexdigest(), manifest_hash):
            raise ArtifactError("artifact_delivery_not_available")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactError("artifact_delivery_not_available") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("version_id") != version_id
            or manifest.get("storage_key") != storage_key
            or manifest.get("entrypoint_path") not in {"", None}
            or not isinstance(manifest.get("files"), list)
        ):
            raise ArtifactError("artifact_delivery_not_available")
        files: list[dict] = []
        relative_names: set[str] = set()
        storage_names: set[str] = set()
        for raw in manifest["files"]:
            if not isinstance(raw, dict):
                raise ArtifactError("artifact_delivery_not_available")
            relative = normalize_relative_path(raw.get("relative_path"))
            storage_name = normalize_relative_path(raw.get("storage_name"))
            if relative in relative_names or storage_name in storage_names:
                raise ArtifactError("artifact_delivery_not_available")
            relative_names.add(relative)
            storage_names.add(storage_name)
            files.append({**raw, "relative_path": relative, "storage_name": storage_name})
        return build_download_payload(
            version_id,
            files,
            lambda item: self._read_verified_file(storage_key, dict(item)),
        )


class ArtifactPreviewHandler(BaseHTTPRequestHandler):
    server_version = "ArtifactPreview/1"
    sys_version = ""

    @property
    def app(self) -> ArtifactPreviewApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, pattern: str, *args) -> None:
        # Never write capability tokens, session cookies or user file paths to logs.
        status = str(args[1]) if len(args) > 1 else "-"
        sys.stderr.write(f"artifact_preview method={self.command} status={status}\n")

    def _send(
        self,
        status: int,
        body: bytes = b"",
        *,
        media_type: str = "text/plain; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in COMMON_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _attachment(self, payload: bytes, media_type: str, filename: str, *, head_only: bool) -> None:
        self.send_response(200)
        for key, value in attachment_response_headers(payload, media_type, filename).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _html(self, status: int, title: str, content: str, *, csp: str, head_only: bool = False) -> None:
        page = (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>"
            "body{margin:0;background:#071625;color:#eaf5ff;font:16px/1.6 system-ui,sans-serif}"
            "main{max-width:42rem;margin:10vh auto;padding:2rem}"
            "button{min-height:44px;padding:.7rem 1rem;border:0;border-radius:.65rem;"
            "background:#45c4e8;color:#03111d;font-weight:700}"
            "iframe{position:fixed;inset:0;width:100%;height:100%;border:0;background:white}"
            ".note{color:#a9bdca}</style>"
            f"<body>{content}</body></html>"
        ).encode("utf-8")
        self._send(
            status, page, media_type="text/html; charset=utf-8", head_only=head_only,
            extra_headers={"Content-Security-Policy": csp, "Cross-Origin-Resource-Policy": "same-origin"},
        )

    def _path(self) -> str:
        try:
            return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path, errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ArtifactError("preview_path_invalid") from exc

    def _session(self) -> str:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return ""
        morsel = jar.get(SESSION_COOKIE)
        return str(morsel.value if morsel else "")

    def _require_host(self) -> bool:
        if self.app.validate_host(self.headers.get("Host", "")):
            return True
        self._send(421, "Misdirected Request".encode("utf-8"))
        return False

    def do_HEAD(self) -> None:
        if not self._require_host():
            return
        try:
            path = self._path()
            if path.startswith("/r/"):
                self._send(404, head_only=True)
                return
            if path.startswith("/activate/"):
                self._send(204, head_only=True)
                return
            self._serve_get(path, head_only=True)
        except (ArtifactError, FileNotFoundError):
            self._send(404, head_only=True)

    def do_GET(self) -> None:
        if not self._require_host():
            return
        try:
            self._serve_get(self._path(), head_only=False)
        except (ArtifactError, FileNotFoundError) as exc:
            self._error(exc)

    def _serve_get(self, path: str, *, head_only: bool) -> None:
        match = re.fullmatch(r"/r/([a-zA-Z0-9_-]{32,200})", path)
        if match:
            try:
                authorization = self.app.broker.request("redeem", token=match.group(1))
                payload, media_type, filename = self.app.download_payload(authorization)
            except (ArtifactError, FileNotFoundError, OSError, ValueError):
                self._delivery_error(head_only=head_only)
                return
            self._attachment(payload, media_type, filename, head_only=head_only)
            return
        if path.startswith("/r/"):
            self._delivery_error(head_only=head_only)
            return
        if path == "/health":
            try:
                health = self.app.broker.request("health")
            except Exception:
                health = {}
            valid = bool(
                health.get("ok") is True
                and health.get("service") == "artifact-authorization-broker"
                and health.get("security") == "linux_so_peercred"
            )
            body = json.dumps(
                {
                    "ok": valid,
                    "service": "artifact-preview",
                    "broker": health.get("service") if valid else "unavailable",
                    "security": health.get("security") if valid else "unverified",
                },
                separators=(",", ":"),
            ).encode()
            self._send(200 if valid else 503, body, media_type="application/json", head_only=head_only)
            return
        if path.startswith("/activate/"):
            token = path.removeprefix("/activate/")
            if not TOKEN.fullmatch(token):
                raise ArtifactError("preview_grant_not_found")
            challenge = self.app.broker.request("challenge", token=token)
            content = (
                "<main><h1>打开隔离预览</h1>"
                "<p>此成品将在独立来源中运行，不会获得管理端登录凭据。</p>"
                f"<form method=\"post\" action=\"/activate/{html.escape(token)}\">"
                f"<input type=\"hidden\" name=\"challenge\" value=\"{html.escape(str(challenge['challenge']))}\">"
                "<button type=\"submit\">继续打开</button></form>"
                "<p class=\"note\">链接仅可激活一次，并会自动过期。</p></main>"
            )
            self._html(200, "确认打开成品", content, csp=ACTIVATION_CSP, head_only=head_only)
            return
        match = re.fullmatch(r"/p/([a-zA-Z0-9-]{8,120})/?", path)
        if match:
            publication_id = match.group(1)
            authorization = self.app.broker.request(
                "authorize", session=self._session(), publication_id=publication_id, path="",
            )
            entrypoint = normalize_relative_path(authorization["entrypoint_path"])
            content = (
                f"<iframe title=\"成品预览\" sandbox=\"allow-scripts\" "
                f"src=\"/p/{publication_id}/content/{_quoted_path(entrypoint)}\"></iframe>"
            )
            self._html(200, "成品预览", content, csp=SHELL_CSP, head_only=head_only)
            return
        match = re.fullmatch(r"/p/([a-zA-Z0-9-]{8,120})/content/(.+)", path)
        if match:
            publication_id = match.group(1)
            relative = normalize_relative_path(match.group(2))
            authorization = self.app.broker.request(
                "authorize", session=self._session(), publication_id=publication_id, path=relative,
            )
            payload = self.app.read_authorized_file(authorization)
            media_type = str(authorization["media_type"])
            if media_type.startswith("text/html") and self.headers.get("Sec-Fetch-Dest", "").lower() != "iframe":
                raise ArtifactError("preview_html_iframe_required")
            self._send(
                200, payload, media_type=media_type, head_only=head_only,
                extra_headers={
                    "Content-Security-Policy": _content_csp(self.app.expected_host),
                    "Cross-Origin-Resource-Policy": "cross-origin",
                    "Cross-Origin-Opener-Policy": "same-origin",
                },
            )
            return
        raise ArtifactError("preview_not_found")

    def do_POST(self) -> None:
        if not self._require_host():
            return
        try:
            path = self._path()
            if not path.startswith("/activate/"):
                raise ArtifactError("preview_method_not_allowed")
            token = path.removeprefix("/activate/")
            if not TOKEN.fullmatch(token):
                raise ArtifactError("preview_grant_not_found")
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > MAX_ACTIVATION_BODY:
                raise ArtifactError("preview_activation_body_invalid")
            media = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            payload = self.rfile.read(length)
            if media == "application/x-www-form-urlencoded":
                challenge = urllib.parse.parse_qs(payload.decode("utf-8"), strict_parsing=True).get("challenge", [""])[0]
            elif media == "application/json":
                challenge = str(json.loads(payload.decode("utf-8")).get("challenge") or "")
            else:
                raise ArtifactError("preview_activation_media_invalid")
            activation = self.app.broker.request("activate", token=token, challenge=challenge)
            publication_id = str(activation["publication_id"])
            if not IDENTIFIER.fullmatch(publication_id):
                raise ArtifactError("preview_publication_invalid")
            self.send_response(303)
            self.send_header("Location", f"/p/{publication_id}/")
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={activation['session']}; Path=/p/{publication_id}/; "
                "Max-Age=3600; Secure; HttpOnly; SameSite=None",
            )
            for key, value in COMMON_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ArtifactError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self._error(exc)

    def _error(self, exc: Exception) -> None:
        code = str(exc)
        status = 401 if any(word in code for word in ("grant", "challenge", "session")) else 404
        self._html(
            status, "无法打开成品",
            "<main><h1>无法打开这个成品</h1><p>链接可能已过期、被撤销，或文件不再可用。</p></main>",
            csp=ACTIVATION_CSP,
        )

    def _delivery_error(self, *, head_only: bool) -> None:
        self._html(
            404, "无法下载成品",
            "<main><h1>无法下载这个成品</h1><p>链接可能已过期、被撤销，或文件不再可用。</p></main>",
            csp=ACTIVATION_CSP,
            head_only=head_only,
        )


class ArtifactPreviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ArtifactPreviewApplication) -> None:
        self.app = app
        super().__init__(address, ArtifactPreviewHandler)


def main() -> int:
    host = os.environ.get("ARTIFACT_PREVIEW_LISTEN", "127.0.0.1")
    port = int(os.environ.get("ARTIFACT_PREVIEW_PORT", "18778"))
    expected_host = os.environ.get("ARTIFACT_PREVIEW_HOST", "")
    storage = Path(os.environ.get("ARTIFACT_STORAGE_ROOT", "/var/lib/agent-artifacts"))
    socket_path = Path(os.environ.get("ARTIFACT_BROKER_SOCKET", "/run/agent-artifact/broker.sock"))
    app = ArtifactPreviewApplication(ArtifactBrokerClient(socket_path), storage, expected_host)
    server = ArtifactPreviewHTTPServer((host, port), app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
