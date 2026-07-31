#!/usr/bin/env python3
"""Explainable, atomic model-routing presets for Gate 8."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from bridge_model_control import capabilities_from_row, role_model_compatible, sync_legacy_mirror
from bridge_model_registry import MODEL_ROLES, ensure_model_registry_tables, utc_now


PRESETS = {
    "balanced": {
        "label": "自动均衡",
        "description": "聊天优先已验证且响应较快的模型，规划偏向上下文能力，执行器保持受信任工具边界。",
    },
    "quality_first": {
        "label": "质量优先",
        "description": "优先上下文窗口与声明能力更完整的模型；未知质量不会被伪装成高质量。",
    },
    "low_cost": {
        "label": "成本优先",
        "description": "只在价格已登记时按价格选择；价格未知时保留透明警告。",
    },
    "local_privacy": {
        "label": "本地优先",
        "description": "聊天和规划优先 Ollama/LM Studio；工作执行仍须满足受信任执行器边界。",
    },
    "codex_first": {
        "label": "Codex 优先",
        "description": "优先使用 ChatGPT 登录态的 Codex 模型；平台仍只保存路由元数据。",
    },
}
ROLE_ORDER = (
    "interaction_classifier",
    "conversation_engagement",
    "conversation_reply",
    "work_planner",
    "work_executor",
)
GOOD_TEST_STATES = {"ok", "success", "passed", "healthy"}
LOCAL_PROVIDER_KINDS = {"ollama", "lm-studio"}


def _ensure_registry_exists(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='model_providers'
        """,
    ).fetchone()
    if table:
        return
    ensure_model_registry_tables(conn)
    if conn.in_transaction:
        conn.commit()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _model_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.*,p.name AS provider_name,p.kind AS provider_kind,
               p.transport,p.billing_scope,p.runtime_owner,p.config_mode,
               p.trusted_for_executor,p.enabled AS provider_enabled,
               p.last_test_status,p.last_test_latency
        FROM model_catalog m
        JOIN model_providers p ON p.id=m.provider_id
        WHERE m.enabled=1 AND p.enabled=1
        ORDER BY p.name,m.label,m.id
        """,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["capabilities"] = capabilities_from_row(item)
        item["tested"] = str(item.get("last_test_status") or "").lower() in GOOD_TEST_STATES
        input_price = item.get("input_price_per_million")
        output_price = item.get("output_price_per_million")
        item["known_total_price"] = (
            float(input_price) + float(output_price)
            if input_price is not None and output_price is not None
            else None
        )
        result.append(item)
    return result


def _compatible(models: list[dict], role: str) -> list[dict]:
    return [
        model
        for model in models
        if role_model_compatible(role, model)[0]
    ]


def _latency(model: dict) -> float:
    try:
        value = float(model.get("last_test_latency"))
    except (TypeError, ValueError):
        return 1_000_000_000.0
    return value if value >= 0 else 1_000_000_000.0


def _quality_key(model: dict) -> tuple:
    return (
        0 if model.get("tested") else 1,
        -int(model.get("context_window") or 0),
        -len(model.get("capabilities") or []),
        _latency(model),
        str(model.get("id") or ""),
    )


def _balanced_key(model: dict, role: str) -> tuple:
    codex_penalty = (
        0
        if role == "work_executor"
        else (
            1
            if str(model.get("transport") or "") in {
                "codex_cli_chatgpt",
                "codex_cli_custom_provider",
            }
            else 0
        )
    )
    return (
        0 if model.get("tested") else 1,
        codex_penalty,
        _latency(model),
        -int(model.get("context_window") or 0),
        str(model.get("id") or ""),
    )


def _price_key(model: dict) -> tuple:
    local = str(model.get("provider_kind") or "") in LOCAL_PROVIDER_KINDS
    known = model.get("known_total_price") is not None
    return (
        0 if local or known else 1,
        0.0 if local else float(model.get("known_total_price") or 1_000_000_000),
        0 if model.get("tested") else 1,
        _latency(model),
        str(model.get("id") or ""),
    )


def _codex_key(model: dict) -> tuple:
    transport = str(model.get("transport") or "")
    billing = str(model.get("billing_scope") or "")
    return (
        0 if transport == "codex_cli_chatgpt" and billing == "chatgpt_subscription" else 1,
        0 if transport == "codex_cli_custom_provider" else 1,
        0 if model.get("tested") else 1,
        _latency(model),
        str(model.get("id") or ""),
    )


def _selection_reason(preset: str, model: dict, role: str) -> str:
    if preset == "codex_first":
        return "chatgpt_subscription_codex" if model.get("billing_scope") == "chatgpt_subscription" else "codex_compatible"
    if preset == "local_privacy" and model.get("provider_kind") in LOCAL_PROVIDER_KINDS:
        return "local_provider"
    if preset == "low_cost" and model.get("known_total_price") is not None:
        return "registered_price"
    if preset == "quality_first":
        return "declared_context_and_capabilities"
    return "verified_latency_and_role_compatibility" if model.get("tested") else f"{role}_compatible_fallback"


def _select(
    preset: str,
    models: list[dict],
    role: str,
    current_primary_id: str,
) -> tuple[dict | None, list[str]]:
    candidates = _compatible(models, role)
    warnings: list[str] = []
    if not candidates:
        return None, [f"{role}:no_compatible_model"]
    if preset == "local_privacy" and role != "work_executor":
        local = [
            model
            for model in candidates
            if str(model.get("provider_kind") or "") in LOCAL_PROVIDER_KINDS
        ]
        if not local:
            return None, [f"{role}:local_model_unavailable"]
        candidates = local
        selected = sorted(candidates, key=lambda item: _balanced_key(item, role))[0]
    elif preset == "local_privacy" and role == "work_executor":
        current = next(
            (model for model in candidates if model["id"] == current_primary_id),
            None,
        )
        if current is None:
            return None, ["work_executor:trusted_current_executor_missing"]
        selected = current
        warnings.append("work_executor:kept_existing_remote_trusted_executor")
    elif preset == "quality_first":
        selected = sorted(candidates, key=_quality_key)[0]
    elif preset == "low_cost":
        selected = sorted(candidates, key=_price_key)[0]
        if selected.get("known_total_price") is None and selected.get("provider_kind") not in LOCAL_PROVIDER_KINDS:
            warnings.append(f"{role}:price_unknown")
    elif preset == "codex_first":
        selected = sorted(candidates, key=_codex_key)[0]
        if str(selected.get("transport") or "") not in {
            "codex_cli_chatgpt",
            "codex_cli_custom_provider",
        }:
            warnings.append(f"{role}:codex_compatible_model_unavailable")
    else:
        selected = sorted(candidates, key=lambda item: _balanced_key(item, role))[0]
    if not selected.get("tested"):
        warnings.append(f"{role}:selected_model_test_status_unknown")
    return selected, warnings


def _current_bindings(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        str(row["role"]): dict(row)
        for row in conn.execute(
            "SELECT * FROM model_role_bindings",
        ).fetchall()
    }


def routing_preset_preview(conn: sqlite3.Connection, preset: str) -> dict:
    name = str(preset or "").strip()
    if name not in PRESETS:
        raise ValueError("unknown_routing_preset")
    models = _model_rows(conn)
    current = _current_bindings(conn)
    mappings = []
    warnings: list[str] = []
    blockers: list[str] = []
    for role in ROLE_ORDER:
        binding = current.get(
            role,
            {
                "primary_model_id": "",
                "fallback_model_id": "",
                "updated_at": "",
            },
        )
        selected, role_messages = _select(
            name,
            models,
            role,
            str(binding.get("primary_model_id") or ""),
        )
        for message in role_messages:
            if "unavailable" in message or "missing" in message:
                blockers.append(message)
            else:
                warnings.append(message)
        if selected is None:
            mappings.append(
                {
                    "role": role,
                    "current_primary_model_id": binding.get("primary_model_id") or "",
                    "target_primary_model_id": "",
                    "target_fallback_model_id": "",
                    "selection_reason": "no_compatible_model",
                    "changed": False,
                },
            )
            continue
        fallback_id = ""
        if role != "work_executor":
            fallback_candidates = [
                model
                for model in _compatible(models, role)
                if model["id"] != selected["id"]
            ]
            if fallback_candidates:
                fallback_id = sorted(
                    fallback_candidates,
                    key=lambda item: _balanced_key(item, role),
                )[0]["id"]
        mappings.append(
            {
                "role": role,
                "current_primary_model_id": binding.get("primary_model_id") or "",
                "current_fallback_model_id": binding.get("fallback_model_id") or "",
                "target_primary_model_id": selected["id"],
                "target_fallback_model_id": fallback_id,
                "model_label": selected.get("label") or selected["id"],
                "provider_name": selected.get("provider_name") or "",
                "transport": selected.get("transport") or "",
                "billing_scope": selected.get("billing_scope") or "",
                "tested": bool(selected.get("tested")),
                "price_known": selected.get("known_total_price") is not None,
                "selection_reason": _selection_reason(name, selected, role),
                "changed": (
                    str(binding.get("primary_model_id") or "") != selected["id"]
                    or str(binding.get("fallback_model_id") or "") != fallback_id
                ),
            },
        )
    payload = {
        "preset": name,
        "definition": PRESETS[name],
        "mappings": mappings,
        "warnings": sorted(set(warnings)),
        "blockers": sorted(set(blockers)),
    }
    payload["ok"] = not payload["blockers"]
    payload["fingerprint"] = _fingerprint(payload)
    return payload


def list_routing_presets(conn: sqlite3.Connection) -> dict:
    _ensure_registry_exists(conn)
    previews = []
    for name in PRESETS:
        preview = routing_preset_preview(conn, name)
        previews.append(
            {
                "id": name,
                **PRESETS[name],
                "available": preview["ok"],
                "warnings": preview["warnings"],
                "blockers": preview["blockers"],
                "changed_roles": sum(
                    1 for item in preview["mappings"] if item["changed"]
                ),
            },
        )
    meta = conn.execute(
        "SELECT value,updated_at FROM model_registry_meta WHERE key='active_routing_preset'",
    ).fetchone()
    active = {}
    if meta:
        try:
            active = json.loads(str(meta["value"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            active = {}
        active["updated_at"] = meta["updated_at"]
    return {"presets": previews, "active": active}


def apply_routing_preset(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    changed_by: str = "admin",
    client_ip: str = "",
) -> dict:
    _ensure_registry_exists(conn)
    preset = str(payload.get("preset") or "").strip()
    expected = str(payload.get("fingerprint") or "").strip()
    conn.execute("BEGIN IMMEDIATE")
    try:
        preview = routing_preset_preview(conn, preset)
        if expected != preview["fingerprint"]:
            raise ValueError("stale_routing_preset_preview")
        if not preview["ok"]:
            raise ValueError("routing_preset_unavailable")
        now = utc_now()
        for change in preview["mappings"]:
            if not change["changed"]:
                continue
            role = change["role"]
            conn.execute(
                """
                INSERT INTO model_role_bindings(
                    role,primary_model_id,fallback_model_id,updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(role) DO UPDATE SET
                    primary_model_id=excluded.primary_model_id,
                    fallback_model_id=excluded.fallback_model_id,
                    updated_at=excluded.updated_at
                """,
                (
                    role,
                    change["target_primary_model_id"],
                    change["target_fallback_model_id"],
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO role_binding_change_log(
                    id,role,old_primary_model_id,new_primary_model_id,
                    old_fallback_model_id,new_fallback_model_id,
                    changed_by,client_ip,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex,
                    role,
                    change["current_primary_model_id"],
                    change["target_primary_model_id"],
                    change.get("current_fallback_model_id") or "",
                    change["target_fallback_model_id"],
                    str(changed_by or "admin")[:80],
                    str(client_ip or "")[:80],
                    now,
                ),
            )
        sync_legacy_mirror(conn)
        active = {
            "preset": preset,
            "preview_fingerprint": preview["fingerprint"],
            "binding_fingerprint": _fingerprint(
                {
                    item["role"]: {
                        "primary_model_id": item["target_primary_model_id"],
                        "fallback_model_id": item["target_fallback_model_id"],
                    }
                    for item in preview["mappings"]
                },
            ),
            "warnings": preview["warnings"],
        }
        conn.execute(
            """
            INSERT INTO model_registry_meta(key,value,updated_at)
            VALUES('active_routing_preset',?,?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,updated_at=excluded.updated_at
            """,
            (_canonical(active), now),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        **preview,
        "applied": True,
        "applied_at": now,
        "active": active,
    }


__all__ = [
    "PRESETS",
    "apply_routing_preset",
    "list_routing_presets",
    "routing_preset_preview",
]
