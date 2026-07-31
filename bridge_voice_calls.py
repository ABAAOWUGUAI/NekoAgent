#!/usr/bin/env python3
"""Deterministic policy and lifecycle rules for realtime voice calls.

This module deliberately contains no QQ, X11, audio, HTTP or OpenAI code.  A
channel adapter may control the real call, while the application layer remains
responsible for persistence and supplies current budget/concurrency facts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping


class CallState(str, Enum):
    RINGING = "ringing"
    AUTHORIZING = "authorizing"
    CONNECTING = "connecting"
    ACTIVE = "active"
    ENDING = "ending"
    COOLDOWN = "cooldown"
    FAILED = "failed"


ALLOWED_TRANSITIONS: Mapping[CallState, frozenset[CallState]] = {
    CallState.RINGING: frozenset({CallState.AUTHORIZING, CallState.ENDING, CallState.FAILED}),
    CallState.AUTHORIZING: frozenset({CallState.CONNECTING, CallState.ENDING, CallState.FAILED}),
    CallState.CONNECTING: frozenset({CallState.ACTIVE, CallState.ENDING, CallState.FAILED}),
    CallState.ACTIVE: frozenset({CallState.ENDING, CallState.FAILED}),
    CallState.ENDING: frozenset({CallState.COOLDOWN, CallState.FAILED}),
    CallState.COOLDOWN: frozenset(),
    CallState.FAILED: frozenset({CallState.ENDING, CallState.COOLDOWN}),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("voice_call_timestamp_must_be_timezone_aware")
    return value


@dataclass(frozen=True)
class VoiceCallPolicy:
    enabled: bool = False
    allowed_callers: frozenset[str] = frozenset()
    max_concurrent_calls: int = 1
    max_duration_seconds: int = 30 * 60
    connect_timeout_seconds: int = 30
    cooldown_seconds: int = 60
    daily_budget_seconds: int = 60 * 60
    retain_transcript: bool = False
    transcript_retention_days: int = 0

    def __post_init__(self) -> None:
        normalized = frozenset(str(item).strip() for item in self.allowed_callers if str(item).strip())
        object.__setattr__(self, "allowed_callers", normalized)
        if self.enabled and not normalized:
            raise ValueError("voice_call_allowlist_required")
        if self.max_concurrent_calls != 1:
            raise ValueError("voice_call_single_concurrency_required")
        for name in (
            "max_duration_seconds",
            "connect_timeout_seconds",
            "cooldown_seconds",
            "daily_budget_seconds",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"invalid_{name}")
        if self.retain_transcript and self.transcript_retention_days <= 0:
            raise ValueError("voice_call_transcript_retention_required")
        if not self.retain_transcript and self.transcript_retention_days != 0:
            raise ValueError("voice_call_transcript_retention_must_be_zero")


@dataclass(frozen=True)
class VoiceCallFacts:
    active_calls: int = 0
    used_seconds_today: int = 0
    last_call_ended_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.active_calls < 0 or self.used_seconds_today < 0:
            raise ValueError("invalid_voice_call_facts")
        if self.last_call_ended_at is not None:
            _aware(self.last_call_ended_at)


@dataclass(frozen=True)
class VoiceCallDecision:
    accepted: bool
    reason: str


def authorize_incoming_call(
    policy: VoiceCallPolicy,
    caller_id: str,
    facts: VoiceCallFacts,
    *,
    now: datetime | None = None,
) -> VoiceCallDecision:
    """Apply fail-closed server rules before the channel accepts a call."""

    current = _aware(now or utc_now())
    caller = str(caller_id).strip()
    if not policy.enabled:
        return VoiceCallDecision(False, "policy_disabled")
    if not caller or caller not in policy.allowed_callers:
        return VoiceCallDecision(False, "caller_not_allowed")
    if facts.active_calls >= policy.max_concurrent_calls:
        return VoiceCallDecision(False, "concurrency_limit")
    if facts.used_seconds_today >= policy.daily_budget_seconds:
        return VoiceCallDecision(False, "daily_budget_exhausted")
    if facts.last_call_ended_at is not None:
        cooldown_until = facts.last_call_ended_at + timedelta(seconds=policy.cooldown_seconds)
        if current < cooldown_until:
            return VoiceCallDecision(False, "cooldown_active")
    return VoiceCallDecision(True, "allowed")


@dataclass
class VoiceCallSession:
    call_id: str
    caller_id: str
    state: CallState = CallState.RINGING
    state_changed_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    reason: str = "incoming_call"
    history: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.call_id = str(self.call_id).strip()
        self.caller_id = str(self.caller_id).strip()
        if not self.call_id or not self.caller_id:
            raise ValueError("voice_call_identity_required")
        _aware(self.state_changed_at)
        self._record(self.state, self.reason, self.state_changed_at)

    def _record(self, state: CallState, reason: str, at: datetime) -> None:
        self.history.append({"state": state.value, "reason": reason, "at": at.isoformat()})

    def transition(self, state: CallState, reason: str, *, at: datetime | None = None) -> None:
        current = _aware(at or utc_now())
        if state not in ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid_voice_call_transition:{self.state.value}:{state.value}")
        if current < self.state_changed_at:
            raise ValueError("voice_call_time_moved_backwards")
        self.state = state
        self.state_changed_at = current
        self.reason = str(reason).strip() or "unspecified"
        if state is CallState.ACTIVE and self.started_at is None:
            self.started_at = current
        if state in {CallState.COOLDOWN, CallState.FAILED}:
            self.ended_at = current
        self._record(state, self.reason, current)

    def timeout_reason(self, policy: VoiceCallPolicy, *, now: datetime | None = None) -> str | None:
        current = _aware(now or utc_now())
        if self.state is CallState.CONNECTING:
            deadline = self.state_changed_at + timedelta(seconds=policy.connect_timeout_seconds)
            return "connect_timeout" if current >= deadline else None
        if self.state is CallState.ACTIVE and self.started_at is not None:
            deadline = self.started_at + timedelta(seconds=policy.max_duration_seconds)
            return "max_duration" if current >= deadline else None
        return None

    def public_snapshot(self, *, caller_salt: str) -> dict[str, object]:
        if not caller_salt:
            raise ValueError("voice_call_caller_salt_required")
        caller_hash = hashlib.sha256(f"{caller_salt}:{self.caller_id}".encode("utf-8")).hexdigest()[:16]
        duration = 0
        if self.started_at is not None:
            finish = self.ended_at or utc_now()
            duration = max(0, int((finish - self.started_at).total_seconds()))
        return {
            "call_id": self.call_id,
            "caller_hash": caller_hash,
            "state": self.state.value,
            "reason": self.reason,
            "duration_seconds": duration,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CallState",
    "VoiceCallDecision",
    "VoiceCallFacts",
    "VoiceCallPolicy",
    "VoiceCallSession",
    "authorize_incoming_call",
]
