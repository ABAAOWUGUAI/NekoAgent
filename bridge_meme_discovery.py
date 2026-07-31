#!/usr/bin/env python3
"""Bounded meme discovery with provenance, quarantine, and owner review."""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import bridge_meme_social as meme_assets


BRIDGE_DIR = Path(os.environ.get("ASSISTANT_PLATFORM_BRIDGE_DIR", "/opt/agent-stack/codex-qq-bridge"))
CANDIDATE_DIR=Path(os.environ.get("MEME_DISCOVERY_CANDIDATE_DIR",str(BRIDGE_DIR/"assets/meme-candidates")))
OPENVERSE_API = "https://api.openverse.org/v1/images/"
SAMPLE_OFFICIAL_OPUS = meme_assets.OFFICIAL_SAMPLE_OPUS
FAN_REFERENCE_OPUS = "https://www.bilibili.com/opus/905451613661954053"
USER_AGENT = "GeneralPersonalAgent-MemeDiscovery/1.0"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = meme_assets.MAX_UPLOAD_BYTES
MAX_QUERY_CHARS = 120
MAX_DISCOVERY_LIMIT = 12
DISCOVERY_PROXY = os.environ.get("MEME_DISCOVERY_PROXY", "http://127.0.0.1:7890").strip()

PROVIDERS = (
    {
        "id": "auto",
        "name": "自动选择",
        "description": "永雏示例查询优先官方公开动态，其他查询使用开放许可媒体。",
        "license_policy": "review_required",
    },
    {
        "id": "sample_official",
        "name": "永雏示例官方公开动态",
        "description": "从固定白名单中的官方动态提取公开 emoji 素材。",
        "license_policy": "private_use_review_required",
    },
    {
        "id": "openverse",
        "name": "Openverse 开放媒体",
        "description": "检索开放许可媒体并保留作者、许可和原始页面。",
        "license_policy": "license_review_required",
    },
    {
        "id": "fan_reference",
        "name": "二创表情候选（私用审核）",
        "description": "采集非官方二创候选；保留作者和来源，仅允许私用审核，不代表当前助手身份。",
        "license_policy": "private_use_review_required",
    },
)
PROVIDER_IDS = frozenset(item["id"] for item in PROVIDERS)

EMOTION_RULES = (
    ("comfort", ("哭", "泪", "委屈", "难过", "抱抱", "安慰", "伤心")),
    ("happy", ("笑", "开心", "谢谢", "好耶", "启动", "加油", "庆祝", "可爱")),
    ("playful", ("汗", "无语", "尴尬", "坏笑", "调皮", "doge", "乐")),
    ("curious", ("问号", "疑惑", "什么", "好奇", "思考")),
    ("work", ("工作", "代码", "上线", "完成", "通过")),
)
EMOTION_LABELS = {
    "daily": "日常回应",
    "happy": "开心或庆祝",
    "comfort": "难过或安慰",
    "playful": "玩笑或无语",
    "curious": "疑问或好奇",
    "work": "工作反馈",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _plain_text(value: object, limit: int = 500) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _normalise_title(value: object, fallback: str = "表情候选") -> str:
    text = _plain_text(value, 120).strip()
    wrappers = {"[": "]", "【": "】", "(": ")", "（": "）"}
    while len(text) >= 2 and wrappers.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text or fallback


def _friendly_sample_title(value: object) -> str:
    """Turn public emoji package labels into short owner-facing asset names."""

    title = _normalise_title(value, "示例表情")
    for prefix in ("永雏示例叮咚集动态_", "仲夏集表情动态包_", "永雏示例表情包_"):
        if title.startswith(prefix):
            title = title[len(prefix) :].strip("_ -")
            break
    if title and "示例" not in title:
        title = f"示例·{title}"
    return title or "示例表情"


def _normalise_media_url(value: object) -> str:
    url = str(value or "").strip().replace("\\/", "/")
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url


def _tags(values: Iterable[object], limit: int = 20) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in re.split(r"[,，;/|\s]+", _plain_text(value, 300)):
            item = part.strip("#[]【】()（） ")[:30]
            key = item.casefold()
            if not item or key in seen:
                continue
            seen.add(key)
            result.append(item)
            if len(result) >= limit:
                return ",".join(result)
    return ",".join(result)


def infer_emotion(*values: object) -> str:
    text = " ".join(str(value or "") for value in values).casefold()
    for emotion, keywords in EMOTION_RULES:
        if any(keyword.casefold() in text for keyword in keywords):
            return emotion
    return "daily"


def metadata_description(title: str, emotion: str, *, creator: str = "", context: str = "") -> str:
    subject = _normalise_title(title)
    scene = EMOTION_LABELS.get(emotion, EMOTION_LABELS["daily"])
    detail = next(
        (
            description
            for keywords, description in (
                (("比耶", "好耶", "启动"), "表达开心、赞同或准备出发"),
                (("奸笑", "坏笑"), "带有调皮、得意的笑意"),
                (("大头",), "突出角色正面表情和存在感"),
                (("哭", "玉玉", "泪"), "表达委屈、低落或希望得到安慰"),
                (("汗", "无语", "尴尬"), "表达尴尬、无语或小小慌张"),
                (("谢谢", "感谢"), "表达感谢和友好回应"),
            )
            if any(keyword in subject for keyword in keywords)
        ),
        "",
    )
    owner = f"，来源标注为{_plain_text(creator, 80)}" if creator else ""
    suffix = f"；与“{_plain_text(context, 60)}”查询相关" if context else ""
    opening = f"{subject}：{detail}，适合用于{scene}" if detail else f"{subject}表情，适合用于{scene}"
    return f"{opening}{owner}{suffix}。"[:500]


def ensure_meme_discovery_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meme_discovery_jobs (
            id TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            requested_limit INTEGER NOT NULL DEFAULT 8,
            discovered_count INTEGER NOT NULL DEFAULT 0,
            imported_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meme_candidates (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            source_item_id TEXT NOT NULL,
            source_page_url TEXT NOT NULL DEFAULT '',
            media_url TEXT NOT NULL DEFAULT '',
            thumbnail_url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            description_method TEXT NOT NULL DEFAULT 'source_metadata',
            emotion TEXT NOT NULL DEFAULT 'daily',
            tags TEXT NOT NULL DEFAULT '',
            creator TEXT NOT NULL DEFAULT '',
            license_name TEXT NOT NULL DEFAULT '',
            license_url TEXT NOT NULL DEFAULT '',
            license_note TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            file_hash TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            file_size INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            asset_id TEXT NOT NULL DEFAULT '',
            duplicate_asset_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            seen_count INTEGER NOT NULL DEFAULT 1,
            last_seen_at TEXT NOT NULL,
            reviewed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider, source_item_id)
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meme_discovery_jobs_created ON meme_discovery_jobs(created_at DESC)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meme_candidates_status ON meme_candidates(status, updated_at DESC)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meme_candidates_hash ON meme_candidates(file_hash)",
    )


def list_discovery_providers() -> list[dict]:
    return [dict(item) for item in PROVIDERS]


def _host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    normal = str(host or "").strip(".").casefold()
    return any(normal == allowed.casefold() or normal.endswith("." + allowed.casefold()) for allowed in allowed_hosts)


def validate_remote_url(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    resolver: Callable[..., list] = socket.getaddrinfo,
) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ValueError("remote_url_not_allowed")
    if parsed.port not in {None, 443} or not _host_allowed(host, allowed_hosts):
        raise ValueError("remote_host_not_allowed")
    try:
        addresses = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("remote_dns_failed") from exc
    if not addresses:
        raise ValueError("remote_dns_failed")
    for entry in addresses:
        raw = str(entry[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("remote_address_invalid") from exc
        if not address.is_global:
            raise ValueError("remote_address_not_public")
    return parsed


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_remote_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_discovery_proxy() -> str:
    """Return the configured local egress proxy, never an arbitrary remote proxy."""

    try:
        parsed = urllib.parse.urlparse(DISCOVERY_PROXY)
        if (
            parsed.scheme != "http"
            or (parsed.hostname or "").casefold() not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            return ""
    except ValueError:
        return ""
    return DISCOVERY_PROXY


def _read_remote(
    url: str,
    allowed_hosts: tuple[str, ...],
    *,
    max_bytes: int,
    timeout: int = 8,
    headers: dict[str, str] | None = None,
    use_proxy: bool | None = None,
) -> bytes:
    validate_remote_url(url, allowed_hosts)
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html,image/*"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    proxy = _safe_discovery_proxy()
    attempts = [bool(use_proxy)] if use_proxy is not None else ([False, True] if proxy else [False])
    payload = b""
    last_error: ValueError | None = None
    for proxy_attempt in attempts:
        handlers: list[object] = [
            urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy} if proxy_attempt and proxy else {},
            ),
            _CheckedRedirectHandler(allowed_hosts),
        ]
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=max(2, min(timeout, 20))) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError("remote_content_too_large")
                payload = response.read(max_bytes + 1)
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            last_error = ValueError(f"remote_http_{exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = ValueError("remote_unavailable")
        if use_proxy is not None:
            break
    if last_error is not None:
        raise last_error
    if len(payload) > max_bytes:
        raise ValueError("remote_content_too_large")
    return payload


def _detect_image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("unsupported_image_type")


def parse_bilibili_opus(html_text: str, query: str, *, source_url: str = SAMPLE_OFFICIAL_OPUS) -> list[dict]:
    marker = "__INITIAL_STATE__="
    start = html_text.find(marker)
    if start < 0:
        raise ValueError("bilibili_initial_state_missing")
    try:
        state, _ = json.JSONDecoder().raw_decode(html_text[start + len(marker) :])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("bilibili_initial_state_invalid") from exc
    result: list[dict] = []
    seen: set[str] = set()
    modules = ((state or {}).get("detail") or {}).get("modules") or []
    for module in modules:
        paragraphs = ((module or {}).get("module_content") or {}).get("paragraphs") or []
        for paragraph in paragraphs:
            nodes = (((paragraph or {}).get("text") or {}).get("nodes") or [])
            for node in nodes:
                rich = (node or {}).get("rich") or {}
                emoji = rich.get("emoji") or {}
                if rich.get("type") != "RICH_TEXT_NODE_TYPE_EMOJI" or not emoji:
                    continue
                media_url = _normalise_media_url(
                    emoji.get("webp_url") or emoji.get("gif_url") or emoji.get("icon_url"),
                )
                if not media_url or media_url in seen:
                    continue
                seen.add(media_url)
                title = _friendly_sample_title(emoji.get("text") or rich.get("text"))
                emotion = infer_emotion(query, title)
                source_item_id = f"{emoji.get('package_id') or 'package'}:{emoji.get('id') or hashlib.sha1(media_url.encode()).hexdigest()[:12]}"
                result.append(
                    {
                        "provider": "sample_official",
                        "source_item_id": source_item_id,
                        "source_page_url": source_url,
                        "media_url": media_url,
                        "thumbnail_url": media_url,
                        "download_url": media_url,
                        "download_hosts": ("hdslb.com",),
                        "title": title,
                        "description": metadata_description(title, emotion, creator="永雏示例官方账号", context=query),
                        "description_method": "source_metadata",
                        "emotion": emotion,
                        "tags": _tags(("永雏示例", "示例", title, EMOTION_LABELS.get(emotion), query)),
                        "creator": "永雏示例官方账号",
                        "license_name": "official-public-display",
                        "license_url": source_url,
                        "license_note": "来自永雏示例官方 Bilibili 动态公开展示，仅用于拥有者私有聊天；批准前仍需复核，不作二次分发。",
                    },
                )
    return result


def parse_bilibili_fan_reference(
    html_text: str,
    query: str,
    *,
    source_url: str = FAN_REFERENCE_OPUS,
) -> list[dict]:
    """Extract bounded fan-made image candidates with explicit provenance."""

    urls: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"(?:https?:)?//i0\.hdslb\.com/bfs/new_dyn/[^\"'\\ <]+", html_text):
        media_url = _normalise_media_url(raw)
        if media_url and media_url not in seen:
            seen.add(media_url)
            urls.append(media_url)
    result: list[dict] = []
    for index, media_url in enumerate(urls[:MAX_DISCOVERY_LIMIT], start=1):
        title = f"二创表情候选 {index:03d}"
        emotion = infer_emotion(query, title)
        result.append(
            {
                "provider": "fan_reference",
                "source_item_id": hashlib.sha1(media_url.encode("utf-8")).hexdigest()[:24],
                "source_page_url": source_url,
                "media_url": media_url,
                "thumbnail_url": media_url,
                "download_url": media_url,
                "download_hosts": ("i0.hdslb.com",),
                "title": title,
                "description": metadata_description(
                    title, emotion,
                    creator="原合集作者/转载者信息需复核",
                    context=query,
                ),
                "description_method": "source_page_only",
                "emotion": emotion,
                "tags": _tags(("二创", "表情参考", query, EMOTION_LABELS.get(emotion))),
                "creator": "檀夕二 / 原合集注明 cv32642409",
                "license_name": "fan-compilation-unverified",
                "license_url": source_url,
                "license_note": "非官方二创合集；页面未提供统一再分发授权，仅进入私用审核，不进入公开分发包。",
            },
        )
    return result


def normalise_openverse_results(payload: dict, query: str) -> list[dict]:
    result: list[dict] = []
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        thumbnail = _normalise_media_url(raw.get("thumbnail"))
        if not thumbnail:
            continue
        title = _normalise_title(raw.get("title"), f"{query}候选")
        creator = _plain_text(raw.get("creator"), 120)
        raw_tags = [item.get("name") for item in raw.get("tags") or [] if isinstance(item, dict)]
        emotion = infer_emotion(query, title, *raw_tags)
        description = _plain_text((raw.get("meta_data") or {}).get("description"), 500)
        if not description:
            description = metadata_description(title, emotion, creator=creator, context=query)
        license_name = "-".join(
            part for part in (_clip(raw.get("license"), 40), _clip(raw.get("license_version"), 20)) if part
        )
        item_id = _clip(raw.get("id"), 160) or hashlib.sha1(thumbnail.encode()).hexdigest()
        result.append(
            {
                "provider": "openverse",
                "source_item_id": item_id,
                "source_page_url": _normalise_media_url(raw.get("foreign_landing_url")),
                "media_url": _normalise_media_url(raw.get("url")),
                "thumbnail_url": thumbnail,
                "download_url": thumbnail,
                "download_hosts": ("api.openverse.org",),
                "title": title,
                "description": description,
                "description_method": "source_metadata",
                "emotion": emotion,
                "tags": _tags((query, title, *raw_tags)),
                "creator": creator,
                "license_name": license_name or "open-license-unverified",
                "license_url": _normalise_media_url(raw.get("license_url")),
                "license_note": "Openverse 聚合的开放许可信息；使用前必须复核原始页面、许可范围和署名要求。",
            },
        )
    return result


def _discover_sample(query: str) -> list[dict]:
    request = {
        "max_bytes": MAX_HTML_BYTES,
        "timeout": 10,
        "headers": {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Referer": "https://www.bilibili.com/",
        },
    }
    body = _read_remote(SAMPLE_OFFICIAL_OPUS, ("www.bilibili.com",), **request)
    try:
        return parse_bilibili_opus(body.decode("utf-8", "replace"), query)
    except ValueError as exc:
        # Bilibili sometimes returns a small 200 interstitial to a datacenter
        # IP.  Retry the same fixed, validated URL through the loopback egress
        # proxy before reporting a provider failure.
        if str(exc) not in {"bilibili_initial_state_missing", "bilibili_initial_state_invalid"} or not _safe_discovery_proxy():
            raise
        body = _read_remote(
            SAMPLE_OFFICIAL_OPUS,
            ("www.bilibili.com",),
            **request,
            use_proxy=True,
        )
        return parse_bilibili_opus(body.decode("utf-8", "replace"), query)


def _discover_fan_reference(query: str) -> list[dict]:
    request = {
        "max_bytes": MAX_HTML_BYTES,
        "timeout": 10,
        "headers": {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Referer": "https://www.bilibili.com/",
        },
    }
    body = _read_remote(FAN_REFERENCE_OPUS, ("www.bilibili.com",), **request)
    return parse_bilibili_fan_reference(body.decode("utf-8", "replace"), query)


def _discover_openverse(query: str, limit: int) -> list[dict]:
    params = urllib.parse.urlencode({"q": query, "page_size": max(1, min(limit, MAX_DISCOVERY_LIMIT)), "mature": "false"})
    body = _read_remote(f"{OPENVERSE_API}?{params}", ("api.openverse.org",), max_bytes=MAX_HTML_BYTES, timeout=10)
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("openverse_response_invalid") from exc
    return normalise_openverse_results(payload, query)


def discover_candidates(query: str, provider: str, limit: int) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    warnings: list[str] = []
    wants_sample = any(token in query.casefold() for token in ("示例", "永雏", "sample"))
    if provider == "sample_official" or (provider == "auto" and wants_sample):
        try:
            items.extend(_discover_sample(query))
        except ValueError as exc:
            warnings.append(str(exc))
    if provider == "fan_reference":
        try:
            items.extend(_discover_fan_reference(query))
        except ValueError as exc:
            warnings.append(str(exc))
    if provider == "openverse" or (provider == "auto" and len(items) < limit):
        try:
            items.extend(_discover_openverse(query, limit - len(items)))
        except ValueError as exc:
            warnings.append(str(exc))
    unique: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("provider") or ""), str(item.get("source_item_id") or ""))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    if not unique and warnings:
        raise ValueError(warnings[0])
    return unique, warnings


def download_candidate(item: dict) -> bytes:
    hosts = tuple(str(host) for host in item.get("download_hosts") or ())
    if not hosts:
        raise ValueError("candidate_download_hosts_missing")
    return _read_remote(str(item.get("download_url") or ""), hosts, max_bytes=MAX_IMAGE_BYTES, timeout=10)


def _candidate_id(provider: str, source_item_id: str) -> str:
    digest = hashlib.sha256(f"{provider}\n{source_item_id}".encode("utf-8", "ignore")).hexdigest()
    return f"mec_{digest[:20]}"


def _safe_candidate_path(filename: str) -> Path:
    name = Path(str(filename or "")).name
    if name != str(filename or "") or not re.fullmatch(r"[a-f0-9]{20}\.(?:png|jpg|gif|webp)", name):
        raise ValueError("invalid_candidate_path")
    root = CANDIDATE_DIR.resolve()
    path = (root / name).resolve()
    if root not in path.parents:
        raise ValueError("invalid_candidate_path")
    return path


def _write_candidate(data: bytes, digest: str, suffix: str) -> Path:
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = _safe_candidate_path(f"{digest[:20]}{suffix}")
    if target.exists():
        return target
    handle, temp_name = tempfile.mkstemp(prefix=".candidate-", dir=str(CANDIDATE_DIR))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
    return target


def _upsert_candidate(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    item: dict,
    status: str,
    file_path: str = "",
    file_hash: str = "",
    mime_type: str = "",
    file_size: int = 0,
    duplicate_asset_id: str = "",
    error: str = "",
) -> dict:
    now = utc_now()
    provider = _clip(item.get("provider"), 80)
    source_item_id = _clip(item.get("source_item_id"), 240)
    candidate_id = _candidate_id(provider, source_item_id)
    conn.execute(
        """
        INSERT INTO meme_candidates(
            id, job_id, provider, source_item_id, source_page_url, media_url,
            thumbnail_url, title, description, description_method, emotion, tags,
            creator, license_name, license_url, license_note, file_path, file_hash,
            mime_type, file_size, status, duplicate_asset_id, error, seen_count,
            last_seen_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(provider, source_item_id) DO UPDATE SET
            job_id = excluded.job_id,
            source_page_url = excluded.source_page_url,
            media_url = excluded.media_url,
            thumbnail_url = excluded.thumbnail_url,
            title = excluded.title,
            description = excluded.description,
            description_method = excluded.description_method,
            emotion = excluded.emotion,
            tags = excluded.tags,
            creator = excluded.creator,
            license_name = excluded.license_name,
            license_url = excluded.license_url,
            license_note = excluded.license_note,
            file_path = CASE WHEN meme_candidates.status IN ('approved', 'rejected') THEN meme_candidates.file_path ELSE excluded.file_path END,
            file_hash = CASE WHEN meme_candidates.status IN ('approved', 'rejected') THEN meme_candidates.file_hash ELSE excluded.file_hash END,
            mime_type = CASE WHEN meme_candidates.status IN ('approved', 'rejected') THEN meme_candidates.mime_type ELSE excluded.mime_type END,
            file_size = CASE WHEN meme_candidates.status IN ('approved', 'rejected') THEN meme_candidates.file_size ELSE excluded.file_size END,
            status = CASE WHEN meme_candidates.status IN ('approved', 'rejected') THEN meme_candidates.status ELSE excluded.status END,
            duplicate_asset_id = CASE WHEN meme_candidates.status IN ('approved', 'rejected') THEN meme_candidates.duplicate_asset_id ELSE excluded.duplicate_asset_id END,
            error = CASE WHEN meme_candidates.status IN ('approved', 'rejected') THEN meme_candidates.error ELSE excluded.error END,
            seen_count = meme_candidates.seen_count + 1,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        (
            candidate_id,
            job_id,
            provider,
            source_item_id,
            _clip(item.get("source_page_url"), 1000),
            _clip(item.get("media_url"), 1000),
            _clip(item.get("thumbnail_url"), 1000),
            _normalise_title(item.get("title")),
            _plain_text(item.get("description"), 500),
            _clip(item.get("description_method"), 40) or "source_metadata",
            _clip(item.get("emotion"), 40) or "daily",
            _clip(item.get("tags"), 600),
            _clip(item.get("creator"), 160),
            _clip(item.get("license_name"), 120),
            _clip(item.get("license_url"), 1000),
            _clip(item.get("license_note"), 800),
            file_path,
            file_hash,
            mime_type,
            int(file_size or 0),
            status,
            _clip(duplicate_asset_id, 120),
            _clip(error, 300),
            now,
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM meme_candidates WHERE id = ?", (candidate_id,)).fetchone()
    return dict(row)


def run_meme_discovery(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    discoverer: Callable[[str, str, int], tuple[list[dict], list[str]]] = discover_candidates,
    downloader: Callable[[dict], bytes] = download_candidate,
) -> dict:
    ensure_meme_discovery_tables(conn)
    query = _plain_text(payload.get("query"), MAX_QUERY_CHARS)
    if len(query) < 2:
        raise ValueError("meme_query_too_short")
    provider = _clip(payload.get("provider"), 40) or "auto"
    if provider not in PROVIDER_IDS:
        raise ValueError("meme_provider_invalid")
    try:
        limit = int(payload.get("limit") or 8)
    except (TypeError, ValueError) as exc:
        raise ValueError("meme_limit_invalid") from exc
    limit = max(1, min(limit, MAX_DISCOVERY_LIMIT))
    now = utc_now()
    job_id = f"mdj_{uuid.uuid4().hex[:20]}"
    conn.execute(
        """
        INSERT INTO meme_discovery_jobs(
            id, query, provider, status, requested_limit, started_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (job_id, query, provider, limit, now, now, now),
    )
    conn.commit()
    try:
        items, warnings = discoverer(query, provider, limit)
    except Exception as exc:
        error = _clip(exc, 300) or "meme_discovery_failed"
        completed = utc_now()
        conn.execute(
            "UPDATE meme_discovery_jobs SET status='failed', error=?, completed_at=?, updated_at=? WHERE id=?",
            (error, completed, completed, job_id),
        )
        conn.commit()
        return discovery_state(conn, job_id=job_id)

    imported = duplicates = failed = 0
    selected_items = items[:limit]
    # Downloads are independent and bounded to four workers. This keeps an
    # eight-candidate search responsive without creating an unbounded crawler.
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(selected_items)))) as pool:
        downloads = [(item, pool.submit(downloader, item)) for item in selected_items]
    for item, future in downloads:
        try:
            data = future.result()
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ValueError("image_size_invalid")
            mime_type, suffix = _detect_image_type(data)
            digest = hashlib.sha256(data).hexdigest()
            asset = conn.execute("SELECT id FROM meme_assets WHERE file_hash = ? LIMIT 1", (digest,)).fetchone()
            duplicate_id = str(asset["id"]) if asset else ""
            if not duplicate_id:
                current_candidate_id = _candidate_id(
                    _clip(item.get("provider"), 80),
                    _clip(item.get("source_item_id"), 240),
                )
                other = conn.execute(
                    "SELECT id FROM meme_candidates WHERE file_hash = ? AND id <> ? LIMIT 1",
                    (digest, current_candidate_id),
                ).fetchone()
                duplicate_id = f"candidate:{other['id']}" if other else ""
            if duplicate_id:
                _upsert_candidate(
                    conn,
                    job_id=job_id,
                    item=item,
                    status="duplicate",
                    file_hash=digest,
                    mime_type=mime_type,
                    file_size=len(data),
                    duplicate_asset_id=duplicate_id,
                )
                duplicates += 1
                continue
            target = _write_candidate(data, digest, suffix)
            row = _upsert_candidate(
                conn,
                job_id=job_id,
                item=item,
                status="pending",
                file_path=str(target),
                file_hash=digest,
                mime_type=mime_type,
                file_size=len(data),
            )
            if row.get("status") == "duplicate":
                duplicates += 1
            else:
                imported += 1
        except Exception as exc:
            _upsert_candidate(
                conn,
                job_id=job_id,
                item=item,
                status="failed",
                error=_clip(exc, 300) or "candidate_import_failed",
            )
            failed += 1
    completed = utc_now()
    if failed or warnings:
        status = "partial" if imported or duplicates else "failed"
    elif not items:
        status = "empty"
    else:
        status = "succeeded"
    conn.execute(
        """
        UPDATE meme_discovery_jobs SET
            status=?, discovered_count=?, imported_count=?, duplicate_count=?,
            failed_count=?, error=?, completed_at=?, updated_at=?
        WHERE id=?
        """,
        (
            status,
            len(items),
            imported,
            duplicates,
            failed,
            _clip(",".join(warnings), 300),
            completed,
            completed,
            job_id,
        ),
    )
    conn.commit()
    return discovery_state(conn, job_id=job_id)


def _public_candidate(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    file_path = str(item.pop("file_path", "") or "")
    item["preview_url"] = ""
    if file_path:
        try:
            path = Path(file_path).resolve()
            if CANDIDATE_DIR.resolve() in path.parents and path.is_file():
                item["preview_url"] = f"/assistant/memes/discovery/candidate/{urllib.parse.quote(path.name)}"
        except OSError:
            pass
    return item


def _discovery_evidence(job: dict | None) -> list[dict]:
    if not job:
        return []
    provider = str(job.get("provider") or "auto")
    query = str(job.get("query") or "")
    if provider == "fan_reference":
        source_name = "Bilibili 二创表情候选合集（私用审核）"
        source_url = FAN_REFERENCE_OPUS
    elif provider == "sample_official" or (provider == "auto" and ("示例" in query or "sample" in query.casefold())):
        source_name = "永雏示例官方 Bilibili 公开动态"
        source_url = SAMPLE_OFFICIAL_OPUS
    else:
        source_name = "Openverse API"
        source_url = f"https://openverse.org/search/image?q={urllib.parse.quote(query)}"
    fetched_at = str(job.get("completed_at") or job.get("started_at") or utc_now())
    try:
        valid_until = (datetime.fromisoformat(fetched_at) + timedelta(hours=24)).isoformat()
    except ValueError:
        valid_until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    return [{
        "source_name": source_name,
        "source_url": source_url,
        "data_time": fetched_at,
        "fetched_at": fetched_at,
        "valid_until": valid_until,
    }]


def discovery_state(
    conn: sqlite3.Connection,
    *,
    job_id: str = "",
    job_limit: int = 20,
    candidate_limit: int = 120,
) -> dict:
    ensure_meme_discovery_tables(conn)
    jobs = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM meme_discovery_jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(job_limit or 20), 100)),),
        ).fetchall()
    ]
    candidates = [
        _public_candidate(row)
        for row in conn.execute(
            """
            SELECT * FROM meme_candidates
            ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 WHEN 'duplicate' THEN 2 ELSE 3 END,
                     updated_at DESC
            LIMIT ?
            """,
            (max(1, min(int(candidate_limit or 120), 300)),),
        ).fetchall()
    ]
    counts = {str(row["status"]): int(row["count"]) for row in conn.execute(
        "SELECT status, COUNT(*) AS count FROM meme_candidates GROUP BY status",
    ).fetchall()}
    selected_job = next((item for item in jobs if item["id"] == job_id), None) if job_id else (jobs[0] if jobs else None)
    return {
        "ok": True,
        "job": selected_job if job_id else None,
        "jobs": jobs,
        "candidates": candidates,
        "counts": counts,
        "providers": list_discovery_providers(),
        "evidence": _discovery_evidence(selected_job),
    }


def review_meme_candidate(conn: sqlite3.Connection, payload: dict) -> dict:
    ensure_meme_discovery_tables(conn)
    candidate_id = _clip(payload.get("candidate_id") or payload.get("id"), 80)
    decision = _clip(payload.get("decision"), 20).casefold()
    if decision not in {"approve", "reject"}:
        raise ValueError("meme_review_decision_invalid")
    row = conn.execute("SELECT * FROM meme_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("meme_candidate_not_found")
    candidate = dict(row)
    if decision == "reject":
        now = utc_now()
        conn.execute(
            "UPDATE meme_candidates SET status='rejected', reviewed_at=?, updated_at=? WHERE id=?",
            (now, now, candidate_id),
        )
        conn.commit()
        return discovery_state(conn)
    if candidate.get("status") == "approved" and candidate.get("asset_id"):
        return discovery_state(conn)
    if candidate.get("status") in {"duplicate", "failed"}:
        raise ValueError("meme_candidate_not_approvable")
    path = Path(str(candidate.get("file_path") or "")).resolve()
    if CANDIDATE_DIR.resolve() not in path.parents or not path.is_file():
        raise ValueError("meme_candidate_file_missing")
    data = path.read_bytes()
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image_size_invalid")
    digest = hashlib.sha256(data).hexdigest()
    if digest != candidate.get("file_hash"):
        raise ValueError("meme_candidate_hash_mismatch")
    duplicate = conn.execute("SELECT id FROM meme_assets WHERE file_hash = ? LIMIT 1", (digest,)).fetchone()
    if duplicate:
        now = utc_now()
        conn.execute(
            """
            UPDATE meme_candidates SET status='duplicate', duplicate_asset_id=?, reviewed_at=?, updated_at=?
            WHERE id=?
            """,
            (duplicate["id"], now, now, candidate_id),
        )
        conn.commit()
        return discovery_state(conn)
    enabled = str(payload.get("enabled", "0")).casefold() in {"1", "true", "yes", "on"}
    upload = {
        "data_base64": base64.b64encode(data).decode("ascii"),
        "name": _normalise_title(payload.get("name") or candidate.get("title")),
        "description": _plain_text(payload.get("description") or candidate.get("description"), 500),
        "description_method": _clip(candidate.get("description_method"), 40),
        "emotion": _clip(payload.get("emotion") or candidate.get("emotion"), 40) or "daily",
        "tags": _clip(payload.get("tags") or candidate.get("tags"), 600),
        "pack": _clip(payload.get("pack"), 80) or f"discovered-{candidate.get('provider')}",
        "creator": _clip(candidate.get("creator"), 160),
        "source": _clip(candidate.get("source_page_url"), 1000),
        "license_note": _clip(candidate.get("license_note"), 800),
        "license_url": _clip(candidate.get("license_url"), 1000),
        "cooldown_minutes": payload.get("cooldown_minutes") or 90,
        "max_daily": payload.get("max_daily") or 3,
        "review_status": "approved",
        "enabled": "1" if enabled else "0",
    }
    asset = meme_assets.save_uploaded_meme(conn, upload)
    now = utc_now()
    conn.execute(
        """
        UPDATE meme_candidates SET status='approved', asset_id=?, reviewed_at=?, updated_at=?
        WHERE id=?
        """,
        (asset["id"], now, now, candidate_id),
    )
    conn.commit()
    result = discovery_state(conn)
    result["asset"] = asset
    return result


def candidate_asset(filename: str) -> tuple[bytes, str] | None:
    try:
        path = _safe_candidate_path(urllib.parse.unquote(str(filename or "")))
        data = path.read_bytes()
        mime, _ = _detect_image_type(data)
        return data, mime
    except (OSError, ValueError):
        return None


__all__ = [
    "CANDIDATE_DIR",
    "candidate_asset",
    "discover_candidates",
    "discovery_state",
    "download_candidate",
    "ensure_meme_discovery_tables",
    "infer_emotion",
    "list_discovery_providers",
    "metadata_description",
    "normalise_openverse_results",
    "parse_bilibili_fan_reference",
    "parse_bilibili_opus",
    "review_meme_candidate",
    "run_meme_discovery",
    "validate_remote_url",
]
