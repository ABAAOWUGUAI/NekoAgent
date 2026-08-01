#!/usr/bin/env python3
"""Authentication principal and least-privilege HTTP route policy."""

from __future__ import annotations

import re
import hmac
from enum import Enum
from pathlib import Path


class PrincipalKind(str, Enum):
    ANONYMOUS = "anonymous"
    ADMIN_SESSION = "admin_session"
    ADMIN_TOKEN = "admin_token"
    QQ_CHANNEL = "qq_channel"


def read_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return ""


def secrets_distinct(first: str, second: str) -> bool:
    if not first or not second:
        return True
    return not hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def resolve_principal(
    has_admin_session: bool,
    admin_token: str,
    channel_token: str,
    supplied_admin_token: str,
    supplied_channel_token: str,
    client_allowed: bool,
    allow_public_admin: bool,
) -> PrincipalKind:
    if has_admin_session:
        return PrincipalKind.ADMIN_SESSION
    if admin_token and hmac.compare_digest(
        supplied_admin_token.encode("utf-8"), admin_token.encode("utf-8"),
    ) and (allow_public_admin or client_allowed):
        return PrincipalKind.ADMIN_TOKEN
    if channel_token and client_allowed and hmac.compare_digest(
        supplied_channel_token.encode("utf-8"), channel_token.encode("utf-8"),
    ):
        return PrincipalKind.QQ_CHANNEL
    return PrincipalKind.ANONYMOUS


_CHANNEL_GET_ROUTES = frozenset(
    {
        "/status",
        "/server/status",
        "/github/trending",
        "/projects",
        "/assistant/memories",
        "/assistant/settings",
        "/tasks",
        "/tasks/stats",
        "/tasks/delivery/pending",
        "/qq/channel/runtime-config",
    },
)

_CHANNEL_POST_ROUTES = frozenset(
    {
        "/qq/access/check",
        "/qq/channel/heartbeat",
        "/qq/voice/transport-probe",
        "/qq/voice/fetch",
        "/qq/voice/input",
        "/qq/events",
        "/deliveries/claim",
        "/projects",
        "/projects/current",
        "/assistant/memories",
        "/assistant/memories/delete",
        "/assistant/settings",
        "/assistant/dispatch",
        "/assistant/group/dispatch",
        "/assistant/memes/mark",
        "/tasks",
    },
)

_CHANNEL_ROUTE_PATTERNS = {
    "GET": (
        re.compile(r"^/tasks/[^/]+$"),
    ),
    "POST": (
        re.compile(r"^/deliveries/[^/]+/(?:send-start|ack|retry|ambiguous)$"),
        re.compile(r"^/tasks/[^/]+/(?:cancel|retry|delivery)$"),
    ),
}


def route_allowed(principal: PrincipalKind, method: str, path: str) -> bool:
    """Return whether a principal may enter a protected Bridge route.

    Public routes are handled before this policy. Admin principals retain the
    existing control-plane contract; the QQ channel is default-deny.
    """

    if principal in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}:
        return True
    if principal is not PrincipalKind.QQ_CHANNEL:
        return False

    normalized_method = str(method or "").upper()
    normalized_path = str(path or "").split("?", 1)[0]
    exact_routes = _CHANNEL_GET_ROUTES if normalized_method == "GET" else _CHANNEL_POST_ROUTES
    if normalized_path in exact_routes:
        return True
    return any(
        pattern.fullmatch(normalized_path)
        for pattern in _CHANNEL_ROUTE_PATTERNS.get(normalized_method, ())
    )


__all__ = [
    "PrincipalKind",
    "read_secret",
    "resolve_principal",
    "route_allowed",
    "secrets_distinct",
]
