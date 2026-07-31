#!/usr/bin/env python3
"""Admin-only HTTP adapter for Gate C3 cutover."""

from __future__ import annotations

from bridge_auth import PrincipalKind
from bridge_reliability_cutover import reliability_cutover_plan, set_reliability_feature


def _public_delivery_error(error: object) -> tuple[str, str]:
    """Return a useful, redacted reason for an operator-facing dead letter.

    Outbox errors can contain a destination, a provider body, or user content.
    The task centre needs an actionable category, never the original error.
    """

    raw = str(error or "").strip()
    lower = raw.lower()
    if "actionfailed" in lower:
        return (
            "qq_transport_rejected",
            "QQ 发送被平台拒绝；请复查该群权限、禁言或连接状态。历史记录不代表当前仍是同一状态。",
        )
    if "timeout" in lower:
        return ("timeout", "发送或上游响应超时；如显示“结果不确定”，重新送达可能产生重复消息。")
    if "auth" in lower or "forbidden" in lower or "403" in lower:
        return ("authorization", "渠道或上游拒绝授权；请核对当前连接与权限后再重试。")
    if "rate" in lower or "429" in lower:
        return ("rate_limited", "渠道或上游触发频率限制；请稍后再重试。")
    if "network" in lower or "connection" in lower or "unreachable" in lower:
        return ("network", "渠道或上游连接失败；请核对服务状态和网络后再重试。")
    if not raw:
        return ("unknown", "没有可安全展示的失败详情；重新送达前请先确认当前渠道状态。")
    return ("delivery_failed", "该次送达失败；详细原始错误不会在控制台展示，以保护消息和渠道信息。")


def _public_delivery_source(row: dict) -> str:
    """Classify delivery purpose from safe payload metadata only."""

    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if str(payload.get("group_id") or "").strip() or str(payload.get("user_id") or "").startswith("group:"):
        return "群聊回复"
    kind = str(payload.get("kind") or "")
    if kind == "assistant_reply":
        return "私聊回复"
    if kind in {"task_result", "task_progress", "approval_request"}:
        return "任务送达"
    if kind:
        return "系统送达"
    return "未标注来源"


class ReliabilityHttpApi:
    PATH = "/reliability/cutover"

    def __init__(self, connect, json_response, channel_token_distinct, qq_ready, delivery_reader=None, delivery_requeue=None):
        self.connect = connect
        self.json_response = json_response
        self.channel_token_distinct = channel_token_distinct
        self.qq_ready = qq_ready
        self.delivery_reader = delivery_reader
        self.delivery_requeue = delivery_requeue

    @staticmethod
    def _admin(principal) -> bool:
        return principal in {PrincipalKind.ADMIN_SESSION, PrincipalKind.ADMIN_TOKEN}

    @staticmethod
    def matches_post(path: str) -> bool:
        return path == ReliabilityHttpApi.PATH or (
            path.startswith("/reliability/dead-letters/") and path.endswith("/requeue")
        )

    def handle_get(self, request, path: str, principal) -> bool:
        if path not in {self.PATH, "/reliability/dead-letters"}:
            return False
        if not self._admin(principal):
            self.json_response(request, 403, {"ok": False, "error": "forbidden"})
            return True
        if path == "/reliability/dead-letters":
            rows = self.delivery_reader("dead_letter", 50) if self.delivery_reader else []
            ambiguous = self.delivery_reader("ambiguous", 50) if self.delivery_reader else []
            rows = list({str(row.get("id") or ""): row for row in [*rows, *ambiguous]}.values())
            items = []
            for row in rows:
                error_kind, error_summary = _public_delivery_error(row.get("last_error"))
                certainty = str(row.get("delivery_certainty") or "pending")
                items.append({
                    "id": str(row.get("id") or ""), "channel": str(row.get("channel") or ""),
                    "attempt": int(row.get("attempt") or 0), "max_attempts": int(row.get("max_attempts") or 0),
                    "error_kind": error_kind, "error_summary": error_summary,
                    "source_kind": _public_delivery_source(row),
                    "dead_lettered_at": str(row.get("dead_lettered_at") or ""),
                    "delivery_certainty": certainty,
                    "review_status": "结果不确定，须确认重复风险" if certainty == "ambiguous" else "历史失败，已停止自动重试",
                    "requires_duplicate_risk_confirmation": certainty == "ambiguous",
                })
            self.json_response(request, 200, {
                "ok": True,
                "dead_letters": items,
                "requires_owner_review": len(items),
                "explanatory_note": "这里是已停止自动重试的历史送达记录，不是当前待发送队列；只有确认后重新送达才会回到队列。",
            })
            return True
        with self.connect() as conn:
            result = reliability_cutover_plan(
                conn, channel_token_distinct=self.channel_token_distinct(), qq_ready=self.qq_ready(),
            )
        self.json_response(request, 200, {"ok": True, **result})
        return True

    def handle_post(self, request, path: str, payload: dict, principal) -> bool:
        requeue = path != self.PATH and self.matches_post(path)
        if not self.matches_post(path):
            return False
        if not self._admin(principal):
            self.json_response(request, 403, {"ok": False, "error": "forbidden"})
            return True
        if requeue:
            if payload.get("confirm_requeue") is not True:
                self.json_response(request, 400, {"ok": False, "error": "delivery_requeue_confirmation_required"})
                return True
            delivery_id = path.split("/")[-2]
            ambiguous = self.delivery_reader("ambiguous", 500) if self.delivery_reader else []
            if any(str(row.get("id") or "") == delivery_id for row in ambiguous):
                if payload.get("confirm_duplicate_risk") is not True:
                    self.json_response(
                        request,
                        409,
                        {"ok": False, "error": "delivery_duplicate_risk_confirmation_required"},
                    )
                    return True
            try:
                delivery = self.delivery_requeue(delivery_id) if self.delivery_requeue else None
            except ValueError as exc:
                self.json_response(request, 409, {"ok": False, "error": str(exc)})
                return True
            self.json_response(request, 200 if delivery else 404, {"ok": bool(delivery), "delivery_id": delivery_id})
            return True
        try:
            if not isinstance(payload.get("enabled"), bool):
                raise ValueError("reliability_enabled_boolean_required")
            with self.connect() as conn:
                result = set_reliability_feature(
                    conn, bool(payload["enabled"]),
                    expect_plan_checksum=str(payload.get("expect_plan_checksum") or ""),
                    channel_token_distinct=self.channel_token_distinct(), qq_ready=self.qq_ready(),
                )
        except ValueError as exc:
            self.json_response(request, 409, {"ok": False, "error": str(exc)})
            return True
        self.json_response(request, 200, {"ok": True, **result})
        return True


__all__ = ["ReliabilityHttpApi"]
