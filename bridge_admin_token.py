#!/usr/bin/env python3
"""Pure validation and public status helpers for the fixed admin token."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
from pathlib import Path
import re


TOKEN_MIN_LENGTH = 8
TOKEN_MAX_LENGTH = 256
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]+$")


def admin_token_value_valid(value: object) -> bool:
    """Return whether a persisted Admin Token satisfies the public policy."""
    token = str(value or "")
    return (
        token == token.strip()
        and TOKEN_MIN_LENGTH <= len(token) <= TOKEN_MAX_LENGTH
        and bool(TOKEN_PATTERN.fullmatch(token))
    )


def fixed_token_status(path: Path, *, configured: bool) -> dict:
    try:
        updated_at = datetime.fromtimestamp(
            path.stat().st_mtime,
            timezone.utc,
        ).isoformat(timespec="seconds")
    except OSError:
        updated_at = ""
    return {
        "configured": bool(configured),
        "storage": "fixed_file",
        "path": str(path),
        "minimum_length": TOKEN_MIN_LENGTH,
        "maximum_length": TOKEN_MAX_LENGTH,
        "updated_at": updated_at,
    }


def validate_admin_token(
    value: object,
    confirmation: object,
    *,
    current_token: str,
    channel_token: str,
) -> str:
    token = str(value or "")
    confirmed = str(confirmation or "")
    if token != token.strip() or confirmed != confirmed.strip():
        raise ValueError("token_whitespace_not_allowed")
    if token != confirmed:
        raise ValueError("token_confirmation_mismatch")
    if not TOKEN_MIN_LENGTH <= len(token) <= TOKEN_MAX_LENGTH:
        raise ValueError("token_length_invalid")
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("token_characters_invalid")
    if current_token and hmac.compare_digest(
        token.encode("utf-8"),
        current_token.encode("utf-8"),
    ):
        raise ValueError("token_unchanged")
    if channel_token and hmac.compare_digest(
        token.encode("utf-8"),
        channel_token.encode("utf-8"),
    ):
        raise ValueError("token_matches_channel_secret")
    return token


__all__ = [
    "TOKEN_MAX_LENGTH",
    "TOKEN_MIN_LENGTH",
    "TOKEN_PATTERN",
    "admin_token_value_valid",
    "fixed_token_status",
    "validate_admin_token",
]
