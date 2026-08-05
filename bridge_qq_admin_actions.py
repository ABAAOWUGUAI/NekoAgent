#!/usr/bin/env python3
"""Deterministic QQ administrator actions used by private assistant dispatch.

The LLM is intentionally not involved in parsing parameters, authorising the
actor, writing access settings, or producing completion claims.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import re
import sqlite3
import uuid

from bridge_qq_access_service import (
    check_qq_access,
    get_qq_access_settings,
    update_qq_access_settings,
)
from bridge_social_engine import get_group_policy, upsert_group_policy


_GROUP_ID_PATTERN = re.compile(r"(?<!\d)([1-9][0-9]{4,19})(?!\d)")
_GROUP_CONTEXT_HINTS = (
    "qq群",
    "qq 群",
    "群聊",
    "群白名单",
    "群准入",
    "这个群",
    "该群",
    "目标群",
    "模板群",
    "参考群",
)
_ENABLE_HINTS = ("开放", "加入", "添加", "允许", "启用")
_DISABLE_HINTS = ("移出", "移除", "删除", "撤销", "取消", "关闭", "禁用")
_STATUS_HINTS = ("查询", "查看", "检查", "状态", "是否", "好了吗", "好了么", "生效")
# Generic words such as “是否” also occur in unrelated requests (for example
# weather forecasts).  Only these bounded phrases may carry a status question
# across turns without repeating the group reference.
_STATUS_FOLLOWUP_HINTS = (
    "生效了吗", "生效了么", "好了吗", "好了么", "成功了吗", "完成了吗",
    "配置怎么样", "状态怎么样", "确认一下状态", "查一下状态",
)
_ALLOWLIST_LIST_HINTS = ("准入列表", "白名单列表", "哪些群", "多少个群", "几个群", "所有准入群", "已加入的群")
_DIAGNOSTIC_HINTS = ("查日志", "看日志", "直接查", "排查", "没有回复", "没回复", "不回复")
_POLICY_CLONE_HINTS = ("对齐", "保持一致", "一致", "复制", "同步", "一样", "相同")
_GROUP_POLICY_COPY_FIELDS = (
    "participation_mode",
    "enabled",
    "mention_only",
    "active_reply",
    "reply_probability",
    "cooldown_seconds",
    "quiet_start",
    "quiet_end",
    "timezone",
    "max_context",
    "allow_work",
    "allowed_work_senders",
    "meme_enabled",
    "quiet_gap_seconds",
    "burst_window_seconds",
    "burst_max_messages",
    "daily_reply_budget",
    "continuation_window_seconds",
    "max_auto_continuations",
)


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _actor_ref(actor_id: str) -> str:
    digest = hashlib.sha256(str(actor_id or "").encode("utf-8")).hexdigest()[:12]
    return f"qq_admin:{digest}"


def _candidate_group_id(
    message: str,
    history: list[dict] | None = None,
    *,
    current_group_id: str = "",
) -> str:
    texts = [str(message or "")]
    texts.extend(str(item.get("content") or "") for item in reversed(history or []))
    for text in texts:
        lowered = text.lower()
        if not any(hint in lowered for hint in _GROUP_CONTEXT_HINTS):
            continue
        match = _GROUP_ID_PATTERN.search(text)
        if match:
            return match.group(1)
    if current_group_id and any(
        hint in str(message or "").lower() for hint in _GROUP_CONTEXT_HINTS
    ):
        return str(current_group_id).strip()
    return ""


def _context_text(message: str, history: list[dict] | None = None) -> str:
    """Return bounded recent control context without turning it into a log."""

    recent = [str(item.get("content") or "") for item in (history or [])[-12:]]
    return "\n".join([*recent, str(message or "")]).lower()


def _current_group_reference(text: str) -> bool:
    """Whether the current turn itself names or points at a QQ group."""

    value = str(text or "").lower()
    return bool(_GROUP_ID_PATTERN.search(value)) or any(
        hint in value for hint in _GROUP_CONTEXT_HINTS
    )


def _status_followup_reference(text: str, history: list[dict] | None) -> bool:
    """Allow only explicit, bounded status continuations to use group history."""

    value = str(text or "").strip().lower()
    if not value or len(value) > 80:
        return False
    if not any(hint in value for hint in _STATUS_FOLLOWUP_HINTS):
        return False
    recent = "\n".join(
        str(item.get("content") or "")
        for item in (history or [])[-4:]
        if isinstance(item, dict)
    ).lower()
    return any(hint in recent for hint in _GROUP_CONTEXT_HINTS) and bool(
        _GROUP_ID_PATTERN.search(recent)
    )


def _normalize_access_terms(text: str) -> str:
    """Normalize bounded Chinese typos before routing; raw text remains untouched."""

    return str(text or "").replace("准人", "准入")


def _group_ids_in_text(message: str) -> list[str]:
    return list(dict.fromkeys(_GROUP_ID_PATTERN.findall(str(message or ""))))


def _clone_source_group_id(
    message: str,
    history: list[dict] | None,
    *,
    target_group_id: str,
) -> str:
    """Resolve an explicitly referenced source, preferring the current turn."""

    texts = [str(message or "")]
    texts.extend(str(item.get("content") or "") for item in reversed(history or []))
    for text in texts:
        for group_id in _group_ids_in_text(text):
            if group_id != target_group_id:
                return group_id
    return ""


def _clone_target_group_id(
    message: str,
    history: list[dict] | None,
    *,
    current_group_id: str = "",
) -> str:
    """Choose the target without mistaking a one-ID current template for it."""

    current_ids = _group_ids_in_text(message)
    if len(current_ids) >= 2:
        return current_ids[0]
    history_target = _candidate_group_id("", history, current_group_id=current_group_id)
    return history_target or _candidate_group_id(
        message,
        history,
        current_group_id=current_group_id,
    )


def _clone_requested(text: str) -> bool:
    """Recognise an imperative clone, never a bare comparison question."""

    if not any(hint in text for hint in _POLICY_CLONE_HINTS):
        return False
    if any(hint in text for hint in _ENABLE_HINTS):
        return True
    return any(hint in text for hint in ("把", "将", "请", "给"))


def _allowlist_list_requested(text: str) -> bool:
    """Recognise a read-only whole-list query, not a group mutation."""

    return any(hint in text for hint in _ALLOWLIST_LIST_HINTS) and any(
        hint in text for hint in ("多少", "几个", "哪些", "查询", "查看", "当前", "现在", "有")
    )


def parse_qq_admin_action(
    message: str,
    history: list[dict] | None = None,
    *,
    current_group_id: str = "",
) -> dict | None:
    text = _normalize_access_terms(str(message or "").strip().lower())
    if not text:
        return None
    context_text = _normalize_access_terms(_context_text(message, history))
    # Continuation turns often say only “then align it with …”.  The target
    # remains explicit in recent private context, so retain it for deterministic
    # routing instead of falling through to a general-purpose chat model.
    group_context = any(hint in context_text for hint in _GROUP_CONTEXT_HINTS)
    current_group_context = any(hint in text for hint in _GROUP_CONTEXT_HINTS)
    access_context = "白名单" in context_text or "准入" in context_text
    group_id = _candidate_group_id(
        message,
        history,
        current_group_id=current_group_id,
    )
    if group_context and _clone_requested(text):
        group_id = _clone_target_group_id(
            message,
            history,
            current_group_id=current_group_id,
        )
        source_group_id = _clone_source_group_id(
            message,
            history,
            target_group_id=group_id,
        )
        if group_id and source_group_id:
            return {
                "action_type": "qq_group_policy_clone",
                "group_id": group_id,
                "source_group_id": source_group_id,
            }
        if group_id:
            return {"action_type": "qq_group_clone_clarification", "group_id": group_id}
    if group_context and access_context and any(hint in text for hint in _ENABLE_HINTS):
        return {"action_type": "qq_group_allowlist_enable", "group_id": group_id}
    if group_context and access_context and any(hint in text for hint in _DISABLE_HINTS):
        return {"action_type": "qq_group_allowlist_disable", "group_id": group_id}
    if current_group_context and access_context and not group_id and _allowlist_list_requested(text):
        return {"action_type": "qq_group_allowlist_list"}
    if group_id and any(hint in text for hint in _DIAGNOSTIC_HINTS):
        return {"action_type": "qq_group_diagnose", "group_id": group_id}
    if group_id and (
        ((_current_group_reference(text) or _status_followup_reference(text, history))
         and any(hint in text for hint in _STATUS_HINTS))
        or any(hint in text for hint in ("好了吗", "好了么", "没有回复", "没回复", "不回复"))
    ):
        return {"action_type": "qq_group_status_read", "group_id": group_id}
    if current_group_context and access_context and not group_id:
        return {"action_type": "qq_group_clarification", "group_id": ""}
    return None


def _authorise(conn: sqlite3.Connection, actor_id: str) -> dict:
    access = check_qq_access(
        conn,
        {
            "sender_id": actor_id,
            "event_type": "private",
            "requested_action": "settings",
        },
    )
    if not access.get("allowed") or access.get("role") not in {"super_admin", "admin"}:
        raise PermissionError("qq_admin_action_forbidden")
    return access


def _settings_payload(current: dict, *, groups: list[dict], group_chat_enabled: bool) -> dict:
    settings = dict(current["settings"])
    settings["group_chat_enabled"] = bool(group_chat_enabled)
    return {
        "expected_version": int(settings.get("config_version") or 0),
        "settings": settings,
        "administrators": list(current.get("administrators") or []),
        "private_allowlist": list(current.get("private_allowlist") or []),
        "group_allowlist": groups,
    }


def _policy_payload(existing: dict | None, group_id: str, *, enabled: bool) -> dict:
    payload = dict(existing or {})
    payload.update(
        {
            "group_id": group_id,
            "enabled": bool(enabled),
            # Access only enables conversation.  Work execution and proactive
            # participation remain separate, fail-closed permissions.
            "mention_only": True,
            "active_reply": False,
            "allow_work": False,
            "allowed_work_senders": "",
        },
    )
    return payload


def _clone_policy_payload(
    source: dict,
    existing_target: dict | None,
    target_group_id: str,
) -> dict:
    """Copy only configurable behavior; never copy identity or runtime counters."""

    payload = {
        key: source[key]
        for key in _GROUP_POLICY_COPY_FIELDS
        if key in source
    }
    payload.update(
        {
            "group_id": target_group_id,
            # A source group's display/session identity must not leak into a
            # different group merely because its behavior was cloned.
            "group_name": str((existing_target or {}).get("group_name") or ""),
            "session": str((existing_target or {}).get("session") or ""),
        },
    )
    return payload


def _policies_match(target: dict | None, source: dict | None) -> bool:
    if not target or not source:
        return False
    return all(target.get(key) == source.get(key) for key in _GROUP_POLICY_COPY_FIELDS)


def _receipt(action_type: str, status: str, group_id: str, **facts: object) -> dict:
    return {
        "receipt_id": "qq-action-" + uuid.uuid4().hex,
        "action_type": action_type,
        "status": status,
        "target_type": "qq_group",
        "target_id": group_id,
        "facts": facts,
    }


def _group_access_state(conn: sqlite3.Connection, group_id: str) -> dict:
    current = get_qq_access_settings(conn)
    groups = list(current.get("group_allowlist") or [])
    entry = next((item for item in groups if str(item.get("group_id") or "") == group_id), None)
    policy = get_group_policy(conn, group_id)
    return {
        "config_version": int(current["settings"].get("config_version") or 0),
        "group_chat_enabled": bool(current["settings"].get("group_chat_enabled")),
        "allowlisted": bool(entry and entry.get("enabled")),
        "policy_enabled": bool(policy and int(policy.get("enabled") or 0)),
        "mention_only": bool(policy is None or int(policy.get("mention_only") or 0)),
        "allow_work": bool(policy and int(policy.get("allow_work") or 0)),
    }


def _recent_group_metadata(conn: sqlite3.Connection, group_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT created_at,is_mention,decision,decision_reason,replied
        FROM group_messages WHERE group_id=? ORDER BY id DESC LIMIT 8
        """,
        (group_id,),
    ).fetchall()
    return [
        {
            "created_at": str(row[0] or ""),
            "is_mention": bool(row[1]),
            "decision": str(row[2] or ""),
            "decision_reason": _clip(row[3], 120),
            "replied": bool(row[4]),
        }
        for row in rows
    ]


def _format_state_reply(group_id: str, state: dict, *, diagnostic: dict | None = None) -> str:
    effective = state["group_chat_enabled"] and state["allowlisted"] and state["policy_enabled"]
    lines = [
        f"群 {group_id} 当前{'已生效' if effective else '未生效'}。",
        f"- QQ 群聊总开关：{'开启' if state['group_chat_enabled'] else '关闭'}",
        f"- 群准入：{'已加入' if state['allowlisted'] else '未加入'}",
        f"- 群回复策略：{'启用' if state['policy_enabled'] else '禁用'}，{'仅被 @ 时回复' if state['mention_only'] else '允许主动参与'}",
        f"- 群内工作执行：{'允许' if state['allow_work'] else '关闭'}",
        f"- 配置版本：{state['config_version']}",
    ]
    if diagnostic is not None:
        recent = diagnostic.get("recent") or []
        models = diagnostic.get("models") or {}
        if recent:
            latest = recent[0]
            lines.extend(
                [
                    f"- 最近群事件：{latest.get('created_at') or '未知时间'}，决策 {latest.get('decision') or '未决策'}，"
                    f"{'已回复' if latest.get('replied') else '未回复'}",
                    f"- 最近决策原因：{latest.get('decision_reason') or '未记录'}",
                ],
            )
        else:
            lines.append("- 最近群事件：Bridge 未记录到该群的入站/回复决策元数据。")
        for role in ("interaction_classifier", "conversation_reply"):
            item = models.get(role) if isinstance(models, dict) else None
            if isinstance(item, dict):
                lines.append(
                    f"- {role} 模型：{'就绪' if item.get('ready') else '未就绪'}"
                    + (f"（{item.get('reason')}）" if item.get("reason") else ""),
                )
        lines.append("以上只读取配置和链路元数据，没有读取或发送群消息正文。")
    return "\n".join(lines)


def execute_qq_admin_action(
    connect: Callable[[], sqlite3.Connection],
    *,
    actor_id: str,
    action: dict,
    trace_id: str = "",
    model_readiness: Callable[[], dict] | None = None,
) -> dict:
    action_type = str(action.get("action_type") or "")
    group_id = str(action.get("group_id") or "")
    if action_type == "qq_group_clarification":
        return {
            "ok": True,
            "dispatch": "control_clarification",
            "reply": "请告诉我要调整的 QQ 群号；我只会修改 Bridge 的可审计准入配置，不会改源码或插件环境变量。",
        }
    if action_type == "qq_group_clone_clarification":
        return {
            "ok": True,
            "dispatch": "control_clarification",
            "reply": "我已识别到要对齐群配置，但缺少作为模板的 QQ 群号；本轮没有修改任何配置。",
            "action_receipts": [_receipt(action_type, "not_started", "", reason="source_group_required")],
        }
    if not group_id and action_type != "qq_group_allowlist_list":
        return None

    try:
        with connect() as conn:
            _authorise(conn, actor_id)
            if action_type == "qq_group_allowlist_list":
                current = get_qq_access_settings(conn)
                groups = [
                    dict(item)
                    for item in current.get("group_allowlist") or []
                    if item.get("enabled")
                ]
                group_ids = [str(item.get("group_id") or "") for item in groups if item.get("group_id")]
                receipt = _receipt(
                    action_type,
                    "completed",
                    "",
                    config_version=int(current["settings"].get("config_version") or 0),
                    group_allowlist_count=len(group_ids),
                    group_ids=group_ids,
                )
                lines = [f"已通过 Bridge 只读查询，当前 QQ 群准入列表共 {len(group_ids)} 个群。"]
                if group_ids:
                    lines.extend(f"- {group_id}" for group_id in group_ids)
                else:
                    lines.append("- 当前没有已启用的准入群。")
                lines.append(f"- 配置版本：{receipt['facts']['config_version']}")
                return {
                    "ok": True,
                    "dispatch": "control_status",
                    "reply": "\n".join(lines),
                    "action_receipts": [receipt],
                }
            if action_type in {"qq_group_status_read", "qq_group_diagnose"}:
                state = _group_access_state(conn, group_id)
                diagnostic = None
                if action_type == "qq_group_diagnose":
                    diagnostic = {
                        "recent": _recent_group_metadata(conn, group_id),
                        "models": model_readiness() if model_readiness else {},
                    }
                receipt = _receipt(action_type, "completed", group_id, **state)
                return {
                    "ok": True,
                    "dispatch": "control_diagnostic" if diagnostic is not None else "control_status",
                    "reply": _format_state_reply(group_id, state, diagnostic=diagnostic),
                    "action_receipts": [receipt],
                    "diagnostic": diagnostic,
                }

            if action_type not in {
                "qq_group_allowlist_enable",
                "qq_group_allowlist_disable",
                "qq_group_policy_clone",
            }:
                return None
            source_group_id = str(action.get("source_group_id") or "")
            if action_type == "qq_group_policy_clone" and (not source_group_id or source_group_id == group_id):
                return {
                    "ok": True,
                    "dispatch": "control_failed",
                    "reply": "群配置对齐没有执行：目标群与模板群必须是两个不同且已明确的群。",
                    "action_receipts": [_receipt(action_type, "failed", group_id, reason="source_group_invalid")],
                }
            enable = action_type == "qq_group_allowlist_enable"
            current = get_qq_access_settings(conn)
            source_policy = get_group_policy(conn, source_group_id) if source_group_id else None
            target_policy = get_group_policy(conn, group_id)
            if action_type == "qq_group_policy_clone" and not source_policy:
                return {
                    "ok": True,
                    "dispatch": "control_failed",
                    "reply": "群配置对齐没有执行：模板群没有可读取的群策略配置。",
                    "action_receipts": [_receipt(action_type, "failed", group_id, reason="source_policy_missing")],
                }
            groups = [
                dict(item)
                for item in current.get("group_allowlist") or []
                if str(item.get("group_id") or "") != group_id
            ]
            existing_state = _group_access_state(conn, group_id)
            if action_type == "qq_group_policy_clone":
                existing_entry = next(
                    (
                        item for item in current.get("group_allowlist") or []
                        if str(item.get("group_id") or "") == group_id
                    ),
                    None,
                )
                groups.append({
                    "group_id": group_id,
                    "enabled": True,
                    "remark": str((existing_entry or {}).get("remark") or "Owner-approved policy clone"),
                })
                desired_matches = (
                    existing_state["allowlisted"]
                    and existing_state["group_chat_enabled"]
                    and _policies_match(target_policy, source_policy)
                )
                if desired_matches:
                    receipt = _receipt(
                        action_type,
                        "no_op",
                        group_id,
                        source_group_id=source_group_id,
                        config_version=existing_state["config_version"],
                        policy_aligned=True,
                    )
                    return {
                        "ok": True,
                        "dispatch": "control_action",
                        "reply": "目标群已在准入范围内，并且可配置的群参与、权限与通知策略已与模板群一致；本轮没有重复写入。",
                        "action_receipts": [receipt],
                    }
            default_desired_matches = (
                action_type != "qq_group_policy_clone"
                and existing_state["allowlisted"] == enable
                and existing_state["policy_enabled"] == enable
                and (not enable or existing_state["group_chat_enabled"])
                and existing_state["mention_only"]
                and not existing_state["allow_work"]
            )
            if default_desired_matches:
                receipt = _receipt(action_type, "no_op", group_id, **existing_state)
                return {
                    "ok": True,
                    "dispatch": "control_action",
                    "reply": _format_state_reply(group_id, existing_state),
                    "action_receipts": [receipt],
                }
            if enable:
                groups.append({"group_id": group_id, "enabled": True, "remark": "QQ 管理员私聊授权"})
            changed_by = _actor_ref(actor_id)
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            try:
                updated = update_qq_access_settings(
                    conn,
                    _settings_payload(
                        current,
                        groups=groups,
                        group_chat_enabled=bool(groups),
                    ),
                    idempotency_key=(
                        f"qq-admin:{action_type}:{group_id}:"
                        f"{int(current['settings'].get('config_version') or 0)}:{_clip(trace_id, 80)}"
                    ),
                    changed_by=changed_by,
                )
                if action_type == "qq_group_policy_clone":
                    upsert_group_policy(
                        conn,
                        _clone_policy_payload(source_policy, target_policy, group_id),
                    )
                else:
                    upsert_group_policy(
                        conn,
                        _policy_payload(target_policy, group_id, enabled=enable),
                    )
                state = _group_access_state(conn, group_id)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            receipt_facts = {**state, "audit_event": "qq_access_settings_updated"}
            if action_type == "qq_group_policy_clone":
                receipt_facts.update({"source_group_id": source_group_id, "policy_aligned": True})
            receipt = _receipt(action_type, "completed", group_id, **receipt_facts)
            if action_type == "qq_group_policy_clone":
                return {
                    "ok": True,
                    "dispatch": "control_action",
                    "reply": (
                        f"群 {group_id} 已加入准入，且可配置的群参与、权限与通知策略已原子对齐到群 {source_group_id}；"
                        f"配置版本 {updated['settings']['config_version']}。"
                    ),
                    "action_receipts": [receipt],
                }
            verb = "已加入群准入" if enable else "已撤销群准入"
            reply = (
                f"群 {group_id} {verb}，配置版本 {updated['settings']['config_version']}。"
                + ("当前仅在被 @ 时回复；群内工作执行仍关闭。" if enable else "该群回复策略也已停用。")
            )
            return {
                "ok": True,
                "dispatch": "control_action",
                "reply": reply,
                "action_receipts": [receipt],
            }
    except PermissionError:
        return {
            "ok": True,
            "dispatch": "control_denied",
            "reply": "这项操作需要 QQ 管理员权限，本轮没有修改任何配置。",
            "action_receipts": [_receipt(action_type, "denied", group_id, reason="administrator_required")],
        }
    except (sqlite3.Error, ValueError) as exc:
        return {
            "ok": True,
            "dispatch": "control_failed",
            "reply": f"这项操作没有完成，Bridge 返回：{_clip(exc, 160) or 'unknown_error'}。配置未被当作成功处理。",
            "action_receipts": [_receipt(action_type, "failed", group_id, reason=_clip(exc, 160))],
        }


def build_qq_control_model_readiness(
    get_fallback: Callable[[], dict],
    get_role_settings: Callable[[str, dict], dict],
    readiness_check: Callable[[dict], tuple[bool, str]],
) -> dict:
    """Return configuration readiness only; never expose provider secrets."""

    fallback = get_fallback()
    result: dict[str, dict] = {}
    for role in ("interaction_classifier", "conversation_reply"):
        try:
            settings = get_role_settings(role, fallback)
            ready, reason = readiness_check(settings)
            result[role] = {
                "ready": bool(ready),
                "reason": str(reason or ""),
                "provider": str(settings.get("chat_provider") or "codex"),
                "model": str(settings.get("chat_model") or settings.get("codex_model") or ""),
            }
        except (sqlite3.Error, ValueError) as exc:
            result[role] = {"ready": False, "reason": str(exc)[:120]}
    return result


def build_qq_control_mode_decision(action: dict) -> dict:
    action_type = str(action.get("action_type") or "qq_control")
    plan = {
        "schema_version": 1,
        "summary_mode": "work",
        "primary_intent": "ops",
        "confidence": 1.0,
        "reason": "命中 Bridge 受支持的确定性 QQ 管理动作。",
        "intents": [
            {
                "id": "intent-1",
                "type": "ops",
                "confidence": 1.0,
                "objective": "执行或查询 QQ 群准入状态",
                "requires_tools": False,
                "risk_level": "low" if action_type.endswith(("read", "diagnose")) else "medium",
            },
        ],
        "reply_parts": [],
        # The server-issued ActionReceipt represents execution.  The plan only
        # declares that a factual response will be assembled; it grants no LLM
        # control-plane authority.
        "actions": [
            {
                "id": "action-1",
                "type": "respond",
                "intent_id": "intent-1",
                "objective": f"返回 {action_type} 的结构化事实与回执",
                "requires_tools": False,
                "risk_level": "none",
            },
        ],
        "approval_requests": [],
        "memory_candidates": [],
    }
    return {
        "mode": "work",
        "intent": "ops",
        "confidence": 1.0,
        "reason": plan["reason"],
        "work_lifecycle": "none",
        "end_work": False,
        "allow_emoji": False,
        "need_tools": False,
        "response_style": "structured",
        "emotion": "neutral",
        "reply_length": "medium",
        "meme_intent": "none",
        "engagement": "respond",
        "source": "qq_control_router",
        "interaction_plan": plan,
    }


def dispatch_qq_admin_action(
    connect: Callable[[], sqlite3.Connection],
    store: object,
    actor_id: str,
    message: str,
    history: list[dict] | None,
    trace_id: str,
    source: str,
    get_fallback: Callable[[], dict],
    get_role_settings: Callable[[str, dict], dict],
    readiness_check: Callable[[dict], tuple[bool, str]],
    current_group_id: str = "",
) -> dict | None:
    """Run one supported control route and persist its factual exchange."""

    action = parse_qq_admin_action(
        message,
        history,
        current_group_id=current_group_id,
    )
    if action is None:
        return None
    result = execute_qq_admin_action(
        connect,
        actor_id=actor_id,
        action=action,
        trace_id=trace_id,
        model_readiness=lambda: build_qq_control_model_readiness(
            get_fallback, get_role_settings, readiness_check,
        ),
    )
    if result is None:
        return None
    mode_decision = build_qq_control_mode_decision(action)
    plan_record = store.persist(actor_id, mode_decision, source=source)
    store.record_exchange(
        actor_id,
        message,
        str(result.get("reply") or ""),
        mode_decision,
        source=source,
    )
    result.update(
        {
            "mode": "work",
            "intent": "ops",
            "mode_decision": mode_decision,
            "interaction_plan": mode_decision["interaction_plan"],
            "interaction_plan_record": plan_record,
        },
    )
    return result


__all__ = [
    "build_qq_control_mode_decision",
    "build_qq_control_model_readiness",
    "dispatch_qq_admin_action",
    "execute_qq_admin_action",
    "parse_qq_admin_action",
]
