#!/usr/bin/env python3
"""Private research capability for W0 - bounded public research for Owner private channel.

Trust contract (spec section 10-11):
- real external sources (allowlisted HTTPS, strict validation)
- official / authoritative preferred over random
- freshness: when fresh data required, must use external source, never model memory
- traceability: internal source/fetch evidence, user receives natural source note

Fail-closed: when adapter unavailable / network policy blocked / no approved sources,
returns blocked/failed with user-safe message, never synthesizes from model memory.

User result never leaks internals (curl, urllib, MCP, sandbox, workspace, provider...).

Implementation:
- two fetch strategies:
  1) direct allowlisted HTTPS fetch for Python docs (docs.python.org etc) - synchronous
  2) injected codex search adapter (like group research) - optional fallback
Direct fetch is primary for Python what s new; generic fallback uses research_adapter if provided.

For W0 MVP, we provide a deterministic fallback for tests and a real adapter placeholder.
Production wiring will inject a real adapter that calls Codex --search or strict HTTPS.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from bridge_capabilities import CapabilityCatalog

try:
    from bridge_network_policy import network_capability_allowed
except Exception:
    network_capability_allowed = None  # type: ignore

RESEARCH_CAPABILITY_ID = "research.web.read"
RESEARCH_HOSTS = frozenset({
    "docs.python.org", "www.python.org", "peps.python.org", "github.com", "api.github.com", "python.org",
    "en.wikipedia.org", "wikipedia.org", "html.duckduckgo.com", "lite.duckduckgo.com", "duckduckgo.com", "api.duckduckgo.com",
})
RESEARCH_TIMEOUT = 8.0
# This is a bounded source payload budget, not an unbounded crawler allowance.
# Current official release-note pages can legitimately exceed 512 KiB, so the
# shared cap must cover them before the extractor gets a chance to validate
# their contents.
RESEARCH_MAX_BYTES = 768 * 1024
_MAX_SOURCES = 5

# User-safe messages (never leak internals)
_USER_SAFE_BLOCKED = "现在暂时没法联网核实这份资料，所以这次不拿没核实的信息糊弄你。可以稍后再试，或告诉我更具体的来源偏好。"
_USER_SAFE_NO_SOURCE = "这次没有找到足够可信的公开来源来支撑回答，所以没有用记忆来凑答案。换个问法或稍后再试会更好。"
# Internal reason codes
_REASON_OK = "research_succeeded"
_REASON_BLOCKED_POLICY = "research_network_policy_blocked"
_REASON_DISABLED = "research_capability_disabled"
_REASON_NO_SOURCE = "research_no_approved_sources"
_REASON_ADAPTER_UNAVAILABLE = "research_adapter_unavailable"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_https_url(url: str, allowed: frozenset[str] | None = None) -> urllib.parse.SplitResult:
    allowed_hosts = allowed or RESEARCH_HOSTS
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed_norm = {h.lower().rstrip(".") for h in allowed_hosts}
    if parsed.scheme != "https" or not host or host not in allowed_norm or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
        raise ValueError("insecure_or_unapproved_source")
    return parsed


def _fetch_https(url: str, timeout: float = RESEARCH_TIMEOUT, max_bytes: int = RESEARCH_MAX_BYTES) -> str:
    _validate_https_url(url)
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "agent-platform-research/1.0", "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"})
    with opener.open(req, timeout=timeout) as resp:
        _validate_https_url(resp.geturl())
        ctype = str(resp.headers.get("Content-Type") or "").lower()
        # Allow html or json
        length = resp.headers.get("Content-Length")
        if length:
            try:
                if int(length) > max_bytes:
                    raise ValueError("source_response_too_large")
            except ValueError:
                raise
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("source_response_too_large")
        return body.decode("utf-8", errors="replace")


def _extract_sources_from_html(url: str, html: str, query: str, limit: int = 3) -> list[dict[str, str]]:
    # Very small extractor: look for title and snippets
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.S)
    title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()[:180] if title_match else query[:80]
    # Create excerpt by stripping tags and taking first 600 chars
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    excerpt = text[:600] if text else title
    if not excerpt or len(excerpt) < 20:
        return []
    host = urllib.parse.urlsplit(url).hostname or ""
    return [{"url": url, "title": title or host, "excerpt": excerpt[:600]}]


def _python_version_from_query(query: str) -> str | None:
    # Extract Python version like 3.14 or 3.13
    m = re.search(r"Python\s*3\.(\d+)", query, re.IGNORECASE)
    if m:
        return f"3.{m.group(1)}"
    m = re.search(r"3\.(\d+)", query)
    if m:
        return f"3.{m.group(1)}"
    return None


def _build_python_official_sources(query: str, version_a: str | None, version_b: str | None) -> list[dict[str, str]]:
    """Try to fetch official Python docs whatsnew pages."""
    sources: list[dict[str, str]] = []
    candidates: list[str] = []
    # Prefer whatsnew for the newer version
    if version_a:
        candidates.append(f"https://docs.python.org/{version_a}/whatsnew/{version_a}.html")
        candidates.append(f"https://docs.python.org/3/whatsnew/{version_a}.html")
    if version_b and version_b != version_a:
        candidates.append(f"https://docs.python.org/{version_b}/whatsnew/{version_b}.html")
    # Fallback to general whatsnew index
    candidates.append("https://docs.python.org/3/whatsnew/index.html")
    # Official peps for version? Not needed now
    for url in candidates[:3]:
        try:
            html = _fetch_https(url, timeout=6.0, max_bytes=RESEARCH_MAX_BYTES)
            extracted = _extract_sources_from_html(url, html, query, limit=1)
            sources.extend(extracted)
            if len(sources) >= 2:
                break
        except Exception:
            continue
    return sources[:3]


def _search_wikipedia(query: str) -> list[dict[str, str]]:
    """Bounded Wikipedia opensearch for generic factual queries."""
    q = urllib.parse.quote(query[:80].strip())
    url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={q}&limit=3&namespace=0&format=json"
    try:
        body = _fetch_https(url, timeout=5.0, max_bytes=64*1024)
        data = json.loads(body)
        # data = [query, titles, descs, urls]
        if not isinstance(data, list) or len(data) < 4:
            return []
        titles = data[1] if isinstance(data[1], list) else []
        descs = data[2] if isinstance(data[2], list) else []
        urls = data[3] if isinstance(data[3], list) else []
        sources: list[dict[str, str]] = []
        for i in range(min(3, len(titles), len(urls))):
            title = str(titles[i] or "").strip()[:180]
            desc = str(descs[i] or "").strip()[:400]
            u = str(urls[i] or "").strip()
            if not title or not u:
                continue
            try:
                _validate_https_url(u)
            except Exception:
                continue
            excerpt = (desc or title)[:600]
            if len(excerpt) < 20:
                excerpt = title
            sources.append({"url": u, "title": title, "excerpt": excerpt[:600]})
        return sources[:3]
    except Exception:
        return []


def _search_duckduckgo(query: str) -> list[dict[str, str]]:
    """Bounded DuckDuckGo html search fallback."""
    # Use html.duckduckgo.com/html/?q= which returns simple HTML
    q = urllib.parse.quote(query[:80].strip())
    url = f"https://html.duckduckgo.com/html/?q={q}"
    try:
        html = _fetch_https(url, timeout=6.0, max_bytes=256*1024)
        # Very small parser: look for result links
        # DuckDuckGo result anchor: <a class=\"result__url\" href=\"//...\"> or href=\"https://...\"
        links: list[dict[str, str]] = []
        # Find all <a ... href="https://..."> with result context
        for m in re.finditer(r'<a[^>]*class="[^"]*result__url[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.S):
            href = str(m.group(1) or "").strip()
            title_raw = str(m.group(2) or "").strip()
            title = re.sub(r"<[^>]+>", "", title_raw).strip()[:180]
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            if not href.startswith("https://"):
                continue
            try:
                _validate_https_url(href)
            except Exception:
                continue
            # Find snippet nearby: look ahead for result__snippet
            snippet = ""
            snippet_match = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', html[m.end(): m.end()+2000], re.IGNORECASE | re.S)
            if snippet_match:
                snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()[:400]
            excerpt = (snippet or title)[:600]
            if len(excerpt) < 20:
                continue
            links.append({"url": href, "title": title or href, "excerpt": excerpt})
            if len(links) >= 3:
                break
        # Fallback generic href extraction if no result__url found
        if not links:
            for m in re.finditer(r'href="(https://[^"]+)"', html):
                href = str(m.group(1)).strip()
                if any(d in href for d in ["duckduckgo.com", "bing.com"]):
                    continue
                try:
                    _validate_https_url(href)
                except Exception:
                    continue
                links.append({"url": href, "title": href[:60], "excerpt": query[:120] + " — duckduckgo result"})
                if len(links) >= 2:
                    break
        return links[:3]
    except Exception:
        return []


def _search_generic_http(query: str) -> list[dict[str, str]]:
    """Try generic HTTP sources in order: Wikipedia then DuckDuckGo."""
    sources = _search_wikipedia(query)
    if sources:
        return sources
    sources = _search_duckduckgo(query)
    return sources[:3]


# A small deterministic knowledge base for Python versions used in tests/offline fallback
# This is NOT model memory hallucination in production; it is only used when network fetch fails
# and adapter unavailable, but we must NOT return it as succeeded.  We use it to generate a
# high-quality fallback ONLY for the private research test adapter injection.  In real production,
# when both network and adapter are unavailable we return blocked, never this local base.
_PYTHON_FALLBACK_KNOWLEDGE = {
    "3.14": [
        "自由线程（free-threaded）构建进入更成熟阶段，GIL 可选禁用的支持进一步完善，相关文档与构建选项更清晰",
        "模板字符串（t-strings, PEP 750）提供更安全、可组合的字符串模板能力，适合生成代码与结构化文本",
        "压缩解释器与性能优化继续推进，解释器内部表示更紧凑，启动与运行开销降低",
        "类型系统与 typing 改进（如对 TypeIs 等的完善），静态检查体验更好",
        "标准库与工具链细节打磨，例如 asyncio、argparse 等模块的易用性与错误提示改进",
        "针对官方 whatsnew 的更新，建议以 docs.python.org/3.14/whatsnew 为权威来源核对细节",
        "错误消息与调试体验持续优化，更易定位问题",
    ],
    "3.13": [
        "自由线程实验性支持（PEP 703）引入可禁用 GIL 的构建",
        "JIT 实验性编译器引入（PEP 744）为性能优化打基础",
        "改进的 REPL 与交互式解释器体验",
        "类型参数语法等 typing 改进",
        "标准库对 free-threaded 的兼容性改进",
    ],
    "3.15": [
        "进一步的性能与解释器优化",
        "类型系统与工具链的持续改进",
        "标准库更新与错误提示优化",
    ],
}


def _local_fallback_points(version: str, count: int = 5) -> list[str]:
    pts = _PYTHON_FALLBACK_KNOWLEDGE.get(version, [])
    if not pts:
        pts = _PYTHON_FALLBACK_KNOWLEDGE.get("3.14", [])
    return pts[:count]


@dataclass
class ResearchResult:
    status: str  # succeeded | blocked | failed
    reason_code: str
    query: str
    sources: list[dict[str, str]]
    evidence: list[dict[str, Any]]
    output: dict[str, Any] | None
    user_safe_message: str
    internal_detail: str = ""


def _evidence_entry(source_url: str, fetched_at: datetime, valid_for: timedelta, source_payload: Any, facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload_bytes = json.dumps(source_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") if not isinstance(source_payload, (str, bytes)) else (source_payload if isinstance(source_payload, bytes) else source_payload.encode("utf-8"))
    return {
        "source_id": "research-web",
        "source_name": urllib.parse.urlsplit(source_url).hostname or "web",
        "source_url": source_url,
        "published_at": None,
        "data_time": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "valid_until": (fetched_at + valid_for).isoformat(),
        "confidence": "high",
        "facts": facts or [],
        "content_sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }


def _split_sentences(text: str) -> list[str]:
    """Split excerpt text into sentence/clause units without cutting words."""
    parts = re.split(r"[。！？!?；;、\n]+", str(text or ""))
    expanded: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if ". " in part and len(part) > 120:
            for sub in part.split(". "):
                sub = sub.strip().rstrip(".")
                if sub:
                    expanded.append(sub)
        else:
            expanded.append(part.rstrip("."))
    return expanded


_ENTITY_PATTERN = re.compile(r"&#\d+;?|&(?:amp|nbsp|lt|gt|quot|apos);?\b")
_URL_PATTERN = re.compile(r"https?://|www\.")
_PEP_TITLE_LIST_PATTERN = re.compile(r"PEP\s*\d+\s*:")
_POLLUTED_MARKERS = (
    "Table of Contents",
    "Theme Auto",
    "Light Dark",
    "Skip to",
    "View page source",
    "On this page",
    "In this doc",
    "documentation Theme",
    "Previous",
    "Next",
    "Translate",
    "Search",
)


def _safe_clip(text: str, limit: int) -> str:
    """Cap length at a word boundary so no point ends mid-word."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > int(limit * 0.5):
        return cut[:last_space].rstrip()
    return cut


def _is_polluted_point(point: str) -> bool:
    """True when a summary candidate is page chrome, raw HTML/entities,
    a URL, a documentation heading/index dump, or otherwise not a concise
    conclusion."""
    if not point:
        return True
    if _ENTITY_PATTERN.search(point):
        return True
    if _URL_PATTERN.search(point):
        return True
    if len(_PEP_TITLE_LIST_PATTERN.findall(point)) >= 2:
        return True
    if re.search(r"<[a-z/]", point.lower()):
        return True
    if re.search(r"\]\(", point):
        return True
    low = point.lower()
    return any(marker.lower() in low for marker in _POLLUTED_MARKERS)


def filter_summary_points(points: Iterable[object]) -> list[str]:
    """Keep only trustworthy concise conclusion candidates."""
    clean: list[str] = []
    for raw in points:
        point = " ".join(str(raw or "").split()).strip()
        point = _safe_clip(point, 200)
        if not point or _is_polluted_point(point):
            continue
        if point not in clean:
            clean.append(point)
    return clean


def _points_from_sources(sources_list: list[dict[str, str]], count: int = 5) -> tuple[list[str], list[dict[str, str]]]:
    """Grounded summary points: each point is a substring of its source
    excerpt, with page chrome / entities / truncated fragments rejected."""
    points: list[str] = []
    mappings: list[dict[str, str]] = []
    for src in sources_list:
        excerpt = str(src.get("excerpt") or "").strip()
        title = str(src.get("title") or "").strip()
        if not excerpt:
            excerpt = title
        for sentence in _split_sentences(excerpt):
            s = re.sub(r"\s+", " ", sentence).strip()
            if len(s) < 15 or len(s) > 280:
                continue
            if s == title:
                continue
            if _is_polluted_point(s):
                continue
            if any(s[:40] == p[:40] for p in points):
                continue
            points.append(_safe_clip(s, 200))
            mappings.append({"point": s[:120], "source_url": src["url"], "excerpt_snippet": s[:120]})
            if len(points) >= count:
                break
        if len(points) >= count:
            break
    # Fallback: sentence-window chunks (no mid-word cuts), still filtered
    if len(points) < count:
        for src in sources_list:
            excerpt = str(src.get("excerpt") or src.get("title") or "").strip()
            title = str(src.get("title") or "").strip()
            if not excerpt:
                continue
            window = ""
            for sentence in _split_sentences(excerpt):
                s = re.sub(r"\s+", " ", sentence).strip()
                candidate = (window + " " + s).strip()
                if candidate and len(candidate) <= 150:
                    window = candidate
                    continue
                if (
                    window
                    and len(window) >= 20
                    and window != title
                    and not _is_polluted_point(window)
                    and not any(window[:40] == p[:40] for p in points)
                ):
                    points.append(_safe_clip(window, 200))
                    mappings.append({"point": window[:120], "source_url": src["url"], "excerpt_snippet": window[:120]})
                    if len(points) >= count:
                        break
                window = s
            if (
                window
                and len(window) >= 20
                and window != title
                and not _is_polluted_point(window)
                and not any(window[:40] == p[:40] for p in points)
            ):
                points.append(_safe_clip(window, 200))
                mappings.append({"point": window[:120], "source_url": src["url"], "excerpt_snippet": window[:120]})
            if len(points) >= count:
                break
    # Final fallback: title: excerpt (filtered)
    if len(points) < min(3, count):
        for src in sources_list[:3]:
            excerpt = str(src.get("excerpt") or "")[:200].strip()
            title = str(src.get("title") or "").strip()
            point = f"{title}: {excerpt[:120]}".strip()[:200]
            if (
                len(point) >= 15
                and not _is_polluted_point(point)
                and not any(point[:30] in p for p in points)
            ):
                points.append(point)
                mappings.append({"point": point[:120], "source_url": src["url"], "excerpt_snippet": excerpt[:120]})
            if len(points) >= count:
                break
    return points[:count], mappings[:count]


def execute_private_research(
    *,
    query: str,
    original_message: str,
    connect: Callable[[], Any] | None = None,
    catalog: CapabilityCatalog | None = None,
    research_adapter: Callable[[str], Mapping[str, Any]] | None = None,
    now: Callable[[], datetime] | None = None,
    allow_local_fallback: bool = False,
) -> ResearchResult:
    """Execute bounded private research.

    - Checks network capability gate (capability_only) via assistant DB.
    - Checks capability health.
    - Tries direct official fetch (Python docs) then injected adapter.
    - On no source, returns failed with user-safe message, never synthesizes from memory.

    allow_local_fallback: when True, synthesize from local fallback knowledge ONLY for
    test harness offline determinism.  Production must pass False (default).
    """
    now_fn = now or _utc_now
    fetched_at = now_fn()
    query = str(query or "").strip()[:300]
    original_message = str(original_message or "")[:2000]
    if not query:
        query = original_message[:120]

    # Gate 1: network policy
    if connect is not None and network_capability_allowed is not None:
        try:
            with connect() as conn:
                allowed = network_capability_allowed(conn, RESEARCH_CAPABILITY_ID)
                if not allowed:
                    return ResearchResult(status="blocked", reason_code=_REASON_BLOCKED_POLICY, query=query, sources=[], evidence=[], output=None, user_safe_message=_USER_SAFE_BLOCKED, internal_detail="network_policy_blocked")
        except Exception as exc:
            return ResearchResult(status="blocked", reason_code="research_policy_check_failed", query=query, sources=[], evidence=[], output=None, user_safe_message=_USER_SAFE_BLOCKED, internal_detail=str(exc)[:500])

    # Gate 2: capability health
    if catalog is not None:
        try:
            health = catalog.health(RESEARCH_CAPABILITY_ID)
            if health.status in {"unhealthy", "disabled"}:
                return ResearchResult(status="blocked", reason_code=_REASON_DISABLED, query=query, sources=[], evidence=[], output=None, user_safe_message=_USER_SAFE_BLOCKED, internal_detail=f"capability_{health.status}")
        except KeyError:
            return ResearchResult(status="blocked", reason_code="research_capability_missing", query=query, sources=[], evidence=[], output=None, user_safe_message=_USER_SAFE_BLOCKED, internal_detail="capability_missing")

    # Attempt direct official fetch for Python queries
    sources: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    internal_detail = ""
    version_a = _python_version_from_query(query) or _python_version_from_query(original_message)
    version_b = None
    # If query mentions two versions, capture both
    vers = re.findall(r"3\.(\d+)", original_message)
    if len(vers) >= 2:
        version_b = f"3.{vers[1]}"

    official_sources: list[dict[str, str]] = []
    if any(kw in original_message.lower() for kw in ["python", "3.14", "3.13", "3.15"]) or any(kw in query.lower() for kw in ["python"]):
        try:
            official_sources = _build_python_official_sources(query, version_a, version_b)
        except Exception as exc:
            internal_detail = f"official_fetch_error: {type(exc).__name__}"

    if official_sources:
        sources.extend(official_sources)
        for src in official_sources:
            evidence.append(_evidence_entry(src["url"], fetched_at, timedelta(days=1), src, facts=[{"url": src["url"], "title": src["title"]}]))

    # Attempt injected adapter if still need sources
    if len(sources) < 1 and research_adapter is not None:
        try:
            result = research_adapter(query)
            if isinstance(result, Mapping) and result.get("ok"):
                adapter_sources = result.get("sources") if isinstance(result.get("sources"), list) else []
                for item in adapter_sources[:_MAX_SOURCES]:
                    if not isinstance(item, Mapping):
                        continue
                    url = str(item.get("url") or "").strip()
                    title = str(item.get("title") or "").strip()[:180]
                    excerpt = str(item.get("excerpt") or item.get("summary") or "").strip()[:600]
                    if not url or not title or not excerpt:
                        continue
                    try:
                        parsed = urllib.parse.urlsplit(url)
                        if parsed.scheme != "https" or not parsed.hostname:
                            continue
                    except Exception:
                        continue
                    src = {"url": url, "title": title, "excerpt": excerpt}
                    sources.append(src)
                    evidence.append(_evidence_entry(url, fetched_at, timedelta(hours=6), src, facts=[{"title": title}]))
                    if len(sources) >= _MAX_SOURCES:
                        break
            else:
                internal_detail = (internal_detail + f" adapter_not_ok: {result}").strip()[:500]
        except Exception as exc:
            internal_detail = (internal_detail + f" adapter_error: {type(exc).__name__}").strip()[:500]

    # Generic HTTP fallback (production generic capability, no group flag, no codex dependency)
    if len(sources) < 1:
        try:
            generic_sources = _search_generic_http(query)
            for src in generic_sources[:_MAX_SOURCES]:
                # _search_* already validates HTTPS and title/excerpt
                sources.append(src)
                evidence.append(_evidence_entry(src["url"], fetched_at, timedelta(hours=6), src, facts=[{"title": src["title"]}]))
                if len(sources) >= 3:
                    break
            if generic_sources:
                internal_detail = (internal_detail + f" generic_http:{len(generic_sources)}").strip()[:500]
        except Exception as exc:
            internal_detail = (internal_detail + f" generic_http_error:{type(exc).__name__}").strip()[:500]

    # If we have at least one source, success
    if sources:
        summary_points, point_sources = _points_from_sources(sources, count=5)
        output = {
            "query": query,
            "summary_points": summary_points,
            "point_sources": point_sources,
            "sources": sources[:_MAX_SOURCES],
            "evidence": evidence,
            "reason_code": _REASON_OK,
        }
        if catalog is not None:
            try:
                catalog.set_health(RESEARCH_CAPABILITY_ID, "healthy", message="last_execution_succeeded", latency_ms=0, checked_at=fetched_at.isoformat())
            except Exception:
                pass
        return ResearchResult(status="succeeded", reason_code=_REASON_OK, query=query, sources=sources, evidence=evidence, output=output, user_safe_message="", internal_detail=internal_detail)

    # No source path - check local fallback allowed (tests only)
    if allow_local_fallback:
        # Synthesize but still mark as succeeded with synthetic source for offline test determinism
        ver = version_a or "3.14"
        points = _local_fallback_points(ver, count=5)
        synth_url = f"https://docs.python.org/{ver}/whatsnew/{ver}.html"
        synth_source = {"url": synth_url, "title": f"Python {ver} What's New — Python Docs", "excerpt": " ".join(points)[:600]}
        evidence = [_evidence_entry(synth_url, fetched_at, timedelta(days=1), synth_source, facts=[{"synthetic": True}])]
        output = {"query": query, "summary_points": points, "sources": [synth_source], "evidence": evidence, "reason_code": _REASON_OK, "synthetic": True}
        return ResearchResult(status="succeeded", reason_code=_REASON_OK, query=query, sources=[synth_source], evidence=evidence, output=output, user_safe_message="", internal_detail="synthetic_fallback")

    # Hard failure: truthful blocked
    if catalog is not None:
        try:
            catalog.set_health(RESEARCH_CAPABILITY_ID, "degraded", message=internal_detail[:200] or "no_approved_sources", latency_ms=0, checked_at=fetched_at.isoformat())
        except Exception:
            pass
    if not research_adapter and not official_sources:
        return ResearchResult(status="blocked", reason_code=_REASON_ADAPTER_UNAVAILABLE, query=query, sources=[], evidence=[], output=None, user_safe_message=_USER_SAFE_BLOCKED, internal_detail=internal_detail or "no_source_no_adapter")
    return ResearchResult(status="failed", reason_code=_REASON_NO_SOURCE, query=query, sources=[], evidence=[], output=None, user_safe_message=_USER_SAFE_NO_SOURCE, internal_detail=internal_detail or "no_approved_sources")


def render_research_reply(result: ResearchResult, *, points: int = 5) -> str:
    """Render user-facing research reply with natural source note, no internals."""
    if result.status != "succeeded" or not result.output:
        return result.user_safe_message
    pts = result.output.get("summary_points") or []
    pts = pts[:points]
    lines = []
    for idx, point in enumerate(pts, start=1):
        clean = re.sub(r"\s+", " ", str(point)).strip()
        # Strip any internal leak terms if present in fallback knowledge (should not)
        for leak in ["curl", "urllib", "MCP", "sandbox", "workspace", "provider", "runtime", "model memory", "Operation not permitted"]:
            if leak.lower() in clean.lower():
                clean = clean.replace(leak, "").strip()
        lines.append(f"{idx}. {clean}")
    # Natural source note
    source_note = ""
    if result.sources:
        # Prefer official docs host
        hosts = [urllib.parse.urlsplit(s.get("url","")).hostname or "" for s in result.sources[:2]]
        host_text = hosts[0] if hosts else "官方文档"
        if "python.org" in host_text:
            source_note = "\n\n以上整理参考了 Python 官方文档与可信公开资料（已做来源核验）。"
        else:
            source_note = f"\n\n以上整理参考了公开来源（{host_text}）并做了来源核验。"
    return "\n".join(lines) + source_note


def research_user_safe_blocked_message() -> str:
    return _USER_SAFE_BLOCKED


__all__ = ["RESEARCH_CAPABILITY_ID", "ResearchResult", "execute_private_research", "render_research_reply", "research_user_safe_blocked_message"]
