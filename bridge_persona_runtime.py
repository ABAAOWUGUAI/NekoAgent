#!/usr/bin/env python3
"""Versioned Persona voice contract normalization and workspace projections."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from typing import Mapping


VOICE_CONTRACT_KEY = "voice_contract_v1"
VOICE_CONTRACT_SCHEMA_VERSION = 1

_TEXT_LIMITS = {
    "identity_core": 1200,
    "relationship_stance": 800,
    "work_continuity": 800,
}
_LIST_LIMITS = {
    "values": (12, 160),
    "boundaries": (12, 240),
    "preferred_phrases": (16, 120),
    "avoid_phrases": (16, 120),
    "prohibited_patterns": (16, 200),
}
_ENUMS = {
    "warmth": {"calm", "balanced", "warm", "expressive"},
    "directness": {"gentle", "balanced", "direct"},
    "initiative": {"restrained", "responsive", "proactive"},
    "humor": {"none", "light", "playful", "dry"},
    "rhythm": {"concise", "natural", "varied", "structured"},
    "question_policy": {"minimal", "contextual", "clarify_when_needed", "engaged"},
    "address_policy": {"natural", "preferred", "avoid_repetition"},
    "private_length": {"short", "balanced", "detailed"},
    "group_length": {"brief", "short", "balanced"},
    "work_length": {"compact", "structured_compact", "detailed"},
    "meme_policy": {"never", "contextual", "frequent"},
}
_EXAMPLE_LIMITS = {
    "scenario": 180,
    "intent": 240,
    "preferred_style": 600,
    "avoid_style": 600,
}
_SENSITIVE_EXAMPLE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/=-]+|"
    r"\b(?:api[_ -]?key|access[_ -]?token|password|cookie)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def safe_voice_contract() -> dict:
    """Return a role-name-free, fail-closed contract suitable for all assistants."""

    return {
        "schema_version": VOICE_CONTRACT_SCHEMA_VERSION,
        "identity_core": "稳定、真诚、清楚；不伪造事实、能力、感知或已执行动作。",
        "relationship_stance": "尊重边界，先回应重点，避免谄媚、说教和机械复述。",
        "values": ["真实", "尊重", "可验证"],
        "boundaries": ["不泄露敏感信息", "不把计划或尝试描述为已经完成"],
        "warmth": "balanced",
        "directness": "balanced",
        "initiative": "responsive",
        "humor": "light",
        "rhythm": "natural",
        "question_policy": "contextual",
        "address_policy": "natural",
        "private_length": "short",
        "group_length": "brief",
        "work_length": "structured_compact",
        "work_continuity": "区分计划、执行中、完成和失败；只有可验证结果才能表述为已完成。",
        "meme_policy": "contextual",
        "preferred_phrases": [],
        "avoid_phrases": [],
        "prohibited_patterns": ["客服式开场", "无请求长清单", "连续追问", "伪造执行过程"],
        "examples": [],
    }


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"invalid_voice_contract_{field}")
    normalized = " ".join(value.split())
    if "\x00" in value or len(normalized) > limit or (required and not normalized):
        raise ValueError(f"invalid_voice_contract_{field}")
    return normalized


def _string_list(value: object, field: str, maximum: int, item_limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"invalid_voice_contract_{field}")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _text(item, field, item_limit, required=True)
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _examples(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError("invalid_voice_contract_examples")
    result: list[dict] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - set(_EXAMPLE_LIMITS):
            raise ValueError("invalid_voice_contract_examples")
        item = {
            field: _text(raw.get(field), f"examples_{field}", limit)
            for field, limit in _EXAMPLE_LIMITS.items()
        }
        if not item["scenario"] or not item["preferred_style"]:
            raise ValueError("invalid_voice_contract_examples")
        if _SENSITIVE_EXAMPLE.search(" ".join(item.values())):
            raise ValueError("sensitive_voice_contract_example")
        result.append(item)
    return result


def normalize_voice_contract(value: object) -> dict:
    """Strictly normalize an administrator supplied ``voice_contract_v1``."""

    if not isinstance(value, Mapping):
        raise ValueError("voice_contract_object_required")
    allowed = {
        "schema_version", "examples", *_TEXT_LIMITS, *_LIST_LIMITS, *_ENUMS,
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("unsupported_voice_contract_fields:" + ",".join(unknown))
    try:
        schema_version = int(value.get("schema_version", VOICE_CONTRACT_SCHEMA_VERSION))
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported_voice_contract_schema") from exc
    if schema_version != VOICE_CONTRACT_SCHEMA_VERSION:
        raise ValueError("unsupported_voice_contract_schema")
    defaults = safe_voice_contract()
    result = {"schema_version": schema_version}
    for field, limit in _TEXT_LIMITS.items():
        result[field] = _text(value.get(field, defaults[field]), field, limit)
    for field, (maximum, item_limit) in _LIST_LIMITS.items():
        result[field] = _string_list(value.get(field, defaults[field]), field, maximum, item_limit)
    for field, choices in _ENUMS.items():
        selected = _text(value.get(field, defaults[field]), field, 40, required=True).lower()
        if selected not in choices:
            raise ValueError(f"invalid_voice_contract_{field}")
        result[field] = selected
    result["examples"] = _examples(value.get("examples", defaults["examples"]))
    return result


def resolve_voice_contract(boundaries: object) -> tuple[dict, dict]:
    """Resolve stored boundaries without allowing malformed data into runtime context."""

    source = "safe_neutral_default"
    error = ""
    raw: object = None
    if isinstance(boundaries, Mapping):
        raw = boundaries.get(VOICE_CONTRACT_KEY)
    if raw is not None:
        try:
            contract = normalize_voice_contract(raw)
            source = "persona_version"
        except ValueError as exc:
            contract = safe_voice_contract()
            source = "safe_neutral_invalid_contract"
            error = str(exc)
    else:
        contract = safe_voice_contract()
    return contract, {"source": source, "compile_error": error}


def contract_hash(contract: Mapping[str, object]) -> str:
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compile_voice_contract(
    contract: Mapping[str, object],
    *,
    persona_text: str = "",
    speaking_style: str = "",
    relationship_label: str = "",
) -> dict:
    """Build inspectable context blocks; no model call or conversation write occurs."""

    normalized = normalize_voice_contract(contract)
    tone = {
        key: normalized[key]
        for key in (
            "warmth", "directness", "initiative", "humor", "rhythm",
            "question_policy", "address_policy", "meme_policy",
        )
    }
    common = [
        normalized["identity_core"],
        normalized["relationship_stance"],
        "价值：" + "；".join(normalized["values"]),
        "边界：" + "；".join(normalized["boundaries"]),
        "禁止：" + "；".join(normalized["prohibited_patterns"]),
    ]
    return {
        "schema_version": VOICE_CONTRACT_SCHEMA_VERSION,
        "contract_hash": contract_hash(normalized),
        "identity": {
            "persona": _text(persona_text, "persona", 4000),
            "relationship": _text(relationship_label, "relationship", 80),
            "speaking_style": _text(speaking_style, "style", 4000),
            "contract_lines": [line for line in common if not line.endswith("：")],
        },
        "tone": tone,
        "scenarios": [
            {"mode": "private", "length": normalized["private_length"], "priority": "relationship_then_expression"},
            {"mode": "group", "length": normalized["group_length"], "priority": "group_scope_then_expression"},
            {"mode": "work", "length": normalized["work_length"], "priority": "action_truth_then_persona"},
        ],
        "work_continuity": normalized["work_continuity"],
        "example_count": len(normalized["examples"]),
        "precedence": [
            "platform_truth_safety", "action_truth", "persona_version",
            "relationship_scope", "expression_scope", "turn_expression_plan",
        ],
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone())


def _scope_summary(conn: sqlite3.Connection, assistant_id: str) -> dict:
    relationships: list[dict] = []
    if _table_exists(conn, "relationship_states"):
        rows = conn.execute(
            """SELECT scope_type,count(*) AS total,max(updated_at) AS updated_at
               FROM relationship_states WHERE assistant_id=? GROUP BY scope_type""",
            (assistant_id,),
        ).fetchall()
        relationships = [
            {"scope_type": str(row[0]), "total": int(row[1]), "updated_at": str(row[2] or "")}
            for row in rows
        ]
    expression: list[dict] = []
    if _table_exists(conn, "expression_habits"):
        rows = conn.execute(
            """SELECT subject_type,count(*) AS total,max(updated_at) AS updated_at
               FROM expression_habits WHERE enabled=1 GROUP BY subject_type""",
        ).fetchall()
        expression = [
            {"subject_type": str(row[0]), "total": int(row[1]), "updated_at": str(row[2] or "")}
            for row in rows
        ]
    return {"relationships": relationships, "expression_habits": expression}


def _assistant_or_error(conn: sqlite3.Connection) -> dict:
    from bridge_assistant_identity import current_assistant

    assistant = current_assistant(conn)
    if assistant is None:
        raise ValueError("active_assistant_missing")
    return assistant


def runtime_persona_metadata(conn: sqlite3.Connection) -> dict:
    assistant = _assistant_or_error(conn)
    contract, resolution = resolve_voice_contract(assistant["persona"].get("behavior_boundaries"))
    requested = assistant["persona"]["version_id"]
    applied = requested if resolution["source"] == "persona_version" else "safe-neutral-v1"
    return {
        "assistant_id": assistant["id"],
        "requested_persona_version_id": requested,
        "requested_persona_version": assistant["persona"]["version"],
        "applied_persona_version_id": applied,
        "version_match": requested == applied,
        "contract_schema_version": contract["schema_version"],
        "contract_hash": contract_hash(contract),
        "config_source": resolution["source"],
        "last_compile_error": resolution["compile_error"],
        "updated_at": assistant["persona"].get("updated_at") or assistant["updated_at"],
    }


def persona_workspace(conn: sqlite3.Connection) -> dict:
    assistant = _assistant_or_error(conn)
    contract, resolution = resolve_voice_contract(assistant["persona"].get("behavior_boundaries"))
    compiled = compile_voice_contract(
        contract,
        persona_text=assistant["persona"]["persona"],
        speaking_style=assistant["persona"]["style"],
        relationship_label=assistant["persona"]["relationship"],
    )
    return {
        "assistant": {
            "id": assistant["id"],
            "display_name": assistant["display_name"],
            "updated_at": assistant["updated_at"],
        },
        "persona": {
            "pack_id": assistant["persona"]["pack_id"],
            "version_id": assistant["persona"]["version_id"],
            "version": assistant["persona"]["version"],
            "persona": assistant["persona"]["persona"],
            "style": assistant["persona"]["style"],
            "relationship": assistant["persona"]["relationship"],
            "updated_at": assistant["persona"].get("updated_at") or assistant["updated_at"],
        },
        "voice_contract": contract,
        "config_source": resolution["source"],
        "compile_error": resolution["compile_error"],
        "compiled_summary": {
            "contract_hash": compiled["contract_hash"],
            "tone": compiled["tone"],
            "scenarios": compiled["scenarios"],
            "precedence": compiled["precedence"],
        },
        "scope_summary": _scope_summary(conn, assistant["id"]),
        "runtime": runtime_persona_metadata(conn),
    }


def preview_persona_workspace(conn: sqlite3.Connection, payload: Mapping[str, object]) -> dict:
    assistant = _assistant_or_error(conn)
    current_contract, _ = resolve_voice_contract(assistant["persona"].get("behavior_boundaries"))
    contract = normalize_voice_contract(payload.get("voice_contract", current_contract))
    compiled = compile_voice_contract(
        contract,
        persona_text=payload.get("persona", assistant["persona"]["persona"]),
        speaking_style=payload.get("style", assistant["persona"]["style"]),
        relationship_label=payload.get("relationship", assistant["persona"]["relationship"]),
    )
    return {"persisted": False, "voice_contract": contract, "compiled": compiled}


def save_persona_workspace(conn: sqlite3.Connection, payload: Mapping[str, object]) -> dict:
    from bridge_assistant_identity import update_assistant

    assistant = _assistant_or_error(conn)
    expected = str(payload.get("expected_updated_at") or "").strip()
    if not expected:
        raise ValueError("assistant_version_required")
    allowed = {
        "expected_updated_at", "display_name", "persona", "style", "relationship", "voice_contract",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unsupported_persona_workspace_fields:" + ",".join(unknown))
    current_contract, _ = resolve_voice_contract(assistant["persona"].get("behavior_boundaries"))
    contract = normalize_voice_contract(payload.get("voice_contract", current_contract))
    boundaries = copy.deepcopy(assistant["persona"].get("behavior_boundaries") or {})
    boundaries[VOICE_CONTRACT_KEY] = contract
    patch = {
        key: payload[key]
        for key in ("display_name", "persona", "style", "relationship")
        if key in payload
    }
    patch.update({"behavior_boundaries": boundaries, "expected_updated_at": expected})
    updated = update_assistant(conn, assistant["id"], patch, channel="persona-workspace")
    return persona_workspace(conn) | {
        "saved": True,
        "previous_persona_version_id": assistant["persona"]["version_id"],
        "active_persona_version_id": updated["persona"]["version_id"],
    }


__all__ = [
    "VOICE_CONTRACT_KEY",
    "VOICE_CONTRACT_SCHEMA_VERSION",
    "compile_voice_contract",
    "contract_hash",
    "normalize_voice_contract",
    "persona_workspace",
    "preview_persona_workspace",
    "resolve_voice_contract",
    "runtime_persona_metadata",
    "safe_voice_contract",
    "save_persona_workspace",
]
