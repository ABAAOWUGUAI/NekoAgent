#!/usr/bin/env python3
"""Bounded prior-context selection for continuous private conversation."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence

from bridge_continuous_private_conversation import (
    ResponseCycleRecoveryError,
    validated_response_cycle_predecessors,
)


_VISUAL_COMPONENTS = frozenset({
    "image", "photo", "picture", "gif", "video", "mface", "marketface",
    "market_face", "dynamicface", "dynamic_face",
})
_VISUAL_REFERENCE_NOUN = (
    r"(?:表情包|图片|截图|照片|画面|动图|图像|图(?!标|案|形|层|例|表))"
)
_VISUAL_SHEET_CLASSIFIER = r"(?:一)?(?:张|幅)"
_VISUAL_SHEET_BOUNDARY = (
    r"(?=$|[\s，,。！？?!；;：:（）()]|"
    r"(?:呢|吗|呀|吧|啊|的|是|有|要|和|与|跟|比|对比|比较|区别|不同|差异|怎么|如何|怎样|更))"
)
_VISUAL_SHEET_REFERENCE = (
    rf"{_VISUAL_SHEET_CLASSIFIER}"
    rf"(?:{_VISUAL_REFERENCE_NOUN}|{_VISUAL_SHEET_BOUNDARY})"
)
_DEICTIC_VISUAL_REFERENCE = re.compile(
    rf"(?:这|那){_VISUAL_SHEET_REFERENCE}|{_VISUAL_REFERENCE_NOUN}"
)
_STANDALONE_DEICTIC_VISUAL_REFERENCE = re.compile(
    r"^(?:[^\s，,。！？?!；;：:]{1,16}[，,]\s*)?"
    r"(?:(?:是|就是|看看|你看)?(?:这|那)(?:一)?(?:个|种)(?:怎么追)?|"
    r"(?:刚才|之前|上面)(?:那|这)?(?:一)?(?:张|个))"
    r"(?:呢|吗|呀|吧|啊)?[\s，,。！？?!；;：:～~…]*$"
)
_VISUAL_SHEET_WITH_NOUN = rf"{_VISUAL_SHEET_CLASSIFIER}{_VISUAL_REFERENCE_NOUN}"
_PRIOR_COMPARISON_TARGET = (
    rf"(?:(?:那|另){_VISUAL_SHEET_REFERENCE}|"
    rf"(?:那|另)(?:一)?个{_VISUAL_REFERENCE_NOUN})"
)
_EXPLICIT_TEMPORAL_PRIOR_VISUAL_REFERENCE = re.compile(
    rf"(?:上一|前一|上){_VISUAL_SHEET_REFERENCE}|"
    rf"(?:刚才|刚刚|方才|之前|先前|此前|上次|刚发的|刚发).{{0,12}}"
    rf"(?:(?:那|这){_VISUAL_SHEET_REFERENCE}|{_VISUAL_REFERENCE_NOUN})|"
    rf"(?:刚才|刚刚|之前|前面)发的.{{0,12}}(?:那|这)(?:一)?(?:张|个|位|幅)"
)
_EXPLICIT_PRIOR_VISUAL_COMPARISON = re.compile(
    rf"(?:(?:这|当前){_VISUAL_SHEET_REFERENCE}\s*)?"
    rf"(?:和|与|跟).{{0,8}}{_PRIOR_COMPARISON_TARGET}.{{0,12}}"
    rf"(?:对比|比较|区别|不同|差异)|"
    rf"(?:这|当前){_VISUAL_SHEET_REFERENCE}.{{0,8}}(?:比|对比|比较)"
    rf".{{0,8}}{_PRIOR_COMPARISON_TARGET}"
)
_EXPLICIT_FRONT_PRIOR_VISUAL_REFERENCE = re.compile(
    rf"前面.{{0,8}}(?:那|这){_VISUAL_SHEET_WITH_NOUN}"
)
_CURRENT_VISUAL_FRONT_SPATIAL_REFERENCE = re.compile(
    rf"{_VISUAL_REFERENCE_NOUN}(?:里面|内部|里|内|中|的)?(?:的)?前面"
)


def _explicitly_targets_prior_visual(message: object) -> bool:
    normalized = " ".join(str(message or "").split())[:4000]
    if not normalized:
        return False
    if (
        _EXPLICIT_TEMPORAL_PRIOR_VISUAL_REFERENCE.search(normalized) is not None
        or _EXPLICIT_PRIOR_VISUAL_COMPARISON.search(normalized) is not None
    ):
        return True
    front_match = _EXPLICIT_FRONT_PRIOR_VISUAL_REFERENCE.search(normalized)
    if front_match is None:
        return False
    return not any(
        spatial.start() <= front_match.start() < spatial.end()
        for spatial in _CURRENT_VISUAL_FRONT_SPATIAL_REFERENCE.finditer(normalized)
    )


def _is_deictic_visual_reference(message: object) -> bool:
    normalized = " ".join(str(message or "").split())[:4000]
    return bool(
        normalized
        and (
            _DEICTIC_VISUAL_REFERENCE.search(normalized) is not None
            or _STANDALONE_DEICTIC_VISUAL_REFERENCE.fullmatch(normalized) is not None
        )
    )


def _metadata(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _has_visual(value: object) -> bool:
    metadata = _metadata(value)
    attachments = metadata.get("attachments")
    if isinstance(attachments, list) and any(
        isinstance(item, Mapping)
        and any(
            str(item.get(key) or "").strip().lower() in _VISUAL_COMPONENTS
            for key in ("type", "media_kind", "source_component")
        )
        for item in attachments
    ):
        return True
    components = metadata.get("message_components")
    return bool(
        isinstance(components, list)
        and any(
            isinstance(item, Mapping)
            and str(item.get("type") or "").strip().lower() in _VISUAL_COMPONENTS
            for item in components
        )
    )


def _ready_observation(value: object) -> dict | None:
    if not isinstance(value, Mapping) or str(value.get("status") or "") != "ready":
        return None
    return copy.deepcopy(dict(value))


def _reply_target(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    reply_to_external_message_id: str,
    before_sequence: int,
) -> sqlite3.Row | tuple | None:
    reply_id = str(reply_to_external_message_id or "").strip()[:300]
    if not reply_id:
        return None
    rows = conn.execute(
        """
        SELECT m.id,m.external_message_id,m.metadata_json,s.cycle_id,m.inbound_sequence
        FROM conversation_messages m
        JOIN conversation_response_cycle_sources s ON s.source_message_id=m.id
        WHERE m.thread_id=? AND m.role='user' AND m.inbound_sequence<?
          AND (m.external_message_id=? OR m.external_message_id LIKE ?)
        ORDER BY m.inbound_sequence DESC,m.id DESC
        LIMIT 2
        """,
        (thread_id, before_sequence, reply_id, f"%:{reply_id}"),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _visual_sources_for_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
) -> list[sqlite3.Row | tuple]:
    rows = conn.execute(
        """
        SELECT m.id,m.external_message_id,m.metadata_json,s.cycle_id,m.inbound_sequence
        FROM conversation_response_cycle_sources s
        JOIN conversation_messages m ON m.id=s.source_message_id
        WHERE s.cycle_id=? AND m.role='user'
        ORDER BY s.source_order,m.id
        """,
        (str(cycle_id),),
    ).fetchall()
    return [row for row in rows if _has_visual(row[2])]


def _visual_source_for_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
) -> sqlite3.Row | tuple | None:
    return next(iter(_visual_sources_for_cycle(conn, cycle_id=cycle_id)), None)


def _bounded_history_visual_contexts(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    before_sequence: int,
    external_message_ids: Sequence[str],
    observation_lookup: Callable[[str], object],
) -> list[dict]:
    bounded_ids: list[str] = []
    for value in external_message_ids[:30]:
        token = str(value or "").strip()[:300]
        if token and token not in bounded_ids:
            bounded_ids.append(token)
    if not bounded_ids:
        return []
    placeholders = ",".join("?" for _ in bounded_ids)
    rows = conn.execute(
        f"""
        SELECT m.id,m.external_message_id,m.metadata_json,s.cycle_id,
               m.inbound_sequence,s.source_order,c.state
        FROM conversation_messages m
        JOIN conversation_response_cycle_sources s ON s.source_message_id=m.id
        JOIN conversation_response_cycles c ON c.id=s.cycle_id
        WHERE m.thread_id=? AND m.role='user' AND m.inbound_sequence<?
          AND m.external_message_id IN ({placeholders})
          AND c.thread_id=?
        ORDER BY m.inbound_sequence,s.source_order,m.id
        """,
        (thread_id, before_sequence, *bounded_ids, thread_id),
    ).fetchall()
    bounded_cycle_states: dict[str, str] = {}
    for row in rows:
        cycle_id = str(row[3] or "").strip()
        if cycle_id:
            bounded_cycle_states[cycle_id] = str(row[6] or "").strip()

    eligible_cycle_ids: set[str] = set()
    for cycle_id, state in bounded_cycle_states.items():
        if state != "outbox_queued":
            continue
        eligible_cycle_ids.add(cycle_id)
        try:
            predecessors = validated_response_cycle_predecessors(conn, cycle_id)
        except ResponseCycleRecoveryError:
            continue
        eligible_cycle_ids.update(
            str(item.get("id") or "").strip()
            for item in predecessors
            if str(item.get("id") or "").strip()
        )

    grouped: list[dict] = []
    by_cycle: dict[str, dict] = {}
    for row in rows:
        cycle_id = str(row[3] or "").strip()
        if cycle_id not in eligible_cycle_ids or not _has_visual(row[2]):
            continue
        entry = by_cycle.get(cycle_id)
        if entry is None:
            entry = {
                "source_message_id": str(row[0]),
                "source_message_ids": [],
                "external_message_id": str(row[1] or ""),
                "external_message_ids": [],
                "cycle_id": cycle_id,
                "inbound_sequence": int(row[4]),
                "observation": _ready_observation(observation_lookup(cycle_id)),
            }
            by_cycle[cycle_id] = entry
            grouped.append(entry)
        entry["source_message_ids"].append(str(row[0]))
        entry["external_message_ids"].append(str(row[1] or ""))
    return grouped


def select_prior_visual_context(
    conn: sqlite3.Connection,
    *,
    current_cycle_id: str,
    message: str,
    reply_to_external_message_id: str,
    observation_lookup: Callable[[str], object],
) -> dict | None:
    """Select one exact prior visual fact without changing current sources.

    Exact QQ reply-to wins.  Without reply-to, a narrow deictic phrase may
    select only the immediately preceding user message when that message is
    visual.  The function never searches for an arbitrary "most recent image".
    """

    current = conn.execute(
        """
        SELECT thread_id,from_inbound_sequence
        FROM conversation_response_cycles WHERE id=?
        """,
        (str(current_cycle_id),),
    ).fetchone()
    if current is None:
        raise ValueError("private_situation_current_cycle_missing")
    thread_id = str(current[0])
    before_sequence = int(current[1])

    reply_id = str(reply_to_external_message_id or "").strip()
    reply_target = _reply_target(
        conn,
        thread_id=thread_id,
        reply_to_external_message_id=reply_id,
        before_sequence=before_sequence,
    )
    reason = "reply_to_visual"
    if reply_target is not None:
        candidate = (
            reply_target
            if _has_visual(reply_target[2])
            else _visual_source_for_cycle(
                conn,
                cycle_id=str(reply_target[3]),
            )
        )
    elif reply_id:
        return None
    else:
        normalized = " ".join(str(message or "").split())[:4000]
        if not _is_deictic_visual_reference(normalized):
            return None
        previous_cycle = conn.execute(
            """
            SELECT id FROM conversation_response_cycles
            WHERE thread_id=? AND through_inbound_sequence<?
            ORDER BY through_inbound_sequence DESC,updated_at DESC,id DESC
            LIMIT 1
            """,
            (thread_id, before_sequence),
        ).fetchone()
        candidate = (
            _visual_source_for_cycle(conn, cycle_id=str(previous_cycle[0]))
            if previous_cycle is not None else None
        )
        reason = "immediate_deictic_visual"

    if candidate is None or not _has_visual(candidate[2]):
        return None
    cycle_id = str(candidate[3] or "").strip()
    observation = _ready_observation(observation_lookup(cycle_id))
    if not cycle_id or observation is None:
        return None
    return {
        "source_message_id": str(candidate[0]),
        "source_message_ids": [str(candidate[0])],
        "external_message_id": str(candidate[1] or ""),
        "external_message_ids": [str(candidate[1] or "")],
        "cycle_id": cycle_id,
        "inbound_sequence": int(candidate[4]),
        "selection_reason": reason,
        "observation": observation,
    }


def select_prior_visual_contexts(
    conn: sqlite3.Connection,
    *,
    current_cycle_id: str,
    message: str,
    reply_to_external_message_id: str,
    observation_lookup: Callable[[str], object],
    recent_context_external_message_ids: Sequence[str] = (),
    resume_conversation: bool = False,
    current_visual_present: bool = False,
    current_visual_ready: bool = False,
) -> dict:
    """Return typed prior visual context for one current private cycle."""

    normalized = " ".join(str(message or "").split())[:4000]
    reply_id = str(reply_to_external_message_id or "").strip()
    explicitly_targets_prior = _explicitly_targets_prior_visual(normalized)
    if (
        (current_visual_present or current_visual_ready)
        and not reply_id
        and not explicitly_targets_prior
    ):
        return {
            "status": "none",
            "reason": (
                "current_visual_authoritative"
                if current_visual_ready
                else "current_visual_target_unavailable"
            ),
            "entries": [],
            "missing_cycle_ids": [],
            "missing_source_message_ids": [],
        }

    try:
        predecessors = validated_response_cycle_predecessors(
            conn,
            current_cycle_id,
        )
    except ResponseCycleRecoveryError as exc:
        return {
            "status": "invalid",
            "reason": str(exc)[:200],
            "entries": [],
            "missing_cycle_ids": [],
            "missing_source_message_ids": [],
        }

    if reply_id:
        selected = select_prior_visual_context(
            conn,
            current_cycle_id=current_cycle_id,
            message=message,
            reply_to_external_message_id=reply_id,
            observation_lookup=observation_lookup,
        )
        if selected is None:
            current = conn.execute(
                """
                SELECT thread_id,from_inbound_sequence
                FROM conversation_response_cycles WHERE id=?
                """,
                (str(current_cycle_id),),
            ).fetchone()
            if current is None:
                raise ValueError("private_situation_current_cycle_missing")
            reply_target = _reply_target(
                conn,
                thread_id=str(current[0]),
                reply_to_external_message_id=reply_id,
                before_sequence=int(current[1]),
            )
            candidate = None
            if reply_target is not None:
                candidate = (
                    reply_target
                    if _has_visual(reply_target[2])
                    else _visual_source_for_cycle(
                        conn,
                        cycle_id=str(reply_target[3]),
                    )
                )
            if candidate is not None and _has_visual(candidate[2]):
                return {
                    "status": "unavailable",
                    "reason": "reply_visual_observation_unavailable",
                    "entries": [],
                    "missing_cycle_ids": [str(candidate[3])],
                    "missing_source_message_ids": [str(candidate[0])],
                }
        return {
            "status": "ready" if selected is not None else "none",
            "reason": (
                str(selected.get("selection_reason") or "")
                if selected is not None
                else "no_prior_visual_context"
            ),
            "entries": [selected] if selected is not None else [],
            "missing_cycle_ids": [],
            "missing_source_message_ids": [],
        }

    if not predecessors:
        is_deictic = bool(
            explicitly_targets_prior
            or _is_deictic_visual_reference(normalized)
        )
        if recent_context_external_message_ids and (resume_conversation or is_deictic):
            current = conn.execute(
                """
                SELECT thread_id,from_inbound_sequence
                FROM conversation_response_cycles WHERE id=?
                """,
                (str(current_cycle_id),),
            ).fetchone()
            if current is None:
                raise ValueError("private_situation_current_cycle_missing")
            candidates = _bounded_history_visual_contexts(
                conn,
                thread_id=str(current[0]),
                before_sequence=int(current[1]),
                external_message_ids=recent_context_external_message_ids,
                observation_lookup=observation_lookup,
            )
            if is_deictic and len(candidates) > 1:
                return {
                    "status": "ambiguous",
                    "reason": "multiple_recent_visual_contexts",
                    "entries": [],
                    "missing_cycle_ids": [],
                    "missing_source_message_ids": [],
                }
            selected_candidates = candidates[-3:] if resume_conversation else candidates
            ready_entries: list[dict] = []
            missing_cycle_ids: list[str] = []
            missing_source_message_ids: list[str] = []
            for candidate in selected_candidates:
                observation = candidate.get("observation")
                if observation is None:
                    missing_cycle_ids.append(str(candidate["cycle_id"]))
                    missing_source_message_ids.extend(candidate["source_message_ids"])
                    continue
                ready_entries.append({
                    **candidate,
                    "selection_reason": (
                        "conversation_resume_visual"
                        if resume_conversation
                        else "bounded_deictic_visual"
                    ),
                })
            if is_deictic and missing_cycle_ids:
                return {
                    "status": "unavailable",
                    "reason": "bounded_visual_observation_unavailable",
                    "entries": [],
                    "missing_cycle_ids": missing_cycle_ids,
                    "missing_source_message_ids": missing_source_message_ids,
                }
            if ready_entries:
                return {
                    "status": "ready",
                    "reason": ready_entries[0]["selection_reason"],
                    "entries": ready_entries,
                    "missing_cycle_ids": [],
                    "missing_source_message_ids": [],
                }
        return {
            "status": "none",
            "reason": "no_bounded_prior_visual_context",
            "entries": [],
            "missing_cycle_ids": [],
            "missing_source_message_ids": [],
        }

    entries: list[dict] = []
    missing_cycle_ids: list[str] = []
    missing_source_message_ids: list[str] = []
    for revision, cycle in enumerate(predecessors):
        cycle_id = str(cycle.get("id") or "")
        sources = _visual_sources_for_cycle(conn, cycle_id=cycle_id)
        if not sources:
            continue
        source_message_ids = [str(row[0]) for row in sources]
        observation = _ready_observation(observation_lookup(cycle_id))
        if observation is None:
            missing_cycle_ids.append(cycle_id)
            missing_source_message_ids.extend(source_message_ids)
            continue
        entries.append({
            "source_message_id": source_message_ids[0],
            "source_message_ids": source_message_ids,
            "external_message_id": str(sources[0][1] or ""),
            "external_message_ids": [str(row[1] or "") for row in sources],
            "cycle_id": cycle_id,
            "inbound_sequence": int(sources[0][4]),
            "situation_revision": revision,
            "selection_reason": "predecessor_situation_revision",
            "observation": observation,
        })
    if missing_cycle_ids:
        return {
            "status": "unavailable",
            "reason": "predecessor_visual_observation_unavailable",
            "entries": [],
            "missing_cycle_ids": missing_cycle_ids,
            "missing_source_message_ids": missing_source_message_ids,
        }
    return {
        "status": "ready" if entries else "none",
        "reason": (
            "predecessor_situation_revision"
            if entries
            else "predecessor_chain_has_no_visual_source"
        ),
        "entries": entries,
        "missing_cycle_ids": [],
        "missing_source_message_ids": [],
    }


def _observation_facts(observation: object) -> list[str]:
    if not isinstance(observation, Mapping):
        return []
    facts: list[str] = []
    kind = str(observation.get("media_kind") or "unknown").strip()
    if kind:
        facts.append(f"类型：{kind}")
    visible = [str(item).strip() for item in list(observation.get("visible_elements") or [])[:4] if str(item).strip()]
    if visible:
        facts.append("可见：" + "；".join(visible))
    text = str(observation.get("visible_text") or "").strip()
    if text:
        facts.append("可见文字：" + text[:1200])
    gesture = str(observation.get("depicted_expression_or_gesture") or "").strip()
    if gesture:
        facts.append("画面动作/表情：" + gesture[:600])
    uncertain = [str(item).strip() for item in list(observation.get("uncertain_elements") or [])[:3] if str(item).strip()]
    if uncertain:
        facts.append("不确定：" + "；".join(uncertain))
    return facts


def prior_visual_context_lines(selected: Mapping[str, object] | None) -> list[str]:
    """Render typed observations without changing their source roles."""

    if not isinstance(selected, Mapping):
        return []
    if "status" in selected:
        if selected.get("status") != "ready":
            return []
        entries = selected.get("entries")
        if not isinstance(entries, list):
            return []
        lines: list[str] = []
        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, Mapping):
                continue
            if entry.get("selection_reason") == "conversation_resume_visual":
                facts = _observation_facts(entry.get("observation"))
                if not facts:
                    continue
                lines.append(
                    "PRIVATE_CONVERSATION_VISUAL_HISTORY_V1\n"
                    f"这是同一私聊当前有界历史中的第 {position} 个已确认视觉事实；"
                    "它不是本轮新图片，也不代表当前文字一定在指它。"
                    "只有周边对话确实衔接时才使用，禁止推断发送者心理或把不同图片混成一张。\n"
                    + "；".join(facts)
                )
                continue
            if entry.get("selection_reason") != "predecessor_situation_revision":
                lines.extend(prior_visual_context_lines(entry))
                continue
            facts = _observation_facts(entry.get("observation"))
            if not facts:
                continue
            lines.append(
                "PRIVATE_PRIOR_VISUAL_SITUATION_V2\n"
                f"这是同一未完成对话中更早的第 {position} 个视觉输入的临时客观观察；"
                "它与其他视觉输入彼此独立，不是本轮新图片，也不是发送者心理事实。\n"
                + "；".join(facts)
            )
        return lines

    facts = _observation_facts(selected.get("observation"))
    if not facts:
        return []
    return [
        "PRIVATE_PRIOR_VISUAL_CONTEXT_V1\n"
        "这是当前文字明确引用的此前图片/表情包的临时客观观察，不是本轮新输入，也不是发送者心理事实。\n"
        + "；".join(facts)
    ]


__all__ = [
    "prior_visual_context_lines",
    "select_prior_visual_context",
    "select_prior_visual_contexts",
]
