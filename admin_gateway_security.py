#!/usr/bin/env python3
"""Password, session, CSRF, limiter, and audit primitives for C1.1."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time


MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MIN_SCRYPT_N = 2**17
DEFAULT_SCRYPT_N = 2**17
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
SCRYPT_DKLEN = 32
SESSION_COOKIE_NAME = "__Host-neko_admin_session"


class SecurityStateError(RuntimeError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError("password_length_invalid")
    if "\x00" in password:
        raise ValueError("password_character_invalid")


def hash_password(
    password: str,
    *,
    n: int = DEFAULT_SCRYPT_N,
    r: int = DEFAULT_SCRYPT_R,
    p: int = DEFAULT_SCRYPT_P,
    salt: bytes | None = None,
) -> str:
    _validate_password(password)
    if n < MIN_SCRYPT_N or n & (n - 1) or r < 8 or p < 1:
        raise ValueError("scrypt_parameters_invalid")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=SCRYPT_DKLEN, maxmem=max(256 * 1024 * 1024, 2 * n * r * 128),
    )
    return f"$scrypt$v=1$n={n},r={r},p={p}${_b64encode(salt)}${_b64encode(derived)}"


def _parse_hash(serialized: str) -> tuple[int, int, int, bytes, bytes]:
    parts = serialized.strip().split("$")
    if len(parts) != 6 or parts[:3] != ["", "scrypt", "v=1"]:
        raise ValueError("password_hash_format_invalid")
    parameters: dict[str, int] = {}
    for item in parts[3].split(","):
        name, separator, value = item.partition("=")
        if not separator or name in parameters or name not in {"n", "r", "p"}:
            raise ValueError("password_hash_parameters_invalid")
        parameters[name] = int(value)
    if set(parameters) != {"n", "r", "p"}:
        raise ValueError("password_hash_parameters_invalid")
    n, r, p = parameters["n"], parameters["r"], parameters["p"]
    if n < MIN_SCRYPT_N or n & (n - 1) or n > 2**20 or r < 8 or r > 32 or p < 1 or p > 16:
        raise ValueError("password_hash_parameters_invalid")
    salt = _b64decode(parts[4])
    expected = _b64decode(parts[5])
    if not 16 <= len(salt) <= 64 or len(expected) != SCRYPT_DKLEN:
        raise ValueError("password_hash_value_invalid")
    return n, r, p, salt, expected


class PasswordHashStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def set_password(self, password: str, *, n: int = DEFAULT_SCRYPT_N) -> None:
        serialized = hash_password(password, n=n) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise SecurityStateError("password_hash_symlink_rejected")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=str(self.path.parent), text=False,
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(serialized.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def verify(self, password: str) -> bool:
        try:
            serialized = self.path.read_text(encoding="utf-8")
            if len(serialized) > 4096:
                return False
            n, r, p, salt, expected = _parse_hash(serialized)
            candidate = hashlib.scrypt(
                str(password).encode("utf-8"), salt=salt, n=n, r=r, p=p,
                dklen=len(expected), maxmem=max(256 * 1024 * 1024, 2 * n * r * 128),
            )
        except (OSError, ValueError, TypeError):
            return False
        return hmac.compare_digest(candidate, expected)


class PasswordVerifier:
    """Bound expensive scrypt work without queuing unbounded requests."""

    def __init__(self, maximum_concurrent: int = 2):
        if maximum_concurrent < 1:
            raise ValueError("maximum_concurrent_invalid")
        self._semaphore = threading.BoundedSemaphore(maximum_concurrent)

    def verify(self, store: PasswordHashStore, password: str) -> bool | None:
        if not self._semaphore.acquire(blocking=False):
            return None
        try:
            return store.verify(password)
        finally:
            self._semaphore.release()


def _token_hash(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}\0{value}".encode("utf-8")).hexdigest()


def _derived_csrf(session: str, key: str) -> str:
    digest = hmac.new(
        key.encode("utf-8"), f"neko-csrf-v1\0{session}".encode("utf-8"), hashlib.sha256,
    ).digest()
    return _b64encode(digest)


class GatewayStateStore:
    def __init__(
        self,
        path: Path,
        *,
        login_max_failures: int = 8,
        login_window_seconds: int = 300,
    ):
        self.path = Path(path)
        self.login_max_failures = max(1, int(login_max_failures))
        self.login_window_seconds = max(1, int(login_window_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise SecurityStateError("gateway_state_symlink_rejected")
        self._lock = threading.RLock()
        self._initialize()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS gateway_sessions (
                    session_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    idle_seconds INTEGER NOT NULL,
                    absolute_expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS gateway_login_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gateway_login_failures_at
                    ON gateway_login_failures(occurred_at);
                CREATE TABLE IF NOT EXISTS gateway_security_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                """
            )

    def create_session(
        self,
        *,
        now: float | None = None,
        idle_seconds: int = 1800,
        absolute_seconds: int = 43200,
        csrf_key: str = "",
    ) -> tuple[str, str]:
        now = float(time.time() if now is None else now)
        session = secrets.token_urlsafe(32)
        csrf = _derived_csrf(session, csrf_key) if csrf_key else secrets.token_urlsafe(32)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_sessions "
                "(session_hash, csrf_hash, created_at, last_seen_at, idle_seconds, absolute_expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    _token_hash("session", session), _token_hash("csrf", csrf), now, now,
                    int(idle_seconds), now + int(absolute_seconds),
                ),
            )
        return session, csrf

    def validate_session(self, session: str, *, now: float | None = None, touch: bool = True) -> dict | None:
        if not session:
            return None
        now = float(time.time() if now is None else now)
        digest = _token_hash("session", session)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_sessions WHERE session_hash = ?", (digest,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            if now > float(row["absolute_expires_at"]) or now - float(row["last_seen_at"]) > int(row["idle_seconds"]):
                connection.execute(
                    "UPDATE gateway_sessions SET revoked_at = ? WHERE session_hash = ?", (now, digest),
                )
                return None
            if touch:
                connection.execute(
                    "UPDATE gateway_sessions SET last_seen_at = ? WHERE session_hash = ?", (now, digest),
                )
            return dict(row)

    def validate_csrf(self, session: str, csrf: str, *, now: float | None = None) -> bool:
        row = self.validate_session(session, now=now, touch=False)
        return bool(row and csrf and hmac.compare_digest(str(row["csrf_hash"]), _token_hash("csrf", csrf)))

    def csrf_token(self, session: str, *, csrf_key: str, now: float | None = None) -> str:
        """Reconstruct a stable per-session CSRF token without storing it raw."""

        if not csrf_key:
            return ""
        row = self.validate_session(session, now=now, touch=False)
        if row is None:
            return ""
        csrf = _derived_csrf(session, csrf_key)
        return csrf if hmac.compare_digest(str(row["csrf_hash"]), _token_hash("csrf", csrf)) else ""

    def csrf_token_hash(self, session: str, *, now: float | None = None) -> str:
        row = self.validate_session(session, now=now, touch=False)
        return str(row["csrf_hash"]) if row else ""

    def revoke_session(self, session: str, *, now: float | None = None) -> None:
        if not session:
            return
        now = float(time.time() if now is None else now)
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE gateway_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                (now, _token_hash("session", session)),
            )

    def revoke_all_sessions(self, *, now: float | None = None) -> None:
        now = float(time.time() if now is None else now)
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE gateway_sessions SET revoked_at = ? WHERE revoked_at IS NULL", (now,),
            )

    def record_login_failure(self, *, now: float | None = None) -> None:
        now = float(time.time() if now is None else now)
        cutoff = now - self.login_window_seconds
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM gateway_login_failures WHERE occurred_at < ?", (cutoff,))
            connection.execute("INSERT INTO gateway_login_failures (occurred_at) VALUES (?)", (now,))

    def login_retry_after(self, *, now: float | None = None) -> int:
        now = float(time.time() if now is None else now)
        cutoff = now - self.login_window_seconds
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM gateway_login_failures WHERE occurred_at < ?", (cutoff,))
            rows = connection.execute(
                "SELECT occurred_at FROM gateway_login_failures ORDER BY occurred_at ASC",
            ).fetchall()
        if len(rows) < self.login_max_failures:
            return 0
        return max(1, math.ceil(float(rows[-self.login_max_failures]["occurred_at"]) + self.login_window_seconds - now))

    def clear_login_failures(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM gateway_login_failures")

    def audit(self, event_type: str, outcome: str, route_id: str = "", detail: dict | None = None) -> None:
        safe_detail = {
            str(key): value for key, value in (detail or {}).items()
            if str(key) in {"status", "retry_after", "duration_ms", "request_id"}
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_security_audit "
                "(created_at, event_type, outcome, route_id, detail_json) VALUES (?, ?, ?, ?, ?)",
                (time.time(), str(event_type), str(outcome), str(route_id), json.dumps(safe_detail, sort_keys=True)),
            )


def rotate_password(
    password_store: PasswordHashStore,
    state_store: GatewayStateStore,
    password: str,
    *,
    n: int = DEFAULT_SCRYPT_N,
) -> None:
    # Fail closed: a storage error may log the Owner out, but cannot leave old
    # sessions active after a new password has already become authoritative.
    state_store.revoke_all_sessions()
    password_store.set_password(password, n=n)


def session_cookie(value: str, *, max_age: int) -> str:
    return "; ".join(
        (
            f"{SESSION_COOKIE_NAME}={value}",
            "Path=/",
            "Secure",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max(0, int(max_age))}",
        )
    )


def clear_session_cookie() -> str:
    return session_cookie("", max_age=0) + "; Expires=Thu, 01 Jan 1970 00:00:00 GMT"


__all__ = [
    "DEFAULT_SCRYPT_N",
    "GatewayStateStore",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "MIN_SCRYPT_N",
    "PasswordHashStore",
    "PasswordVerifier",
    "SESSION_COOKIE_NAME",
    "SecurityStateError",
    "clear_session_cookie",
    "hash_password",
    "rotate_password",
    "session_cookie",
]
