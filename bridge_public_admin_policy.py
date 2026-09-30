#!/usr/bin/env python3
"""Canonical C1.1 public Admin route policy.

The policy is deliberately hand-maintained.  Frontend inventory tests fail
when the active console adds or removes a request, so a future Bridge route is
never made public merely because it exists.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote_to_bytes, urlencode


class PolicyError(ValueError):
    """The request is ambiguous, malformed, or outside the public policy."""


Validator = Callable[[str], bool]
_SAFE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_VALID_PERCENT = re.compile(r"%(?:[0-9A-Fa-f]{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class QueryRule:
    name: str
    validator: Validator
    required: bool = False


@dataclass(frozen=True)
class RouteRule:
    method: str
    template: str
    query: tuple[QueryRule, ...] = ()
    body_limit: int = 0
    anonymous: bool = False
    browser_access: bool = True
    bridge_access: bool = True
    frontend: bool = True
    gateway_only: bool = False

    @property
    def pattern(self) -> re.Pattern[str]:
        cursor = 0
        parts: list[str] = []
        for match in re.finditer(r"\{[A-Za-z0-9_]+\}", self.template):
            parts.append(re.escape(self.template[cursor:match.start()]))
            parts.append(f"({_SAFE_ID_PATTERN})")
            cursor = match.end()
        parts.append(re.escape(self.template[cursor:]))
        return re.compile("^" + "".join(parts) + "$")


@dataclass(frozen=True)
class PolicyMatch:
    rule: RouteRule
    canonical_path: str
    query: tuple[tuple[str, str], ...]

    @property
    def canonical_target(self) -> str:
        encoded = urlencode(self.query, doseq=False, safe=":")
        return self.canonical_path + (f"?{encoded}" if encoded else "")


def _integer(minimum: int, maximum: int) -> Validator:
    def validate(value: str) -> bool:
        return bool(re.fullmatch(r"0|[1-9][0-9]*", value)) and minimum <= int(value) <= maximum
    return validate


def _one_of(*values: str) -> Validator:
    allowed = frozenset(values)
    return lambda value: value in allowed


def _opaque(maximum: int, *, allow_empty: bool = True) -> Validator:
    def validate(value: str) -> bool:
        if not value:
            return allow_empty
        return len(value) <= maximum and not _CONTROL.search(value) and "\\" not in value
    return validate


def _safe_id(value: str) -> bool:
    return bool(re.fullmatch(_SAFE_ID_PATTERN, value))


def _asset_version(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{16}", value))


LIMIT_20 = QueryRule("limit", _integer(1, 20))
LIMIT_50 = QueryRule("limit", _integer(1, 50))
LIMIT_80 = QueryRule("limit", _integer(1, 80))
LIMIT_100 = QueryRule("limit", _integer(1, 100))
CURSOR = QueryRule("cursor", _opaque(512))


def _route(
    method: str,
    template: str,
    *,
    query: tuple[QueryRule, ...] = (),
    body_limit: int = 0,
    anonymous: bool = False,
    browser_access: bool = True,
    bridge_access: bool = True,
    frontend: bool = True,
    gateway_only: bool = False,
) -> RouteRule:
    return RouteRule(
        method=method,
        template=template,
        query=query,
        body_limit=body_limit,
        anonymous=anonymous,
        browser_access=browser_access,
        bridge_access=bridge_access,
        frontend=frontend,
        gateway_only=gateway_only,
    )


_STATIC_ASSETS = (
    "/admin/static/admin-v4-product.css",
    "/admin/static/v4-product-contract.js",
    "/admin/static/v4-product-attention.js",
    "/admin/static/v4-product-adapters.js",
    "/admin/static/v4-product-app.js",
)


PUBLIC_ADMIN_ROUTES: tuple[RouteRule, ...] = (
    _route("GET", "/", anonymous=True, bridge_access=False, frontend=False),
    _route("HEAD", "/", anonymous=True, bridge_access=False, frontend=False),
    _route("GET", "/admin", anonymous=True, bridge_access=False, frontend=False),
    _route("HEAD", "/admin", anonymous=True, bridge_access=False, frontend=False),
    _route("GET", "/admin/", anonymous=True, bridge_access=False, frontend=False),
    _route("HEAD", "/admin/", anonymous=True, bridge_access=False, frontend=False),
    *tuple(
        _route(method, path, query=(QueryRule("v", _asset_version),), anonymous=True, bridge_access=False, frontend=False)
        for path in _STATIC_ASSETS for method in ("GET", "HEAD")
    ),
    _route("GET", "/admin/assets/sample-background.jpg", bridge_access=False, frontend=False),
    _route("HEAD", "/admin/assets/sample-background.jpg", bridge_access=False, frontend=False),
    _route("GET", "/admin/bootstrap", anonymous=True, bridge_access=False),
    _route("POST", "/admin/login", body_limit=4096, anonymous=True, bridge_access=False),
    _route("POST", "/admin/logout", body_limit=4096, bridge_access=False),
    _route("GET", "/admin/appearance", browser_access=False, frontend=False, gateway_only=True),
    _route("POST", "/admin/appearance", body_limit=65536),

    _route("GET", "/assistant/owner-brief", query=(LIMIT_20,)),
    _route("GET", "/assistant/conversations", query=(QueryRule("channel_type", _one_of("web"), True), LIMIT_80, CURSOR)),
    _route("GET", "/assistant/conversations/{thread_id}/messages", query=(QueryRule("channel_type", _one_of("web"), True), LIMIT_80, CURSOR)),
    _route("GET", "/assistant/qq/conversations", query=(QueryRule("kind", _one_of("group", "private"), True), LIMIT_80, CURSOR)),
    _route("GET", "/assistant/qq/conversations/{conversation_ref}/timeline", query=(LIMIT_80, CURSOR)),
    _route("GET", "/assistant/qq/conversations/{conversation_ref}/summary", query=(QueryRule("window_hours", _integer(1, 168)),)),
    _route("GET", "/assistant/qq/events/{event_ref}/inspector"),

    _route("GET", "/qq/settings"),
    _route("GET", "/qq/groups/state"),
    _route("POST", "/qq/settings", body_limit=65536),
    _route("GET", "/qq/group-participation/windows"),
    _route("POST", "/qq/group-participation/windows", body_limit=65536),
    _route("GET", "/assistant/groups"),
    _route("GET", "/assistant/groups/messages", query=(QueryRule("group_id", _safe_id, True), LIMIT_80)),
    _route("POST", "/assistant/groups", body_limit=65536),
    _route("GET", "/assistant/group-research", query=(LIMIT_100,)),
    _route("POST", "/assistant/group-research/policy", body_limit=65536),
    _route("GET", "/deliveries", query=(QueryRule("state", _one_of("all"), True), QueryRule("channel", _one_of("qq"), True), LIMIT_100)),

    _route("GET", "/tasks", query=(LIMIT_100,)),
    _route("GET", "/tasks/{task_id}"),
    _route("GET", "/execution/overview", query=(LIMIT_100,)),
    _route("GET", "/assistant/approvals", query=(QueryRule("status", _one_of("pending"), True), LIMIT_100)),
    _route("POST", "/assistant/approvals/{approval_id}/decision", body_limit=65536),
    _route("GET", "/automations/overview"),
    _route("GET", "/automations/jobs"),
    _route("POST", "/automations/jobs", body_limit=65536),
    _route("GET", "/projects", query=(QueryRule("include_archived", _one_of("true")),)),
    _route("GET", "/projects/current"),

    _route("GET", "/assistant/artifacts", query=(LIMIT_100, QueryRule("offset", _integer(0, 10000)))),
    _route("GET", "/assistant/artifacts/{artifact_id}"),
    _route("GET", "/assistant/artifacts/{artifact_id}/versions"),
    _route("GET", "/assistant/artifacts/{artifact_id}/events", query=(LIMIT_100,)),
    _route("GET", "/assistant/artifacts/versions/{version_id}/download"),
    _route("POST", "/assistant/artifacts/{artifact_id}/revise", body_limit=131072),

    _route("GET", "/assistant/memories", query=(LIMIT_100,)),
    _route("GET", "/assistant/qq-memories", query=(
        QueryRule("channel", _one_of("group", "private"), True),
        QueryRule("subject_id", _safe_id, True),
    )),
    _route("POST", "/assistant/qq-memories", body_limit=4096),
    _route("POST", "/assistant/memories", body_limit=65536),
    _route("PATCH", "/assistant/memories/{memory_id}", body_limit=4096),
    _route("POST", "/assistant/memories/delete", body_limit=65536),
    _route("POST", "/assistant/memories/{memory_id}/promote", body_limit=65536),
    _route("GET", "/assistant/knowledge/workspace"),
    _route("GET", "/assistant/learning"),
    _route("GET", "/assistant/learning/trace", query=(LIMIT_50,)),
    _route("POST", "/assistant/learning/feedback", body_limit=65536),

    _route("GET", "/assistant/behavior-growth"),
    _route("GET", "/assistant/behavior-growth/cases", query=(LIMIT_50,)),
    _route("GET", "/assistant/behavior-growth/evidence-collection/cutover"),
    _route("POST", "/assistant/behavior-growth/evidence-collection/cutover", body_limit=65536),
    _route("GET", "/assistant/behavior-growth/optimizer/cutover"),
    _route("POST", "/assistant/behavior-growth/optimizer/cutover", body_limit=65536),
    _route("GET", "/assistant/behavior-growth/authorizations"),
    _route("POST", "/assistant/behavior-growth/authorizations/plan", body_limit=65536),
    _route("POST", "/assistant/behavior-growth/authorizations/create", body_limit=65536),
    _route("POST", "/assistant/behavior-growth/authorizations/revoke", body_limit=65536),
    _route("GET", "/assistant/behavior-growth/paired-shadow/cutover"),
    _route("POST", "/assistant/behavior-growth/paired-shadow/cutover/set", body_limit=65536),
    _route("POST", "/assistant/behavior-growth/paired-shadow/cutover/revoke", body_limit=65536),

    _route("GET", "/assistant/persona-workspace"),
    _route("POST", "/assistant/persona-workspace/preview", body_limit=65536),
    _route("POST", "/assistant/persona-workspace", body_limit=65536),
    _route("GET", "/assistant/persona-presets"),
    _route("POST", "/assistant/persona-presets", body_limit=65536),
    _route("POST", "/assistant/persona-presets/update", body_limit=65536),
    _route("POST", "/assistant/persona-presets/archive", body_limit=65536),
    _route("POST", "/assistant/persona-presets/apply", body_limit=65536),
    _route(
        "GET",
        "/assistant/relationship",
        query=(
            QueryRule("user_id", _safe_id, required=True),
            QueryRule("scope_type", _one_of("private_user"), required=True),
            QueryRule("scope_id", _one_of(""), required=True),
        ),
    ),
    _route("POST", "/assistant/relationship", body_limit=65536),
    _route(
        "GET",
        "/assistant/proactive/social-policy",
        query=(QueryRule("user_id", _safe_id, required=True),),
    ),
    _route("POST", "/assistant/proactive/social-policy", body_limit=65536),
    _route("GET", "/assistant/voice-response-policy"),
    _route("GET", "/assistant/pets"),
    _route("GET", "/assistant/pets/assets/{pack_id}/portrait", query=(QueryRule("v", _asset_version, required=True),), frontend=False),

    _route("GET", "/assistant/models"),
    _route("POST", "/assistant/models/provider", body_limit=65536),
    _route("POST", "/assistant/models/model", body_limit=65536),
    _route("POST", "/assistant/models/model/delete", body_limit=65536),
    _route("POST", "/assistant/models/provider/delete", body_limit=65536),
    _route("POST", "/assistant/models/discover", body_limit=65536),
    _route("POST", "/assistant/models/bind", body_limit=65536),
    _route("POST", "/assistant/models/work-executor/activate", body_limit=65536),
    _route("POST", "/assistant/models/test", body_limit=65536),
    _route("POST", "/assistant/models/executor/verify", body_limit=65536),
    _route("GET", "/assistant/network-policy"),
    _route("GET", "/proxy/subscriptions"),
    _route("GET", "/proxy/groups"),
    _route("POST", "/proxy/subscriptions/create", body_limit=4096),
    _route("POST", "/proxy/subscriptions/update", body_limit=4096),
    _route("POST", "/proxy/subscriptions/refresh", body_limit=4096),
    _route("POST", "/proxy/subscriptions/enable", body_limit=4096),
    _route("POST", "/proxy/subscriptions/switch", body_limit=4096),
    _route("POST", "/proxy/subscriptions/disable", body_limit=4096),
    _route("POST", "/proxy/subscriptions/delete", body_limit=4096),
    _route("POST", "/proxy/delay", body_limit=4096),
    _route("POST", "/proxy/select", body_limit=4096),
    _route("GET", "/proxy/consumers/aiclient2api"),
    _route("GET", "/proxy/consumers/aiclient2api/model-observation"),
    _route("POST", "/proxy/consumers/aiclient2api/save", body_limit=4096),
    _route("POST", "/proxy/consumers/aiclient2api/test", body_limit=4096),
    _route("POST", "/proxy/consumers/aiclient2api/apply", body_limit=4096),
    _route("POST", "/proxy/consumers/aiclient2api/rollback", body_limit=4096),
    _route("GET", "/capabilities/summary"),
    _route("GET", "/capabilities/plugins"),
    _route("POST", "/capabilities/plugins/toggle", body_limit=65536),
    _route("GET", "/capabilities/skills"),
    _route("GET", "/reliability/dead-letters"),
    _route("POST", "/reliability/dead-letters/{delivery_id}/requeue", body_limit=65536),
    _route("GET", "/qq/diagnostics"),
    _route("GET", "/qq/qrcode"),
    _route("POST", "/qq/qrcode/refresh", body_limit=4096),
    _route("GET", "/services"),
    _route("GET", "/logs", query=(QueryRule("target", _one_of("bridge"), True), QueryRule("lines", _integer(1, 200), True))),
    _route("POST", "/assistant/dispatch", body_limit=65536),
)


def _validate_percent_encoding(value: str) -> None:
    index = 0
    while index < len(value):
        if value[index] == "%":
            if not _VALID_PERCENT.match(value, index):
                raise PolicyError("invalid_percent_encoding")
            index += 3
        else:
            index += 1


def _canonical_path(raw_path: str) -> str:
    if not raw_path.startswith("/") or raw_path.startswith("//"):
        raise PolicyError("absolute_or_network_path_rejected")
    if "\\" in raw_path or _CONTROL.search(raw_path):
        raise PolicyError("path_character_rejected")
    _validate_percent_encoding(raw_path)
    lowered = raw_path.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise PolicyError("encoded_separator_rejected")
    try:
        decoded = unquote_to_bytes(raw_path).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PolicyError("path_encoding_rejected") from exc
    if "\\" in decoded or _CONTROL.search(decoded) or "//" in decoded:
        raise PolicyError("path_ambiguity_rejected")
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise PolicyError("dot_segment_rejected")
    return decoded


def _canonical_query(raw_query: str, rule: RouteRule) -> tuple[tuple[str, str], ...]:
    _validate_percent_encoding(raw_query)
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=False, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise PolicyError("query_encoding_rejected") from exc
    seen: set[str] = set()
    accepted = {item.name: item for item in rule.query}
    values: dict[str, str] = {}
    for name, value in pairs:
        if name in seen:
            raise PolicyError("duplicate_query_parameter")
        seen.add(name)
        query_rule = accepted.get(name)
        if query_rule is None or not query_rule.validator(value):
            raise PolicyError("query_parameter_rejected")
        values[name] = value
    if any(item.required and item.name not in values for item in rule.query):
        raise PolicyError("query_parameter_required")
    return tuple((item.name, values[item.name]) for item in rule.query if item.name in values)


def match_public_admin_request(method: str, raw_target: str, *, browser: bool) -> PolicyMatch:
    normalized_method = str(method or "").upper()
    if normalized_method not in {"GET", "HEAD", "POST", "PATCH"}:
        raise PolicyError("method_rejected")
    if not isinstance(raw_target, str) or "#" in raw_target:
        raise PolicyError("request_target_rejected")
    raw_path, separator, raw_query = raw_target.partition("?")
    canonical_path = _canonical_path(raw_path)
    for rule in PUBLIC_ADMIN_ROUTES:
        accessible = rule.browser_access if browser else rule.bridge_access
        if accessible and rule.method == normalized_method and rule.pattern.fullmatch(canonical_path):
            query = _canonical_query(raw_query if separator else "", rule)
            return PolicyMatch(rule=rule, canonical_path=canonical_path, query=query)
    raise PolicyError("public_admin_route_denied")


def frontend_inventory() -> tuple[tuple[str, str], ...]:
    return tuple((rule.method, rule.template) for rule in PUBLIC_ADMIN_ROUTES if rule.frontend)


def bridge_gateway_route_allowed(method: str, raw_target: str) -> bool:
    try:
        match_public_admin_request(method, raw_target, browser=False)
        return True
    except PolicyError:
        return False


__all__ = [
    "PUBLIC_ADMIN_ROUTES",
    "PolicyError",
    "PolicyMatch",
    "QueryRule",
    "RouteRule",
    "bridge_gateway_route_allowed",
    "frontend_inventory",
    "match_public_admin_request",
]
