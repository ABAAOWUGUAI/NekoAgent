#!/usr/bin/env python3
"""Peer-pinned, bounded HTTPS transport for authenticated QQ voice records."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile
from typing import Callable, Iterable, Iterator, Mapping
from urllib.parse import urljoin, urlsplit

from bridge_voice_message_source import (
    MAX_QQ_VOICE_BYTES,
    validate_qq_private_record_source,
)


CHUNK_BYTES = 64 * 1024
MAX_REDIRECTS = 2
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15


class VoiceMediaFetchError(ValueError):
    """Sanitized, stable failure from the controlled media boundary."""


@dataclass(frozen=True)
class FetchedVoiceMedia:
    path: Path
    size_bytes: int
    sha256: str
    detected_media_type: str
    transport_host_suffix: str
    redirect_count: int


def _public_addresses(host: str, resolver: Callable[..., list]) -> tuple[str, ...]:
    try:
        rows = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise VoiceMediaFetchError("qq_voice_dns_failed") from exc
    values: set[str] = set()
    for row in rows:
        raw = str(row[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise VoiceMediaFetchError("qq_voice_dns_address_invalid") from exc
        if not address.is_global:
            raise VoiceMediaFetchError("qq_voice_dns_address_not_public")
        values.add(address.compressed)
    if not values:
        raise VoiceMediaFetchError("qq_voice_dns_failed")
    return tuple(sorted(values, key=lambda item: (ipaddress.ip_address(item).version, item)))


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, *, connect_timeout: float, read_timeout: float) -> None:
        super().__init__(host, 443, timeout=connect_timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip
        self._read_timeout = read_timeout

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, 443), self.timeout)
        try:
            peer = ipaddress.ip_address(str(raw.getpeername()[0]).split("%", 1)[0])
            if not peer.is_global or peer != ipaddress.ip_address(self._pinned_ip):
                raise VoiceMediaFetchError("qq_voice_peer_ip_mismatch")
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(self._read_timeout)
        except Exception:
            raw.close()
            raise


def _media_type(prefix: bytes) -> str:
    if prefix.startswith(b"#!SILK_V3") or prefix.startswith(b"\x02#!SILK_V3"):
        return "audio/silk"
    if prefix.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return "audio/amr"
    if prefix.startswith(b"OggS"):
        return "audio/ogg"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    if prefix.startswith(b"ID3") or (len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    raise VoiceMediaFetchError("qq_voice_media_signature_unsupported")


def _connection(host: str, address: str, connect_timeout: float, read_timeout: float):
    return _PinnedHttpsConnection(
        host,
        address,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )


@contextmanager
def fetch_qq_voice_to_temp(
    source: Mapping[str, object],
    *,
    allowed_host_suffixes: Iterable[object],
    max_bytes: int = MAX_QQ_VOICE_BYTES,
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
    read_timeout_seconds: float = READ_TIMEOUT_SECONDS,
    max_redirects: int = MAX_REDIRECTS,
    temp_root: str | os.PathLike[str] | None = None,
    resolver: Callable[..., list] = socket.getaddrinfo,
    connection_factory: Callable[[str, str, float, float], object] = _connection,
) -> Iterator[FetchedVoiceMedia]:
    """Fetch once, yield a 0600 transient file, and always delete it."""

    suffixes = tuple(str(item or "").strip().lower().strip(".") for item in allowed_host_suffixes)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_QQ_VOICE_BYTES:
        raise VoiceMediaFetchError("qq_voice_max_bytes_policy_invalid")
    if not 1 <= float(connect_timeout_seconds) <= 30:
        raise VoiceMediaFetchError("qq_voice_connect_timeout_policy_invalid")
    if not 1 <= float(read_timeout_seconds) <= 60:
        raise VoiceMediaFetchError("qq_voice_read_timeout_policy_invalid")
    if not isinstance(max_redirects, int) or isinstance(max_redirects, bool) or not 0 <= max_redirects <= 5:
        raise VoiceMediaFetchError("qq_voice_redirect_policy_invalid")
    validated = validate_qq_private_record_source(source, allowed_host_suffixes=suffixes)
    declared_size = validated.get("declared_size_bytes")
    if declared_size is not None and int(declared_size) > max_bytes:
        raise VoiceMediaFetchError("qq_voice_media_too_large")
    current = str(validated["transport_url"])
    directory = Path(temp_root).resolve() if temp_root is not None else None
    if directory is not None and (not directory.is_dir() or directory.is_symlink()):
        raise VoiceMediaFetchError("qq_voice_temp_root_invalid")
    descriptor, name = tempfile.mkstemp(prefix="qq-voice-", suffix=".media", dir=str(directory) if directory else None)
    path = Path(name)
    os.chmod(path, 0o600)
    try:
        redirects = 0
        while True:
            parsed = urlsplit(current)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            # Re-run the complete source URL policy at every redirect.
            redirected = dict(source)
            redirected["transport_url"] = current
            validate_qq_private_record_source(redirected, allowed_host_suffixes=suffixes)
            address = _public_addresses(host, resolver)[0]
            conn = connection_factory(
                host,
                address,
                float(connect_timeout_seconds),
                float(read_timeout_seconds),
            )
            try:
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
                conn.request("GET", target, headers={"User-Agent": "NekoAgent-Voice/1", "Accept": "audio/*,application/octet-stream"})
                response = conn.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = str(response.getheader("Location") or "").strip()
                    if not location or redirects >= max_redirects:
                        raise VoiceMediaFetchError("qq_voice_redirect_forbidden")
                    current = urljoin(current, location)
                    redirects += 1
                    continue
                if response.status != 200:
                    raise VoiceMediaFetchError("qq_voice_upstream_http_error")
                length = response.getheader("Content-Length")
                if length:
                    try:
                        declared_length = int(length)
                    except ValueError as exc:
                        raise VoiceMediaFetchError("qq_voice_content_length_invalid") from exc
                    if declared_length <= 0 or declared_length > max_bytes:
                        raise VoiceMediaFetchError("qq_voice_media_too_large")
                digest = hashlib.sha256()
                size = 0
                prefix = b""
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    while True:
                        chunk = response.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise VoiceMediaFetchError("qq_voice_media_too_large")
                        if len(prefix) < 16:
                            prefix = (prefix + chunk)[:16]
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if not size:
                    raise VoiceMediaFetchError("qq_voice_media_empty")
                result = FetchedVoiceMedia(
                    path=path,
                    size_bytes=size,
                    sha256=digest.hexdigest(),
                    detected_media_type=_media_type(prefix),
                    transport_host_suffix=host,
                    redirect_count=redirects,
                )
                yield result
                return
            finally:
                conn.close()
    except (ssl.SSLError, socket.timeout, TimeoutError, OSError) as exc:
        raise VoiceMediaFetchError("qq_voice_transport_failed") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass


__all__ = ["FetchedVoiceMedia", "VoiceMediaFetchError", "fetch_qq_voice_to_temp"]
