#!/usr/bin/env python3
"""Durable schedules and bounded proactive-contact policies.

This module owns time calculation, deterministic policy gates and audit state.
It deliberately does not know HTTP, QQ or model-provider details; callers supply
execution, generation and Delivery Outbox adapters.
"""
from __future__ import annotations

import json
import re
import hashlib
import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bridge_automation_schema import ensure_automation_tables
from bridge_automation_contracts import (
    DEFAULT_OUTPUT_CONTRACT,
    automation_config_hash,
    normalize_output_contract,
    output_contract_hash,
)
from bridge_automation_runs import (
    finish_automation_run,
    list_automation_seen_items,
    reserve_automation_items,
    settle_automation_dispatch,
)
from bridge_reliability_service import stage_proactive_delivery
from bridge_proactive_messaging_policy import policy_gate_if_present
import bridge_group_proactive_scheduler as group_proactive


DEFAULT_TIMEZONE = "Asia/Shanghai"
TRUE_VALUES = {"1", "true", "yes", "on"}
SCHEDULE_TYPES = {"once", "daily", "weekly", "interval"}
ACTION_TYPES = {"reminder", "agent"}
INITIATIVE_MODES = {"conservative", "balanced", "warm"}
PROACTIVE_INTENTS = {"follow_up", "share", "check_in", "celebrate", "reminder", "silence"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | str | None = None) -> str:
    current = value or utc_now()
    if isinstance(current, str):
        current = parse_datetime(current)
    if current is None:
        raise ValueError("invalid_datetime")
    return current.astimezone(timezone.utc).isoformat()


def parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_local_datetime(value: object, zone_name: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_timezone(zone_name))
    return parsed.astimezone(timezone.utc)


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _timezone(value: object):
    name = _clip(value or DEFAULT_TIMEZONE, 80) or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception as exc:
        # Some minimal Windows Python distributions ship without the system
        # IANA database. Keep deterministic local tests and emergency admin
        # operations usable for the two built-in zones; production Linux still
        # uses ZoneInfo and arbitrary validated IANA names.
        if name in {"UTC", "Etc/UTC"}:
            return timezone.utc
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        raise ValueError("invalid_timezone") from exc


def _clock(value: object, default: str = "09:00") -> time:
    text = _clip(value or default, 5)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("invalid_time_of_day")
    hour, minute = (int(part) for part in text.split(":"))
    return time(hour=hour, minute=minute)


def _weekdays(value: object) -> tuple[int, ...]:
    if isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = str(value or "0").split(",")
    try:
        result = tuple(sorted({int(part) for part in parts if str(part).strip() != ""}))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_weekdays") from exc
    if not result or any(day < 0 or day > 6 for day in result):
        raise ValueError("invalid_weekdays")
    return result


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def calculate_next_due(job: dict, *, after: datetime | None = None) -> datetime | None:
    """Return the first occurrence strictly after ``after`` for repeating jobs."""

    current = (after or utc_now()).astimezone(timezone.utc)
    schedule_type = _clip(job.get("schedule_type"), 20)
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError("invalid_schedule_type")
    if schedule_type == "once":
        run_at = parse_datetime(job.get("run_at"))
        if run_at is None:
            raise ValueError("run_at_required")
        return run_at
    if schedule_type == "interval":
        minutes = max(15, min(int(job.get("interval_minutes") or 1440), 525600))
        return current + timedelta(minutes=minutes)

    zone = _timezone(job.get("timezone"))
    clock = _clock(job.get("time_of_day"))
    local_now = current.astimezone(zone)
    allowed = set(_weekdays(job.get("weekdays"))) if schedule_type == "weekly" else set(range(7))
    for offset in range(0, 8):
        day = local_now.date() + timedelta(days=offset)
        if day.weekday() not in allowed:
            continue
        candidate = datetime.combine(day, clock, tzinfo=zone)
        if candidate > local_now:
            return candidate.astimezone(timezone.utc)
    raise ValueError("next_due_unresolvable")


def _job_payload(payload: dict, existing: dict | None = None) -> dict:
    current = existing or {}
    title = _clip(payload.get("title", current.get("title")), 120)
    instruction = _clip(payload.get("instruction", current.get("instruction")), 4000)
    user_id = _clip(payload.get("user_id", current.get("user_id")), 80)
    action_type = _clip(payload.get("action_type", current.get("action_type") or "reminder"), 20)
    schedule_type = _clip(payload.get("schedule_type", current.get("schedule_type") or "once"), 20)
    if not user_id:
        raise ValueError("user_id_required")
    if not title:
        raise ValueError("title_required")
    if not instruction:
        raise ValueError("instruction_required")
    if action_type not in ACTION_TYPES:
        raise ValueError("invalid_action_type")
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError("invalid_schedule_type")
    zone_name = _clip(payload.get("timezone", current.get("timezone") or DEFAULT_TIMEZONE), 80)
    _timezone(zone_name)
    time_of_day = _clock(payload.get("time_of_day", current.get("time_of_day") or "09:00")).strftime("%H:%M")
    weekdays = ",".join(str(day) for day in _weekdays(payload.get("weekdays", current.get("weekdays") or "0")))
    interval = max(15, min(int(payload.get("interval_minutes", current.get("interval_minutes") or 1440)), 525600))
    run_at = _clip(payload.get("run_at", current.get("run_at")), 80)
    parsed_run_at = parse_local_datetime(run_at, zone_name) if run_at else None
    if schedule_type == "once" and parsed_run_at is None:
        raise ValueError("run_at_required")
    output_contract = normalize_output_contract(
        payload.get("output_contract", current.get("output_contract_json") or DEFAULT_OUTPUT_CONTRACT),
    )
    return {
        "user_id": user_id,
        "title": title,
        "instruction": instruction,
        "parameters_json": json.dumps(
            payload.get("parameters", current.get("parameters_json") or {}),
            ensure_ascii=False,
            sort_keys=True,
        ) if isinstance(payload.get("parameters", current.get("parameters_json") or {}), (dict, list)) else str(
            payload.get("parameters", current.get("parameters_json") or "{}")
        )[:4000],
        "action_type": action_type,
        "schedule_type": schedule_type,
        "run_at": timestamp(parsed_run_at) if parsed_run_at else "",
        "time_of_day": time_of_day,
        "weekdays": weekdays,
        "interval_minutes": interval,
        "timezone": zone_name,
        "enabled": 1 if truthy(payload.get("enabled", current.get("enabled"))) else 0,
        "output_contract_json": json.dumps(
            output_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
        "output_contract_hash": output_contract_hash(output_contract),
    }


def upsert_automation_job(conn: sqlite3.Connection, payload: dict) -> dict:
    ensure_automation_tables(conn)
    job_id = _clip(payload.get("id"), 80) or uuid.uuid4().hex
    existing = _row(conn.execute("SELECT * FROM automation_jobs WHERE id = ?", (job_id,)).fetchone())
    values = _job_payload(payload, existing)
    now = utc_now()
    requested_due = parse_datetime(payload.get("next_due_at"))
    if values["enabled"]:
        next_due = requested_due or calculate_next_due(values, after=now)
        if values["schedule_type"] == "once" and next_due and next_due < now:
            raise ValueError("run_at_must_be_future")
        state = "scheduled"
    else:
        next_due = requested_due or parse_datetime((existing or {}).get("next_due_at"))
        state = "disabled"
    conn.execute(
        """
        INSERT INTO automation_jobs(
            id, user_id, title, action_type, instruction, parameters_json,
            revision, output_contract_json, output_contract_hash, schedule_type, run_at,
            time_of_day, weekdays, interval_minutes, timezone, enabled, state,
            next_due_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            user_id=excluded.user_id, title=excluded.title,
            action_type=excluded.action_type, instruction=excluded.instruction,
            parameters_json=excluded.parameters_json,
            revision=automation_jobs.revision+1,
            output_contract_json=excluded.output_contract_json,
            output_contract_hash=excluded.output_contract_hash,
            schedule_type=excluded.schedule_type, run_at=excluded.run_at,
            time_of_day=excluded.time_of_day, weekdays=excluded.weekdays,
            interval_minutes=excluded.interval_minutes, timezone=excluded.timezone,
            enabled=excluded.enabled, state=excluded.state,
            next_due_at=excluded.next_due_at, lease_until='', updated_at=excluded.updated_at
        """,
        (
            job_id, values["user_id"], values["title"], values["action_type"],
            values["instruction"], values["parameters_json"],
            1, values["output_contract_json"], values["output_contract_hash"],
            values["schedule_type"], values["run_at"],
            values["time_of_day"], values["weekdays"], values["interval_minutes"],
            values["timezone"], values["enabled"], state,
            timestamp(next_due) if next_due else "", timestamp(now), timestamp(now),
        ),
    )
    return dict(conn.execute("SELECT * FROM automation_jobs WHERE id = ?", (job_id,)).fetchone())


def list_automation_jobs(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    ensure_automation_tables(conn)
    rows = conn.execute(
        """SELECT * FROM automation_jobs
           ORDER BY enabled DESC, CASE WHEN next_due_at='' THEN 1 ELSE 0 END,
                    next_due_at ASC, updated_at DESC LIMIT ?""",
        (max(1, min(int(limit or 100), 200)),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_automation_runs(conn: sqlite3.Connection, *, limit: int = 80) -> list[dict]:
    ensure_automation_tables(conn)
    rows = conn.execute(
        """SELECT r.*, j.title, j.action_type FROM automation_runs r
           LEFT JOIN automation_jobs j ON j.id=r.job_id
           ORDER BY r.started_at DESC LIMIT ?""",
        (max(1, min(int(limit or 80), 200)),),
    ).fetchall()
    return [dict(row) for row in rows]


def claim_due_jobs(conn: sqlite3.Connection, *, now: datetime | None = None, limit: int = 5) -> list[dict]:
    ensure_automation_tables(conn)
    current = (now or utc_now()).astimezone(timezone.utc)
    current_text = timestamp(current)
    rows = conn.execute(
        """SELECT * FROM automation_jobs
           WHERE enabled=1 AND state<>'dispatched' AND next_due_at<>'' AND next_due_at<=?
             AND (lease_until='' OR lease_until<=?)
           ORDER BY next_due_at ASC LIMIT ?""",
        (current_text, current_text, max(1, min(int(limit or 5), 20))),
    ).fetchall()
    claimed: list[dict] = []
    for row in rows:
        item = dict(row)
        scheduled_for = item["next_due_at"]
        lease_until = timestamp(current + timedelta(minutes=5))
        updated = conn.execute(
            """UPDATE automation_jobs SET state='running', lease_until=?, updated_at=?
               WHERE id=? AND enabled=1 AND next_due_at=?
                 AND (lease_until='' OR lease_until<=?)""",
            (lease_until, current_text, item["id"], scheduled_for, current_text),
        )
        if updated.rowcount != 1:
            continue
        run_id = uuid.uuid4().hex
        existing = conn.execute(
            "SELECT * FROM automation_runs WHERE job_id=? AND scheduled_for=?",
            (item["id"], scheduled_for),
        ).fetchone()
        owner = "automation-" + uuid.uuid4().hex[:12]
        if existing:
            if str(existing["status"]) != "running":
                continue
            run_id = str(existing["id"])
            conn.execute(
                """UPDATE automation_runs SET lease_owner=?,lease_until=?,
                          attempt_count=attempt_count+1,error='',finished_at=''
                   WHERE id=? AND status='running'""",
                (owner, lease_until, run_id),
            )
        else:
            conn.execute(
                """INSERT INTO automation_runs(
                       id,job_id,user_id,scheduled_for,status,started_at,
                       lease_owner,lease_until,attempt_count,job_revision,
                       config_hash,output_contract_hash
                   ) VALUES(?,?,?,?,'running',?,?,?,1,?,?,?)""",
                (
                    run_id, item["id"], item["user_id"], scheduled_for,
                    current_text, owner, lease_until, int(item.get("revision") or 1),
                    automation_config_hash(item), str(item.get("output_contract_hash") or ""),
                ),
            )
        item.update({"lease_until": lease_until, "run_id": run_id, "scheduled_for": scheduled_for})
        claimed.append(item)
    return claimed


def upsert_proactive_policy(conn: sqlite3.Connection, payload: dict) -> dict:
    ensure_automation_tables(conn)
    user_id = _clip(payload.get("user_id"), 80)
    if not user_id:
        raise ValueError("user_id_required")
    existing = _row(conn.execute("SELECT * FROM proactive_policies WHERE user_id=?", (user_id,)).fetchone()) or {}
    zone_name = _clip(payload.get("timezone", existing.get("timezone") or DEFAULT_TIMEZONE), 80)
    _timezone(zone_name)
    quiet_start = _clock(payload.get("quiet_start", existing.get("quiet_start") or "23:30")).strftime("%H:%M")
    quiet_end = _clock(payload.get("quiet_end", existing.get("quiet_end") or "09:00")).strftime("%H:%M")

    def bounded(name: str, default: int, low: int, high: int) -> int:
        return max(low, min(int(payload.get(name, existing.get(name) or default)), high))

    enabled = 1 if truthy(payload.get("enabled", existing.get("enabled"))) else 0
    authorized = 1 if truthy(payload.get("authorized", existing.get("authorized"))) else 0
    if enabled and not authorized:
        raise ValueError("explicit_authorization_required")
    now = utc_now()
    requested_check = parse_datetime(payload.get("next_check_at"))
    next_check = requested_check or (now + timedelta(minutes=bounded("evaluation_interval_minutes", 60, 15, 1440)))
    state = "scheduled" if enabled and authorized else "disabled"
    initiative_mode = _clip(payload.get("initiative_mode", existing.get("initiative_mode") or "balanced"), 20)
    if initiative_mode not in INITIATIVE_MODES:
        raise ValueError("invalid_initiative_mode")
    raw_intents = payload.get("allowed_intents", existing.get("allowed_intents") or "follow_up,share,check_in,celebrate,reminder")
    if isinstance(raw_intents, (list, tuple, set)):
        intent_items = [str(item).strip() for item in raw_intents]
    else:
        intent_items = [item.strip() for item in str(raw_intents).split(",")]
    allowed_intents = sorted({item for item in intent_items if item in PROACTIVE_INTENTS and item != "silence"})
    if not allowed_intents:
        raise ValueError("proactive_intent_required")
    conn.execute(
        """
        INSERT INTO proactive_policies(
            user_id, enabled, authorized, timezone, quiet_start, quiet_end,
            min_silence_minutes, min_gap_minutes, daily_limit, weekly_limit,
            unanswered_limit, evaluation_interval_minutes, topic_notes,
            include_meme, initiative_mode, allowed_intents, schedule_jitter_minutes,
            topic_cooldown_minutes, state, state_reason, next_check_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            enabled=excluded.enabled, authorized=excluded.authorized,
            timezone=excluded.timezone, quiet_start=excluded.quiet_start,
            quiet_end=excluded.quiet_end, min_silence_minutes=excluded.min_silence_minutes,
            min_gap_minutes=excluded.min_gap_minutes, daily_limit=excluded.daily_limit,
            weekly_limit=excluded.weekly_limit, unanswered_limit=excluded.unanswered_limit,
            evaluation_interval_minutes=excluded.evaluation_interval_minutes,
            topic_notes=excluded.topic_notes, include_meme=excluded.include_meme,
            initiative_mode=excluded.initiative_mode, allowed_intents=excluded.allowed_intents,
            schedule_jitter_minutes=excluded.schedule_jitter_minutes,
            topic_cooldown_minutes=excluded.topic_cooldown_minutes,
            state=excluded.state, state_reason='', next_check_at=excluded.next_check_at,
            lease_until='', updated_at=excluded.updated_at
        """,
        (
            user_id, enabled, authorized, zone_name, quiet_start, quiet_end,
            bounded("min_silence_minutes", 180, 15, 43200),
            bounded("min_gap_minutes", 360, 30, 43200),
            bounded("daily_limit", 2, 1, 12), bounded("weekly_limit", 5, 1, 50),
            bounded("unanswered_limit", 2, 1, 10), bounded("evaluation_interval_minutes", 60, 15, 1440),
            _clip(payload.get("topic_notes", existing.get("topic_notes")), 1000),
            1 if truthy(payload.get("include_meme", existing.get("include_meme"))) else 0,
            initiative_mode, ",".join(allowed_intents),
            bounded("schedule_jitter_minutes", 20, 0, 180),
            bounded("topic_cooldown_minutes", 1440, 30, 43200),
            state, timestamp(next_check), timestamp(now), timestamp(now),
        ),
    )
    group_proactive.persist_subject_metadata(conn, payload=payload, existing=existing, user_id=user_id)
    return dict(conn.execute("SELECT * FROM proactive_policies WHERE user_id=?", (user_id,)).fetchone())


def list_proactive_policies(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    ensure_automation_tables(conn)
    rows = conn.execute(
        """SELECT p.*, s.session AS known_session,
                  (SELECT COUNT(*) FROM proactive_events e WHERE e.user_id=p.user_id) AS event_count
           FROM proactive_policies p LEFT JOIN qq_sessions s ON s.user_id=p.user_id
           ORDER BY p.enabled DESC, p.next_check_at ASC LIMIT ?""",
        (max(1, min(int(limit or 100), 200)),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_proactive_events(conn: sqlite3.Connection, *, user_id: str = "", limit: int = 80) -> list[dict]:
    ensure_automation_tables(conn)
    if user_id:
        rows = conn.execute(
            "SELECT * FROM proactive_events WHERE user_id=? ORDER BY decision_at DESC LIMIT ?",
            (_clip(user_id, 80), max(1, min(int(limit or 80), 200))),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM proactive_events ORDER BY decision_at DESC LIMIT ?",
            (max(1, min(int(limit or 80), 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def reconcile_group_proactive_policies(conn: sqlite3.Connection) -> int: return group_proactive.reconcile_group_proactive_policies(conn, upsert_policy=upsert_proactive_policy)
def reconcile_owner_proactive_policy(conn: sqlite3.Connection) -> int: return group_proactive.reconcile_owner_proactive_policy(conn, upsert_policy=upsert_proactive_policy)


def _quiet_end(current: datetime, policy: dict) -> datetime | None:
    zone = _timezone(policy.get("timezone"))
    start, end = _clock(policy.get("quiet_start"), "23:30"), _clock(policy.get("quiet_end"), "09:00")
    local = current.astimezone(zone)
    minute = local.hour * 60 + local.minute
    start_minute, end_minute = start.hour * 60 + start.minute, end.hour * 60 + end.minute
    if start_minute == end_minute:
        return None
    inside = start_minute <= minute < end_minute if start_minute < end_minute else minute >= start_minute or minute < end_minute
    if not inside:
        return None
    target = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if start_minute >= end_minute and minute >= start_minute:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _period_start(current: datetime, zone, *, week: bool) -> datetime:
    local = current.astimezone(zone)
    day = local.date() - timedelta(days=local.weekday() if week else 0)
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)


def _defer_policy(conn: sqlite3.Connection, user_id: str, state: str, reason: str, next_check: datetime, now: datetime) -> None:
    conn.execute(
        """UPDATE proactive_policies SET state=?, state_reason=?, next_check_at=?,
                  lease_until='', last_evaluated_at=?, updated_at=? WHERE user_id=?""",
        (state, reason, timestamp(next_check), timestamp(now), timestamp(now), user_id),
    )


def claim_due_proactive_policies(conn: sqlite3.Connection, *, now: datetime | None = None, limit: int = 3) -> list[dict]:
    """Apply all deterministic gates and lease only policies needing a model decision."""

    ensure_automation_tables(conn)
    current = (now or utc_now()).astimezone(timezone.utc)
    current_text = timestamp(current)
    rows = conn.execute(group_proactive.proactive_due_query(conn), (current_text, current_text)).fetchall()
    claimed: list[dict] = []
    for row in rows:
        if len(claimed) >= max(1, min(int(limit or 3), 10)):
            break
        item = dict(row)
        user_id = item["user_id"]
        is_group = str(item.get("policy_kind") or "") == "group_social"
        interval = timedelta(minutes=max(15, int(item["evaluation_interval_minutes"])))
        messaging_gate = policy_gate_if_present(conn, user_id)
        if messaging_gate and not messaging_gate["allowed"]:
            _defer_policy(
                conn,
                user_id,
                "policy_disabled",
                str(messaging_gate.get("reason") or "proactive_messaging_policy"),
                current + interval,
                current,
            )
            continue
        observed = parse_datetime(item.get("observed_user_at"))
        saved_user = parse_datetime(item.get("last_user_at"))
        last_user = max(filter(None, (observed, saved_user)), default=None)
        if observed and (saved_user is None or observed > saved_user):
            conn.execute(
                "UPDATE proactive_policies SET last_user_at=?, consecutive_unanswered=0 WHERE user_id=?",
                (timestamp(observed), user_id),
            )
            item["consecutive_unanswered"] = 0
        if not _clip(item.get("send_session"), 300):
            _defer_policy(conn, user_id, "pending_session", "qq_session_unavailable", current + timedelta(minutes=15), current)
            continue
        quiet_until = _quiet_end(current, item)
        if quiet_until:
            _defer_policy(conn, user_id, "quiet", "quiet_hours", quiet_until, current)
            continue
        if last_user:
            silence_until = last_user + timedelta(minutes=int(item["min_silence_minutes"]))
            if silence_until > current:
                _defer_policy(conn, user_id, "waiting_silence", "minimum_silence", silence_until, current)
                continue
        dormant_until = (
            group_proactive.dormant_group_next_check(
                last_user=last_user,
                current=current,
            )
            if is_group
            else None
        )
        if dormant_until:
            _defer_policy(
                conn,
                user_id,
                "waiting_activity",
                "group_activity_stale",
                dormant_until,
                current,
            )
            continue
        last_sent = parse_datetime(item.get("last_sent_at"))
        if last_sent:
            gap_until = last_sent + timedelta(minutes=int(item["min_gap_minutes"]))
            if gap_until > current:
                _defer_policy(conn, user_id, "cooldown", "minimum_gap", gap_until, current)
                continue
        if not is_group and int(item.get("consecutive_unanswered") or 0) >= int(item["unanswered_limit"]):
            _defer_policy(conn, user_id, "waiting_reply", "unanswered_limit", current + interval, current)
            continue
        pending = conn.execute(
            """SELECT 1 FROM proactive_events WHERE user_id=? AND action='send'
               AND delivered_at='' AND error='' LIMIT 1""",
            (user_id,),
        ).fetchone()
        if pending:
            _defer_policy(conn, user_id, "delivery_pending", "delivery_pending", current + timedelta(minutes=15), current)
            continue
        zone = _timezone(item.get("timezone"))
        day_start = _period_start(current, zone, week=False)
        week_start = _period_start(current, zone, week=True)
        daily = conn.execute(
            "SELECT COUNT(*) FROM proactive_events WHERE user_id=? AND action='send' AND decision_at>=?",
            (user_id, timestamp(day_start)),
        ).fetchone()[0]
        weekly = conn.execute(
            "SELECT COUNT(*) FROM proactive_events WHERE user_id=? AND action='send' AND decision_at>=?",
            (user_id, timestamp(week_start)),
        ).fetchone()[0]
        if daily >= int(item["daily_limit"]):
            tomorrow = datetime.combine(current.astimezone(zone).date() + timedelta(days=1), time.min, tzinfo=zone)
            _defer_policy(conn, user_id, "budget_wait", "daily_limit", tomorrow.astimezone(timezone.utc), current)
            continue
        if weekly >= int(item["weekly_limit"]):
            next_week = datetime.combine(current.astimezone(zone).date() + timedelta(days=7-current.astimezone(zone).weekday()), time.min, tzinfo=zone)
            _defer_policy(conn, user_id, "budget_wait", "weekly_limit", next_week.astimezone(timezone.utc), current)
            continue
        lease_until = timestamp(current + timedelta(minutes=5))
        updated = conn.execute(
            """UPDATE proactive_policies SET state='evaluating', state_reason='', lease_until=?,
                      last_evaluated_at=?, updated_at=?
               WHERE user_id=? AND enabled=1 AND authorized=1
                 AND (lease_until='' OR lease_until<=?)""",
            (lease_until, current_text, current_text, user_id, current_text),
        )
        if updated.rowcount == 1:
            item.update({"last_user_at": timestamp(last_user) if last_user else "", "lease_until": lease_until})
            claimed.append(item)
    return claimed


def record_proactive_decision(
    conn: sqlite3.Connection,
    policy: dict,
    decision: dict,
    *,
    now: datetime | None = None,
) -> dict:
    ensure_automation_tables(conn)
    current = (now or utc_now()).astimezone(timezone.utc)
    action = "send" if str(decision.get("action") or "").strip().lower() == "send" else "skip"
    intent = _clip(decision.get("intent") or ("check_in" if action == "send" else "silence"), 40)
    messaging_gate = policy_gate_if_present(conn, str(policy.get("user_id") or ""))
    if action == "send" and messaging_gate:
        if not messaging_gate["allowed"]:
            action, intent = "skip", "silence"
            decision = {
                **decision,
                "reason": str(messaging_gate.get("reason") or "policy_disabled"),
                "message": "",
                "topic_key": "",
            }
        elif not messaging_gate["send_allowed"]:
            execution_mode = str(messaging_gate.get("execution_mode") or "draft")
            action = "review" if execution_mode == "confirm" else "draft"
            decision = {
                **decision,
                "reason": "policy_requires_review",
                "message": _clip(decision.get("message"), 1200),
                "topic_key": _clip(decision.get("topic_key"), 120),
            }
    allowed = {item.strip() for item in str(policy.get("allowed_intents") or "").split(",") if item.strip()}
    if intent not in PROACTIVE_INTENTS or (action == "send" and intent not in allowed):
        action, intent = "skip", "silence"
        decision = {**decision, "reason": "intent_not_allowed", "message": "", "topic_key": ""}
    reason = _clip(decision.get("reason") or ("model_send" if action == "send" else "model_skip"), 300)
    message = _clip(decision.get("message"), 1200) if action in {"send", "review", "draft"} else ""
    if action in {"send", "review", "draft"} and not message:
        raise ValueError("proactive_message_required")
    try:
        next_minutes = max(15, min(int(decision.get("next_check_minutes") or policy.get("evaluation_interval_minutes") or 60), 10080))
    except (TypeError, ValueError):
        next_minutes = max(15, int(policy.get("evaluation_interval_minutes") or 60))
    topic_key = _clip(decision.get("topic_key"), 120)
    feature_table = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='assistant_feature_flags'
        """,
    ).fetchone()
    event_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(proactive_events)").fetchall()
    }
    gate8_columns = {
        "assistant_id",
        "policy_kind",
        "policy_version",
        "trigger_reason",
        "condition_snapshot_json",
        "idempotency_key",
        "request_hash",
        "blocked_reason",
    }.issubset(event_columns)
    feature_row = (
        conn.execute(
            """
            SELECT enabled FROM assistant_feature_flags
            WHERE name='relationship_proactive_v2'
            """,
        ).fetchone()
        if feature_table and gate8_columns
        else None
    )
    gate8_enabled = bool(feature_row and int(feature_row[0]))
    assistant_id = _clip(policy.get("assistant_id"), 80)
    policy_kind = _clip(policy.get("policy_kind") or "social", 40)
    policy_version = max(1, int(policy.get("policy_version") or 1))
    trigger_reason = _clip(decision.get("trigger_reason") or reason, 300)
    condition_snapshot = {
        "scheduled_for": policy.get("next_check_at") or timestamp(current),
        "last_user_at": policy.get("last_user_at") or "",
        "last_sent_at": policy.get("last_sent_at") or "",
        "consecutive_unanswered": int(policy.get("consecutive_unanswered") or 0),
        "min_silence_minutes": int(policy.get("min_silence_minutes") or 0),
        "min_gap_minutes": int(policy.get("min_gap_minutes") or 0),
        "daily_limit": int(policy.get("daily_limit") or 0),
        "weekly_limit": int(policy.get("weekly_limit") or 0),
    }
    decision_payload = {
        "assistant_id": assistant_id,
        "user_id": policy["user_id"],
        "policy_kind": policy_kind,
        "policy_version": policy_version,
        "scheduled_for": condition_snapshot["scheduled_for"],
        "action": action,
        "intent": intent,
        "reason": reason,
        "message": message,
        "topic_key": topic_key,
        "next_check_minutes": next_minutes,
        "condition_snapshot": condition_snapshot,
        "opportunity_id": _clip(decision.get("opportunity_id"), 80),
        "topic_candidate_id": _clip(decision.get("topic_candidate_id"), 80),
        "why_now": _clip(decision.get("why_now"), 800),
        "approach": _clip(decision.get("approach"), 24),
        "meme_intent": _clip(decision.get("meme_intent") or "none", 16),
        "evidence_snapshot": decision.get("evidence_snapshot") or {},
    }
    request_hash = hashlib.sha256(
        json.dumps(
            decision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    idempotency_key = ""
    if gate8_enabled:
        key_seed = (
            f"{assistant_id}|{policy['user_id']}|{policy_kind}|"
            f"{policy_version}|{condition_snapshot['scheduled_for']}"
        )
        idempotency_key = _clip(
            decision.get("idempotency_key")
            or hashlib.sha256(key_seed.encode("utf-8")).hexdigest(),
            160,
        )
        existing_event = conn.execute(
            "SELECT * FROM proactive_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing_event:
            existing = dict(existing_event)
            if str(existing.get("request_hash") or "") != request_hash:
                raise ValueError("proactive_decision_idempotency_conflict")
            return existing
    event_id = uuid.uuid4().hex
    if action == "send" and topic_key:
        cooldown_since = timestamp(current - timedelta(minutes=max(30, int(policy.get("topic_cooldown_minutes") or 1440))))
        repeated = conn.execute(
            """SELECT 1 FROM proactive_events WHERE user_id=? AND topic_key=?
               AND action='send' AND decision_at>=? LIMIT 1""",
            (policy["user_id"], topic_key, cooldown_since),
        ).fetchone()
        if repeated:
            action, intent, reason, message, topic_key = "skip", "silence", "topic_cooldown", "", ""
    jitter = max(0, min(int(policy.get("schedule_jitter_minutes") or 0), 180))
    if jitter:
        seed = int(hashlib.sha256(event_id.encode("ascii")).hexdigest()[:8], 16)
        next_minutes = max(15, next_minutes + (seed % (jitter * 2 + 1)) - jitter)
    event_record = {
        "id": event_id, "user_id": policy["user_id"], "action": action,
        "reason": reason, "message": message, "topic_key": topic_key, "intent": intent,
        "scheduled_for": policy.get("next_check_at") or timestamp(current),
        "decision_at": timestamp(current),
    }
    if gate8_columns:
        event_record.update({
            "assistant_id": assistant_id, "policy_kind": policy_kind,
            "policy_version": policy_version, "trigger_reason": trigger_reason,
            "condition_snapshot_json": json.dumps(condition_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "idempotency_key": idempotency_key, "request_hash": request_hash,
        "blocked_reason": reason if action != "send" else "",
        })
    if {"opportunity_id", "topic_candidate_id", "why_now", "approach", "meme_intent", "evidence_snapshot_json"}.issubset(event_columns):
        event_record.update({
            "opportunity_id": decision_payload["opportunity_id"],
            "topic_candidate_id": decision_payload["topic_candidate_id"],
            "why_now": decision_payload["why_now"], "approach": decision_payload["approach"],
            "meme_intent": decision_payload["meme_intent"],
            "evidence_snapshot_json": json.dumps(decision_payload["evidence_snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        })
    columns = tuple(event_record)
    conn.execute(
        f"INSERT INTO proactive_events({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(event_record[column] for column in columns),
    )
    state = (
        "delivery_pending"
        if action == "send"
        else ("review_pending" if action == "review" else ("draft_pending" if action == "draft" else "scheduled"))
    )
    conn.execute(
        """UPDATE proactive_policies SET state=?, state_reason=?, next_check_at=?,
                  lease_until='', decision_count=decision_count+1,
                  skip_count=skip_count+?, updated_at=? WHERE user_id=?""",
        (state, reason, timestamp(current + timedelta(minutes=next_minutes)), 1 if action == "skip" else 0, timestamp(current), policy["user_id"]),
    )
    saved = dict(conn.execute("SELECT * FROM proactive_events WHERE id=?", (event_id,)).fetchone())
    if action == "send" and stage_proactive_delivery(conn, policy, event_id, message):
        saved["action_staged"] = True
    return saved


def record_proactive_failure(conn: sqlite3.Connection, policy: dict, error: str, *, now: datetime | None = None) -> None:
    current = (now or utc_now()).astimezone(timezone.utc)
    _defer_policy(conn, policy["user_id"], "retry_wait", _clip(error, 300), current + timedelta(minutes=15), current)
    conn.execute(
        """UPDATE proactive_events SET error=? WHERE id=(
               SELECT id FROM proactive_events WHERE user_id=? AND action='send'
                 AND delivery_id='' AND error='' ORDER BY decision_at DESC LIMIT 1
           )""",
        (_clip(error, 1000), policy["user_id"]),
    )
    conn.execute(
        "UPDATE proactive_policies SET failed_count=failed_count+1 WHERE user_id=?",
        (policy["user_id"],),
    )


def attach_proactive_delivery(conn: sqlite3.Connection, event_id: str, delivery_id: str) -> dict | None:
    ensure_automation_tables(conn)
    conn.execute(
        "UPDATE proactive_events SET delivery_id=? WHERE id=?",
        (_clip(delivery_id, 80), _clip(event_id, 80)),
    )
    return _row(conn.execute("SELECT * FROM proactive_events WHERE id=?", (_clip(event_id, 80),)).fetchone())


def mark_proactive_delivery(conn: sqlite3.Connection, delivery_id: str, *, error: str = "", now: datetime | None = None) -> dict | None:
    ensure_automation_tables(conn)
    from bridge_proactive_feedback import mark_delivery

    return mark_delivery(conn, delivery_id, error=error, now=now)


def note_user_activity(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> dict | None:
    ensure_automation_tables(conn)
    from bridge_proactive_feedback import note_activity

    return note_activity(conn, user_id, now=now)


def seconds_until_next_event(conn: sqlite3.Connection, *, now: datetime | None = None, maximum: float = 60.0) -> float:
    ensure_automation_tables(conn)
    current = (now or utc_now()).astimezone(timezone.utc)
    rows = conn.execute(
        """SELECT next_due_at AS due FROM automation_jobs WHERE enabled=1 AND next_due_at<>''
           UNION ALL
           SELECT next_check_at AS due FROM proactive_policies
           WHERE enabled=1 AND authorized=1 AND next_check_at<>''"""
    ).fetchall()
    delays = []
    for row in rows:
        due = parse_datetime(row["due"])
        if due:
            delays.append(max(0.0, (due - current).total_seconds()))
    return min([max(0.25, float(maximum)), *delays]) if delays else max(0.25, float(maximum))


__all__ = [
    "attach_proactive_delivery",
    "calculate_next_due",
    "claim_due_jobs",
    "claim_due_proactive_policies",
    "ensure_automation_tables",
    "finish_automation_run",
    "list_automation_jobs",
    "list_automation_runs",
    "list_automation_seen_items",
    "list_proactive_events",
    "list_proactive_policies",
    "mark_proactive_delivery",
    "note_user_activity",
    "record_proactive_decision",
    "record_proactive_failure",
    "reconcile_group_proactive_policies",
    "reconcile_owner_proactive_policy",
    "reserve_automation_items",
    "seconds_until_next_event",
    "settle_automation_dispatch",
    "upsert_automation_job",
    "upsert_proactive_policy",
]
