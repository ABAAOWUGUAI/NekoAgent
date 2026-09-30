#!/usr/bin/env python3
"""Proxy-domain helpers with no dependency on the Bridge HTTP server.

The module deliberately keeps read-only inspection separate from mutations.
It never changes a Mihomo group selection and never persists subscription URLs.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from typing import Callable, Mapping
from urllib.parse import quote, urlencode, urljoin, urlparse

try:
    import yaml
except Exception:  # pragma: no cover - production dependency is checked by callers
    yaml = None


MAX_SUBSCRIPTION_BYTES = 4 * 1024 * 1024
MAX_SUBSCRIPTION_REDIRECTS = 5
PROXY_ENVIRONMENT_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_SAFE_SUBSCRIPTION_FETCH_ERRORS = frozenset({
    "invalid_subscription_url",
    "subscription_url_credentials_forbidden",
    "subscription_url_port_forbidden",
    "subscription_host_not_public",
    "subscription_host_resolution_failed",
    "subscription_proxy_environment_forbidden",
    "subscription_peer_mismatch",
    "subscription_payload_too_large",
    "subscription_download_failed",
    "subscription_source_tls_failed",
    "subscription_source_timeout",
    "subscription_source_connection_failed",
    "subscription_source_http_4xx",
    "subscription_source_http_5xx",
    "subscription_source_redirect_failed",
    "subscription_payload_invalid",
})
_SUBSCRIPTION_REDIRECT_ERRORS = frozenset({
    "subscription_redirect_location_missing",
    "subscription_redirect_limit_exceeded",
})
SUPPORTED_URI_SCHEMES = (
    "ss://",
    "ssr://",
    "vmess://",
    "vless://",
    "trojan://",
    "hysteria://",
    "hysteria2://",
    "tuic://",
    "socks5://",
)
SUBCONVERTER_IMAGE = os.environ.get(
    "SUBCONVERTER_IMAGE",
    "ghcr.io/metacubex/subconverter@sha256:573b57e0359b7a6ab9ab3ca4a9e83fbd07b7907b1ce6e67918485b066d5e8527",
)
SUBCONVERTER_PREF_PATH = Path(
    os.environ.get(
        "SUBCONVERTER_PREF_PATH",
        str(Path(__file__).with_name("deploy") / "proxy" / "subconverter-pref.ini"),
    )
)
SUBCONVERTER_TEMP_ROOT = Path("/run/agent-bridge")


def safe_subscription_key(name: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip())
    key = raw.strip("-_").lower()[:48]
    if key:
        return key
    digest = hashlib.sha256((name or "").strip().encode("utf-8")).hexdigest()[:12]
    return f"sub-{digest}" if (name or "").strip() else ""


def redact_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return "[invalid-url]"
    if not parsed.scheme or not parsed.netloc:
        return "[invalid-url]"
    return f"{parsed.scheme}://{parsed.hostname or '[host]'}/[redacted]"


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(address.is_global)


def _raw_port_syntax_valid(value: str) -> bool:
    _, separator, remainder = value.partition("://")
    if not separator:
        return False
    authority = re.split(r"[/?#]", remainder, maxsplit=1)[0]
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            return False
        suffix = authority[closing + 1 :]
        if not suffix:
            return True
        return bool(suffix.startswith(":") and re.fullmatch(r"[0-9]+", suffix[1:]))
    if ":" not in authority:
        return True
    return bool(re.fullmatch(r"[0-9]+", authority.rsplit(":", 1)[1]))


def validate_subscription_url(
    value: str,
    *,
    resolver: Callable[..., list] = socket.getaddrinfo,
) -> dict:
    """Validate a remote subscription before any fetch or persistence.

    DNS resolution is part of validation so loopback/private targets cannot be
    hidden behind a hostname. Callers must fail closed if resolution changes or
    the downloader follows a redirect to a destination that was not rechecked.
    """

    try:
        raw_value = (value or "").strip()
        parsed = urlparse(raw_value)
        host = parsed.hostname
        username = parsed.username
        password = parsed.password
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_subscription_url"}
    if parsed.scheme not in {"http", "https"} or not host:
        return {"ok": False, "error": "invalid_subscription_url"}
    if username is not None or password is not None:
        return {"ok": False, "error": "subscription_url_credentials_forbidden"}
    if not _raw_port_syntax_valid(raw_value):
        return {"ok": False, "error": "invalid_subscription_url"}
    try:
        explicit_port = parsed.port
    except ValueError:
        return {"ok": False, "error": "invalid_subscription_url"}
    port = explicit_port if explicit_port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        return {"ok": False, "error": "invalid_subscription_url"}
    try:
        resolved = resolver(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(str(item[4][0]).split("%", 1)[0] for item in resolved))
    except Exception:
        return {"ok": False, "error": "subscription_host_resolution_failed"}
    if not addresses or any(not _is_public_address(address) for address in addresses):
        return {"ok": False, "error": "subscription_host_not_public"}
    return {
        "ok": True,
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "resolved_addresses": addresses,
        "resolved_count": len(addresses),
    }


def safe_subscription_fetch_error(error: Exception) -> str:
    """Reduce a source-fetch exception to a stable, non-sensitive code."""

    if isinstance(error, ssl.SSLError):
        return "subscription_source_tls_failed"
    if isinstance(error, TimeoutError):
        return "subscription_source_timeout"
    if isinstance(error, (ConnectionError, OSError, http.client.HTTPException)):
        return "subscription_source_connection_failed"
    code = str(error).strip()
    if code in _SAFE_SUBSCRIPTION_FETCH_ERRORS:
        return code
    if code in _SUBSCRIPTION_REDIRECT_ERRORS:
        return "subscription_source_redirect_failed"
    if code == "subscription_content_length_invalid":
        return "subscription_payload_invalid"
    match = re.match(r"^HTTP Error ([0-9]{3})(?:\b|:)", code)
    if match:
        status = int(match.group(1))
        if 300 <= status < 400:
            return "subscription_source_redirect_failed"
        if 400 <= status < 500:
            return "subscription_source_http_4xx"
        if 500 <= status < 600:
            return "subscription_source_http_5xx"
    return "subscription_download_failed"


def _uri_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n")]
    return [line for line in lines if line.startswith(SUPPORTED_URI_SCHEMES)]


def _decode_base64_text(text: str) -> str:
    compact = "".join(text.split())
    if not compact or len(compact) % 4 == 1:
        return ""
    try:
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
        return decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def classify_subscription_payload(payload: bytes, content_type: str = "") -> dict:
    if not payload:
        return {"ok": False, "format": "empty", "error": "subscription_payload_empty"}
    if len(payload) > MAX_SUBSCRIPTION_BYTES:
        return {"ok": False, "format": "oversized", "error": "subscription_payload_too_large"}
    try:
        text = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return {"ok": False, "format": "binary", "error": "subscription_payload_not_utf8"}

    document = None
    if yaml is not None and ("yaml" in content_type.lower() or ":" in text):
        try:
            document = yaml.safe_load(text)
        except Exception:
            document = None
    if isinstance(document, dict) and isinstance(document.get("proxies"), list):
        clash_keys = {"proxy-groups", "rules", "rule-providers", "dns", "mixed-port", "mode"}
        payload_format = "clash_config" if clash_keys.intersection(document) else "provider_yaml"
        return {
            "ok": True,
            "format": payload_format,
            "node_count": len(document["proxies"]),
            "needs_converter": False,
        }

    uri_lines = _uri_lines(text)
    if uri_lines:
        return {
            "ok": True,
            "format": "uri_list",
            "node_count": len(uri_lines),
            "needs_converter": True,
        }
    decoded = _decode_base64_text(text)
    decoded_lines = _uri_lines(decoded)
    if decoded_lines:
        return {
            "ok": True,
            "format": "base64_uri_list",
            "node_count": len(decoded_lines),
            "needs_converter": True,
        }
    return {"ok": False, "format": "unknown", "error": "unsupported_subscription_format"}


def provider_yaml_from_payload(payload: bytes, content_type: str = "") -> tuple[dict, dict]:
    """Return a normalized Mihomo provider document for native YAML inputs."""

    classification = classify_subscription_payload(payload, content_type)
    if not classification.get("ok"):
        raise ValueError(str(classification.get("error") or "unsupported_subscription_format"))
    if classification.get("needs_converter"):
        raise ValueError("subscription_converter_required")
    if yaml is None:
        raise RuntimeError("pyyaml_missing")
    document = yaml.safe_load(payload.decode("utf-8-sig")) or {}
    proxies = document.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        raise ValueError("subscription_has_no_proxies")
    normalized = []
    for index, proxy in enumerate(proxies):
        if not isinstance(proxy, dict):
            raise ValueError(f"invalid_proxy_entry:{index}")
        if not str(proxy.get("name") or "").strip() or not str(proxy.get("type") or "").strip():
            raise ValueError(f"invalid_proxy_entry:{index}")
        normalized.append(dict(proxy))
    return {"proxies": normalized}, classification


def convert_uri_payload_with_subconverter(
    payload: bytes,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    image: str = SUBCONVERTER_IMAGE,
    pref_path: Path = SUBCONVERTER_PREF_PATH,
    temp_root: Path = SUBCONVERTER_TEMP_ROOT,
) -> bytes:
    classification = classify_subscription_payload(payload)
    if not classification.get("ok") or not classification.get("needs_converter"):
        raise ValueError("subscription_converter_input_invalid")
    if not pref_path.is_file():
        raise RuntimeError("subscription_converter_config_missing")
    try:
        temporary_directory_kwargs = {"prefix": "agent-subconverter-"}
        if os.name == "posix":
            temporary_directory_kwargs["dir"] = str(temp_root)
        temporary_directory = tempfile.TemporaryDirectory(**temporary_directory_kwargs)
    except OSError as exc:
        raise RuntimeError("subscription_converter_permission_failed") from exc
    with temporary_directory as directory_name:
        directory = Path(directory_name)
        source = directory / "subscription.txt"
        container_user = "65534:65534"
        try:
            source.write_bytes(payload)
            if os.name == "posix":
                effective_uid = int(os.geteuid())
                effective_gid = int(os.getegid())
                os.chmod(directory, 0o700)
                os.chmod(source, 0o400)
                if effective_uid == 0:
                    os.chown(source, 65534, 65534)
                    os.chown(directory, -1, 65534)
                    os.chmod(directory, 0o710)
                else:
                    container_gid = effective_gid if effective_gid != 0 else 65534
                    container_user = f"{effective_uid}:{container_gid}"
        except PermissionError as exc:
            raise RuntimeError("subscription_converter_permission_failed") from exc
        script = (
            "set -eu; "
            "subconverter >/tmp/subconverter.log 2>&1 & converter_pid=$!; "
            "trap 'kill $converter_pid 2>/dev/null || true' EXIT INT TERM; "
            "sleep 2; kill -0 $converter_pid 2>/dev/null; "
            "wget -qO /tmp/version http://127.0.0.1:25500/version; "
            "wget -qO- 'http://127.0.0.1:25500/sub?target=clash&url=fixture.txt&insert=false&emoji=false&list=false&new_name=true'"
        )
        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--log-driver",
            "none",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "128m",
            "--pids-limit",
            "64",
            "--cpus",
            "0.5",
            "--user",
            container_user,
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--mount",
            f"type=bind,src={source},dst=/base/fixture.txt,readonly",
            "--mount",
            f"type=bind,src={pref_path},dst=/base/pref.ini,readonly",
            image,
            "/bin/sh",
            "-lc",
            script,
        ]
        try:
            completed = runner(command, capture_output=True, timeout=45, check=False)
        except Exception as exc:
            raise RuntimeError("subscription_converter_execution_failed") from exc
        if completed.returncode != 0:
            raise RuntimeError("subscription_converter_failed")
        converted = bytes(completed.stdout or b"")
        if not converted or len(converted) > MAX_SUBSCRIPTION_BYTES:
            raise RuntimeError("subscription_converter_output_invalid")
        normalized, _ = provider_yaml_from_payload(converted, "application/yaml")
        return yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False).encode("utf-8")


def _verify_pinned_peer(sock: socket.socket, pinned_ip: str) -> None:
    try:
        peer_ip = str(sock.getpeername()[0]).split("%", 1)[0]
        peer = ipaddress.ip_address(peer_ip)
        pinned = ipaddress.ip_address(pinned_ip)
    except Exception as exc:
        sock.close()
        raise ValueError("subscription_peer_mismatch") from exc
    if not peer.is_global or peer != pinned:
        sock.close()
        raise ValueError("subscription_peer_mismatch")


def _connect_validated_address(
    addresses: list[str],
    port: int,
    timeout: float,
    source_address,
    socket_connector: Callable[..., socket.socket],
) -> socket.socket:
    last_error: OSError | None = None
    for pinned_ip in addresses:
        try:
            sock = socket_connector((pinned_ip, port), timeout, source_address)
        except OSError as exc:
            last_error = exc
            continue
        _verify_pinned_peer(sock, pinned_ip)
        return sock
    if last_error is not None:
        raise last_error
    raise OSError("subscription_connection_failed")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_addresses: list[str],
        timeout: float,
        socket_connector: Callable[..., socket.socket],
    ):
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_addresses = list(pinned_addresses)
        self._socket_connector = socket_connector

    def connect(self) -> None:
        self.sock = _connect_validated_address(
            self._pinned_addresses,
            self.port,
            self.timeout,
            self.source_address,
            self._socket_connector,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_addresses: list[str],
        timeout: float,
        socket_connector: Callable[..., socket.socket],
        context: ssl.SSLContext | None = None,
    ):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_addresses = list(pinned_addresses)
        self._socket_connector = socket_connector

    def connect(self) -> None:
        sock = _connect_validated_address(
            self._pinned_addresses,
            self.port,
            self.timeout,
            self.source_address,
            self._socket_connector,
        )
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _subscription_host_header(host: str, port: int, scheme: str, *, explicit_port: bool) -> str:
    value = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    return f"{value}:{port}" if explicit_port or port != default_port else value


def fetch_subscription_payload(
    url: str,
    timeout: int = 20,
    *,
    resolver: Callable[..., list] = socket.getaddrinfo,
    socket_connector: Callable[..., socket.socket] = socket.create_connection,
    environment: Mapping[str, str] | None = None,
    max_redirects: int = MAX_SUBSCRIPTION_REDIRECTS,
) -> tuple[bytes, str]:
    active_environment = os.environ if environment is None else environment
    if any(key in active_environment for key in PROXY_ENVIRONMENT_KEYS):
        raise ValueError("subscription_proxy_environment_forbidden")

    request_timeout = max(5, min(int(timeout), 30))
    redirect_limit = max(0, min(int(max_redirects), MAX_SUBSCRIPTION_REDIRECTS))
    current_url = (url or "").strip()
    redirect_count = 0
    while True:
        validation = validate_subscription_url(current_url, resolver=resolver)
        if not validation.get("ok"):
            if redirect_count > 0:
                raise ValueError("subscription_source_redirect_failed")
            raise ValueError(str(validation.get("error") or "invalid_subscription_url"))
        parsed = urlparse(current_url)
        host = str(validation["host"])
        port = int(validation["port"])
        pinned_addresses = [str(address) for address in validation["resolved_addresses"]]
        if validation["scheme"] == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host,
                port,
                pinned_addresses=pinned_addresses,
                timeout=request_timeout,
                socket_connector=socket_connector,
                context=ssl.create_default_context(),
            )
        else:
            connection = _PinnedHTTPConnection(
                host,
                port,
                pinned_addresses=pinned_addresses,
                timeout=request_timeout,
                socket_connector=socket_connector,
            )
        response = None
        try:
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "application/yaml,text/yaml,text/plain,*/*;q=0.8",
                    "User-Agent": "Agent-Control-Subscription/1.0",
                    "Host": _subscription_host_header(
                        host,
                        port,
                        str(validation["scheme"]),
                        explicit_port=parsed.port is not None,
                    ),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = str(response.getheader("Location") or "").strip()
                if not location:
                    raise ValueError("subscription_redirect_location_missing")
                if redirect_count >= redirect_limit:
                    raise ValueError("subscription_redirect_limit_exceeded")
                current_url = urljoin(current_url, location)
                redirect_count += 1
                continue
            if not 200 <= response.status < 300:
                raise ValueError(f"HTTP Error {response.status}: subscription_source_http_error")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise ValueError("subscription_content_length_invalid") from exc
                if declared_length < 0 or declared_length > MAX_SUBSCRIPTION_BYTES:
                    raise ValueError("subscription_payload_too_large")
            payload = response.read(MAX_SUBSCRIPTION_BYTES + 1)
            if len(payload) > MAX_SUBSCRIPTION_BYTES:
                raise ValueError("subscription_payload_too_large")
            return payload, str(response.getheader("Content-Type") or "")
        finally:
            if response is not None:
                response.close()
            connection.close()


MANAGED_FILE_MODE = 0o660


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


class ManagedSubscriptionStore:
    """Transactional native-YAML subscription installation for Mihomo."""

    def __init__(
        self,
        *,
        config_path: Path,
        state_path: Path,
        provider_dir: Path,
        backup_root: Path,
        config_test: Callable[[], tuple[bool, str]],
        reload_config: Callable[[], tuple[bool, str]],
        fetcher: Callable[[str], tuple[bytes, str]] = fetch_subscription_payload,
        converter: Callable[[bytes], bytes] = convert_uri_payload_with_subconverter,
    ):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.provider_dir = Path(provider_dir)
        self.backup_root = Path(backup_root)
        self.config_test = config_test
        self.reload_config = reload_config
        self.fetcher = fetcher
        self.converter = converter

    def _state(self) -> list[dict]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = value if isinstance(value, list) else value.get("subscriptions", [])
        return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _backup(self, key: str, provider_path: Path) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        destination = self.backup_root / f"proxy_subscription_{stamp}_{key}"
        suffix = 1
        while destination.exists():
            destination = self.backup_root / f"proxy_subscription_{stamp}_{key}_{suffix}"
            suffix += 1
        destination.mkdir(parents=True, mode=0o700)
        shutil.copy2(self.config_path, destination / "config.yaml")
        if self.state_path.is_file():
            shutil.copy2(self.state_path, destination / "codex-subscriptions.json")
        if provider_path.is_file():
            shutil.copy2(provider_path, destination / provider_path.name)
        return destination

    def _restore(self, backup: Path, provider_path: Path, provider_existed: bool) -> None:
        _atomic_write(
            self.config_path,
            (backup / "config.yaml").read_bytes(),
            mode=MANAGED_FILE_MODE,
        )
        state_backup = backup / "codex-subscriptions.json"
        if state_backup.is_file():
            _atomic_write(
                self.state_path,
                state_backup.read_bytes(),
                mode=MANAGED_FILE_MODE,
            )
        provider_backup = backup / provider_path.name
        if provider_existed and provider_backup.is_file():
            _atomic_write(
                provider_path,
                provider_backup.read_bytes(),
                mode=MANAGED_FILE_MODE,
            )
        elif not provider_existed:
            try:
                provider_path.unlink()
            except FileNotFoundError:
                pass
        self.reload_config()

    def upsert(
        self,
        name: str,
        url: str,
        *,
        key_override: str = "",
        payload_override: bytes | None = None,
        content_type_override: str = "",
    ) -> dict:
        started = time.monotonic()
        name = (name or "").strip()
        url = (url or "").strip()
        if not name:
            return {"ok": False, "error": "subscription_name_required"}
        key = safe_subscription_key(key_override) if key_override else safe_subscription_key(name)
        if not key:
            return {"ok": False, "error": "subscription_name_invalid"}
        validation = validate_subscription_url(url)
        if not validation.get("ok"):
            return {"ok": False, "error": validation.get("error", "invalid_subscription_url")}
        try:
            if payload_override is None:
                payload, content_type = self.fetcher(url)
            else:
                payload, content_type = payload_override, content_type_override
            classification = classify_subscription_payload(payload, content_type)
            if not classification.get("ok"):
                raise ValueError(str(classification.get("error") or "unsupported_subscription_format"))
            if classification.get("needs_converter"):
                payload = self.converter(payload)
                content_type = "application/yaml"
            provider_document, _ = provider_yaml_from_payload(payload, content_type)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc)[:160],
                "url": redact_url(url),
                "duration": round(time.monotonic() - started, 2),
            }
        provider_key = f"agent-{key}"
        provider_path = self.provider_dir / f"{provider_key}.yaml"
        provider_existed = provider_path.is_file()
        state_before = self._state()
        previous = next((item for item in state_before if item.get("key") == key), {})
        try:
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(config, dict):
                raise ValueError("mihomo_config_not_mapping")
        except Exception as exc:
            return {"ok": False, "error": f"load_config_failed:{str(exc)[:120]}"}
        backup = self._backup(key, provider_path)
        relative_path = f"./proxy-providers/{provider_path.name}"
        providers = config.setdefault("proxy-providers", {})
        previous_provider = str(previous.get("provider") or "")
        if previous_provider and previous_provider != provider_key:
            providers.pop(previous_provider, None)
        providers[provider_key] = {
            "type": "file",
            "path": relative_path,
            "health-check": {
                "enable": True,
                "url": "https://www.gstatic.com/generate_204",
                "interval": 600,
                "timeout": 5000,
                "lazy": True,
            },
        }
        group_name = name[:64]
        groups = config.setdefault("proxy-groups", [])
        if previous_provider:
            for item in groups:
                if isinstance(item, dict) and isinstance(item.get("use"), list):
                    item["use"] = [value for value in item["use"] if value != previous_provider]
        group = next((item for item in groups if isinstance(item, dict) and item.get("name") == group_name), None)
        if group is None:
            groups.append({"name": group_name, "type": "select", "use": [provider_key]})
        else:
            group["type"] = "select"
            group["use"] = [
                item
                for item in dict.fromkeys([provider_key, *(group.get("use") or [])])
                if item != previous_provider or item == provider_key
            ]
        primary = next((item for item in groups if isinstance(item, dict) and item.get("name") == "Proxies"), None)
        if primary is not None and primary.get("name") != group_name:
            primary["proxies"] = list(dict.fromkeys([group_name, *(primary.get("proxies") or [])]))
        try:
            provider_bytes = yaml.safe_dump(provider_document, allow_unicode=True, sort_keys=False).encode("utf-8")
            config_bytes = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
            _atomic_write(provider_path, provider_bytes, mode=MANAGED_FILE_MODE)
            _atomic_write(self.config_path, config_bytes, mode=MANAGED_FILE_MODE)
            tested, _ = self.config_test()
            if not tested:
                raise RuntimeError("mihomo_config_test_failed")
            reloaded, _ = self.reload_config()
            if not reloaded:
                raise RuntimeError("mihomo_reload_failed")
        except Exception as exc:
            self._restore(backup, provider_path, provider_existed)
            return {
                "ok": False,
                "error": str(exc)[:400],
                "rolled_back": True,
                "backup_id": backup.name,
            }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state = [item for item in state_before if item.get("key") != key]
        record = {
            "key": key,
            "name": name,
            "url": url,
            "provider": provider_key,
            "group": group_name,
            "format": classification["format"],
            "node_count": classification["node_count"],
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
            "last_status": "ready",
            "last_error": "",
        }
        state.append(record)
        _atomic_write(
            self.state_path,
            (json.dumps({"subscriptions": state}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            mode=MANAGED_FILE_MODE,
        )
        return {
            "ok": True,
            "duration": round(time.monotonic() - started, 2),
            "subscription": {**record, "url": redact_url(url)},
            "backup_id": backup.name,
        }

    def refresh(self, key: str) -> dict:
        key = safe_subscription_key(key)
        record = next((item for item in self._state() if item.get("key") == key), None)
        if record is None:
            return {"ok": False, "error": "subscription_not_found"}
        result = self.upsert(
            str(record.get("name") or key),
            str(record.get("url") or ""),
            key_override=key,
        )
        if not result.get("ok"):
            error = str(result.get("error") or "refresh_failed")
            match = re.search(r"HTTP Error (\d{3})", error)
            safe_error = f"source_http_{match.group(1)}" if match else error if re.fullmatch(r"[a-z0-9_:-]{1,80}", error) else "refresh_failed"
            state = self._state()
            for item in state:
                if item.get("key") == key:
                    item["last_status"] = "refresh_failed"
                    item["last_error"] = safe_error
                    item["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _atomic_write(
                self.state_path,
                (json.dumps({"subscriptions": state}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                mode=MANAGED_FILE_MODE,
            )
            result["error"] = safe_error
        return result

    def adopt_cached(self, key: str) -> dict:
        key = safe_subscription_key(key)
        record = next((item for item in self._state() if item.get("key") == key), None)
        if record is None:
            return {"ok": False, "error": "subscription_not_found"}
        try:
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            provider = (config.get("proxy-providers") or {}).get(record.get("provider")) or {}
            raw_path = str(provider.get("path") or "")
            if not raw_path.startswith("./proxy-providers/"):
                raise ValueError("managed_provider_cache_not_found")
            cache_path = (self.provider_dir / Path(raw_path).name).resolve()
            if cache_path.parent != self.provider_dir.resolve() or not cache_path.is_file():
                raise ValueError("managed_provider_cache_not_found")
            payload = cache_path.read_bytes()
            if len(payload) > MAX_SUBSCRIPTION_BYTES:
                raise ValueError("subscription_payload_too_large")
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:80]}
        prior_error = str(record.get("last_error") or "source_unavailable")
        result = self.upsert(
            str(record.get("name") or key),
            str(record.get("url") or ""),
            key_override=key,
            payload_override=payload,
            content_type_override="application/yaml",
        )
        if result.get("ok"):
            state = self._state()
            for item in state:
                if item.get("key") == key:
                    item["last_status"] = "ready_cached"
                    item["last_error"] = prior_error
            _atomic_write(
                self.state_path,
                (json.dumps({"subscriptions": state}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                mode=MANAGED_FILE_MODE,
            )
            result["subscription"]["last_status"] = "ready_cached"
            result["subscription"]["last_error"] = prior_error
        return result

    def delete(self, key: str) -> dict:
        started = time.monotonic()
        key = safe_subscription_key(key)
        state = self._state()
        record = next((item for item in state if item.get("key") == key), None)
        if record is None:
            return {"ok": False, "error": "subscription_not_found"}
        provider_key = str(record.get("provider") or "")
        group_name = str(record.get("group") or "")
        try:
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            providers = config.setdefault("proxy-providers", {})
            provider = providers.get(provider_key) if isinstance(providers, dict) else None
            raw_path = str((provider or {}).get("path") or "")
            file_name = Path(raw_path).name if raw_path.startswith("./proxy-providers/") else f"{provider_key}.yaml"
            provider_path = self.provider_dir / file_name
            provider_existed = provider_path.is_file()
            backup = self._backup(key, provider_path)
            providers.pop(provider_key, None)
            groups = config.setdefault("proxy-groups", [])
            config["proxy-groups"] = [
                item
                for item in groups
                if not (isinstance(item, dict) and item.get("name") == group_name)
            ]
            for item in config["proxy-groups"]:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("proxies"), list):
                    item["proxies"] = [value for value in item["proxies"] if value != group_name]
                if isinstance(item.get("use"), list):
                    item["use"] = [value for value in item["use"] if value != provider_key]
            _atomic_write(
                self.config_path,
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"),
                mode=MANAGED_FILE_MODE,
            )
            tested, _ = self.config_test()
            if not tested:
                raise RuntimeError("mihomo_config_test_failed")
            reloaded, _ = self.reload_config()
            if not reloaded:
                raise RuntimeError("mihomo_reload_failed")
            remaining = [item for item in state if item.get("key") != key]
            _atomic_write(
                self.state_path,
                (json.dumps({"subscriptions": remaining}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                mode=MANAGED_FILE_MODE,
            )
            if provider_path.is_file():
                provider_path.unlink()
        except Exception as exc:
            if "backup" in locals():
                self._restore(backup, provider_path, provider_existed)
            return {
                "ok": False,
                "error": str(exc)[:400],
                "rolled_back": "backup" in locals(),
                "backup_id": backup.name if "backup" in locals() else "",
            }
        return {
            "ok": True,
            "deleted": key,
            "duration": round(time.monotonic() - started, 2),
            "backup_id": backup.name,
        }


def concurrent_node_delays(
    api: Callable[..., tuple[int, dict]],
    names: list[str],
    *,
    test_url: str,
    timeout_ms: int,
    max_workers: int = 8,
) -> list[dict]:
    """Measure nodes through Mihomo's per-node delay endpoint without switching groups."""

    timeout_ms = max(1000, min(int(timeout_ms or 6000), 15000))
    unique_names = list(dict.fromkeys(str(name or "").strip() for name in names if str(name or "").strip()))[:80]

    def measure(name: str) -> dict:
        started = time.monotonic()
        query = urlencode({"timeout": timeout_ms, "url": test_url})
        try:
            _, response = api(
                f"/proxies/{quote(name, safe='')}/delay?{query}",
                timeout=max(4, int(timeout_ms / 1000) + 4),
            )
            delay = response.get("delay")
            ok = isinstance(delay, (int, float)) and delay >= 0
            return {
                "name": name,
                "ok": ok,
                "delay": delay if ok else None,
                "error": "" if ok else "delay_not_reported",
                "duration": round(time.monotonic() - started, 3),
            }
        except Exception as exc:
            return {
                "name": name,
                "ok": False,
                "delay": None,
                "error": str(exc)[:200],
                "duration": round(time.monotonic() - started, 3),
            }

    if not unique_names:
        return []
    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique_names))) as executor:
        futures = {executor.submit(measure, name): name for name in unique_names}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [results[name] for name in unique_names]


def read_only_diagnostics(
    *,
    group: str,
    proxy_url: str,
    current: str,
    candidates: list[str],
    delay_results: list[dict],
    duration: float,
) -> dict:
    usable = [item for item in delay_results if item.get("ok")]
    usable.sort(key=lambda item: (item.get("delay") is None, item.get("delay") or 999999))
    first_usable = str((usable[0] if usable else {}).get("name") or "")
    recommendation = (
        f"延迟探测发现可用候选节点：{first_usable}。只读诊断未切换当前节点；如需应用，请显式执行检测并切换。"
        if first_usable
        else "只读延迟探测没有发现可用候选节点；当前节点未改变。"
    )
    return {
        "ok": True,
        "duration": round(duration, 2),
        "group": group,
        "proxy_url": proxy_url,
        "current_before": current,
        "current_after": current,
        "auto_switch": False,
        "switched": False,
        "usable": bool(first_usable),
        "usable_node": first_usable,
        "tested": len(delay_results),
        "candidate_count": len(candidates),
        "restore_error": "",
        "switch_results": {},
        "targets": [{"name": "mihomo_delay", "label": "Mihomo per-node delay", "required": True}],
        "results": [
            {
                "name": item.get("name", ""),
                "ok": bool(item.get("ok")),
                "delay": item.get("delay"),
                "error": item.get("error", ""),
                "tests": [],
            }
            for item in delay_results
        ],
        "recommendation": recommendation,
    }
