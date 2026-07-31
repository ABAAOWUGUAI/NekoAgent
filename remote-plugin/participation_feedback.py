"""Failure-notice policy for QQ group participation decisions."""

from __future__ import annotations


def group_event_requires_failure_notice(
    *,
    is_mention: bool,
    participation_metadata: dict,
    result: dict | None = None,
) -> bool:
    """Notify only direct turns; ambient group classifier failures stay silent."""

    if is_mention or bool(participation_metadata.get("reply_to_assistant")):
        return True
    decision = (result or {}).get("group_decision")
    if not isinstance(decision, dict) or not decision.get("deterministic"):
        return False
    return str(decision.get("participation_action") or "") not in {"", "silent"}


__all__ = ["group_event_requires_failure_notice"]
