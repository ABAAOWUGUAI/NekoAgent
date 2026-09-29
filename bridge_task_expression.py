#!/usr/bin/env python3
"""Persona-consistent operational and task expression.

Pure presentation helpers for task status messages.  They never change task
state, never hide approval/safety facts, and never call a model.  The persona
frame is rendered deterministically from the existing ``voice_contract_v1``
structured fields (``warmth``/``directness``/``humor``/``rhythm``; validated by
``bridge_persona_runtime.normalize_voice_contract``) plus the configured
``display_name``, so different assistants with different real Voice Contracts
produce perceptibly different style blocks while the immutable factual block
stays identical.  No persona taxonomy is invented and no persona name is
hardcoded.  The feature flag ``task_expression_v1`` defaults off; when
disabled the original system-language messages remain unchanged.

"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import uuid
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlsplit

from bridge_interaction_contract import (
    interaction_plan_persona_blocks,
    render_response_blocks,
    response_blocks,
)


TASK_EXPRESSION_FLAG = "task_expression_v1"
TRUE_VALUES = {"1", "true", "yes", "on"}
TASK_ACCEPTED = "TASK_ACCEPTED"
MEANINGFUL_PROGRESS = "MEANINGFUL_PROGRESS"
FALLBACK_HEARTBEAT = "FALLBACK_HEARTBEAT"
TASK_SUCCEEDED = "TASK_SUCCEEDED"
ARTIFACT_READY = "ARTIFACT_READY"
TASK_FAILED = "TASK_FAILED"
REVISION_STARTED = "REVISION_STARTED"
REVISION_SUCCEEDED = "REVISION_SUCCEEDED"

PERSONA_FRAME_ACTIONS = {
    "accepted", "append", "approval", "blocked", "meaningful_progress",
    "fallback_heartbeat", "completed", "artifact_ready", "failed",
    "revision_started", "revision_succeeded",
}

# Enum values mirror bridge_persona_runtime._ENUMS (warmth/humor).  Stored
# contracts are already validated by normalize_voice_contract; these sets only
# provide a safe fallback for absent/malformed runtime settings.
_WARMTH_VALUES = {"calm", "balanced", "warm", "expressive"}
_HUMOR_VALUES = {"none", "light", "playful", "dry"}
_DIRECTNESS_VALUES = {"gentle", "balanced", "direct"}
_RHYTHM_VALUES = {"concise", "natural", "varied", "structured"}

# Natural first-person liveness statements.  They prove only that the task is
# still running; none claims a milestone, source, percentage, phase, or
# near-completion.  ``persona_frame`` and ``_event_fact_sentences`` rotate the
# three deterministic variants per warmth.
_HEARTBEAT_WORDING = {
    "calm": ("还在处理中。", "仍然在继续。", "没有中断，还在弄。"),
    "balanced": ("我还在弄。", "还在处理，没丢。", "还在继续。"),
    "warm": ("还在弄着呢。", "还在稳稳地弄。", "还在继续弄，没停。"),
    "expressive": ("没跑，还在干。", "还在这儿弄着呢。", "接着弄，没歇着。"),
}

# Scope values that are generic runtime placeholders rather than user content.
# They are consumed but never echoed verbatim into visible copy.
_GENERIC_SCOPES = {"", "当前请求", "这项任务"}

# Authoritative milestone keys map one-to-one to natural statements.  Each
# statement claims exactly the verified change and nothing after it.
_MILESTONE_NATURAL = {
    "research_started": "开始查资料了。",
    "research_verified": "资料查完，也核验过了。",
    "work_started": "我开始整理成果了。",
    "artifact_generation_started": "成果文件开始生成了。",
    "artifact_ready": "成果文件好了，已核验。",
    "revision_started": "开始改版了，原版不动。",
}

# Bounded humor envelopes.  ``restrained`` always forces ``none``.
_HUMOR_TAIL = {
    "accepted": {
        "none": "",
        "light": "",
        "dry": "不拿口头答应当开工。",
        "playful": "进度条走起来。",
    },
    "milestone": {
        "none": "",
        "light": "",
        "dry": "不是报平安，是实打实走到的。",
        "playful": "进度条真走了一格。",
    },
    "revision_started": {
        "none": "",
        "light": "",
        "dry": "不会拿旧版试错。",
        "playful": "新版开始动工。",
    },
}

# Generic stage phrases driven by the closed failure-stage values.  No
# scenario-specific Research/Document assumption is hardcoded.
_FAILURE_STAGE_PHRASES = {
    "research": "查资料",
    "work": "整理成果",
    "execution_setup": "开工前准备",
}

_ACTION_PHRASES = {
    "retry": "再来一次",
    "change_method": "换个方法",
    "deliver_research_summary": "先发查到的资料摘要给你",
}

# First-person persona frames used when the channel already shows the
# Character's identity (QQ).  The named variants in ``_FRAME`` remain the
# default for channels that do not display the sender.
_FIRST_PERSON_FRAMES = {
    "append": {
        "calm": "我把这条补进了正在办的事里。",
        "balanced": "我记下了，补进正在处理的事里。",
        "warm": "好，我把这条也接进来了。",
        "expressive": "收到，补进去了。",
    },
    "approval": {
        "calm": "我需要你确认后才能继续。",
        "balanced": "我需要你确认一件事。",
        "warm": "我先等一等，需要你确认。",
        "expressive": "先不继续，等你确认。",
    },
    "blocked": {
        "calm": "这边没有办成，原因如下。",
        "balanced": "没能办成，原因如下。",
        "warm": "这边卡住了，原因如下。",
        "expressive": "没办成，你看下原因。",
    },
    "completed": {
        "calm": "已经处理好了。",
        "balanced": "办好了。",
        "warm": "已经办好了，结果在这里。",
        "expressive": "办好了，给你结果。",
    },
    "failed": {
        "calm": "这次没有办成，情况如下。",
        "balanced": "这次没能办成，情况如下。",
        "warm": "这边没办成，先把情况说清楚。",
        "expressive": "这次没办成，情况在这里。",
    },
}

_AUTHORITATIVE_MILESTONE_FACTS = {
    "research_started": "资料查找已经开始。",
    "research_verified": "资料查找已经完成并通过核验。",
    "work_started": "成果整理已经开始。",
    "artifact_generation_started": "成果文件生成已经开始。",
    "artifact_ready": "成果文件已经生成并通过校验。",
    "revision_started": "原版本保持不变，修订已经开始。",
}

_EVENT_ACTIONS = {
    TASK_ACCEPTED: "accepted",
    MEANINGFUL_PROGRESS: "meaningful_progress",
    FALLBACK_HEARTBEAT: "fallback_heartbeat",
    TASK_SUCCEEDED: "completed",
    ARTIFACT_READY: "artifact_ready",
    TASK_FAILED: "failed",
    REVISION_STARTED: "revision_started",
    REVISION_SUCCEEDED: "revision_succeeded",
}
_FRAME = {
    "append": {
        "calm": "{name}把这条补进了正在办的事里。",
        "balanced": "{name}记下了，补进正在处理的事里。",
        "warm": "好，{name}把这条也接进来了。",
        "expressive": "{name}收到，补进去了。",
    },
    "approval": {
        "calm": "{name}需要你确认后才能继续。",
        "balanced": "{name}需要你确认一件事。",
        "warm": "{name}这边先等一等，需要你确认。",
        "expressive": "{name}先不继续，等你确认。",
    },
    "blocked": {
        "calm": "{name}这边没有办成，原因如下。",
        "balanced": "{name}没能办成，原因如下。",
        "warm": "{name}这边卡住了，原因如下。",
        "expressive": "{name}没办成，你看下原因。",
    },
    "completed": {
        "calm": "{name}已经处理好了。",
        "balanced": "{name}办好了。",
        "warm": "{name}已经办好了，结果在这里。",
        "expressive": "{name}办好了，给你结果。",
    },
    "failed": {
        "calm": "{name}这次没有办成，情况如下。",
        "balanced": "{name}这次没能办成，情况如下。",
        "warm": "{name}这边没办成，先把情况说清楚。",
        "expressive": "{name}这次没办成，情况在这里。",
    },
}


def task_expression_enabled(settings: Mapping[str, object]) -> bool:
    """True when the P1-1 expression flag is enabled in assistant settings."""
    return str(settings.get(TASK_EXPRESSION_FLAG) or "").strip().lower() in TRUE_VALUES


def _persona_enabled(settings: Mapping[str, object]) -> bool:
    return str(settings.get("agent_persona_level") or "full").strip().lower() != "off"


def _identity_name(settings: Mapping[str, object]) -> str:
    return str(settings.get("display_name") or "").strip() or "Assistant"


def _contract_field(
    settings: Mapping[str, object],
    field: str,
    valid: set[str],
    default: str,
) -> str:
    contract = settings.get("voice_contract")
    raw = str((contract.get(field) if isinstance(contract, dict) else "") or "").strip().lower()
    return raw if raw in valid else default


def persona_frame(
    settings: Mapping[str, object],
    *,
    action: str,
    variant: int = 0,
    channel_shows_identity: bool = False,
) -> str:
    """Render a deterministic persona-style framing line from the real Voice Contract.

    Consumes existing ``voice_contract_v1`` tone enums plus the configured
    display name. Returns ``""`` when persona is disabled.  When
    ``channel_shows_identity`` is true (QQ already shows the sender), the frame
    is first-person and never repeats the display name in the body.
    """
    if action not in PERSONA_FRAME_ACTIONS or not _persona_enabled(settings):
        return ""
    name = _identity_name(settings)
    warmth = _contract_field(settings, "warmth", _WARMTH_VALUES, "balanced")
    directness = _contract_field(settings, "directness", _DIRECTNESS_VALUES, "balanced")
    humor = _contract_field(settings, "humor", _HUMOR_VALUES, "light")
    if action == "fallback_heartbeat":
        wording = _HEARTBEAT_WORDING[warmth][max(0, int(variant)) % len(_HEARTBEAT_WORDING[warmth])]
        return wording if channel_shows_identity else f"{name}：{wording}"
    frames = _FIRST_PERSON_FRAMES if channel_shows_identity else _FRAME
    if action in frames:
        return frames[action][warmth].format(name="" if channel_shows_identity else name)
    lead = {
        "accepted": "已经接下这件事",
        "meaningful_progress": "有一项进展可以确认",
        "completed": "已经处理完了",
        "artifact_ready": "处理完了，成果也好了",
        "revision_started": "开始改版，原版不动",
        "revision_succeeded": "改版完成，原版没动",
    }.get(action, "把当前情况说明清楚")
    prefix = "先说结果：" if directness == "direct" else "好，" if directness == "gentle" else ""
    suffix = {
        "none": "",
        "light": "，后续只报可确认的变化",
        "dry": "，不拿口头状态算结果",
        "playful": "，进度条开始走",
    }[humor]
    subject = "" if channel_shows_identity else name
    return f"{prefix}{subject}{lead}{suffix}。"


def task_accepted_text() -> str:
    """Neutral user-facing acceptance statement without internal identifiers."""
    return "这件事我记下了，已经在处理，办好了把结果发给你。"


def task_append_text(status: str) -> str:
    """Neutral user-facing append statement without internal identifiers."""
    if str(status or "").strip() == "queued":
        return "收到，我补进正在处理的事项里了，一起办完再告诉你结果。"
    return "记下了，补进当前在办的事里，不会打断正在做的部分，完成后一起给你结果。"


def task_approval_text(approval_code: str) -> str:
    """Approval requirement verbatim: facts that must never be hidden."""
    code = str(approval_code or "")
    return (
        "这项操作可能涉及重启、删除、端口、权限、密钥或生产环境变更，我还没有开始执行。\n"
        f"确认编号：{code}（30 分钟有效）\n"
        f"确认后请回复：确认执行 {code}；也可以回复：拒绝执行 {code}"
    )


def task_terminal_text(status: str) -> str:
    """Neutral truthful terminal statement for a task status."""
    return {
        "done": "这件事办完了。",
        "failed": "这件事没办成，出错了。",
        "timeout": "这件事超时了，没能按时完成。",
        "cancelled": "这件事取消了。",
    }.get(str(status or "").strip(), "这件事已经结束了。")


def task_lifecycle_fact(event: str, *, milestone: str = "") -> str:
    """Return the deterministic fact projection for one bounded C0 event.

    Meaningful progress is closed over an allowlist of authoritative runtime
    milestones. Unknown values fail explicitly instead of being turned into a
    plausible-sounding progress claim.
    """

    normalized = str(event or "").strip().upper()
    if normalized == TASK_ACCEPTED:
        return task_accepted_text()
    if normalized == MEANINGFUL_PROGRESS:
        key = str(milestone or "").strip().lower()
        try:
            return _AUTHORITATIVE_MILESTONE_FACTS[key]
        except KeyError as exc:
            raise ValueError("unsupported_authoritative_milestone") from exc
    if normalized == FALLBACK_HEARTBEAT:
        return "任务仍在运行。"
    if normalized == TASK_SUCCEEDED:
        return task_terminal_text("done")
    if normalized == ARTIFACT_READY:
        return "这件事已经完成，成果文件已经生成并可交付。"
    if normalized == TASK_FAILED:
        return task_terminal_text("failed")
    if normalized == REVISION_STARTED:
        return "原版本保持不变，修订已经开始。"
    if normalized == REVISION_SUCCEEDED:
        return "原版本保持不变，修订后的新版本已经生成并可交付。"
    raise ValueError("unsupported_task_lifecycle_event")


_EVENT_REQUIRED_FACT_SLOTS = {
    TASK_ACCEPTED: ("execution_established", "accepted_scope"),
    MEANINGFUL_PROGRESS: ("milestone",),
    FALLBACK_HEARTBEAT: ("running",),
    TASK_SUCCEEDED: ("completed_scope", "result_detail"),
    ARTIFACT_READY: (
        "completed_scope", "artifact_title", "artifact_version",
        "result_ready", "result_url",
    ),
    TASK_FAILED: (
        "outcome", "completed_stage", "failure_stage", "safe_reason",
        "deliverable_state", "retained_state", "allowed_actions",
    ),
    REVISION_STARTED: (
        "source_version", "requested_change", "preserved_scope", "revision_established",
    ),
    REVISION_SUCCEEDED: (
        "source_version", "preserved_scope", "new_version",
        "result_ready", "result_url",
    ),
}

_FAILURE_STAGE_LABELS = {
    "research": "资料查找阶段",
    "work": "文档整理阶段",
    "execution_setup": "执行准备阶段",
}
_COMPLETED_STAGE_LABELS = {"research": "资料查找", "work": "成果整理"}
_RETAINED_STATE_LABELS = {"research_evidence": "已核验资料"}
_ACTION_LABELS = {
    "retry": "再试一次",
    "change_method": "换一种方法",
    "deliver_research_summary": "先交付已查到的资料摘要",
}


def _plain_fact(value: object, *, maximum: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum or "\x00" in value:
        return ""
    return normalized


def _multiline_fact(value: object, *, maximum: int = 12000) -> str:
    """Validate bounded structured text without flattening its layout."""

    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in value:
        return ""
    return normalized


def _canonical_result_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value
    if (
        not text
        or text != text.strip()
        or len(text) > 2048
        or any(character.isspace() or ord(character) < 32 for character in text)
    ):
        return ""
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    suffix = parsed.path[len("/r/"):] if parsed.path.startswith("/r/") else ""
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not _valid_https_hostname(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or port == 0
        or re.fullmatch(r"[A-Za-z0-9_-]+", suffix) is None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return text


def _valid_fact_slot_values(event: str, slots: Mapping[str, object]) -> bool:
    if event == TASK_ACCEPTED:
        return slots.get("execution_established") is True and bool(
            _plain_fact(slots.get("accepted_scope")),
        )
    if event == MEANINGFUL_PROGRESS:
        return str(slots.get("milestone") or "") in _AUTHORITATIVE_MILESTONE_FACTS
    if event == FALLBACK_HEARTBEAT:
        return slots.get("running") is True
    if event == TASK_SUCCEEDED:
        return bool(
            _plain_fact(slots.get("completed_scope"))
            and _multiline_fact(slots.get("result_detail"))
        )
    if event == ARTIFACT_READY:
        return bool(
            _plain_fact(slots.get("completed_scope"))
            and _plain_fact(slots.get("artifact_title"))
            and _plain_fact(slots.get("artifact_version"), maximum=80)
            and slots.get("result_ready") is True
            and _canonical_result_url(slots.get("result_url"))
        )
    if event == TASK_FAILED:
        outcome = str(slots.get("outcome") or "")
        completed = slots.get("completed_stage")
        retained = slots.get("retained_state")
        actions = slots.get("allowed_actions")
        return bool(
            outcome in {"failed", "timeout", "cancelled"}
            and isinstance(completed, list)
            and len(completed) == len(set(completed))
            and set(completed) <= set(_COMPLETED_STAGE_LABELS)
            and str(slots.get("failure_stage") or "") in _FAILURE_STAGE_LABELS
            and _plain_fact(slots.get("safe_reason"))
            and str(slots.get("deliverable_state") or "") in {"available", "not_available"}
            and isinstance(retained, list)
            and len(retained) == len(set(retained))
            and set(retained) <= set(_RETAINED_STATE_LABELS)
            and isinstance(actions, list)
            and len(actions) == len(set(actions))
            and set(actions) <= set(_ACTION_LABELS)
            and ("research_evidence" not in retained or "research" in completed)
            and (
                "deliver_research_summary" not in actions
                or "research_evidence" in retained
            )
        )
    if event == REVISION_STARTED:
        return slots.get("revision_established") is True and all(
            _plain_fact(slots.get(key))
            for key in ("source_version", "requested_change", "preserved_scope")
        )
    if event == REVISION_SUCCEEDED:
        return bool(
            all(
                _plain_fact(slots.get(key))
                for key in ("source_version", "preserved_scope", "new_version")
            )
            and slots.get("result_ready") is True
            and _canonical_result_url(slots.get("result_url"))
        )
    return False


def _voice_source(settings: Mapping[str, object]) -> dict:
    contract = settings.get("voice_contract")
    avoids = contract.get("avoid_phrases") if isinstance(contract, Mapping) else []
    avoid_phrases = [
        text for item in (avoids if isinstance(avoids, list) else [])
        if (text := _plain_fact(item, maximum=120))
    ]
    return {
        "version": "voice_contract_v1",
        "display_name": _identity_name(settings),
        "directness": _contract_field(settings, "directness", _DIRECTNESS_VALUES, "balanced"),
        "warmth": _contract_field(settings, "warmth", _WARMTH_VALUES, "balanced"),
        "humor": _contract_field(settings, "humor", _HUMOR_VALUES, "light"),
        "rhythm": _contract_field(settings, "rhythm", _RHYTHM_VALUES, "natural"),
        "avoid_phrases": avoid_phrases,
    }


def _join_voice_sentences(sentences: list[str], rhythm: str) -> str:
    if rhythm == "structured":
        return "\n".join(sentences)
    if rhythm == "concise":
        return "".join(sentences)
    if rhythm == "varied" and len(sentences) > 2:
        return sentences[0] + sentences[1] + "\n" + "".join(sentences[2:])
    return " ".join(sentences)


def _styled_sentences(
    sentences: list[str],
    source: Mapping[str, object],
    *,
    event: str,
    restrained: bool,
    channel_shows_identity: bool = False,
) -> str:
    """Join natural statements with the Voice Contract; add identity only when
    the channel does not already display the Character's name."""
    directness = str(source["directness"])
    rhythm = str(source["rhythm"])
    rendered = list(sentences)
    if event != FALLBACK_HEARTBEAT and event != TASK_FAILED:
        if not restrained:
            if directness == "gentle":
                rendered[0] = "好，" + rendered[0]
            elif directness == "balanced":
                rendered[0] = "行，" + rendered[0]
    text = _join_voice_sentences(rendered, rhythm)
    name = str(source["display_name"])
    if name and not channel_shows_identity:
        text = f"{name}：{text}"
    return text


class _TrackedFactSlots(Mapping[str, object]):
    """Read-only fact slots that record the renderer's actual field access."""

    def __init__(self, values: Mapping[str, object]):
        self._values = values
        self._access_counts: dict[str, int] = {}
        self._access_order: list[str] = []

    def __getitem__(self, key: str) -> object:
        if key not in self._values:
            raise KeyError(key)
        self._access_counts[key] = self._access_counts.get(key, 0) + 1
        if key not in self._access_order:
            self._access_order.append(key)
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    @property
    def consumed(self) -> list[str]:
        return list(self._access_order)

    def consumed_exactly_once(self, required: list[str]) -> bool:
        return (
            set(self._access_counts) == set(required)
            and all(self._access_counts.get(key) == 1 for key in required)
        )


def _scope_phrase(value: object, *, limit: int = 60) -> str:
    """Compress a scope slot into a natural spoken phrase.

    Generic runtime placeholders are replaced by a conversational reference;
    meaningful decision-relevant scope is echoed up to a bounded length; longer
    scopes fall back to a context reference (the recent conversation carries
    the detail).  The slot is still accessed exactly once by the caller.
    """
    text = _plain_fact(value)
    if not text or text in _GENERIC_SCOPES:
        return "这件事"
    if len(text) <= limit:
        return text
    return "你刚说的这件事"


def _actions_sentence(action_values: list[str]) -> str:
    """Recovery options derive only from the actual allowed_actions subset."""
    if not action_values:
        return ""
    joined = "、".join(_ACTION_PHRASES[str(item)] for item in action_values)
    if len(action_values) == 1:
        return f"要我{joined}。"
    return f"要我{joined}，都行。"


def _deliverable_sentence(deliverable_state: str) -> str:
    if deliverable_state == "available":
        return "之前的结果还在，能直接看。"
    return "这次没有拿到能确认的成品。"


def _completed_sentence(completed_values: list[str], retained_values: list[str]) -> str:
    """Natural what-completed statement driven by the closed stage allowlist.

    Every schema-valid completed value must never render as "nothing
    completed".  Production projections only emit ``research`` and/or empty,
    but the schema also allows ``work``, so it is handled truthfully too.
    """
    if "research" in completed_values:
        if "work" in completed_values:
            return "资料查完，成果也整理完了。"
        if "research_evidence" in retained_values:
            return "资料已经查完，查到的资料还在。"
        return "资料已经查完。"
    if "work" in completed_values:
        return "成果整理这一步已经完成。"
    return "这次没有能确认的阶段性成果。"


def _failure_sentence(outcome: str, failure_stage: str, reason: str, humor: str) -> str:
    stage = _FAILURE_STAGE_PHRASES.get(failure_stage, "这一步")
    if outcome == "timeout":
        frames = {
            "none": f"在{stage}这一步超时了：{reason}",
            "light": f"这次在{stage}这一步超时了：{reason}",
            "dry": f"{stage}这一步超时，结果没落地：{reason}",
            "playful": f"进度条在{stage}这一步跑过了头：{reason}",
        }
    elif outcome == "cancelled":
        frames = {
            "none": f"这次在{stage}这一步取消了：{reason}",
            "light": f"这次在{stage}这一步取消了：{reason}",
            "dry": f"{stage}这一步取消，不把中止算完成：{reason}",
            "playful": f"进度条在{stage}这一步停了：{reason}",
        }
    else:
        frames = {
            "none": f"卡在{stage}这一步：{reason}",
            "light": f"这次卡在{stage}这一步：{reason}",
            "dry": f"没绕过去的是{stage}这一步：{reason}",
            "playful": f"这次翻车点在{stage}这一步：{reason}",
        }
    return frames[humor]


def _preserved_phrase(preserved_scope: str) -> str:
    """Natural what-stays phrasing driven by the preserved-scope value.

    Only the exact generic delivery labels produced by the runtime map to
    "其他地方不动"; any other preserved-scope value is echoed verbatim so the
    user-facing meaning never contradicts the authoritative value.
    """
    if preserved_scope in {"原版本保持不变", "其他部分与原版本保持不变"}:
        return "其他地方不动"
    return preserved_scope or "其他地方不动"


def _event_fact_sentences(
    event: str,
    slots: Mapping[str, object],
    source: Mapping[str, object],
    *,
    variant: int,
    restrained: bool,
) -> list[str]:
    """Build natural user-facing statements from the closed fact slots.

    Each required slot is read exactly once (tracked by the caller).  Slots
    that carry decision-relevant meaning (scope, stage, reason, deliverable,
    actions, title/version, URL, change/preserve) are visibly represented;
    internal-only labels are consumed silently.
    """
    humor = "none" if restrained else str(source["humor"])
    if event == TASK_ACCEPTED:
        execution_established = slots["execution_established"] is True
        scope = _scope_phrase(slots["accepted_scope"])
        bodies = {
            "calm": f"接了，{scope}。",
            "balanced": f"{scope}，这事我接了。",
            "warm": f"{scope}，我来弄，好了直接发你。",
            "expressive": f"{scope}，这题我接了，我去弄。",
        }
        sentence = bodies[str(source["warmth"])] if execution_established else f"接了，{scope}。"
        tail = _HUMOR_TAIL["accepted"][humor]
        return [sentence + tail] if tail else [sentence]
    if event == MEANINGFUL_PROGRESS:
        milestone = str(slots["milestone"])
        sentence = _MILESTONE_NATURAL[milestone]
        tail = _HUMOR_TAIL["milestone"][humor]
        return [sentence + tail] if tail else [sentence]
    if event == FALLBACK_HEARTBEAT:
        running = slots["running"] is True
        wording = _HEARTBEAT_WORDING[str(source["warmth"])][max(0, int(variant)) % 3]
        return [wording if running else ""]
    if event == TASK_SUCCEEDED:
        scope = _plain_fact(slots["completed_scope"])
        detail = _multiline_fact(slots["result_detail"])
        scope_text = "" if scope in _GENERIC_SCOPES else f"{scope}，"
        bodies = {
            "calm": "办好了，结果在这：",
            "balanced": "办好了，结果在这：",
            "warm": "弄好了，结果在这：",
            "expressive": "搞定，结果在这：",
        }
        return [scope_text + bodies[str(source["warmth"])], detail]
    if event == ARTIFACT_READY:
        scope = _plain_fact(slots["completed_scope"])
        title = _plain_fact(slots["artifact_title"])
        version = _plain_fact(slots["artifact_version"], maximum=80)
        result_ready = slots["result_ready"] is True
        url = _canonical_result_url(slots["result_url"])
        name = title or "成果"
        if version:
            name = f"{name}（{version}）"
        scope_text = "" if scope in _GENERIC_SCOPES else f"{scope}，"
        bodies = {
            "calm": f"弄好了，{name}在这：",
            "balanced": f"弄好了，{name}在这：",
            "warm": f"{name}弄好了，在这：",
            "expressive": f"搞定，{name}在这：",
        }
        sentence = scope_text + bodies[str(source["warmth"])]
        return [sentence, f"{url}。" if result_ready else ""]
    if event == TASK_FAILED:
        outcome = str(slots["outcome"])
        completed_values = slots["completed_stage"]
        failure_stage = str(slots["failure_stage"])
        reason = _plain_fact(slots["safe_reason"])
        deliverable_state = str(slots["deliverable_state"])
        retained_values = slots["retained_state"]
        action_values = slots["allowed_actions"]
        completed = _completed_sentence(completed_values, retained_values)
        failure = _failure_sentence(outcome, failure_stage, reason, humor)
        deliverable = _deliverable_sentence(deliverable_state)
        actions = _actions_sentence(action_values)
        if str(source["directness"]) == "direct":
            sentences = [failure, completed, deliverable, actions]
        else:
            sentences = [completed, failure, deliverable, actions]
        return [sentence for sentence in sentences if sentence]
    if event == REVISION_STARTED:
        # source_version is consumed exactly once; the delivered-version label
        # ("当前已交付版本"/"已交付的原版本") is a generic placeholder retained
        # in the neutral block and conversational context, so it is not echoed.
        source_version = _plain_fact(slots["source_version"])
        requested_change = _plain_fact(slots["requested_change"])
        preserved_scope = _plain_fact(slots["preserved_scope"])
        revision_established = slots["revision_established"] is True
        change_text = requested_change or "按你这次的要求"
        if len(change_text) > 80:
            change_text = "按你这次的要求"
        preserved_text = _preserved_phrase(preserved_scope)
        sentence = f"{change_text}。{preserved_text}，我现在改。" if revision_established else f"{change_text}。{preserved_text}。"
        tail = _HUMOR_TAIL["revision_started"][humor]
        return [sentence + tail] if tail else [sentence]
    # source_version consumed exactly once; generic placeholder, retained in
    # the neutral block (see REVISION_STARTED comment).
    source_version = _plain_fact(slots["source_version"])
    preserved_scope = _plain_fact(slots["preserved_scope"])
    new_version = _plain_fact(slots["new_version"])
    result_ready = slots["result_ready"] is True
    url = _canonical_result_url(slots["result_url"])
    new_text = new_version or "新版本"
    preserved_text = _preserved_phrase(preserved_scope)
    if preserved_text == "其他地方不动":
        preserved_text = "原版没动"
    bodies = {
        "calm": f"改好了，{new_text}在这：",
        "balanced": f"改好了，{new_text}在这：",
        "warm": f"{new_text}改好了，在这：",
        "expressive": f"搞定，{new_text}在这：",
    }
    return [bodies[str(source["warmth"])], f"{url}。" if result_ready else "", f"{preserved_text}。"]


def render_task_character_expression(
    settings: Mapping[str, object],
    *,
    event: str,
    fact_slots: Mapping[str, object],
    neutral_text: str,
    variant: int = 0,
    channel_shows_identity: bool = False,
) -> dict:
    """Render complete lifecycle facts through the actual Voice Contract fields.

    The immutable neutral projection remains the hash/source of truth.  Visible
    copy is emitted only when the event schema is closed and every required
    slot is consumed exactly once; malformed inputs fail closed to the complete
    neutral projection.  ``channel_shows_identity`` suppresses the display-name
    label when the channel already shows the Character's identity (QQ).
    """

    normalized = str(event or "").strip().upper()
    neutral = neutral_text if isinstance(neutral_text, str) else str(neutral_text or "")
    if normalized not in _EVENT_REQUIRED_FACT_SLOTS:
        raise ValueError("unsupported_task_lifecycle_event")
    if not neutral.strip():
        raise ValueError("neutral_fact_text_required")
    required = list(_EVENT_REQUIRED_FACT_SLOTS[normalized])
    source = _voice_source(settings)
    slots = dict(fact_slots) if isinstance(fact_slots, Mapping) else {}
    schema_closed = set(slots) == set(required) and _valid_fact_slot_values(normalized, slots)
    fell_back = not schema_closed or not _persona_enabled(settings)
    reported_required = list(required)
    missing_fact_slots = sorted(set(required) - set(slots))
    unsupported_fact_slots = sorted(set(slots) - set(required))
    avoidance = "none"
    content = neutral
    consumed: list[str] = list(required) if fell_back else []
    if not fell_back:
        tracked_slots = _TrackedFactSlots(slots)
        sentences = _event_fact_sentences(
            normalized, tracked_slots, source, variant=variant, restrained=False,
        )
        if not tracked_slots.consumed_exactly_once(required):
            fell_back = True
            avoidance = "neutral_fallback"
        else:
            consumed = tracked_slots.consumed
            content = _styled_sentences(
                sentences, source, event=normalized, restrained=False,
                channel_shows_identity=channel_shows_identity,
            )
            conflicts = [
                phrase for phrase in source["avoid_phrases"]
                if phrase.casefold() in content.casefold()
            ]
            if conflicts:
                avoidance = "restrained_alternate"
                tracked_alternate = _TrackedFactSlots(slots)
                sentences = _event_fact_sentences(
                    normalized,
                    tracked_alternate,
                    source,
                    variant=variant,
                    restrained=True,
                )
                content = _styled_sentences(
                    sentences, source, event=normalized, restrained=True,
                    channel_shows_identity=channel_shows_identity,
                )
                consumed = tracked_alternate.consumed
                if (
                    not tracked_alternate.consumed_exactly_once(required)
                    or any(
                        phrase.casefold() in content.casefold()
                        for phrase in source["avoid_phrases"]
                    )
                ):
                    fell_back = True
                    avoidance = "neutral_fallback"
        if fell_back:
            content = neutral
            consumed = list(required)
    fact_hash = hashlib.sha256(neutral.encode("utf-8")).hexdigest()
    accounting_source = "neutral_text" if fell_back else "fact_slots"
    style_source = {
        key: source[key]
        for key in ("version", "display_name", "directness", "warmth", "humor", "rhythm")
    }
    style_source["avoid_phrase_count"] = len(source["avoid_phrases"])
    style_source["avoidance"] = avoidance
    blocks = [
        {
            "id": "block-" + uuid.uuid4().hex,
            "type": "persona_text",
            "content": content,
            "mutable": True,
            "source": "voice_contract_v1",
        },
        {
            "id": "block-" + uuid.uuid4().hex,
            "type": "status",
            "content": neutral,
            "mutable": False,
            "source": "runtime",
            "content_hash": fact_hash,
            "required_fact_slots": reported_required,
            "consumed_fact_slots": consumed,
            "fact_slot_accounting_source": accounting_source,
        },
    ]
    return {
        "content": content,
        "event_type": normalized,
        "fact_hash": fact_hash,
        "required_fact_slots": reported_required,
        "consumed_fact_slots": consumed,
        "fact_slot_accounting_source": accounting_source,
        "style_source": style_source,
        "content_blocks": blocks,
        "fell_back": fell_back,
        "fallback_source": "neutral_text" if fell_back else "",
        "missing_fact_slots": missing_fact_slots,
        "unsupported_fact_slots": unsupported_fact_slots,
    }


PRESENTATION_CONTRACT = "task_expression_v1"


# User-safe blocked/error text keyed by the existing error_kind values (mirrors
# the raw `_USER_ERROR_MAP` detail).  Keeps the truthful semantics (cannot
# execute / environment-or-permission limitation / actionable next step) while
# removing backend implementation detail (provider names, runtime, sandbox,
# execution lane, internal policy names).  The raw detail is retained in the
# dispatch result as `error_detail` for Console/logs/audit.
_BLOCKED_USER_SAFE = {
    "executor_snapshot_missing": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "executor_adapter_missing": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "executor_profile_missing": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "executor_runtime_not_applied": "完成这件事需要的执行能力还没准备好，所以暂时没法继续。",
    "executor_model_missing": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "executor_credential_missing": "现在缺少完成这件事所需的访问授权，所以暂时没法继续。",
    "work_executor_binding_missing": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "deepseek_proxy_access_key_missing": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "deepseek_proxy_model_missing": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "proxy_unreachable": "现在暂时没有能完成这件事的执行能力，所以暂时没法继续。",
    "proxy_auth_required": "完成这件事需要的访问授权有问题，所以暂时没法继续。",
    "upstream_auth_failed": "完成这件事需要的访问授权有问题，所以暂时没法继续。",
    "cwd_not_allowed_for_proxy": "这件事需要的操作在当前项目里不被允许，所以暂时没法继续。",
    "danger_full_access_not_allowed_for_proxy": "这件事需要的操作级别在当前环境里不被允许，所以暂时没法继续。",
    "executor_sandbox_unavailable": "执行这件事所需的安全环境还没准备好，所以暂时没法继续。",
    "executor_profile_changed": "执行这件事所需的环境在排队期间有变化，所以暂时没法继续，可以再让我试一次。",
    "upstream_rate_limited": "现在请求有点多，暂时没排上，稍等一下再试。",
    "hard_quota": "当前执行容量已经用尽，暂时无法继续。需要由 Owner 处理额度或手动调整模型配置。",
    "incomplete_stream": "这件事执行到一半结果没有完整回来，暂时没法确认完成，可以再试一次。",
    "task_network_authorization_expired": "这件事需要的联网授权已经关闭或到期，没有执行联网步骤。",
}
_BLOCKED_USER_SAFE_DEFAULT = (
    "这件事暂时没办成。原因已经记录在后台，可以稍后重试，或换个说法再让我试试。"
)

FAILURE_PROJECTION_VERSION = "m1_r_failure_v1"

_RESEARCH_FAILURE_KINDS = {
    "research_blocked",
    "research_execution_failed",
    "research_no_approved_sources",
}
_CAPABILITY_FAILURE_KINDS = {
    "executor_snapshot_missing",
    "executor_adapter_missing",
    "executor_profile_missing",
    "executor_runtime_not_applied",
    "executor_model_missing",
    "executor_credential_missing",
    "work_executor_binding_missing",
    "executor_sandbox_unavailable",
    "execution_activation_failed",
}
_PUBLIC_FAILURE_KINDS = (
    set(_BLOCKED_USER_SAFE)
    | _RESEARCH_FAILURE_KINDS
    | _CAPABILITY_FAILURE_KINDS
    | {
        "no_business_evidence",
        "network",
        "service_restart",
        "turn_failed",
        "hard_quota",
    }
)


def _meaningful_evidence_value(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).strip() and _meaningful_evidence_value(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_meaningful_evidence_value(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def meaningful_evidence_content(item: Mapping[str, object]) -> bool:
    excerpt = str(item.get("excerpt") or item.get("content") or "").strip()
    return bool(excerpt) or _meaningful_evidence_value(item.get("facts"))


def _valid_https_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253:
        return False
    labels = ascii_hostname.split(".")
    return bool(labels) and all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and re.fullmatch(r"[A-Za-z0-9-]+", label) is not None
        for label in labels
    )


def valid_retained_research_evidence(
    item: object,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether one Research evidence item is structurally reusable."""

    if not isinstance(item, Mapping):
        return False
    source_uri = str(
        item.get("source_uri") or item.get("url") or item.get("source_url") or ""
    ).strip()
    try:
        parsed = urlsplit(source_uri)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not _valid_https_hostname(hostname)
        or parsed.username
        or parsed.password
        or parsed.netloc.endswith(":")
    ):
        return False
    content_hash = str(
        item.get("content_hash") or item.get("content_sha256") or ""
    ).strip().lower()
    if re.fullmatch(r"[a-f0-9]{64}", content_hash) is None:
        return False
    if not meaningful_evidence_content(item):
        return False
    expires_text = str(item.get("expires_at") or item.get("valid_until") or "").strip()
    if expires_text:
        try:
            expires_at = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
            return False
    return True


def _has_retained_research(task: Mapping[str, object]) -> bool:
    if task.get("_retained_research_valid") is True:
        return True
    evidence = task.get("evidence")
    return isinstance(evidence, list) and any(
        valid_retained_research_evidence(item) for item in evidence
    )


def _failure_stage(error_kind: str) -> str:
    if error_kind in _RESEARCH_FAILURE_KINDS:
        return "research"
    if error_kind in _CAPABILITY_FAILURE_KINDS:
        return "execution_setup"
    return "work"


def _safe_failure_reason(error_kind: str, *, retained_research: bool) -> str:
    if error_kind == "no_business_evidence":
        if retained_research:
            return "资料已经查到了，但整理文档时没有得到可以核验的正文或文件，所以这次没有成果可交。"
        return "整理成果时没有得到可以核验的正文或文件，所以这次没有成果可交。"
    if error_kind == "research_blocked":
        return "资料查找阶段没有得到满足要求且可以核验的来源，因此没有继续生成成果。"
    if error_kind in {"research_execution_failed", "research_no_approved_sources"}:
        return "资料查找阶段没有得到可以确认的研究结果，因此没有继续生成成果。"
    if error_kind in _CAPABILITY_FAILURE_KINDS:
        return "完成这件事所需的执行条件没有准备好，因此这次没有产生可交付成果。"
    if error_kind in {"network", "incomplete_stream", "upstream_rate_limited"}:
        return "执行过程没有完整得到可以确认的结果，因此这次没有产生可交付成果。"
    if error_kind == "hard_quota":
        return "当前执行容量已经用尽，因此这次没有产生可交付成果。需要由 Owner 处理额度或手动调整模型配置。"
    return "执行过程中没有得到可以确认的结果，这次没有可交付成果。"


def task_failure_projection(task: Mapping[str, object]) -> dict:
    """Project private worker failure state into bounded user-safe facts.

    This function intentionally never reads raw stdout, stderr, output, or
    error text.  Known categories are mapped to controlled product facts and
    unknown categories degrade to a generic safe statement.
    """

    error_kind = str(task.get("error_kind") or "").strip()
    public_error_kind = error_kind if error_kind in _PUBLIC_FAILURE_KINDS else "unknown_failure"
    retained_research = _has_retained_research(task)
    raw_status = str(task.get("status") or "").strip()
    outcome = raw_status if raw_status in {"failed", "timeout", "cancelled"} else "failed"
    retryable = outcome in {"failed", "timeout"} and error_kind != "hard_quota"
    explicit_delivery = task.get("_has_valid_delivery_access")
    deliverable_available = (
        explicit_delivery
        if isinstance(explicit_delivery, bool)
        else False
    )
    allowed_actions = ["retry", "change_method"] if retryable else ["change_method"] if error_kind == "hard_quota" else []
    if retained_research:
        allowed_actions.append("deliver_research_summary")
    if outcome == "timeout":
        safe_reason = "任务达到时间限制前没有得到可以确认的结果。"
    elif outcome == "cancelled":
        safe_reason = "任务已经取消，取消前没有产生新的可交付成果。"
    else:
        safe_reason = _safe_failure_reason(
            error_kind,
            retained_research=retained_research,
        )
    failure_stage = (
        "execution_setup"
        if outcome in {"timeout", "cancelled"}
        and not str(task.get("started_at") or "").strip()
        else _failure_stage(error_kind)
    )
    return {
        "schema_version": FAILURE_PROJECTION_VERSION,
        "outcome": outcome,
        "completed": ["research"] if retained_research else [],
        "failed_stage": failure_stage,
        "user_safe_reason": safe_reason,
        "retained_state": ["research_evidence"] if retained_research else [],
        "deliverable_state": "available" if deliverable_available else "not_available",
        "retryable": retryable,
        "allowed_actions": allowed_actions,
        "task_id": str(task.get("id") or ""),
        "run_id": str(task.get("run_id") or ""),
        "goal_id": str(task.get("goal_id") or ""),
        "error_kind": public_error_kind,
    }


def normalize_task_failure_projection(
    projection: Mapping[str, object],
    *,
    retained_research: bool | None = None,
    deliverable_available: bool | None = None,
) -> dict:
    """Rebuild a persisted projection from allowlisted facts only.

    Persisted projections are durable inputs, not trusted presentation text.
    Rebuilding also lets callers reconcile retained Research against current
    authoritative evidence validity without exposing a stored internal kind or
    an arbitrary stored reason.
    """

    completed = {str(item) for item in projection.get("completed") or []}
    retained = {str(item) for item in projection.get("retained_state") or []}
    has_research = (
        "research" in completed and "research_evidence" in retained
        if retained_research is None
        else bool(retained_research)
    )
    has_deliverable = (
        deliverable_available
        if isinstance(deliverable_available, bool)
        else False
    )
    outcome = str(projection.get("outcome") or "")
    if outcome not in {"failed", "timeout", "cancelled"}:
        outcome = "failed"
    rebuilt = task_failure_projection({
        "id": str(projection.get("task_id") or "")[:200],
        "run_id": str(projection.get("run_id") or "")[:200],
        "goal_id": str(projection.get("goal_id") or "")[:200],
        "status": outcome,
        "error_kind": str(projection.get("error_kind") or "")[:120],
        "_retained_research_valid": has_research,
        "_has_valid_delivery_access": has_deliverable,
    })
    return rebuilt


def task_failure_projection_text(projection: Mapping[str, object]) -> str:
    """Render all decision-relevant failure facts without private internals."""

    completed = {str(item) for item in projection.get("completed") or []}
    retained = {str(item) for item in projection.get("retained_state") or []}
    stage = str(projection.get("failed_stage") or "work")
    reason = str(projection.get("user_safe_reason") or "").strip() or _safe_failure_reason(
        "",
        retained_research="research" in completed,
    )
    lines = []
    if "research" in completed:
        suffix = "，查到的资料仍保留着" if "research_evidence" in retained else ""
        lines.append(f"资料查找已经完成{suffix}。")
    else:
        lines.append("目前没有可以确认并保留的阶段性成果。")
    stage_label = {
        "research": "资料查找阶段",
        "work": "文档整理阶段",
        "execution_setup": "执行准备阶段",
    }.get(stage, "执行阶段")
    outcome = str(projection.get("outcome") or "failed")
    if outcome == "timeout":
        lines.append(f"任务在{stage_label}超时：{reason}")
    elif outcome == "cancelled":
        lines.append(f"任务已在{stage_label}取消：{reason}")
    else:
        lines.append(f"失败发生在{stage_label}：{reason}")
    if str(projection.get("deliverable_state") or "not_available") == "available":
        lines.append("已有可交付内容仍然可用。")
    else:
        lines.append("因此这次没有可交付的正文、文档或文件。")
    actions = {str(item) for item in projection.get("allowed_actions") or []}
    choices = []
    if "retry" in actions:
        choices.append("再试一次")
    if "change_method" in actions:
        choices.append("换一种方法")
    if "deliver_research_summary" in actions:
        choices.append("先把已查到的资料摘要发给你")
    if choices:
        lines.append("接下来可以" + "、".join(choices) + "。")
    return "\n".join(lines)


def task_failure_fact_slots(projection: Mapping[str, object]) -> dict:
    """Map the durable failure projection to the closed C0.1 slot schema."""

    return {
        "outcome": str(projection.get("outcome") or ""),
        "completed_stage": [str(item) for item in projection.get("completed") or []],
        "failure_stage": str(projection.get("failed_stage") or ""),
        "safe_reason": str(projection.get("user_safe_reason") or "").strip(),
        "deliverable_state": str(projection.get("deliverable_state") or ""),
        "retained_state": [str(item) for item in projection.get("retained_state") or []],
        "allowed_actions": [str(item) for item in projection.get("allowed_actions") or []],
    }


def task_blocked_error(error_kind: str) -> str:
    """Return a user-safe, abstracted blocked/error text for the QQ reply.

    Keeps truthful semantics (cannot execute / environment-or-permission
    limitation / actionable next step) while removing backend implementation
    detail.  The original detailed error kind/text stays in the dispatch
    result and logs for Console/audit.
    """
    return _BLOCKED_USER_SAFE.get(
        str(error_kind or "").strip(),
        _BLOCKED_USER_SAFE_DEFAULT,
    )


def task_terminal_presentation(
    status: str,
    raw: str = "",
    *,
    settings: Mapping[str, object] | None = None,
    channel_shows_identity: bool = False,
) -> str:
    """Persona-consistent, truthful terminal presentation for a task state.

    Always carries the state-consistent ``task_terminal_text`` statement and
    appends the real worker result/error when present, so the four terminal
    states (done/failed/timeout/cancelled) are all presented in Assistant
    expression while the underlying fact stays visible.
    """
    normalized_status = str(status or "").strip()
    text = task_terminal_text(normalized_status)
    detail = str(raw or "").strip()
    action = "completed" if normalized_status == "done" else "failed"
    frame = persona_frame(
        settings or {}, action=action, channel_shows_identity=channel_shows_identity,
    ) if settings else ""
    if frame:
        text = frame + "\n" + text
    if detail:
        return text + "\n" + detail
    return text


def task_status_blocks(
    plan: Mapping[str, object],
    settings: Mapping[str, object],
    factual_text: str,
    *,
    factual_type: str = "status",
    action: str = "",
    variant: int = 0,
    channel_shows_identity: bool = False,
) -> tuple[list[dict], str]:
    """Assemble persona framing + immutable factual block for a task event.

    Reuses the existing interaction-contract block mechanism.  The persona
    frame is added only when the plan itself does not already provide a
    persona part, so model-produced acknowledgement is never duplicated.
    """
    existing_persona = any(
        str(part.get("type") or "") in {"social_ack", "transition"}
        and str(part.get("text") or "").strip()
        for part in (plan.get("reply_parts") or [])
    )
    blocks: list[dict] = []
    if not existing_persona:
        frame = persona_frame(
            settings, action=action, variant=variant,
            channel_shows_identity=channel_shows_identity,
        )
        if frame:
            blocks.append(
                {
                    "id": "block-" + uuid.uuid4().hex,
                    "type": "persona_text",
                    "content": frame,
                    "mutable": True,
                    "source": TASK_EXPRESSION_FLAG,
                },
            )
    blocks.extend(response_blocks(plan, factual_text, factual_type=factual_type))
    return blocks, render_response_blocks(blocks)


def task_lifecycle_blocks(
    plan: Mapping[str, object],
    settings: Mapping[str, object],
    *,
    event: str,
    factual_text: str = "",
    fact_slots: Mapping[str, object] | None = None,
    milestone: str = "",
    variant: int = 0,
    channel_shows_identity: bool = False,
) -> tuple[list[dict], str]:
    """Compose one lifecycle event with mutable character and immutable fact blocks."""

    normalized = str(event or "").strip().upper()
    try:
        action = _EVENT_ACTIONS[normalized]
    except KeyError as exc:
        raise ValueError("unsupported_task_lifecycle_event") from exc
    fact = str(factual_text or "") or task_lifecycle_fact(
        normalized,
        milestone=milestone,
    )
    if fact_slots is not None:
        rendered = render_task_character_expression(
            settings,
            event=normalized,
            fact_slots=fact_slots,
            neutral_text=fact,
            variant=variant,
            channel_shows_identity=channel_shows_identity,
        )
        plan_blocks = interaction_plan_persona_blocks(plan)
        blocks = [*plan_blocks, *rendered["content_blocks"]]
        visible_blocks = [*plan_blocks, rendered["content_blocks"][0]]
        return blocks, render_response_blocks(visible_blocks)
    return task_status_blocks(
        plan,
        settings,
        fact,
        factual_type="status",
        action=action,
        variant=variant,
        channel_shows_identity=channel_shows_identity,
    )


__all__ = [
    "ARTIFACT_READY",
    "FALLBACK_HEARTBEAT",
    "MEANINGFUL_PROGRESS",
    "PRESENTATION_CONTRACT",
    "FAILURE_PROJECTION_VERSION",
    "REVISION_STARTED",
    "REVISION_SUCCEEDED",
    "TASK_ACCEPTED",
    "TASK_EXPRESSION_FLAG",
    "TASK_FAILED",
    "TASK_SUCCEEDED",
    "persona_frame",
    "render_task_character_expression",
    "task_accepted_text",
    "task_append_text",
    "task_approval_text",
    "task_blocked_error",
    "task_expression_enabled",
    "task_failure_projection",
    "normalize_task_failure_projection",
    "meaningful_evidence_content",
    "task_failure_projection_text",
    "task_failure_fact_slots",
    "task_lifecycle_blocks",
    "task_lifecycle_fact",
    "valid_retained_research_evidence",
    "task_status_blocks",
    "task_terminal_presentation",
    "task_terminal_text",
]
