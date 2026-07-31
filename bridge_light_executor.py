#!/usr/bin/env python3
"""Fail-closed executor for small, deterministic, read-only requests.

This module is deliberately not a general intent router.  It accepts only a
single high-confidence target whose input can be mapped completely to a fixed
Capability schema.  Everything else returns a structured fallback so the main
Agent/Codex path can retain context, research depth, approval, and write safety.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from bridge_capabilities import CapabilityCatalog


MIN_ROUTE_CONFIDENCE = 0.90
MAX_REQUEST_CHARS = 280
WEATHER_TOTAL_TIMEOUT_SECONDS = 8.0
WEATHER_REQUEST_TIMEOUT_SECONDS = 4.0
GEOCODING_MAX_BYTES = 64 * 1024
FORECAST_MAX_BYTES = 512 * 1024
GITHUB_HANDLER_MAX_BYTES = 512 * 1024
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_HOSTS = frozenset({"geocoding-api.open-meteo.com", "api.open-meteo.com"})


JsonTransport = Callable[[str, float, int], Mapping[str, Any]]
GithubTrendingHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


class SourceError(RuntimeError):
    """Safe, classified source failure that may be returned to the router."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RouteDecision:
    matched: bool
    capability_id: str | None
    confidence: float
    arguments: Mapping[str, Any]
    reason: str

    @property
    def fallback(self) -> bool:
        return not self.matched

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "fallback": self.fallback,
            "capability_id": self.capability_id,
            "confidence": round(self.confidence, 4),
            "arguments": dict(self.arguments),
            "reason": self.reason,
        }


_DISASTER_PATTERN = re.compile(
    r"台风|热带气旋|飓风|风暴潮|海啸|地震|洪水|山洪|泥石流|龙卷风|"
    r"暴雨|灾害|预警|警报|应急|撤离|影响多大|危险吗|"
    r"\b(?:typhoon|hurricane|cyclone|storm surge|tsunami|earthquake|flood|tornado|"
    r"disaster|warning|alert|evacuat(?:e|ion))\b",
    re.IGNORECASE,
)
_WRITE_PATTERN = re.compile(
    r"写入|写到|保存到|修改(?:文件|代码|配置|系统)?|删除|上传|发(?:送)?(?:消息|文件)|"
    r"推送|重启|部署|安装|创建(?:文件|任务|项目)|提交|合并|执行(?:命令|脚本)|"
    r"\b(?:write|save\s+to|modify|delete|upload|send|restart|deploy|install|commit|merge|"
    r"execute\s+(?:a\s+)?(?:command|script))\b",
    re.IGNORECASE,
)
_COMPOUND_PATTERN = re.compile(
    r"顺便|并且|然后|另外|同时帮|再帮|接着|之后再|\b(?:and then|also|after that)\b",
    re.IGNORECASE,
)
_CLOCK_PATTERN = re.compile(
    r"现在几点|几点了|当前(?:的)?时间|现在(?:的)?时间|今天几号|今天(?:的)?日期|"
    r"北京时间|中国时间|上海时间|\b(?:what time|current time|time now|today'?s date|date today)\b",
    re.IGNORECASE,
)
_WEATHER_PATTERN = re.compile(
    r"天气预报|天气|气温|温度|降雨|下雨|晴天|阴天|空气温度|"
    r"\b(?:weather|forecast|temperature|rain forecast|will it rain)\b",
    re.IGNORECASE,
)
_GITHUB_PATTERN = re.compile(
    r"github.{0,12}(?:trending|趋势|热门|热榜)|(?:trending|趋势|热门|热榜).{0,12}github",
    re.IGNORECASE,
)
_AMBIGUOUS_LOCATION_PATTERN = re.compile(
    r"^(?:这里|这边|那边|当地|附近|我这|我们这|当前位置|home|here|near me)$",
    re.IGNORECASE,
)


def _safe_confidence(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(result):
        return 0.0
    return max(0.0, min(result, 1.0))


def _normalise_text(message: Any) -> str:
    return re.sub(r"\s+", " ", str(message or "")).strip()


def _fallback_decision(reason: str, confidence: float = 0.0) -> RouteDecision:
    return RouteDecision(False, None, confidence, {}, reason)


def _clock_arguments(text: str) -> tuple[dict[str, Any] | None, str]:
    lower = text.lower()
    if re.search(r"(?:utc|gmt)(?:\+?0)?", lower):
        return {"timezone": "UTC"}, ""
    if re.search(r"北京|中国|上海|china|beijing|shanghai", text, re.IGNORECASE):
        return {"timezone": "Asia/Shanghai"}, ""

    # A bare clock request uses the platform owner's default timezone.  If the
    # user names some other place, failing closed is safer than returning China
    # time under the wrong label.
    named_zone = re.search(
        r"(?P<place>[\u4e00-\u9fff]{2,12})(?:现在)?几点|"
        r"(?:time|date).{0,12}(?:in|for)\s+(?P<english>[A-Za-z][A-Za-z .-]{1,30})",
        text,
        re.IGNORECASE,
    )
    if named_zone:
        place = (named_zone.group("place") or named_zone.group("english") or "").strip()
        if place and place not in {"现在", "当前", "今天"}:
            return None, "unsupported_timezone"
    return {"timezone": "Asia/Shanghai"}, ""


_WEATHER_PREFIXES = (
    "请问",
    "请帮我",
    "请帮忙",
    "帮我",
    "帮忙",
    "麻烦",
    "查询",
    "查一下",
    "查查",
    "看看",
    "看一下",
    "告诉我",
    "我想知道",
    "想知道",
    "给我",
)


def _clean_location(value: str) -> str:
    result = value.strip(" ，,。.!！？?：:;；的")
    changed = True
    while changed:
        changed = False
        for prefix in _WEATHER_PREFIXES:
            if result.startswith(prefix):
                result = result[len(prefix) :].strip(" ，,。.!！？?：:;；的")
                changed = True
    result = re.sub(r"^(?:今天|明天|后天|未来(?:[一二三四五六七1-7]天)?|近期)", "", result)
    result = re.sub(r"(?:今天|明天|后天|未来(?:[一二三四五六七1-7]天)?|近期)$", "", result)
    result = re.sub(r"(?:会不会|是否|会|情况|怎么样|如何)$", "", result)
    return re.sub(r"\s+", " ", result).strip(" ，,。.!！？?：:;；的")


def _extract_weather_location(text: str) -> str:
    probe = re.sub(r"今天|明天|后天|未来(?:[一二三四五六七1-7]天)?|近期", " ", text)
    probe = re.sub(r"会不会|是否|会", " ", probe)
    patterns = (
        r"(?P<location>[\u4e00-\u9fffA-Za-z·.\- ]{2,80}?)(?:的)?(?:天气预报|天气|气温|温度|降雨|下雨)",
        r"(?:天气预报|天气|气温|温度|降雨|下雨)(?:情况)?(?:在|for|in)?\s*(?P<location>[\u4e00-\u9fffA-Za-z·.\- ]{2,80})",
        r"(?:weather|forecast|temperature|will it rain)(?:\s+(?:in|for))?\s+(?P<location>[A-Za-z][A-Za-z .\-]{1,60})",
    )
    for pattern in patterns:
        match = re.search(pattern, probe, re.IGNORECASE)
        if not match:
            continue
        location = _clean_location(match.group("location"))
        location = re.sub(
            r"\b(?:today|tomorrow|tonight|this week|next week|please|now)\b.*$",
            "",
            location,
            flags=re.IGNORECASE,
        ).strip()
        if 2 <= len(location) <= 80:
            return location
    return ""


def _forecast_days(text: str) -> int:
    match = re.search(r"未来\s*([1-7一二三四五六七])\s*(?:天|日)", text)
    if not match:
        match = re.search(r"([1-7一二三四五六七])\s*(?:天|日)(?:天气|预报)", text)
    if match:
        value = match.group(1)
        return {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}.get(
            value,
            int(value) if value.isdigit() else 3,
        )
    if "后天" in text:
        return 3
    if "明天" in text or re.search(r"\btomorrow\b", text, re.IGNORECASE):
        return 2
    if "今天" in text or re.search(r"\btoday\b", text, re.IGNORECASE):
        return 1
    return 3


def _weather_arguments(text: str) -> tuple[dict[str, Any] | None, str]:
    location = _extract_weather_location(text)
    if not location or _AMBIGUOUS_LOCATION_PATTERN.fullmatch(location):
        return None, "missing_or_ambiguous_location"
    return {"location": location, "forecast_days": _forecast_days(text)}, ""


_GITHUB_LANGUAGES = (
    "TypeScript",
    "JavaScript",
    "Python",
    "Rust",
    "Kotlin",
    "Swift",
    "Java",
    "Ruby",
    "PHP",
    "C++",
    "C#",
    "Go",
)


def _github_arguments(text: str) -> dict[str, Any]:
    language = ""
    for candidate in _GITHUB_LANGUAGES:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])", text, re.IGNORECASE):
            language = candidate
            break
    period = "daily"
    if re.search(r"本周|这周|一周|weekly", text, re.IGNORECASE):
        period = "weekly"
    elif re.search(r"本月|这个月|monthly", text, re.IGNORECASE):
        period = "monthly"
    limit_match = re.search(r"(?:前|top\s*)(20|1[0-9]|[1-9])\s*(?:个|项|repos?|projects?)?", text, re.IGNORECASE)
    topic = ""
    if re.search(r"ai\s*agents?|aiagent|智能体|agentic", text, re.IGNORECASE):
        topic = "ai-agent"
    elif re.search(r"(?<![A-Za-z])ai(?![A-Za-z])|人工智能", text, re.IGNORECASE):
        topic = "ai"
    return {
        "language": language,
        "period": period,
        "limit": int(limit_match.group(1)) if limit_match else 10,
        "topic": topic,
        "output_language": "zh-CN" if "中文" in text or "简体中文" in text else "auto",
        "exclude_repos": [],
    }


def github_arguments_from_text(text: str) -> dict[str, Any]:
    """Build GitHub arguments after a Skill contract has admitted the capability."""

    return _github_arguments(_normalise_text(text))


def route_light_request(
    message: str,
    *,
    confidence_hint: float = 1.0,
    catalog: CapabilityCatalog | None = None,
) -> dict[str, Any]:
    """Classify one message against the narrow light-path allowlist."""

    executor = LightExecutor(catalog=catalog)
    return executor.route(message, confidence_hint=confidence_hint).to_dict()


def _validate_https_url(url: str, allowed_hosts: Sequence[str]) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SourceError("invalid_source_url") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {value.lower().rstrip(".") for value in allowed_hosts}
    if (
        parsed.scheme != "https"
        or not host
        or host not in allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise SourceError("insecure_or_unapproved_source")
    return parsed


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Sequence[str]):
        super().__init__()
        self._allowed_hosts = tuple(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_https_url(newurl, self._allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def strict_https_json_get(
    url: str,
    timeout_seconds: float,
    max_bytes: int,
    *,
    allowed_hosts: Sequence[str] = OPEN_METEO_HOSTS,
) -> Mapping[str, Any]:
    """Fetch bounded JSON from an exact HTTPS host allowlist."""

    _validate_https_url(url, allowed_hosts)
    if not (0 < timeout_seconds <= WEATHER_REQUEST_TIMEOUT_SECONDS):
        raise SourceError("invalid_source_timeout")
    if not (0 < max_bytes <= FORECAST_MAX_BYTES):
        raise SourceError("invalid_source_size_limit")
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        _StrictRedirectHandler(allowed_hosts),
        urllib.request.HTTPSHandler(context=context),
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "agent-platform-light-executor/1.0"},
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            _validate_https_url(response.geturl(), allowed_hosts)
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "application/json" not in content_type and "+json" not in content_type:
                raise SourceError("unexpected_source_content_type")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise SourceError("invalid_source_content_length") from exc
                if declared_size < 0 or declared_size > max_bytes:
                    raise SourceError("source_response_too_large")
            body = response.read(max_bytes + 1)
    except SourceError:
        raise
    except urllib.error.HTTPError as exc:
        raise SourceError(f"source_http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
        raise SourceError("source_unavailable") from exc
    if len(body) > max_bytes:
        raise SourceError("source_response_too_large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("invalid_source_json") from exc
    if not isinstance(payload, dict):
        raise SourceError("invalid_source_shape")
    return payload


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceError("source_not_json_serializable") from exc


def _evidence(
    *,
    source_id: str,
    source_name: str,
    source_url: str,
    data_time: str,
    fetched_at: datetime,
    valid_for: timedelta,
    facts: list[dict[str, Any]],
    source_payload: Any,
    published_at: str | None = None,
) -> dict[str, Any]:
    content = _canonical_json(source_payload)
    return {
        "source_id": source_id,
        "source_name": source_name,
        "source_url": source_url,
        "published_at": published_at,
        "data_time": data_time,
        "fetched_at": fetched_at.isoformat(),
        "valid_until": (fetched_at + valid_for).isoformat(),
        "confidence": "high",
        "facts": facts,
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }


def _number(value: Any, reason: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise SourceError(reason)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceError(reason) from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise SourceError(reason)
    return result


def _data_time_with_offset(value: Any, utc_offset_seconds: Any) -> str:
    try:
        local = datetime.fromisoformat(str(value))
        offset = int(utc_offset_seconds)
    except (TypeError, ValueError) as exc:
        raise SourceError("weather_data_time_missing") from exc
    if local.tzinfo is None:
        local = local.replace(tzinfo=timezone(timedelta(seconds=offset)))
    return local.isoformat()


def _daily_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    daily = payload.get("daily")
    if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
        raise SourceError("weather_daily_missing")
    fields = (
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "precipitation_probability_max",
        "wind_speed_10m_max",
    )
    count = len(daily["time"])
    if not 1 <= count <= 7:
        raise SourceError("weather_daily_invalid")
    for field in fields:
        if not isinstance(daily.get(field), list) or len(daily[field]) != count:
            raise SourceError("weather_daily_invalid")
    return [
        {"date": daily["time"][index], **{field: daily[field][index] for field in fields}}
        for index in range(count)
    ]


class LightExecutor:
    """Execute only the three fixed low-risk read adapters."""

    def __init__(
        self,
        *,
        catalog: CapabilityCatalog | None = None,
        json_transport: JsonTransport | None = None,
        github_handler: GithubTrendingHandler | None = None,
        now: Clock | None = None,
        monotonic: MonotonicClock | None = None,
    ):
        self.catalog = catalog or CapabilityCatalog()
        self._json_transport = json_transport or strict_https_json_get
        self._github_handler = github_handler
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic

    def route(self, message: str, *, confidence_hint: float = 1.0) -> RouteDecision:
        text = _normalise_text(message)
        hint = _safe_confidence(confidence_hint)
        if not text:
            return _fallback_decision("empty_request")
        if len(text) > MAX_REQUEST_CHARS:
            return _fallback_decision("request_too_complex", hint)
        if hint < MIN_ROUTE_CONFIDENCE:
            return _fallback_decision("low_confidence", hint)
        if _DISASTER_PATTERN.search(text):
            return _fallback_decision("hazard_or_disaster_requires_research", hint)
        if _WRITE_PATTERN.search(text):
            return _fallback_decision("side_effect_requested", hint)
        if _COMPOUND_PATTERN.search(text):
            return _fallback_decision("compound_request", hint)

        candidates: list[str] = []
        if _CLOCK_PATTERN.search(text):
            candidates.append("clock.current.read")
        if _WEATHER_PATTERN.search(text):
            candidates.append("weather.forecast.read")
        if _GITHUB_PATTERN.search(text):
            candidates.append("github.trending.read")
        if not candidates:
            return _fallback_decision("unsupported_or_ambiguous_request", hint)
        if len(candidates) != 1:
            return _fallback_decision("multiple_targets", hint)

        capability_id = candidates[0]
        arguments: dict[str, Any] | None
        argument_error = ""
        base_confidence = 0.99
        if capability_id == "clock.current.read":
            arguments, argument_error = _clock_arguments(text)
        elif capability_id == "weather.forecast.read":
            arguments, argument_error = _weather_arguments(text)
            base_confidence = 0.96
        else:
            arguments = _github_arguments(text)
            base_confidence = 0.96
        confidence = min(hint, base_confidence)
        if argument_error or arguments is None:
            return _fallback_decision(argument_error or "input_schema_not_satisfied", confidence)
        if confidence < MIN_ROUTE_CONFIDENCE:
            return _fallback_decision("low_confidence", confidence)

        manifest = self.catalog.manifest(capability_id)
        if not manifest.read_only or manifest.risk_level != "low":
            return _fallback_decision("capability_not_lightweight", confidence)
        health = self.catalog.health(capability_id)
        if health.status in {"unhealthy", "disabled"}:
            return _fallback_decision("capability_unhealthy", confidence)
        return RouteDecision(True, capability_id, confidence, arguments, "matched")

    def execute(self, message: str, *, confidence_hint: float = 1.0) -> dict[str, Any]:
        decision = self.route(message, confidence_hint=confidence_hint)
        if not decision.matched:
            return {
                "status": "fallback",
                **decision.to_dict(),
                "output": None,
                "evidence": [],
            }
        return self._execute_decision(decision)

    def execute_capability(
        self,
        capability_id: str,
        arguments: Mapping[str, Any],
        *,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Execute a capability already admitted by a reviewed Skill contract."""

        try:
            manifest = self.catalog.manifest(capability_id)
        except KeyError:
            return {
                "status": "fallback",
                "matched": False,
                "fallback": True,
                "capability_id": None,
                "confidence": 0.0,
                "arguments": {},
                "reason": "unknown_capability",
                "output": None,
                "evidence": [],
            }
        if not manifest.read_only or manifest.risk_level != "low":
            reason = "capability_not_lightweight"
        elif self.catalog.health(capability_id).status in {"unhealthy", "disabled"}:
            reason = "capability_unhealthy"
        else:
            reason = ""
        if reason:
            return {
                "status": "fallback",
                "matched": False,
                "fallback": True,
                "capability_id": capability_id,
                "confidence": round(_safe_confidence(confidence), 4),
                "arguments": dict(arguments),
                "reason": reason,
                "output": None,
                "evidence": [],
            }
        return self._execute_decision(
            RouteDecision(
                True,
                capability_id,
                _safe_confidence(confidence),
                dict(arguments),
                "skill_contract",
            ),
        )

    def _execute_decision(self, decision: RouteDecision) -> dict[str, Any]:
        started = self._monotonic()
        capability_id = str(decision.capability_id)
        try:
            if capability_id == "clock.current.read":
                output, evidence = self._execute_clock(decision.arguments)
            elif capability_id == "weather.forecast.read":
                output, evidence = self._execute_weather(decision.arguments)
            elif capability_id == "github.trending.read":
                output, evidence = self._execute_github(decision.arguments)
            else:  # pragma: no cover - fixed router invariant
                raise SourceError("handler_not_registered")
        except SourceError as exc:
            elapsed_ms = max(0, round((self._monotonic() - started) * 1000))
            self.catalog.set_health(
                capability_id,
                "degraded",
                message=exc.reason,
                latency_ms=elapsed_ms,
                checked_at=_aware_utc(self._now()).isoformat(),
            )
            return {
                "status": "fallback",
                "matched": True,
                "fallback": True,
                "capability_id": capability_id,
                "confidence": round(decision.confidence, 4),
                "arguments": dict(decision.arguments),
                "reason": exc.reason,
                "output": None,
                "evidence": [],
            }
        except Exception:  # injected handlers must never take down dispatch
            elapsed_ms = max(0, round((self._monotonic() - started) * 1000))
            self.catalog.set_health(
                capability_id,
                "degraded",
                message="handler_error",
                latency_ms=elapsed_ms,
                checked_at=_aware_utc(self._now()).isoformat(),
            )
            return {
                "status": "fallback",
                "matched": True,
                "fallback": True,
                "capability_id": capability_id,
                "confidence": round(decision.confidence, 4),
                "arguments": dict(decision.arguments),
                "reason": "handler_error",
                "output": None,
                "evidence": [],
            }

        elapsed_ms = max(0, round((self._monotonic() - started) * 1000))
        self.catalog.set_health(
            capability_id,
            "healthy",
            message="last_execution_succeeded",
            latency_ms=elapsed_ms,
            checked_at=_aware_utc(self._now()).isoformat(),
        )
        output = dict(output)
        output["evidence"] = evidence
        return {
            "status": "completed",
            "matched": True,
            "fallback": False,
            "capability_id": capability_id,
            "confidence": round(decision.confidence, 4),
            "arguments": dict(decision.arguments),
            "reason": "completed",
            "output": output,
            "evidence": evidence,
        }

    def _execute_clock(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        timezone_name = str(arguments.get("timezone") or "Asia/Shanghai")
        if timezone_name == "UTC":
            target_timezone = timezone.utc
        elif timezone_name == "Asia/Shanghai":
            target_timezone = timezone(timedelta(hours=8), name="Asia/Shanghai")
        else:
            raise SourceError("unsupported_timezone")
        fetched_at = _aware_utc(self._now())
        local_time = fetched_at.astimezone(target_timezone)
        output = {
            "timezone": timezone_name,
            "local_time": local_time.isoformat(),
            "utc_time": fetched_at.isoformat(),
            "date": local_time.date().isoformat(),
            "weekday": local_time.isoweekday(),
        }
        evidence = _evidence(
            source_id="system-clock",
            source_name="Server system clock",
            source_url="urn:system:clock",
            data_time=local_time.isoformat(),
            fetched_at=fetched_at,
            valid_for=timedelta(seconds=60),
            facts=[{"timezone": timezone_name, "local_time": local_time.isoformat()}],
            source_payload=output,
        )
        return output, [evidence]

    def _fetch_weather_json(self, url: str, deadline: float, max_bytes: int) -> Mapping[str, Any]:
        _validate_https_url(url, OPEN_METEO_HOSTS)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise SourceError("source_timeout")
        timeout = min(WEATHER_REQUEST_TIMEOUT_SECONDS, remaining)
        try:
            payload = self._json_transport(url, timeout, max_bytes)
        except SourceError:
            raise
        except (TimeoutError, socket.timeout):
            raise SourceError("source_timeout")
        except Exception as exc:
            raise SourceError("source_unavailable") from exc
        if self._monotonic() > deadline:
            raise SourceError("source_timeout")
        if not isinstance(payload, Mapping):
            raise SourceError("invalid_source_shape")
        if len(_canonical_json(payload)) > max_bytes:
            raise SourceError("source_response_too_large")
        return payload

    def _execute_weather(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        location_query = str(arguments.get("location") or "").strip()
        forecast_days = int(arguments.get("forecast_days") or 0)
        if not 2 <= len(location_query) <= 80 or not 1 <= forecast_days <= 7:
            raise SourceError("weather_input_invalid")
        deadline = self._monotonic() + WEATHER_TOTAL_TIMEOUT_SECONDS
        geocoding_url = OPEN_METEO_GEOCODING_URL + "?" + urllib.parse.urlencode(
            {"name": location_query, "count": 1, "language": "zh", "format": "json"},
        )
        geocoding = self._fetch_weather_json(geocoding_url, deadline, GEOCODING_MAX_BYTES)
        results = geocoding.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
            raise SourceError("weather_location_not_found")
        place = results[0]
        latitude = _number(place.get("latitude"), "weather_location_invalid", -90, 90)
        longitude = _number(place.get("longitude"), "weather_location_invalid", -180, 180)
        resolved_name = str(place.get("name") or "").strip()
        if not resolved_name:
            raise SourceError("weather_location_invalid")

        forecast_params = {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,"
                "weather_code,wind_speed_10m"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                "precipitation_probability_max,wind_speed_10m_max"
            ),
            "timezone": "auto",
            "forecast_days": forecast_days,
        }
        forecast_url = OPEN_METEO_FORECAST_URL + "?" + urllib.parse.urlencode(forecast_params)
        forecast = self._fetch_weather_json(forecast_url, deadline, FORECAST_MAX_BYTES)
        current = forecast.get("current")
        if not isinstance(current, Mapping):
            raise SourceError("weather_current_missing")
        required_current = (
            "time",
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
        )
        if any(value not in current for value in required_current):
            raise SourceError("weather_current_invalid")
        data_time = _data_time_with_offset(current["time"], forecast.get("utc_offset_seconds"))
        daily_rows = _daily_rows(forecast)
        fetched_at = _aware_utc(self._now())
        resolved_location = {
            "query": location_query,
            "name": resolved_name,
            "admin1": str(place.get("admin1") or ""),
            "country": str(place.get("country") or ""),
            "country_code": str(place.get("country_code") or ""),
            "latitude": latitude,
            "longitude": longitude,
            "timezone": str(forecast.get("timezone") or place.get("timezone") or ""),
        }
        output = {
            "provider": "Open-Meteo",
            "attribution": "Weather data by Open-Meteo.com",
            "location": resolved_location,
            "current": dict(current),
            "current_units": dict(forecast.get("current_units") or {}),
            "daily": daily_rows,
            "daily_units": dict(forecast.get("daily_units") or {}),
        }
        evidence = [
            _evidence(
                source_id="open-meteo-geocoding",
                source_name="Open-Meteo Geocoding API",
                source_url=geocoding_url,
                data_time=fetched_at.isoformat(),
                fetched_at=fetched_at,
                valid_for=timedelta(days=1),
                facts=[
                    {
                        "query": location_query,
                        "resolved_name": resolved_name,
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                ],
                source_payload=geocoding,
            ),
            _evidence(
                source_id="open-meteo-forecast",
                source_name="Open-Meteo Weather Forecast API",
                source_url=forecast_url,
                data_time=data_time,
                fetched_at=fetched_at,
                valid_for=timedelta(minutes=30),
                facts=[
                    {
                        "location": resolved_name,
                        "temperature_2m": current["temperature_2m"],
                        "precipitation": current["precipitation"],
                        "weather_code": current["weather_code"],
                    },
                ],
                source_payload=forecast,
            ),
        ]
        return output, evidence

    def _execute_github(self, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self._github_handler is None:
            raise SourceError("github_handler_unavailable")
        payload = self._github_handler(dict(arguments))
        if not isinstance(payload, Mapping):
            raise SourceError("github_handler_invalid")
        encoded = _canonical_json(payload)
        if len(encoded) > GITHUB_HANDLER_MAX_BYTES:
            raise SourceError("source_response_too_large")
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > int(arguments.get("limit") or 10):
            raise SourceError("github_handler_invalid")
        if any(not isinstance(item, Mapping) for item in items):
            raise SourceError("github_handler_invalid")
        source_url = str(payload.get("source_url") or "https://github.com/trending")
        _validate_https_url(source_url, ("github.com", "api.github.com"))
        fetched_at = _aware_utc(self._now())
        data_time = str(payload.get("data_time") or fetched_at.isoformat())
        output = {
            "items": [dict(item) for item in items],
            "language": str(arguments.get("language") or ""),
            "period": str(arguments.get("period") or "daily"),
            "topic": str(arguments.get("topic") or ""),
            "output_language": str(arguments.get("output_language") or "auto"),
        }
        evidence = _evidence(
            source_id="github-trending",
            source_name="GitHub Trending",
            source_url=source_url,
            data_time=data_time,
            fetched_at=fetched_at,
            valid_for=timedelta(minutes=15),
            facts=[
                {
                    "period": output["period"],
                    "language": output["language"],
                    "topic": output["topic"],
                    "output_language": output["output_language"],
                    "item_count": len(items),
                },
            ],
            source_payload=payload,
        )
        return output, [evidence]


def execute_light_request(
    message: str,
    *,
    confidence_hint: float = 1.0,
    catalog: CapabilityCatalog | None = None,
    json_transport: JsonTransport | None = None,
    github_handler: GithubTrendingHandler | None = None,
    now: Clock | None = None,
) -> dict[str, Any]:
    """Convenience facade used by a future Bridge integration."""

    return LightExecutor(
        catalog=catalog,
        json_transport=json_transport,
        github_handler=github_handler,
        now=now,
    ).execute(message, confidence_hint=confidence_hint)


__all__ = [
    "LightExecutor",
    "RouteDecision",
    "SourceError",
    "execute_light_request",
    "github_arguments_from_text",
    "route_light_request",
    "strict_https_json_get",
]
