#!/usr/bin/env python3
"""P1-2 Relationship Accumulation — deterministic, slow, instance-scoped.

Minimal accumulation engine. Reuses existing ``relationship_states.familiarity_context``
(new/familiar/long_term). No numeric affection scores, no game mechanics.

Transition must come from sustained interaction evidence (active days + span + volume)
not from single message or single-day burst. Time dimension is explicit:
100 messages in one day != 30 days of contact.

Deterministic policy adjudicates all writes. LLM may provide correction signal,
policy decides. Every transition records current_state, candidate_state,
reason/evidence, timestamp.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Mapping

from bridge_migrations import utc_now

try:
    from bridge_assistant_identity import current_assistant
except Exception:  # pragma: no cover - fallback for isolated tests
    current_assistant = None  # type: ignore


# ---- thresholds (minimal, slow) ---------------------------------------------

# new -> familiar
NEW_TO_FAMILIAR_MIN_ACTIVE_DAYS = 3
NEW_TO_FAMILIAR_MIN_SPAN_DAYS = 2
NEW_TO_FAMILIAR_MIN_USER_MESSAGES = 12

# familiar -> long_term
FAMILIAR_TO_LONG_TERM_MIN_ACTIVE_DAYS = 10
FAMILIAR_TO_LONG_TERM_MIN_SPAN_DAYS = 14
FAMILIAR_TO_LONG_TERM_MIN_USER_MESSAGES = 30

FAMILIARITY_ORDER = {"new": 0, "familiar": 1, "long_term": 2}
REVERSE_FAMILIARITY = {0: "new", 1: "familiar", 2: "long_term"}

CORRECTION_PHRASES = [
    "没那么熟",
    "沒那麼熟",
    "不熟",
    "我们不熟",
    "我們不熟",
    "还没那么熟",
    "還沒那麼熟",
    "别这样叫我",
    "别這麼叫我",
    "别这样叫",
    "别這麼叫",
    "别叫我",
    "别再叫我",
    "别叫我那个",
    "不要这样叫",
    "不要這麼叫",
    "别这么叫我",
    "别這麼叫我",
    "别这么亲密",
    "別這麼親密",
    "别这样亲密",
    "怎么这么亲密",
    "怎麼這麼親密",
    "太亲密",
    "太親密",
    "你今天怎么这么亲密",
    "你今天怎麼這麼親密",
    "我们其实没那么熟",
    "我們其實沒那麼熟",
    "我们没那么亲密",
    "保持距离",
    "保持距離",
]
_PREFERRED_ADDRESS_PATTERNS = (
    re.compile(r"(?:以后|之后|今后)(?:请|就)?叫我([^，,。.!！?？；;：:\s]{1,20})", re.IGNORECASE),
    re.compile(r"(?:请|改)(?:再)?叫我([^，,。.!！?？；;：:\s]{1,20})", re.IGNORECASE),
)

ACCUMULATION_FLAG = "relationship_accumulation_v1"
ACCUMULATION_MODE_KEY = "relationship_accumulation_mode"  # off / shadow / active


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_correction_signal(text: str) -> bool:
    """Detect user correction of intimacy (lower familiarity)."""
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    # normalize without spaces for phrase match
    compact = "".join(raw.split())
    for phrase in CORRECTION_PHRASES:
        norm = "".join(phrase.lower().split())
        if norm and norm in raw:
            return True
        if norm and norm in compact:
            return True
    return False


def preferred_address_correction(text: str) -> str:
    """Extract only an explicit self-designated address from Owner text."""

    raw = str(text or "").strip()
    for pattern in _PREFERRED_ADDRESS_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip("‘’“”\"'()（）[]【】")
        candidate = candidate.rstrip("吧呀啊哦啦呢")
        if 1 <= len(candidate) <= 20 and re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff-]+", candidate):
            return candidate
    return ""


def accumulation_mode_from_settings(settings: Mapping[str, object]) -> str:
    """Return off/shadow/active. Checks both flag and mode keys."""
    mode_raw = str(settings.get(ACCUMULATION_MODE_KEY) or "").strip().lower()
    if mode_raw in {"off", "shadow", "active"}:
        return mode_raw
    # fallback to flag boolean
    flag = str(settings.get(ACCUMULATION_FLAG) or "").strip().lower()
    if flag in {"1", "true", "yes", "on", "active"}:
        return "active"
    if flag == "shadow":
        return "shadow"
    return "off"


def accumulation_enabled(settings: Mapping[str, object]) -> bool:
    return accumulation_mode_from_settings(settings) == "active"


def accumulation_shadow(settings: Mapping[str, object]) -> bool:
    return accumulation_mode_from_settings(settings) == "shadow"


# ---- storage ----------------------------------------------------------------

def ensure_relationship_accumulation_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS relationship_state_transitions (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            from_familiarity TEXT NOT NULL,
            to_familiarity TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_relationship_transitions_lookup
        ON relationship_state_transitions(assistant_id,user_id,scope_type,scope_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_relationship_transitions_user
        ON relationship_state_transitions(assistant_id,user_id,created_at);
        """
    )


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    if current_assistant is None:
        raise ValueError("active_assistant_missing")
    assistant = current_assistant(conn)  # type: ignore
    if not assistant:
        raise ValueError("active_assistant_missing")
    return str(assistant["id"])


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now_utc(value: object | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        parsed = _parse_iso(value)
        if parsed:
            return parsed
    return datetime.now(timezone.utc)


# ---- correction persistence -------------------------------------------------

def _latest_correction_time(
    conn: sqlite3.Connection,
    assistant_id: str,
    user_id: str,
    scope_type: str,
    scope_id: str,
) -> datetime | None:
    """Return the most recent user_correction_downgrade timestamp for this scope."""
    ensure_relationship_accumulation_tables(conn)
    try:
        row = conn.execute(
            """
            SELECT created_at FROM relationship_state_transitions
            WHERE assistant_id=? AND user_id=? AND scope_type=? AND scope_id=?
              AND reason='user_correction_downgrade'
            ORDER BY created_at DESC LIMIT 1
            """,
            (assistant_id, user_id, scope_type, scope_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return _parse_iso(row[0])


# ---- evidence ---------------------------------------------------------------

def collect_evidence(
    conn: sqlite3.Connection,
    assistant_id: str,
    user_id: str,
    scope_type: str = "private_user",
    scope_id: str = "",
    *,
    now: datetime | str | None = None,
) -> dict:
    """Collect deterministic evidence for private_user scope.

    For other scopes we return empty evidence (no transition).
    Evidence is derived solely from persisted conversation_messages /
    conversation_threads — no LLM, no score.
    Post-correction evidence window: only messages with created_at >
    latest user_correction_downgrade are counted for automatic upgrade.
    This makes correction persistent: old 30-day history cannot immediately
    re-promote after a downgrade.
    """
    user_id = _clip(user_id, 80)
    scope_type = _clip(scope_type or "private_user", 40)
    scope_id = _clip(scope_id or "", 160)
    assistant_id = _clip(assistant_id, 80)
    now_dt = _now_utc(now)

    if scope_type != "private_user":
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "active_days": 0,
            "active_day_list": [],
            "total_user_messages": 0,
            "total_assistant_messages": 0,
            "total_messages": 0,
            "first_at": "",
            "last_at": "",
            "span_days": 0,
            "recent_days": None,
        }

    ensure_relationship_accumulation_tables(conn)

    # Find threads for this assistant+user (private). external_thread_ref == user_id
    try:
        rows = conn.execute(
            "SELECT id, created_at FROM conversation_threads WHERE assistant_id=? AND external_thread_ref=?",
            (assistant_id, user_id),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    thread_ids = [str(r[0]) for r in rows] if rows else []
    if not thread_ids:
        # fallback: try to find any thread with subject_actor_ref == user_id (legacy)
        try:
            rows2 = conn.execute(
                "SELECT id FROM conversation_threads WHERE assistant_id=? AND subject_actor_ref=?",
                (assistant_id, user_id),
            ).fetchall()
            thread_ids = [str(r[0]) for r in rows2]
        except sqlite3.Error:
            thread_ids = []
    if not thread_ids:
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "active_days": 0,
            "active_day_list": [],
            "total_user_messages": 0,
            "total_assistant_messages": 0,
            "total_messages": 0,
            "first_at": "",
            "last_at": "",
            "span_days": 0,
            "recent_days": None,
        }

    # correction persistence: only evidence after latest correction counts for auto upgrade
    correction_time = _latest_correction_time(conn, assistant_id, user_id, scope_type, scope_id)
    evidence_since = correction_time.isoformat() if correction_time else ""

    placeholders = ",".join(["?"] * len(thread_ids))
    try:
        msgs = conn.execute(
            f"SELECT created_at, role FROM conversation_messages WHERE thread_id IN ({placeholders}) ORDER BY created_at",
            tuple(thread_ids),
        ).fetchall()
    except sqlite3.Error:
        msgs = []

    user_dates: set[str] = set()
    user_count = 0
    assistant_count = 0
    first_dt: datetime | None = None
    last_dt: datetime | None = None
    for row in msgs:
        created = str(row[0] or "")
        role = str(row[1] or "")
        dt = _parse_iso(created)
        if dt is None:
            continue
        # filter pre-correction messages for auto evidence
        if correction_time and dt <= correction_time:
            continue
        if first_dt is None or dt < first_dt:
            first_dt = dt
        if last_dt is None or dt > last_dt:
            last_dt = dt
        if role == "user":
            user_count += 1
            # active day is UTC date of user message
            user_dates.add(dt.date().isoformat())
        elif role == "assistant":
            assistant_count += 1

    active_list = sorted(user_dates)
    span_days = 0
    if first_dt and last_dt:
        span_days = (last_dt.date() - first_dt.date()).days
    recent_days = None
    if last_dt:
        recent_days = (now_dt.date() - last_dt.date()).days

    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "active_days": len(active_list),
        "active_day_list": active_list,
        "total_user_messages": user_count,
        "total_assistant_messages": assistant_count,
        "total_messages": user_count + assistant_count,
        "first_at": first_dt.isoformat() if first_dt else "",
        "last_at": last_dt.isoformat() if last_dt else "",
        "span_days": span_days,
        "recent_days": recent_days,
        "evidence_since": evidence_since,
        "correction_applied": bool(correction_time),
    }


# ---- policy -----------------------------------------------------------------

def evaluate_transition(
    current_familiarity: str,
    evidence: dict,
    *,
    correction_signal: bool = False,
) -> dict:
    """Deterministic policy: returns candidate, reason, should_transition.

    Never exposes internal scoring; reason is for governance audit.
    """
    cur = _clip(current_familiarity or "new", 30).lower()
    if cur not in FAMILIARITY_ORDER:
        cur = "new"
    evidence = dict(evidence or {})
    active_days = int(evidence.get("active_days") or 0)
    span_days = int(evidence.get("span_days") or 0)
    total_user = int(evidence.get("total_user_messages") or 0)

    # correction has highest priority: downgrade one level
    if correction_signal:
        if cur == "long_term":
            return {
                "current": cur,
                "candidate": "familiar",
                "should_transition": True,
                "reason": "user_correction_downgrade",
                "evidence": evidence,
            }
        if cur == "familiar":
            return {
                "current": cur,
                "candidate": "new",
                "should_transition": True,
                "reason": "user_correction_downgrade",
                "evidence": evidence,
            }
        # new stays new, but we still record that correction was acknowledged
        return {
            "current": cur,
            "candidate": "new",
            "should_transition": False,
            "reason": "user_correction_acknowledged_already_new",
            "evidence": evidence,
        }

    # forward transitions only
    if cur == "new":
        if (
            active_days >= NEW_TO_FAMILIAR_MIN_ACTIVE_DAYS
            and span_days >= NEW_TO_FAMILIAR_MIN_SPAN_DAYS
            and total_user >= NEW_TO_FAMILIAR_MIN_USER_MESSAGES
        ):
            return {
                "current": cur,
                "candidate": "familiar",
                "should_transition": True,
                "reason": f"accumulation_active_days_{active_days}_span_{span_days}_user_messages_{total_user}",
                "evidence": evidence,
            }
        return {
            "current": cur,
            "candidate": "new",
            "should_transition": False,
            "reason": "accumulation_insufficient_for_familiar",
            "evidence": evidence,
        }

    if cur == "familiar":
        if (
            active_days >= FAMILIAR_TO_LONG_TERM_MIN_ACTIVE_DAYS
            and span_days >= FAMILIAR_TO_LONG_TERM_MIN_SPAN_DAYS
            and total_user >= FAMILIAR_TO_LONG_TERM_MIN_USER_MESSAGES
        ):
            return {
                "current": cur,
                "candidate": "long_term",
                "should_transition": True,
                "reason": f"accumulation_active_days_{active_days}_span_{span_days}_user_messages_{total_user}",
                "evidence": evidence,
            }
        return {
            "current": cur,
            "candidate": "familiar",
            "should_transition": False,
            "reason": "accumulation_insufficient_for_long_term",
            "evidence": evidence,
        }

    # long_term is terminal for auto accumulation
    return {
        "current": cur,
        "candidate": "long_term",
        "should_transition": False,
        "reason": "already_long_term_no_auto_downgrade",
        "evidence": evidence,
    }


def _relationship_current(conn: sqlite3.Connection, assistant_id: str, user_id: str, scope_type: str, scope_id: str) -> dict:
    # reuse existing service for read; fallback to direct SQL if service unavailable
    try:
        from bridge_relationship_service import get_relationship_state  # type: ignore
        return get_relationship_state(conn, user_id=user_id, scope_type=scope_type, scope_id=scope_id)
    except Exception:
        row = conn.execute(
            "SELECT familiarity_context, version FROM relationship_states WHERE assistant_id=? AND user_id=? AND scope_type=? AND scope_id=?",
            (assistant_id, user_id, scope_type, scope_id),
        ).fetchone()
        if row:
            return {"familiarity_context": str(row[0]), "version": int(row[1] or 0), "id": "direct"}
        return {"familiarity_context": "new", "version": 0, "id": ""}


def _write_relationship_state(
    conn: sqlite3.Connection,
    assistant_id: str,
    user_id: str,
    scope_type: str,
    scope_id: str,
    candidate_familiarity: str,
    expected_version: int,
    preferred_address: str | None = None,
) -> dict:
    """Direct versioned write bypassing proactive feature gate (accumulation owns it).

    Preserves other fields (preferred_address, interaction_style, etc.) as-is.
    """
    ensure_relationship_accumulation_tables(conn)
    # fetch existing row to preserve other columns
    row = conn.execute(
        "SELECT id, preferred_address, interaction_style, allowed_topics_json, blocked_topics_json, social_proactive_enabled, version FROM relationship_states WHERE assistant_id=? AND user_id=? AND scope_type=? AND scope_id=?",
        (assistant_id, user_id, scope_type, scope_id),
    ).fetchone()
    now = utc_now()
    candidate_familiarity = _clip(candidate_familiarity, 30)
    if candidate_familiarity not in FAMILIARITY_ORDER:
        raise ValueError("invalid_familiarity_context")
    if row:
        rid, preferred, style, allowed, blocked, proactive, ver = row
        if int(ver) != int(expected_version):
            raise ValueError("stale_relationship_version")
        next_version = int(ver) + 1
        selected_address = preferred if preferred_address is None else _clip(preferred_address, 80)
        conn.execute(
            """
            UPDATE relationship_states
            SET familiarity_context=?,preferred_address=?, version=?, updated_at=?
            WHERE assistant_id=? AND user_id=? AND scope_type=? AND scope_id=?
            """,
            (
                candidate_familiarity, selected_address, next_version, now,
                assistant_id, user_id, scope_type, scope_id,
            ),
        )
        return {
            "id": str(rid),
            "version": next_version,
            "familiarity_context": candidate_familiarity,
            "preferred_address": selected_address,
        }
    else:
        if int(expected_version) != 0:
            raise ValueError("stale_relationship_version")
        rid = f"rel_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO relationship_states(
                id,assistant_id,user_id,scope_type,scope_id,preferred_address,
                interaction_style,familiarity_context,allowed_topics_json,
                blocked_topics_json,social_proactive_enabled,version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rid, assistant_id, user_id, scope_type, scope_id,
                _clip(preferred_address, 80), "natural", candidate_familiarity, "[]", "[]", 0, 1, now, now,
            ),
        )
        return {
            "id": rid,
            "version": 1,
            "familiarity_context": candidate_familiarity,
            "preferred_address": _clip(preferred_address, 80),
        }


def record_transition(
    conn: sqlite3.Connection,
    assistant_id: str,
    user_id: str,
    scope_type: str,
    scope_id: str,
    from_familiarity: str,
    to_familiarity: str,
    reason: str,
    evidence: dict,
    *,
    mode: str = "active",
    created_at: str | None = None,
) -> dict:
    ensure_relationship_accumulation_tables(conn)
    tid = f"rtrans_{uuid.uuid4().hex}"
    now = created_at or utc_now()
    evidence_json = _canonical_json(evidence or {})
    # deterministic idempotency: same assistant/user/scope/from/to/reason/evidence hash within same day should not duplicate
    # we use unique id per call; caller handles duplicate suppression via version check
    conn.execute(
        """
        INSERT INTO relationship_state_transitions(
            id,assistant_id,user_id,scope_type,scope_id,from_familiarity,to_familiarity,reason,evidence_json,created_at,mode
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (tid, assistant_id, user_id, scope_type, scope_id, from_familiarity, to_familiarity, reason, evidence_json, now, mode),
    )
    return {
        "id": tid,
        "assistant_id": assistant_id,
        "user_id": user_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "from_familiarity": from_familiarity,
        "to_familiarity": to_familiarity,
        "reason": reason,
        "evidence": evidence,
        "created_at": now,
        "mode": mode,
    }


def apply_accumulation(
    conn: sqlite3.Connection,
    user_id: str,
    scope_type: str = "private_user",
    scope_id: str = "",
    *,
    now: datetime | str | None = None,
    message_text: str | None = None,
    mode: str = "active",
    # for testing: allow explicit assistant_id
    assistant_id: str | None = None,
) -> dict:
    """Evaluate and optionally apply one accumulation step.

    Returns dict with keys: current, candidate, should_transition, applied, reason, evidence, transition.
    Caller must be in a transaction or we manage one.
    Deterministic: no LLM.
    """
    user_id = _clip(user_id, 80)
    scope_type = _clip(scope_type or "private_user", 40)
    scope_id = _clip(scope_id or "", 160)
    if not user_id:
        raise ValueError("user_id_required")
    if scope_type not in {"private_user", "channel_thread", "qq_group", "project", "global_preference", "sensitive_private"}:
        raise ValueError("invalid_relationship_scope")

    aid = _clip(assistant_id or "", 80) or _active_assistant_id(conn)
    ensure_relationship_accumulation_tables(conn)

    current = _relationship_current(conn, aid, user_id, scope_type, scope_id)
    cur_fam = str(current.get("familiarity_context") or "new")
    cur_ver = int(current.get("version") or 0)

    evidence = collect_evidence(conn, aid, user_id, scope_type, scope_id, now=now)
    correction = is_correction_signal(message_text) if message_text is not None else False
    preferred_address = preferred_address_correction(message_text or "")
    decision = evaluate_transition(cur_fam, evidence, correction_signal=correction)

    candidate = str(decision["candidate"])
    should = bool(decision["should_transition"])
    reason = str(decision["reason"])
    # shadow mode: never write, only log
    if mode == "shadow":
        if should:
            trans = record_transition(conn, aid, user_id, scope_type, scope_id, cur_fam, candidate, reason, evidence, mode="shadow")
        else:
            trans = None
        return {
            "assistant_id": aid,
            "user_id": user_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "current": cur_fam,
            "candidate": candidate,
            "should_transition": should,
            "applied": False,
            "reason": reason,
            "evidence": evidence,
            "transition": trans,
            "version": cur_ver,
        }

    current_address = str(current.get("preferred_address") or "")
    address_changed = bool(preferred_address and preferred_address != current_address)
    if not should and not address_changed:
        return {
            "assistant_id": aid,
            "user_id": user_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "current": cur_fam,
            "candidate": candidate,
            "should_transition": False,
            "applied": False,
            "reason": reason,
            "evidence": evidence,
            "transition": None,
            "version": cur_ver,
        }

    # prevent skipping: enforce single step
    cur_order = FAMILIARITY_ORDER.get(cur_fam, 0)
    cand_order = FAMILIARITY_ORDER.get(candidate, 0)
    if cand_order - cur_order > 1:
        # clamp to next step only
        candidate = REVERSE_FAMILIARITY[cur_order + 1]
        reason = reason + "_clamped_to_next_step"

    # idempotent: if already at candidate, no write
    if candidate == cur_fam and not address_changed:
        return {
            "assistant_id": aid,
            "user_id": user_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "current": cur_fam,
            "candidate": candidate,
            "should_transition": False,
            "applied": False,
            "reason": "already_at_candidate",
            "evidence": evidence,
            "transition": None,
            "version": cur_ver,
        }

    familiarity_changed = candidate != cur_fam
    applied_reason = (
        "user_address_correction"
        if address_changed and not familiarity_changed
        else reason
    )

    # perform versioned write
    in_tx = conn.in_transaction
    try:
        if not in_tx:
            conn.execute("BEGIN IMMEDIATE")
        written = _write_relationship_state(
            conn,
            aid,
            user_id,
            scope_type,
            scope_id,
            candidate,
            cur_ver,
            preferred_address=preferred_address if address_changed else None,
        )
        trans = (
            record_transition(
                conn, aid, user_id, scope_type, scope_id,
                cur_fam, candidate, reason, evidence, mode="active",
            )
            if familiarity_changed
            else None
        )
        if not in_tx:
            conn.commit()
    except Exception:
        if not in_tx:
            try:
                conn.rollback()
            except Exception:
                pass
        raise

    return {
        "assistant_id": aid,
        "user_id": user_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "current": cur_fam,
        "candidate": candidate,
        "should_transition": familiarity_changed,
        "applied": True,
        "reason": applied_reason,
        "preferred_address": str(written.get("preferred_address") or current_address),
        "evidence": evidence,
        "transition": trans,
        "version": int(written["version"]),
    }


def list_transitions(
    conn: sqlite3.Connection,
    user_id: str,
    scope_type: str = "private_user",
    scope_id: str = "",
    *,
    limit: int = 20,
) -> list[dict]:
    ensure_relationship_accumulation_tables(conn)
    aid = _active_assistant_id(conn)
    user_id = _clip(user_id, 80)
    scope_type = _clip(scope_type or "private_user", 40)
    scope_id = _clip(scope_id or "", 160)
    limit = max(1, min(int(limit or 20), 100))
    rows = conn.execute(
        """
        SELECT id,assistant_id,user_id,scope_type,scope_id,from_familiarity,to_familiarity,reason,evidence_json,created_at,mode
        FROM relationship_state_transitions
        WHERE assistant_id=? AND user_id=? AND scope_type=? AND scope_id=?
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (aid, user_id, scope_type, scope_id, limit),
    ).fetchall()
    result = []
    for r in rows:
        try:
            ev = json.loads(str(r[8] or "{}"))
        except json.JSONDecodeError:
            ev = {}
        result.append({
            "id": str(r[0]),
            "assistant_id": str(r[1]),
            "user_id": str(r[2]),
            "scope_type": str(r[3]),
            "scope_id": str(r[4]),
            "from_familiarity": str(r[5]),
            "to_familiarity": str(r[6]),
            "reason": str(r[7]),
            "evidence": ev,
            "created_at": str(r[9]),
            "mode": str(r[10]),
        })
    return result


def reset_relationship(
    conn: sqlite3.Connection,
    user_id: str,
    scope_type: str = "private_user",
    scope_id: str = "",
    *,
    to_familiarity: str = "new",
    reason: str = "admin_reset",
) -> dict:
    """Governance reset: directly set familiarity to desired value.

    Used for Owner/Admin correction/reset. Still versioned and logged.
    """
    to_familiarity = _clip(to_familiarity, 30)
    if to_familiarity not in FAMILIARITY_ORDER:
        raise ValueError("invalid_familiarity_context")
    user_id = _clip(user_id, 80)
    scope_type = _clip(scope_type or "private_user", 40)
    scope_id = _clip(scope_id or "", 160)
    aid = _active_assistant_id(conn)
    ensure_relationship_accumulation_tables(conn)
    current = _relationship_current(conn, aid, user_id, scope_type, scope_id)
    cur_fam = str(current.get("familiarity_context") or "new")
    cur_ver = int(current.get("version") or 0)
    if cur_fam == to_familiarity:
        return {"applied": False, "current": cur_fam, "candidate": to_familiarity, "reason": "already_at_target"}
    in_tx = conn.in_transaction
    try:
        if not in_tx:
            conn.execute("BEGIN IMMEDIATE")
        written = _write_relationship_state(conn, aid, user_id, scope_type, scope_id, to_familiarity, cur_ver)
        evidence = {"manual_reset": True, "from": cur_fam, "to": to_familiarity}
        trans = record_transition(conn, aid, user_id, scope_type, scope_id, cur_fam, to_familiarity, reason, evidence, mode="reset")
        if not in_tx:
            conn.commit()
    except Exception:
        if not in_tx:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    return {
        "applied": True,
        "current": cur_fam,
        "candidate": to_familiarity,
        "version": int(written["version"]),
        "transition": trans,
    }


__all__ = [
    "ACCUMULATION_FLAG",
    "ACCUMULATION_MODE_KEY",
    "FAMILIARITY_ORDER",
    "NEW_TO_FAMILIAR_MIN_ACTIVE_DAYS",
    "NEW_TO_FAMILIAR_MIN_SPAN_DAYS",
    "NEW_TO_FAMILIAR_MIN_USER_MESSAGES",
    "FAMILIAR_TO_LONG_TERM_MIN_ACTIVE_DAYS",
    "FAMILIAR_TO_LONG_TERM_MIN_SPAN_DAYS",
    "FAMILIAR_TO_LONG_TERM_MIN_USER_MESSAGES",
    "CORRECTION_PHRASES",
    "accumulation_enabled",
    "accumulation_mode_from_settings",
    "accumulation_shadow",
    "apply_accumulation",
    "collect_evidence",
    "ensure_relationship_accumulation_tables",
    "evaluate_transition",
    "is_correction_signal",
    "preferred_address_correction",
    "list_transitions",
    "record_transition",
    "reset_relationship",
]
