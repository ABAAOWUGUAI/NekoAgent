#!/usr/bin/env python3
"""Deterministic, send-independent policy for bounded visual observation.

This module decides whether the current visual attachment may enter a bounded
observation attempt.  It never calls a model, reads media bytes, or authorizes
an outbound reply.  The event identifier is used only as input to a stable
SHA-256 sample so repeated delivery of one event cannot change the result.
"""

from __future__ import annotations

import hashlib
import math


# Keep the media burst guard aligned with the existing AC-4 default.  The
# caller passes the current burst count; a count at this bound is exhausted.
DEFAULT_MEDIA_BURST_LIMIT = 6
_DECISIONS = frozenset({"observe", "deferred", "blocked"})
_REASON_CATEGORIES = frozenset(
    {
        "addressed_media",
        "ambient_probability_sample",
        "ambient_probability_miss",
        "media_budget_exhausted",
        "media_burst_exhausted",
        "media_observation_disabled",
        "media_topic_inactive",
        "media_participation_mode",
        "media_participation_disabled",
        "media_event_invalid",
    },
)


def _bounded_probability(value: object) -> float:
    try:
        probability = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(probability):
        return 0.0
    return max(0.0, min(probability, 1.0))


def _bounded_count(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _stable_sample(event_id: object) -> float:
    """Map an event id to a stable value in ``[0, 1)`` without global state."""

    value = str(event_id or "").strip().encode("utf-8", "ignore")
    digest = hashlib.sha256(value).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _result(decision: str, reason_category: str) -> dict[str, str]:
    # Keep the public shape intentionally small: there is no send permission
    # field to accidentally interpret as authorization for an outbound reply.
    if decision not in _DECISIONS:
        decision = "blocked"
    if reason_category not in _REASON_CATEGORIES:
        reason_category = "media_event_invalid"
    return {"decision": decision, "reason_category": reason_category}


def select_media_observation(
    *,
    event_id: str,
    participation_mode: str,
    addressed: bool,
    topic_active: bool,
    probability: float,
    burst_count: int,
    daily_remaining: int,
    burst_limit: int = DEFAULT_MEDIA_BURST_LIMIT,
) -> dict:
    """Select one bounded visual observation attempt.

    Explicitly addressed visual turns are observed in every enabled
    participation mode.  Ambient visual turns are eligible only for natural
    participation while a topic is active and are then sampled by the
    supplied probability.  Budget checks happen first and always block.
    """

    daily = _bounded_count(daily_remaining, default=0)
    # An invalid burst count is treated as exhausted (fail closed).  Negative
    # values are also invalid rather than an accidental unlimited allowance.
    burst = _bounded_count(burst_count, default=DEFAULT_MEDIA_BURST_LIMIT)
    limit = max(1, _bounded_count(burst_limit, default=DEFAULT_MEDIA_BURST_LIMIT))
    if daily <= 0:
        return _result("blocked", "media_budget_exhausted")
    if burst < 0 or burst >= limit:
        return _result("blocked", "media_burst_exhausted")

    mode = str(participation_mode or "").strip().lower()
    if mode in {"", "disabled"}:
        return _result("blocked", "media_participation_disabled")

    if bool(addressed):
        return _result("observe", "addressed_media")

    if mode != "natural_participation":
        return _result("deferred", "media_participation_mode")
    if not bool(topic_active):
        return _result("deferred", "media_topic_inactive")

    chance = _bounded_probability(probability)
    if chance <= 0.0:
        return _result("deferred", "media_observation_disabled")
    if _stable_sample(event_id) < chance:
        return _result("observe", "ambient_probability_sample")
    return _result("deferred", "ambient_probability_miss")


__all__ = [
    "DEFAULT_MEDIA_BURST_LIMIT",
    "select_media_observation",
]
