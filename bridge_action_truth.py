#!/usr/bin/env python3
"""Server-side guard for ungrounded operational claims.

Conversation models may propose wording, but only Bridge-owned executors can
claim that a control-plane or runtime action actually happened.  This module
keeps that boundary deterministic and independent from model prompts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


_OPERATION_TERMS = (
    "白名单",
    "准入",
    "配置",
    "provider",
    "服务",
    "服务器",
    "容器",
    "日志",
    "数据库",
    "模型",
    "部署",
    "上线",
    "重启",
    "重载",
    "reload",
    "测试消息",
)

_CLAIM_MARKERS = (
    "我已经",
    "我已",
    "我刚刚",
    "我刚才",
    "我现在就",
    "我马上",
    "我正在",
    "这边已经",
    "后台已经",
    "已经加好",
    "已经添加",
    "已经加入",
    "已经开放",
    "已经启用",
    "已经关闭",
    "已经撤销",
    "已经修改",
    "已经写入",
    "已经重载",
    "已经重启",
    "已经修复",
    "已经测试",
    "已经验证",
    "已加好",
    "已添加",
    "已加入",
    "已开放",
    "已启用",
    "已关闭",
    "已撤销",
    "已修改",
    "已写入",
    "已重载",
    "已重启",
    "已修复",
    "已测试",
    "已验证",
    "重载完了",
    "配置好了",
    "测试成功",
    "确认能正常",
)


_OPERATION_TERMS = _OPERATION_TERMS + (
    "cron",
    "\u5b9a\u65f6\u4efb\u52a1",
    "\u5b9a\u65f6\u8ba1\u5212",
    "\u4efb\u52a1",
)
_CLAIM_MARKERS = _CLAIM_MARKERS + (
    "\u67e5\u8fc7\u4e86",
    "\u770b\u8fc7\u4e86",
    "\u770b\u4e86\u4e00\u4e0b",
    "\u786e\u8ba4\u4e86\u4e00\u4e0b",
    "\u68c0\u67e5\u8fc7\u4e86",
    "\u6838\u5bf9\u8fc7\u4e86",
    "\u7edf\u8ba1\u8fc7\u4e86",
)


def completed_receipts(receipts: Iterable[Mapping[str, object]] | None) -> list[dict]:
    return [
        dict(item)
        for item in receipts or ()
        if str(item.get("status") or "").strip().lower() in {"completed", "no_op"}
        and str(item.get("action_type") or "").strip()
    ]


def has_ungrounded_action_claim(
    reply: object,
    receipts: Iterable[Mapping[str, object]] | None = None,
) -> bool:
    """Return True when a reply claims operations without a completed receipt."""

    if completed_receipts(receipts):
        return False
    text = str(reply or "").strip().lower()
    if not text or not any(term in text for term in _OPERATION_TERMS):
        return False
    return any(marker in text for marker in _CLAIM_MARKERS)


def enforce_action_truth(
    reply: object,
    receipts: Iterable[Mapping[str, object]] | None = None,
) -> tuple[str, bool]:
    """Replace an unsupported execution claim with a fail-closed fact block."""

    text = str(reply or "").strip()
    if not has_ungrounded_action_claim(text, receipts):
        return text, False
    return (
        "我不能把这项操作说成已经完成：本轮没有产生可验证的动作回执。"
        "当前只能确认尚未通过 Bridge 执行配置修改、服务重启、日志检查或测试。"
        "请给出明确操作目标；受支持的操作会走可审计动作，不支持的操作我会直接说明。",
        True,
    )


__all__ = [
    "completed_receipts",
    "enforce_action_truth",
    "has_ungrounded_action_claim",
]
