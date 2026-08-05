#!/usr/bin/env python3
"""Versioned Automation output contracts and evidence-backed presenters.

Automation configuration decides *what* a scheduled result must look like.
Capabilities still produce structured facts and Evidence; this module turns
those facts into a Channel-friendly Artifact without changing source values.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Mapping


OUTPUT_CONTRACT_SCHEMA_VERSION = 1
DEFAULT_OUTPUT_CONTRACT = {
    "schema_version": OUTPUT_CONTRACT_SCHEMA_VERSION,
    "scope": "current_automation_job",
    "language": "zh-CN",
    "layout": "conclusion_then_items",
    "item_fields": ["repository_name", "purpose_summary_zh", "stars", "link"],
    "missing_description": "state_unavailable",
    "hide_internal_metadata": True,
}
_ITEM_FIELDS = {"repository_name", "purpose_summary_zh", "stars", "link"}
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_output_contract(value: object | None) -> dict:
    if value is None or value == "":
        raw: dict = dict(DEFAULT_OUTPUT_CONTRACT)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("automation_output_contract_invalid_json") from exc
        if not isinstance(parsed, dict):
            raise ValueError("automation_output_contract_invalid")
        raw = parsed
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise ValueError("automation_output_contract_invalid")
    allowed = set(DEFAULT_OUTPUT_CONTRACT)
    if set(raw) - allowed:
        raise ValueError("automation_output_contract_unknown_field")
    merged = {**DEFAULT_OUTPUT_CONTRACT, **raw}
    if int(merged.get("schema_version") or 0) != OUTPUT_CONTRACT_SCHEMA_VERSION:
        raise ValueError("automation_output_contract_version_unsupported")
    if merged.get("scope") != "current_automation_job":
        raise ValueError("automation_output_contract_scope_invalid")
    if merged.get("language") != "zh-CN":
        raise ValueError("automation_output_contract_language_invalid")
    if merged.get("layout") != "conclusion_then_items":
        raise ValueError("automation_output_contract_layout_invalid")
    fields = merged.get("item_fields")
    if not isinstance(fields, list) or not fields or set(fields) != _ITEM_FIELDS or len(fields) != len(_ITEM_FIELDS):
        raise ValueError("automation_output_contract_item_fields_invalid")
    if merged.get("missing_description") != "state_unavailable":
        raise ValueError("automation_output_contract_missing_description_invalid")
    merged["hide_internal_metadata"] = bool(merged.get("hide_internal_metadata"))
    merged["item_fields"] = list(DEFAULT_OUTPUT_CONTRACT["item_fields"])
    return merged


def output_contract_hash(contract: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(normalize_output_contract(contract)).encode("utf-8")).hexdigest()


def automation_config_hash(job: Mapping[str, object]) -> str:
    payload = {
        key: job.get(key)
        for key in (
            "id", "user_id", "action_type", "instruction", "parameters_json",
            "schedule_type", "run_at", "time_of_day", "weekdays",
            "interval_minutes", "timezone", "enabled", "revision",
            "output_contract_hash",
            "execution_contract_hash",
        )
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def output_contract_from_job(job: Mapping[str, object]) -> dict:
    raw = job.get("output_contract_json")
    if raw:
        return normalize_output_contract(raw)
    try:
        parameters = json.loads(str(job.get("parameters_json") or "{}"))
    except json.JSONDecodeError:
        parameters = {}
    if isinstance(parameters, dict) and (
        parameters.get("delivery_format") == "conversation"
        or parameters.get("output_language") == "zh-CN"
    ):
        return normalize_output_contract(DEFAULT_OUTPUT_CONTRACT)
    return normalize_output_contract(DEFAULT_OUTPUT_CONTRACT)


def normalized_delivery_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def delivery_text_sha256(value: object) -> str:
    return hashlib.sha256(normalized_delivery_text(value).encode("utf-8")).hexdigest()


def build_github_purpose_prompt(items: list[dict]) -> list[dict[str, str]]:
    source = [
        {
            "repository_name": str(item.get("repo") or item.get("name") or "")[:160],
            "source_description": str(item.get("description") or "")[:500],
        }
        for item in items[:20]
        if isinstance(item, dict) and str(item.get("repo") or item.get("name") or "").strip()
    ]
    return [
        {
            "role": "system",
            "content": (
                "你是自动化结果的事实呈现器，只输出 JSON。不得使用训练知识补充仓库用途。"
                "只能把 source_description 忠实改写成一句简体中文用途说明；描述为空时 summary 必须为空。"
                "仓库名必须原样返回。格式为 {\"items\":[{\"repository_name\":\"...\",\"summary\":\"...\"}]}。"
            ),
        },
        {"role": "user", "content": json.dumps({"items": source}, ensure_ascii=False)},
    ]


def parse_github_purpose_summaries(raw_text: object, items: list[dict]) -> dict[str, str]:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        text = match.group(0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    source = {
        str(item.get("repo") or item.get("name") or ""): str(item.get("description") or "").strip()
        for item in items
        if isinstance(item, dict)
    }
    result: dict[str, str] = {}
    for row in rows[:20]:
        if not isinstance(row, dict) or set(row) - {"repository_name", "summary"}:
            return {}
        name = str(row.get("repository_name") or "").strip()
        summary = str(row.get("summary") or "").strip()[:180]
        if name not in source:
            return {}
        if not source[name]:
            if summary:
                return {}
            continue
        if summary and not _CJK_RE.search(summary):
            return {}
        if summary:
            result[name] = summary
    return result


def present_github_trending(
    light_result: Mapping[str, object],
    contract: Mapping[str, object],
    *,
    purpose_summaries: Mapping[str, str] | None = None,
) -> dict:
    normalized = normalize_output_contract(contract)
    output = light_result.get("output") if isinstance(light_result.get("output"), dict) else {}
    raw_items = output.get("items") if isinstance(output.get("items"), list) else []
    summaries = dict(purpose_summaries or {})
    lines = [f"今天筛选出 {len(raw_items)} 个值得关注的 GitHub AI / AI Agent 项目，下面按用途说明。"]
    presented: list[dict] = []
    unavailable = 0
    for index, item in enumerate(raw_items[:20], start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("repo") or item.get("name") or "").strip()
        if not name:
            raise ValueError("automation_presenter_repository_name_missing")
        source_description = str(item.get("description") or "").strip()
        purpose = str(summaries.get(name) or "").strip()
        if not purpose and source_description and _CJK_RE.search(source_description):
            purpose = source_description[:180]
        if not purpose:
            purpose = "仓库未提供可可靠转换的用途简介。"
            unavailable += 1
        stars = str(item.get("stars_today") or item.get("stars") or "热度未返回").strip()
        link = str(item.get("url") or "").strip()
        if not link.startswith("https://github.com/"):
            raise ValueError("automation_presenter_repository_link_invalid")
        lines.extend((
            "",
            f"{index}. {name}",
            f"做什么：{purpose}",
            f"热度：{stars}",
            f"链接：{link}",
        ))
        presented.append({
            "repository_name": name,
            "purpose_summary_zh": purpose,
            "stars": stars,
            "link": link,
            "purpose_available": purpose != "仓库未提供可可靠转换的用途简介。",
        })
    if not presented:
        raise ValueError("automation_presenter_items_missing")
    content = "\n".join(lines).strip()
    return {
        "content": content,
        "artifact": {
            "kind": "automation_result_presentation",
            "schema_version": 1,
            "output_contract_hash": output_contract_hash(normalized),
            "item_count": len(presented),
            "purpose_unavailable_count": unavailable,
            "items": presented,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        },
    }


__all__ = [
    "DEFAULT_OUTPUT_CONTRACT",
    "OUTPUT_CONTRACT_SCHEMA_VERSION",
    "build_github_purpose_prompt",
    "automation_config_hash",
    "delivery_text_sha256",
    "normalize_output_contract",
    "normalized_delivery_text",
    "output_contract_from_job",
    "output_contract_hash",
    "parse_github_purpose_summaries",
    "present_github_trending",
]
