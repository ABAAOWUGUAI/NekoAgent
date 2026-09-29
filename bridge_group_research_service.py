#!/usr/bin/env python3
"""R7-B/R7-C bounded research for a live QQ group topic.

This is intentionally not a general web-search wrapper.  The caller supplies
only the current anchor text; this module first decides whether a *public,
factual and low-impact* question may use the isolated research adapter.  It
persists a redacted query and public-source packet, never the source group
message or a transcript.  The feature and its one-group pilot are disabled by
default.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bridge_group_research_schema import (
    GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG,
    GROUP_RESEARCH_FEATURE_FLAG,
    GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE,
    GROUP_RESEARCH_POLICY_TABLE,
    GROUP_RESEARCH_RUN_TABLE,
    GROUP_RESEARCH_SCOPE_MODE_ADMITTED,
    GROUP_RESEARCH_SCOPE_MODE_SINGLE,
    GROUP_RESEARCH_SCOPE_MODES,
    require_group_research_scope_schema,
)


LOW_PUBLIC = "low_public"
REVIEW_REQUIRED = "review_required"
RESTRICTED_PRIVATE = "restricted_private"
PROHIBITED = "prohibited"
AUTO_RESEARCH_ACTOR = "group-research-auto"
_UTC = timezone.utc
_PHONE = re.compile(r"(?<!\d)(?:1\d{10}|\+?\d[\d -]{7,}\d)(?!\d)")
_QQ_OR_ID = re.compile(r"(?i)(?:qq|群号|身份证|证件号|账号)\s*[:：#]?\s*\d{5,}")
_MENTION = re.compile(r"@[\w\-\u3400-\u9fff]{1,80}")
_SECRET = re.compile(
    r"(?i)(?:api[_ -]?key|token|password|passwd|secret|private[_ -]?key|cookie|"
    r"access[_ -]?key|sk-[A-Za-z0-9_-]{8,})",
)
_PRIVATE = re.compile(
    r"(?:聊天记录|私聊|住址|地址|手机号|电话|身份证|病历|病例|薪资|工资|"
    r"家庭住址|学校档案|公司内部|内部项目|个人资料|隐私)",
    re.IGNORECASE,
)
_PROMPT_INJECTION = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions|"
    r"system\s*prompt|开发者指令|忽略(?:之前|上文|所有)?指令)",
)
_HIGH_IMPACT = re.compile(
    r"(?:诊断|疾病|治疗|用药|药物|处方|法律|起诉|合同|税务|投资|股票|基金|理财|"
    r"加密货币|金融|漏洞|攻击|绕过|渗透|安全事件|政治|选举|政策)",
    re.IGNORECASE,
)
_FACTUAL_CUE = re.compile(
    r"(?:[?？]|是什么|是谁|哪里|何时|什么时候|为什么|怎么(?:样|办)?|如何|是否|"
    r"版本|发布|价格|规则|定义|来源|新闻|最新)",
    re.IGNORECASE,
)
_PUBLIC_SUBJECT = re.compile(
    r"(?:[a-z][a-z0-9._/-]{2,}|官网|公开资料|开源|软件|产品|论文|标准|发布公告|官方文档)",
    re.IGNORECASE,
)
_SOCIAL_OR_DEICTIC_CONTEXT = re.compile(
    r"(?:(?:我|我们|咱们|你|你们|他|她|他们|她们)|"
    r"群里|本群|刚才|上面|前面|这条|那条|这个人|那个人|私下|聊天|"
    r"我朋友|我同事|我家|我公司|我们公司|我学校|本人)",
    re.IGNORECASE,
)
_OPAQUE_LONG_NUMBER = re.compile(r"(?<!\d)\d{5,}(?!\d)")
_MULTI_LABEL_PUBLIC_SUFFIXES = {
    "ac.cn", "com.au", "com.br", "com.cn", "com.hk", "com.mx", "com.sg",
    "co.jp", "co.kr", "co.uk", "edu.cn", "gov.cn", "net.cn", "org.cn",
    "org.uk",
}


def _now(value=None) -> datetime:
    if value is None:
        return datetime.now(_UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _stamp(value=None) -> str:
    return _now(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
    ).fetchone()
    if not row:
        raise ValueError("active_assistant_required")
    return str(row[0])


def _flag(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM assistant_feature_flags WHERE name=?", (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and int(row[0]))


def _clip(value: object, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _public_query(raw: str) -> str:
    """Derive an external-search query without reusing a group utterance.

    This is deliberately a narrow allowlist, not an LLM judgement.  A message
    that includes a person, group-local reference, long opaque identifier or
    multiple conversational clauses has no safe public query and must remain
    local.  The returned phrase is transient: it is never written to an audit
    row, Knowledge item or retrieval audit.
    """

    if _SOCIAL_OR_DEICTIC_CONTEXT.search(raw) or _OPAQUE_LONG_NUMBER.search(raw):
        return ""
    query = _MENTION.sub("", raw)
    query = re.sub(
        r"^(?:(?:当前助手|助手)[，,:：\s]*)?(?:请问|想问|请帮我|能不能|有人知道|麻烦(?:你)?|帮忙)[，,:：\s]*",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = _clip(query, 80).strip("。！？?；;，, ")
    if not query or re.search(r"[。！？?；;][^。！？?；;]+", query):
        return ""
    if _SECRET.search(query) or _PHONE.search(query) or _QQ_OR_ID.search(query) or _PRIVATE.search(query):
        return ""
    if not _PUBLIC_SUBJECT.search(query):
        return ""
    return query


def _public_query_audit_label(query: object) -> str:
    """Return the only durable projection of a public search query."""

    digest = _hash(query)
    return "public-query:" + digest[:20] if str(query or "").strip() else ""


def _policy_public(row: Mapping[str, object]) -> dict:
    scope_mode = str(row.get("scope_mode") or GROUP_RESEARCH_SCOPE_MODE_SINGLE)
    if scope_mode not in GROUP_RESEARCH_SCOPE_MODES:
        scope_mode = GROUP_RESEARCH_SCOPE_MODE_SINGLE
    return {
        "enabled": bool(int(row.get("enabled") or 0)),
        "scope_mode": scope_mode,
        "pilot_group_id": str(row.get("pilot_group_id") or ""),
        "pilot_group_configured": bool(
            scope_mode == GROUP_RESEARCH_SCOPE_MODE_SINGLE
            and str(row.get("pilot_group_id") or "")
        ),
        "admitted_groups_dynamic": scope_mode == GROUP_RESEARCH_SCOPE_MODE_ADMITTED,
        "auto_publish_low_public": bool(int(row.get("auto_publish_low_public") or 0)),
        "max_runs_per_day": int(row.get("max_runs_per_day") or 0),
        "max_runs_per_group_day": int(row.get("max_runs_per_group_day") or 0),
        "freshness_hours": int(row.get("freshness_hours") or 0),
        "version": int(row.get("version") or 1),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _scope_mode(row: Mapping[str, object]) -> str:
    mode = str(row.get("scope_mode") or GROUP_RESEARCH_SCOPE_MODE_SINGLE)
    return mode if mode in GROUP_RESEARCH_SCOPE_MODES else GROUP_RESEARCH_SCOPE_MODE_SINGLE


def _group_is_currently_admitted(conn: sqlite3.Connection, group_id: object) -> bool:
    """Read the QQ allowlist at the authorization point, never from a copy."""

    group = _clip(group_id, 180)
    if not group:
        return False
    try:
        row = conn.execute(
            """SELECT enabled FROM qq_access_entries
               WHERE subject_type='qq_group' AND subject_id=?""",
            (group,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and int(row[0]))


def _scope_authorizes_group(
    conn: sqlite3.Connection,
    policy: Mapping[str, object],
    group_id: object,
) -> tuple[bool, str]:
    group = _clip(group_id, 180)
    if not group:
        return False, "research_group_not_pilot"
    mode = _scope_mode(policy)
    if mode == GROUP_RESEARCH_SCOPE_MODE_SINGLE:
        if group != str(policy.get("pilot_group_id") or ""):
            return False, "research_group_not_pilot"
    if not _group_is_currently_admitted(conn, group):
        return False, "research_group_not_admitted"
    return True, ""


def _has_currently_admitted_group(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            """SELECT 1 FROM qq_access_entries
               WHERE subject_type='qq_group' AND enabled=1 LIMIT 1""",
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row)


def get_group_research_policy(conn: sqlite3.Connection) -> dict:
    require_group_research_scope_schema(conn)
    assistant_id = _active_assistant_id(conn)
    row = conn.execute(
        "SELECT * FROM group_research_policies WHERE assistant_id=?", (assistant_id,),
    ).fetchone()
    if not row:
        raise ValueError("group_research_policy_missing")
    policy = _policy_public(dict(row))
    # A policy can be prepared before a rollout. Expose the independent runtime
    # switches so a saved pilot never implies that external research is live.
    policy.update({
        "feature_enabled": _flag(conn, GROUP_RESEARCH_FEATURE_FLAG),
        "autoknowledge_feature_enabled": _flag(
            conn, GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG,
        ),
    })
    return policy


def group_research_execution_allowed(conn: sqlite3.Connection, group_id: object) -> bool:
    """Recheck the opt-in policy immediately before an external tool call."""

    require_group_research_scope_schema(conn)
    assistant_id = _active_assistant_id(conn)
    row = conn.execute(
        "SELECT enabled,pilot_group_id,scope_mode FROM group_research_policies WHERE assistant_id=?",
        (assistant_id,),
    ).fetchone()
    return bool(
        _flag(conn, GROUP_RESEARCH_FEATURE_FLAG)
        and row
        and int(row["enabled"])
        and _scope_authorizes_group(conn, dict(row), group_id)[0]
    )


def set_group_research_policy(
    conn: sqlite3.Connection,
    *,
    enabled: object | None = None,
    scope_mode: object | None = None,
    pilot_group_id: object | None = None,
    auto_publish_low_public: object | None = None,
    max_runs_per_day: object | None = None,
    max_runs_per_group_day: object | None = None,
    freshness_hours: object | None = None,
    expected_version: object | None = None,
    actor: str = "owner",
) -> dict:
    """Persist an owner-controlled pilot policy; all omitted fields stay put."""

    require_group_research_scope_schema(conn)
    assistant_id = _active_assistant_id(conn)
    current = conn.execute(
        "SELECT * FROM group_research_policies WHERE assistant_id=?", (assistant_id,),
    ).fetchone()
    if not current:
        raise ValueError("group_research_policy_missing")
    current = dict(current)
    if expected_version not in (None, "") and int(expected_version) != int(current["version"]):
        raise ValueError("group_research_policy_version_conflict")

    def boolean(value: object, fallback: int) -> int:
        if value is None:
            return int(fallback)
        if value in (True, 1, "1", "true", "True", "on"):
            return 1
        if value in (False, 0, "0", "false", "False", "off"):
            return 0
        raise ValueError("group_research_policy_boolean_invalid")

    def bounded(value: object | None, fallback: int, lower: int, upper: int) -> int:
        if value is None:
            return int(fallback)
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("group_research_policy_limit_invalid") from exc
        if not lower <= parsed <= upper:
            raise ValueError("group_research_policy_limit_invalid")
        return parsed

    next_enabled = boolean(enabled, int(current["enabled"]))
    next_auto = boolean(auto_publish_low_public, int(current["auto_publish_low_public"]))
    next_scope = str(
        scope_mode if scope_mode is not None else current.get("scope_mode") or GROUP_RESEARCH_SCOPE_MODE_SINGLE,
    ).strip()
    if next_scope not in GROUP_RESEARCH_SCOPE_MODES:
        raise ValueError("group_research_scope_mode_invalid")
    next_pilot = (
        _clip(pilot_group_id, 180)
        if pilot_group_id is not None else str(current["pilot_group_id"] or "")
    )
    if next_scope == GROUP_RESEARCH_SCOPE_MODE_ADMITTED:
        # No stale membership or historical single-pilot identifier remains
        # authoritative when the scope is the live QQ allowlist.
        next_pilot = ""
    if next_enabled and next_scope == GROUP_RESEARCH_SCOPE_MODE_SINGLE and not next_pilot:
        raise ValueError("group_research_pilot_group_required")
    if next_enabled and next_scope == GROUP_RESEARCH_SCOPE_MODE_ADMITTED and not _has_currently_admitted_group(conn):
        raise ValueError("group_research_admitted_scope_empty")
    if next_auto and not next_enabled:
        raise ValueError("group_research_auto_publish_requires_enabled")
    next_limits = (
        bounded(max_runs_per_day, int(current["max_runs_per_day"]), 0, 20),
        bounded(max_runs_per_group_day, int(current["max_runs_per_group_day"]), 0, 10),
        bounded(freshness_hours, int(current["freshness_hours"]), 1, 720),
    )
    if next_enabled and (not next_limits[0] or not next_limits[1]):
        raise ValueError("group_research_positive_quota_required")
    now = _stamp()
    changed = conn.execute(
        """UPDATE group_research_policies
           SET enabled=?,scope_mode=?,pilot_group_id=?,auto_publish_low_public=?,max_runs_per_day=?,
               max_runs_per_group_day=?,freshness_hours=?,version=version+1,updated_by=?,updated_at=?
           WHERE assistant_id=? AND version=?""",
        (
            next_enabled, next_scope, next_pilot, next_auto, *next_limits, _clip(actor, 160) or "owner", now,
            assistant_id, int(current["version"]),
        ),
    ).rowcount
    if changed != 1:
        raise ValueError("group_research_policy_version_conflict")
    conn.commit()
    return get_group_research_policy(conn)


def set_group_research_feature_flags(
    conn: sqlite3.Connection,
    *,
    feature_enabled: object | None = None,
    autoknowledge_feature_enabled: object | None = None,
) -> dict:
    """Set the two rollout switches through the Owner API, fail closed."""

    require_group_research_scope_schema(conn)
    assistant_id = _active_assistant_id(conn)
    policy = conn.execute(
        """SELECT enabled,pilot_group_id,scope_mode,auto_publish_low_public
           FROM group_research_policies WHERE assistant_id=?""",
        (assistant_id,),
    ).fetchone()
    if not policy:
        raise ValueError("group_research_policy_missing")

    def boolean(value: object | None, fallback: bool) -> bool:
        if value is None:
            return fallback
        if value in (True, 1, "1", "true", "True", "on"):
            return True
        if value in (False, 0, "0", "false", "False", "off"):
            return False
        raise ValueError("group_research_policy_boolean_invalid")

    next_feature = boolean(feature_enabled, _flag(conn, GROUP_RESEARCH_FEATURE_FLAG))
    next_auto = boolean(
        autoknowledge_feature_enabled,
        _flag(conn, GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG),
    )
    if next_feature and not int(policy["enabled"]):
        raise ValueError("group_research_feature_requires_enabled_scope")
    if next_feature and _scope_mode(dict(policy)) == GROUP_RESEARCH_SCOPE_MODE_SINGLE and not str(policy["pilot_group_id"] or ""):
        raise ValueError("group_research_feature_requires_enabled_pilot")
    if next_feature and _scope_mode(dict(policy)) == GROUP_RESEARCH_SCOPE_MODE_ADMITTED and not _has_currently_admitted_group(conn):
        raise ValueError("group_research_feature_requires_admitted_group")
    if not next_feature:
        # There is no meaningful auto-retention state while research is off.
        next_auto = False
    if next_auto and not int(policy["auto_publish_low_public"]):
        raise ValueError("group_research_autoknowledge_requires_policy")

    now = _stamp()
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
        (int(next_feature), now, GROUP_RESEARCH_FEATURE_FLAG),
    )
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
        (int(next_auto), now, GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG),
    )
    conn.commit()
    return get_group_research_policy(conn)


def list_group_research_runs(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    """Return the owner-facing body-free research audit projection."""

    require_group_research_scope_schema(conn)
    assistant_id = _active_assistant_id(conn)
    rows = conn.execute(
        """SELECT id,group_id,anchor_message_id,query_redacted,query_hash,risk_tier,stage,
                  reason_code,source_count,result_summary,expires_at,created_at,updated_at
           FROM group_research_runs WHERE assistant_id=?
           ORDER BY created_at DESC,id DESC LIMIT ?""",
        (assistant_id, max(1, min(int(limit or 50), 200))),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_research_topic(message: object) -> dict:
    """Classify a group utterance without retaining it in durable state."""

    raw = _clip(message, 700)
    if not raw:
        return {"risk_tier": RESTRICTED_PRIVATE, "eligible": False, "reason_code": "research_empty_topic", "query": ""}
    if _SECRET.search(raw) or _PROMPT_INJECTION.search(raw):
        return {"risk_tier": PROHIBITED, "eligible": False, "reason_code": "research_prohibited_content", "query": ""}
    if _PHONE.search(raw) or _QQ_OR_ID.search(raw) or _MENTION.search(raw) or _PRIVATE.search(raw):
        return {"risk_tier": RESTRICTED_PRIVATE, "eligible": False, "reason_code": "research_private_context", "query": ""}
    if _HIGH_IMPACT.search(raw):
        query = _public_query(raw) if _PUBLIC_SUBJECT.search(raw) else ""
        return {"risk_tier": REVIEW_REQUIRED, "eligible": False, "reason_code": "research_review_required", "query": query}
    if not _FACTUAL_CUE.search(raw):
        return {"risk_tier": RESTRICTED_PRIVATE, "eligible": False, "reason_code": "research_not_factual", "query": ""}
    if not _PUBLIC_SUBJECT.search(raw):
        return {"risk_tier": RESTRICTED_PRIVATE, "eligible": False, "reason_code": "research_public_subject_unproven", "query": ""}
    query = _public_query(raw)
    if len(query) < 4:
        return {
            "risk_tier": RESTRICTED_PRIVATE,
            "eligible": False,
            "reason_code": "research_public_query_unproven",
            "query": "",
        }
    return {"risk_tier": LOW_PUBLIC, "eligible": True, "reason_code": "research_low_public", "query": query}


def _record_run(
    conn: sqlite3.Connection,
    *,
    assistant_id: str,
    group_id: str,
    anchor_message_id: object,
    query: str,
    risk_tier: str,
    stage: str,
    reason_code: str,
    expires_at: str = "",
    result_summary: str = "",
) -> dict:
    now = _stamp()
    run = {
        "id": "group-research-" + uuid.uuid4().hex,
        "assistant_id": assistant_id,
        "group_id": _clip(group_id, 180),
        "anchor_message_id": _clip(anchor_message_id, 180),
        # The actual query is transient data supplied only to the injected
        # adapter.  This record must not become a shortened group utterance.
        "query_redacted": _public_query_audit_label(query),
        "query_hash": _hash(query),
        "risk_tier": risk_tier,
        "stage": stage,
        "reason_code": _clip(reason_code, 120),
        "source_count": 0,
        "result_summary": _clip(result_summary, 500),
        "expires_at": expires_at,
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(
        """INSERT INTO group_research_runs(
            id,assistant_id,group_id,anchor_message_id,query_redacted,query_hash,risk_tier,
            stage,reason_code,source_count,result_summary,expires_at,created_at,updated_at
        ) VALUES(:id,:assistant_id,:group_id,:anchor_message_id,:query_redacted,:query_hash,:risk_tier,
                 :stage,:reason_code,:source_count,:result_summary,:expires_at,:created_at,:updated_at)""",
        run,
    )
    return run


def _quota_available(
    conn: sqlite3.Connection,
    *,
    assistant_id: str,
    group_id: str,
    policy: Mapping[str, object],
    now: datetime,
) -> bool:
    day_prefix = now.date().isoformat() + "%"
    relevant = "stage IN ('running','succeeded','failed') AND created_at LIKE ?"
    all_count = int(conn.execute(
        f"SELECT count(*) FROM {GROUP_RESEARCH_RUN_TABLE} WHERE assistant_id=? AND {relevant}",
        (assistant_id, day_prefix),
    ).fetchone()[0])
    group_count = int(conn.execute(
        f"SELECT count(*) FROM {GROUP_RESEARCH_RUN_TABLE} WHERE assistant_id=? AND group_id=? AND {relevant}",
        (assistant_id, group_id, day_prefix),
    ).fetchone()[0])
    return (
        all_count < int(policy["max_runs_per_day"])
        and group_count < int(policy["max_runs_per_group_day"])
    )


def _existing_fresh_run(
    conn: sqlite3.Connection,
    *,
    assistant_id: str,
    group_id: str,
    query_hash: str,
    now: datetime,
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM group_research_runs
           WHERE assistant_id=? AND group_id=? AND query_hash=? AND stage='succeeded'
                 AND expires_at>? LIMIT 1""",
        (assistant_id, group_id, query_hash, _stamp(now)),
    ).fetchone()
    return bool(row)


def _safe_source(value: object) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    url = _clip(value.get("url"), 500)
    parsed = urlparse(url)
    domain = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not domain or len(domain) > 180:
        return None
    title = _clip(value.get("title"), 180)
    excerpt = _clip(value.get("excerpt") or value.get("summary"), 600)
    if not title or not excerpt:
        return None
    return {
        "url": url,
        "domain": domain,
        "title": title,
        "excerpt": excerpt,
        "hash": _hash("\x1f".join((url, title, excerpt))),
    }


def _registrable_domain(value: object) -> str:
    """Return a conservative registrable-domain key for source diversity.

    URL hostnames alone are too weak: two CDN or subdomain URLs can describe
    the same publisher.  This standard-library-only approximation treats the
    common multi-label public suffixes as one suffix, so subdomains of the
    same registered site cannot satisfy the automatic-publication gate.
    """

    domain = str(value or "").strip(".").lower()
    labels = [item for item in domain.split(".") if item]
    if len(labels) < 2:
        return domain
    suffix = ".".join(labels[-2:])
    width = 3 if suffix in _MULTI_LABEL_PUBLIC_SUFFIXES and len(labels) >= 3 else 2
    return ".".join(labels[-width:])


def _approved_sources(result: object) -> list[dict]:
    if not isinstance(result, Mapping) or not bool(result.get("ok")):
        return []
    sources = result.get("sources")
    if not isinstance(sources, list):
        return []
    approved: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        safe = _safe_source(source)
        if safe and safe["url"] not in seen:
            approved.append(safe)
            seen.add(safe["url"])
        if len(approved) >= 5:
            break
    return approved


def research_unknown_topic(
    conn: sqlite3.Connection,
    *,
    group_id: object,
    anchor_message_id: object,
    message: object,
    knowledge_found: bool,
    research_adapter: Callable[[str], Mapping[str, object]] | None,
    now=None,
) -> dict:
    """Perform one opt-in public research run, or return an audited no-op.

    ``research_adapter`` is injected so this domain module cannot silently
    obtain network access.  Production wiring must provide a separately gated
    adapter; tests use a deterministic local fake.
    """

    require_group_research_scope_schema(conn)
    moment = _now(now)
    assistant_id = _active_assistant_id(conn)
    group = _clip(group_id, 180)
    classification = classify_research_topic(message)
    policy_row = conn.execute(
        "SELECT * FROM group_research_policies WHERE assistant_id=?", (assistant_id,),
    ).fetchone()
    if not policy_row:
        raise ValueError("group_research_policy_missing")
    raw_policy = dict(policy_row)
    policy = _policy_public(raw_policy)
    # ``research_low_public`` is an affirmative classification, not a
    # blocking reason.  Keep the two concepts separate so a valid topic can
    # continue to the live-scope, quota and adapter gates below.
    reason = "" if classification["eligible"] else str(classification["reason_code"])
    query = str(classification.get("query") or "")

    if knowledge_found:
        reason = "research_knowledge_already_available"
    elif not _flag(conn, GROUP_RESEARCH_FEATURE_FLAG):
        reason = "research_feature_disabled"
    elif not policy["enabled"]:
        reason = "research_policy_disabled"
    else:
        scope_allowed, scope_reason = _scope_authorizes_group(conn, raw_policy, group)
        if not scope_allowed:
            reason = scope_reason
    if not reason and not classification["eligible"]:
        pass
    elif not reason and not _quota_available(conn, assistant_id=assistant_id, group_id=group, policy=policy, now=moment):
        reason = "research_quota_exhausted"
    elif not reason and _existing_fresh_run(
        conn, assistant_id=assistant_id, group_id=group, query_hash=_hash(query), now=moment,
    ):
        reason = "research_duplicate_fresh"
    elif not reason and research_adapter is None:
        reason = "research_adapter_unavailable"
    elif not reason:
        reason = ""

    if reason:
        run = _record_run(
            conn, assistant_id=assistant_id, group_id=group, anchor_message_id=anchor_message_id,
            query=query, risk_tier=str(classification["risk_tier"]), stage="blocked",
            reason_code=reason,
        )
        conn.commit()
        return {"status": "blocked", "reason_code": reason, "run": run, "sources": []}

    expires = _stamp(moment + timedelta(hours=int(policy["freshness_hours"])))
    run = _record_run(
        conn, assistant_id=assistant_id, group_id=group, anchor_message_id=anchor_message_id,
        query=query, risk_tier=LOW_PUBLIC, stage="running", reason_code="", expires_at=expires,
    )
    conn.commit()
    try:
        adapter_result = research_adapter(query)
    except Exception as exc:  # The public record intentionally excludes adapter diagnostics.
        adapter_result = {"ok": False, "error_kind": type(exc).__name__}
    sources = _approved_sources(adapter_result)
    now_text = _stamp(moment)
    if not sources:
        conn.execute(
            """UPDATE group_research_runs SET stage='failed',reason_code='research_no_approved_sources',
               updated_at=? WHERE id=?""",
            (now_text, run["id"]),
        )
        conn.commit()
        run.update({"stage": "failed", "reason_code": "research_no_approved_sources"})
        return {"status": "failed", "reason_code": "research_no_approved_sources", "run": run, "sources": []}

    for source in sources:
        conn.execute(
            """INSERT INTO group_research_evidence(
                id,run_id,source_url,source_domain,source_title,public_excerpt,source_hash,retrieved_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "group-research-evidence-" + uuid.uuid4().hex, run["id"], source["url"], source["domain"],
                source["title"], source["excerpt"], source["hash"], now_text,
            ),
        )
    summary = "；".join(source["title"] for source in sources[:2])
    conn.execute(
        """UPDATE group_research_runs SET stage='succeeded',reason_code='research_succeeded',
           source_count=?,result_summary=?,updated_at=? WHERE id=?""",
        (len(sources), _clip(summary, 500), now_text, run["id"]),
    )
    conn.commit()
    run.update({
        "stage": "succeeded", "reason_code": "research_succeeded", "source_count": len(sources),
        "result_summary": _clip(summary, 500),
    })
    return {"status": "succeeded", "reason_code": "research_succeeded", "run": run, "sources": sources}


def public_research_context(result: Mapping[str, object]) -> dict:
    """Return a bounded, citation-only packet suitable for a response model."""

    if str(result.get("status") or "") != "succeeded":
        return {"available": False, "sources": []}
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    return {
        "available": True,
        "sources": [
            {"title": source["title"], "url": source["url"], "excerpt": source["excerpt"]}
            for source in sources[:3]
            if isinstance(source, Mapping)
        ],
        "citation_requirement": "仅根据以下公共来源回答；无法确认时说明不确定，不编造。",
    }


def _knowledge_worthy(sources: list[Mapping[str, object]]) -> bool:
    """Require two independently-addressable registered public sources.

    This is a fail-closed technical proxy for independent publishers.  It is
    intentionally stricter than the old distinct-hostname test; legal or
    editorial independence cannot be inferred from a URL and therefore is
    never asserted by this automatic path.
    """

    return len({_registrable_domain(source.get("domain")) for source in sources if source.get("domain")}) >= 2


def auto_publish_low_public_knowledge(
    conn: sqlite3.Connection,
    result: Mapping[str, object],
    *,
    now=None,
) -> dict:
    """Create a scoped, expiring provisional item only from public evidence.

    It never consumes a group message.  High-impact, private and prohibited
    topics cannot reach this function.  One public source is useful to answer a
    turn, but two independent domains are required before autonomous retention.
    """

    require_group_research_scope_schema(conn)
    run = result.get("run") if isinstance(result.get("run"), Mapping) else {}
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    if str(result.get("status") or "") != "succeeded" or str(run.get("risk_tier") or "") != LOW_PUBLIC:
        return {"status": "not_eligible", "reason_code": "knowledge_requires_low_public_research"}
    policy_row = conn.execute(
        "SELECT * FROM group_research_policies WHERE assistant_id=?", (str(run.get("assistant_id") or ""),),
    ).fetchone()
    if (
        not policy_row
        or not _flag(conn, GROUP_RESEARCH_FEATURE_FLAG)
        or not _flag(conn, GROUP_RESEARCH_AUTOKNOWLEDGE_FEATURE_FLAG)
    ):
        return {"status": "disabled", "reason_code": "knowledge_auto_publish_disabled"}
    policy = dict(policy_row)
    if not int(policy.get("enabled") or 0) or not int(policy.get("auto_publish_low_public") or 0):
        return {"status": "disabled", "reason_code": "knowledge_policy_disabled"}
    scope_allowed, scope_reason = _scope_authorizes_group(conn, policy, run.get("group_id"))
    if not scope_allowed:
        return {"status": "blocked", "reason_code": scope_reason.replace("research_", "knowledge_", 1)}
    if not _knowledge_worthy([item for item in sources if isinstance(item, Mapping)]):
        return {"status": "not_worthy", "reason_code": "knowledge_requires_independent_sources"}
    exists = conn.execute(
        f"SELECT knowledge_item_id FROM {GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE} WHERE run_id=?",
        (str(run.get("id") or ""),),
    ).fetchone()
    if exists:
        return {"status": "idempotent", "knowledge_item_id": str(exists[0])}

    from bridge_knowledge_service import publish_group_research_provisional

    expiry = str(run.get("expires_at") or _stamp(_now(now) + timedelta(hours=int(policy["freshness_hours"]))))
    evidence_refs = [
        str(item["url"])
        for item in sources[:3] if isinstance(item, Mapping)
    ]
    excerpts = "\n".join(
        f"- {item['title']}：{item['excerpt']}（{item['url']}）"
        for item in sources[:3] if isinstance(item, Mapping)
    )
    item = publish_group_research_provisional(
        conn,
        group_id=str(run.get("group_id") or ""),
        run_id=str(run.get("id") or ""),
        payload={
            # Source titles are public evidence.  A group-derived query label
            # is not a useful or safe Knowledge title.
            "title": _clip(sources[0].get("title"), 120),
            "content": "以下为基于公开来源的临时参考，需以来源原文为准：\n" + excerpts,
            "summary": _clip(run.get("result_summary"), 500),
            "tags": ["group_research", "provisional"],
            "confidence": 0.7,
            "evidence_refs": evidence_refs,
            "freshness_status": "fresh",
            "fresh_until": expiry,
            "last_verified_at": _stamp(now),
        },
        actor=AUTO_RESEARCH_ACTOR,
    )
    conn.execute(
        f"""INSERT INTO {GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE}(
            id,run_id,knowledge_item_id,sensitivity_tier,publication_mode,created_at
        ) VALUES(?,?,?,?,?,?)""",
        (
            "group-research-knowledge-" + uuid.uuid4().hex, str(run.get("id") or ""), str(item["id"]),
            LOW_PUBLIC, "auto_published_provisional", _stamp(now),
        ),
    )
    conn.commit()
    return {"status": "published", "knowledge_item": item}


def create_review_required_knowledge_draft(
    conn: sqlite3.Connection,
    result: Mapping[str, object],
    *,
    now=None,
) -> dict:
    """Queue a de-identified high-impact public topic for batch review.

    This is deliberately a Draft with no evidence claim, not a covert
    auto-search.  Private/prohibited topics have no query and cannot enter this
    queue at all.
    """

    require_group_research_scope_schema(conn)
    run = result.get("run") if isinstance(result.get("run"), Mapping) else {}
    if str(run.get("risk_tier") or "") != REVIEW_REQUIRED:
        return {"status": "not_eligible", "reason_code": "review_draft_requires_review_required"}
    run_id = str(run.get("id") or "")
    topic = str(run.get("query_redacted") or "")
    if not run_id or not topic.startswith("public-query:"):
        return {"status": "redacted_discarded", "reason_code": "review_draft_public_topic_unproven"}
    policy_row = conn.execute(
        "SELECT * FROM group_research_policies WHERE assistant_id=?",
        (str(run.get("assistant_id") or ""),),
    ).fetchone()
    if not policy_row:
        return {"status": "blocked", "reason_code": "review_draft_policy_missing"}
    scope_allowed, scope_reason = _scope_authorizes_group(
        conn,
        dict(policy_row),
        run.get("group_id"),
    )
    if not scope_allowed:
        return {"status": "blocked", "reason_code": scope_reason.replace("research_", "review_draft_", 1)}
    existing = conn.execute(
        f"SELECT knowledge_item_id FROM {GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE} WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if existing:
        return {"status": "idempotent", "knowledge_item_id": str(existing[0])}
    from bridge_knowledge_service import create_group_research_review_draft

    item = create_group_research_review_draft(
        conn,
        group_id=str(run.get("group_id") or ""),
        run_id=run_id,
        topic_redacted=topic,
        actor=AUTO_RESEARCH_ACTOR,
    )
    conn.execute(
        f"""INSERT INTO {GROUP_RESEARCH_KNOWLEDGE_LINK_TABLE}(
            id,run_id,knowledge_item_id,sensitivity_tier,publication_mode,created_at
        ) VALUES(?,?,?,?,?,?)""",
        (
            "group-research-knowledge-" + uuid.uuid4().hex, run_id, str(item["id"]),
            REVIEW_REQUIRED, "review_draft", _stamp(now),
        ),
    )
    conn.commit()
    return {"status": "draft", "knowledge_item": item}


__all__ = [
    "AUTO_RESEARCH_ACTOR", "LOW_PUBLIC", "PROHIBITED", "RESTRICTED_PRIVATE", "REVIEW_REQUIRED",
    "auto_publish_low_public_knowledge", "classify_research_topic",
    "create_review_required_knowledge_draft", "get_group_research_policy",
    "group_research_execution_allowed", "public_research_context", "research_unknown_topic",
    "set_group_research_feature_flags", "set_group_research_policy", "list_group_research_runs",
]
