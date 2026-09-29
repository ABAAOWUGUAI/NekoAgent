#!/usr/bin/env python3
"""QQ-specific refinement and rendering boundary for action commitments."""

from __future__ import annotations

from collections.abc import Mapping
import re
import sqlite3


_GROUP_ID_PATTERN = re.compile(r"(?<!\d)([1-9][0-9]{4,19})(?!\d)")
_POLICY_ALIGNMENT_HINTS = ("对齐", "保持一致", "一致", "复制", "同步", "一样", "相同")
_PRIVATE_SOURCES = {"qq", "qq_private", "private"}


def _group_ids(text: str) -> list[str]:
    values: list[str] = []
    for match in _GROUP_ID_PATTERN.finditer(text):
        value = str(match.group(1))
        if value not in values:
            values.append(value)
    return values


def refine_qq_action_commitment(action: Mapping[str, object], message: str) -> dict:
    """Enrich only a stored QQ target from the current Owner reply."""

    result = dict(action)
    if str(result.get("action_type") or "") != "qq_group_allowlist_enable":
        return result
    target_group_id = str(result.get("group_id") or "")
    text = str(message or "").strip().lower()
    if not target_group_id or not any(hint in text for hint in _POLICY_ALIGNMENT_HINTS):
        return result
    source_ids = [group_id for group_id in _group_ids(text) if group_id != target_group_id]
    if len(source_ids) != 1:
        return result
    return {
        "action_type": "qq_group_policy_clone",
        "group_id": target_group_id,
        "source_group_id": source_ids[0],
    }


def bind_qq_status_action_offer(
    *,
    action_commitments: object | None,
    offered: object,
    plan_record: Mapping[str, object] | None,
    actor_id: str,
    source: str,
    reply: str,
) -> tuple[str, dict | None]:
    """Persist a server-issued QQ offer before presenting it to the Owner."""

    if (
        not isinstance(offered, Mapping)
        or not isinstance(plan_record, Mapping)
        or action_commitments is None
        or str(source or "") not in _PRIVATE_SOURCES
    ):
        return reply, None
    action_type = str(offered.get("action_type") or "")
    if action_type == "qq_group_allowlist_disable_all":
        proposed_reply = (
            str(reply or "")
            + "\n如要执行这次批量撤销，请在 15 分钟内回复“确认”。"
            "未确认前不会修改任何群准入配置。"
        )
    else:
        proposed_reply = (
            str(reply or "")
            + "\n如要把该群加入准入，请回复“需要”。"
            "若要同时按模板群复制参与、权限和通知配置，请在同一回复写明模板群号。"
        )
    try:
        commitment = action_commitments.propose(
            actor_id=actor_id,
            thread_ref=f"qq:private:{actor_id}",
            origin_plan_id=str(plan_record["id"]),
            action=dict(offered),
            rendered_reply=proposed_reply,
        )
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return reply, None
    return proposed_reply, dict(commitment)


def invalidate_unrendered_qq_status_offer(
    action_commitments: object | None,
    commitment: Mapping[str, object] | None,
    *,
    actor_id: str,
) -> None:
    """Prevent a persistence failure from leaving an invisible open offer."""

    if action_commitments is None or not isinstance(commitment, Mapping):
        return
    try:
        action_commitments.invalidate_unrendered(
            str(commitment["id"]),
            actor_id=actor_id,
            thread_ref=f"qq:private:{actor_id}",
        )
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return


__all__ = [
    "bind_qq_status_action_offer",
    "invalidate_unrendered_qq_status_offer",
    "refine_qq_action_commitment",
]
