#!/usr/bin/env python3
"""Configurable, explicitly virtual and auditable Assistant life events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bridge_social_virtual_schema import VIRTUAL_LIFE_FEATURE_FLAG


SHARE_POLICIES = {"private_preview_only", "private_reviewable", "disabled"}
GENERATION_MODES = {"manual_only", "manual_or_daily_visible"}
SHARE_LEVELS = {"private", "reviewable"}
FORBIDDEN_REALITY_MARKERS = ("现实中", "亲眼", "真人", "现实世界", "真实住址", "身份证")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _virtual_text(value: object, limit: int, *, required: bool = False, field: str = "text") -> str:
    text = _clip(value, limit)
    if required and not text:
        raise ValueError(f"virtual_life_{field}_required")
    if any(marker in text for marker in FORBIDDEN_REALITY_MARKERS):
        raise ValueError("virtual_life_reality_boundary_violation")
    return text


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: object, fallback):
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _zone(name: object):
    value = _clip(name or "Asia/Shanghai", 80)
    try:
        return ZoneInfo(value)
    except Exception as exc:
        if value == "Asia/Shanghai":
            return timezone(timedelta(hours=8), value)
        if value in {"UTC", "Etc/UTC"}:
            return timezone.utc
        raise ValueError("virtual_life_timezone_invalid") from exc


def _clock(value: object, default: str) -> str:
    result = _clip(value or default, 5)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result):
        raise ValueError("virtual_life_time_invalid")
    return result


def _active_assistant_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM assistant_instances WHERE status='active' ORDER BY updated_at DESC,id LIMIT 1",
    ).fetchone()
    if not row:
        raise ValueError("active_assistant_missing")
    return str(row[0])


def _feature_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT enabled FROM assistant_feature_flags WHERE name=?", (VIRTUAL_LIFE_FEATURE_FLAG,),
    ).fetchone()
    return bool(row and int(row[0] or 0))


def set_virtual_life_feature(conn: sqlite3.Connection, enabled: bool) -> dict:
    now = utc_now()
    conn.execute(
        "UPDATE assistant_feature_flags SET enabled=?,updated_at=? WHERE name=?",
        (1 if enabled else 0, now, VIRTUAL_LIFE_FEATURE_FLAG),
    )
    if not conn.execute("SELECT changes()").fetchone()[0]:
        raise ValueError("virtual_life_schema_unavailable")
    return {"feature": VIRTUAL_LIFE_FEATURE_FLAG, "enabled": bool(enabled), "updated_at": now}


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _idempotent_get(conn: sqlite3.Connection, action: str, key: str, fingerprint: str) -> dict | None:
    key = _clip(key, 160)
    if not key:
        raise ValueError("idempotency_key_required")
    row = conn.execute(
        "SELECT request_hash,response_json FROM assistant_idempotency_records WHERE action=? AND idempotency_key=?",
        (action, key),
    ).fetchone()
    if not row:
        return None
    if str(row[0]) != fingerprint:
        raise ValueError("idempotency_key_payload_conflict")
    result = _load(row[1], None)
    if not isinstance(result, dict):
        raise ValueError("idempotency_record_corrupt")
    return result


def _idempotent_save(conn: sqlite3.Connection, action: str, key: str, fingerprint: str, result: dict) -> None:
    conn.execute(
        """INSERT INTO assistant_idempotency_records(
            action,idempotency_key,request_hash,response_json,created_at
        ) VALUES(?,?,?,?,?)""",
        (action, _clip(key, 160), fingerprint, _json(result), utc_now()),
    )


def _profile_defaults(assistant_id: str) -> dict:
    return {
        "assistant_id": assistant_id, "enabled": 0, "timezone": "Asia/Shanghai",
        "active_start": "08:00", "active_end": "23:00", "virtual_places": [],
        "blocked_categories": [], "share_policy": "private_preview_only",
        "retention_days": 90, "generation_mode": "manual_or_daily_visible",
        "version": 0, "created_at": "", "updated_at": "",
    }


def _present_profile(row: sqlite3.Row | dict | None, assistant_id: str) -> dict:
    if not row:
        return _profile_defaults(assistant_id)
    item = dict(row)
    item["virtual_places"] = _load(item.pop("virtual_places_json"), [])
    item["blocked_categories"] = _load(item.pop("blocked_categories_json"), [])
    item["enabled"] = bool(item["enabled"])
    return item


def get_profile(conn: sqlite3.Connection) -> dict:
    assistant_id = _active_assistant_id(conn)
    row = conn.execute("SELECT * FROM virtual_life_profiles WHERE assistant_id=?", (assistant_id,)).fetchone()
    return {"feature_enabled": _feature_enabled(conn), "profile": _present_profile(row, assistant_id)}


def update_profile(conn: sqlite3.Connection, payload: dict, *, idempotency_key: str) -> dict:
    assistant_id = _active_assistant_id(conn)
    current_row = conn.execute("SELECT * FROM virtual_life_profiles WHERE assistant_id=?", (assistant_id,)).fetchone()
    current = _present_profile(current_row, assistant_id)
    try:
        expected = int(payload.get("expected_version", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_version_required") from exc
    if expected != int(current["version"]):
        raise ValueError("stale_virtual_life_profile_version")
    places = [_virtual_text(item, 80) for item in payload.get("virtual_places", current["virtual_places"]) if _clip(item, 80)]
    blocked = [_clip(item, 80) for item in payload.get("blocked_categories", current["blocked_categories"]) if _clip(item, 80)]
    share_policy = _clip(payload.get("share_policy", current["share_policy"]), 40)
    generation_mode = _clip(payload.get("generation_mode", current["generation_mode"]), 40)
    if share_policy not in SHARE_POLICIES or generation_mode not in GENERATION_MODES:
        raise ValueError("virtual_life_profile_policy_invalid")
    normalized = {
        "assistant_id": assistant_id, "enabled": bool(payload.get("enabled", current["enabled"])),
        "timezone": _clip(payload.get("timezone", current["timezone"]), 80),
        "active_start": _clock(payload.get("active_start", current["active_start"]), "08:00"),
        "active_end": _clock(payload.get("active_end", current["active_end"]), "23:00"),
        "virtual_places": sorted(set(places)), "blocked_categories": sorted(set(blocked)),
        "share_policy": share_policy,
        "retention_days": max(1, min(int(payload.get("retention_days", current["retention_days"])), 3650)),
        "generation_mode": generation_mode, "expected_version": expected,
    }
    _zone(normalized["timezone"])
    fingerprint = _request_hash(normalized)
    if cached := _idempotent_get(conn, "virtual_life_profile", idempotency_key, fingerprint):
        return cached
    now, version = utc_now(), expected + 1
    conn.execute(
        """INSERT INTO virtual_life_profiles(
            assistant_id,enabled,timezone,active_start,active_end,virtual_places_json,
            blocked_categories_json,share_policy,retention_days,generation_mode,version,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(assistant_id) DO UPDATE SET
            enabled=excluded.enabled,timezone=excluded.timezone,active_start=excluded.active_start,
            active_end=excluded.active_end,virtual_places_json=excluded.virtual_places_json,
            blocked_categories_json=excluded.blocked_categories_json,share_policy=excluded.share_policy,
            retention_days=excluded.retention_days,generation_mode=excluded.generation_mode,
            version=excluded.version,updated_at=excluded.updated_at""",
        (
            assistant_id, 1 if normalized["enabled"] else 0, normalized["timezone"],
            normalized["active_start"], normalized["active_end"], _json(normalized["virtual_places"]),
            _json(normalized["blocked_categories"]), share_policy, normalized["retention_days"],
            generation_mode, version, now, now,
        ),
    )
    result = get_profile(conn)
    _idempotent_save(conn, "virtual_life_profile", idempotency_key, fingerprint, result)
    return result


def _validate_template_text(value: object, field: str, limit: int) -> str:
    return _virtual_text(value, limit, required=True, field=field)


def _present_template(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    item["active_days"] = _load(item.pop("active_days_json"), [])
    item["enabled"] = bool(item["enabled"])
    return item


def list_templates(conn: sqlite3.Connection) -> list[dict]:
    assistant_id = _active_assistant_id(conn)
    rows = conn.execute(
        "SELECT * FROM virtual_activity_templates WHERE assistant_id=? ORDER BY category,title_template,id",
        (assistant_id,),
    ).fetchall()
    return [_present_template(row) for row in rows]


def upsert_template(conn: sqlite3.Connection, payload: dict, *, idempotency_key: str) -> dict:
    assistant_id = _active_assistant_id(conn)
    item_id = _clip(payload.get("id"), 80) or uuid.uuid4().hex
    row = conn.execute(
        "SELECT * FROM virtual_activity_templates WHERE id=? AND assistant_id=?", (item_id, assistant_id),
    ).fetchone()
    current = _present_template(row) if row else {"version": 0, "active_days": list(range(7)), "enabled": True}
    expected = int(payload.get("expected_version", -1))
    if expected != int(current["version"]):
        raise ValueError("stale_virtual_life_template_version")
    days = sorted({int(day) for day in payload.get("active_days", current["active_days"])})
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("virtual_life_active_days_invalid")
    share_level = _clip(payload.get("share_level", current.get("share_level") or "private"), 20)
    if share_level not in SHARE_LEVELS:
        raise ValueError("virtual_life_share_level_invalid")
    normalized = {
        "id": item_id, "assistant_id": assistant_id,
        "category": _validate_template_text(payload.get("category", current.get("category")), "category", 80),
        "title_template": _validate_template_text(payload.get("title_template", current.get("title_template")), "title", 200),
        "description_template": _virtual_text(payload.get("description_template", current.get("description_template")), 1000),
        "virtual_place": _virtual_text(payload.get("virtual_place", current.get("virtual_place")), 120),
        "active_days": days,
        "window_start": _clock(payload.get("window_start", current.get("window_start")), "09:00"),
        "window_end": _clock(payload.get("window_end", current.get("window_end")), "22:00"),
        "weight": max(1, min(int(payload.get("weight", current.get("weight") or 1)), 100)),
        "share_level": share_level, "enabled": bool(payload.get("enabled", current["enabled"])),
        "expected_version": expected,
    }
    fingerprint = _request_hash(normalized)
    if cached := _idempotent_get(conn, "virtual_life_template", idempotency_key, fingerprint):
        return cached
    now, version = utc_now(), expected + 1
    conn.execute(
        """INSERT INTO virtual_activity_templates(
            id,assistant_id,category,title_template,description_template,virtual_place,
            active_days_json,window_start,window_end,weight,share_level,enabled,version,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET category=excluded.category,title_template=excluded.title_template,
            description_template=excluded.description_template,virtual_place=excluded.virtual_place,
            active_days_json=excluded.active_days_json,window_start=excluded.window_start,
            window_end=excluded.window_end,weight=excluded.weight,share_level=excluded.share_level,
            enabled=excluded.enabled,version=excluded.version,updated_at=excluded.updated_at""",
        (
            item_id, assistant_id, normalized["category"], normalized["title_template"],
            normalized["description_template"], normalized["virtual_place"], _json(days),
            normalized["window_start"], normalized["window_end"], normalized["weight"], share_level,
            1 if normalized["enabled"] else 0, version, now, now,
        ),
    )
    result = {"template": _present_template(conn.execute("SELECT * FROM virtual_activity_templates WHERE id=?", (item_id,)).fetchone())}
    _idempotent_save(conn, "virtual_life_template", idempotency_key, fingerprint, result)
    return result


def _present_event(row: sqlite3.Row | dict) -> dict:
    return dict(row)


def list_events(conn: sqlite3.Connection, *, include_deleted: bool = False, limit: int = 100) -> list[dict]:
    assistant_id = _active_assistant_id(conn)
    where = "" if include_deleted else "AND status='active'"
    rows = conn.execute(
        f"SELECT * FROM virtual_life_events WHERE assistant_id=? {where} ORDER BY starts_at DESC LIMIT ?",
        (assistant_id, max(1, min(int(limit or 100), 300))),
    ).fetchall()
    return [_present_event(row) for row in rows]


def list_event_audits(conn: sqlite3.Connection, *, event_id: str = "", limit: int = 100) -> list[dict]:
    assistant_id = _active_assistant_id(conn)
    if event_id:
        rows = conn.execute(
            "SELECT * FROM virtual_life_event_audits WHERE assistant_id=? AND event_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (assistant_id, _clip(event_id, 80), max(1, min(int(limit or 100), 300))),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM virtual_life_event_audits WHERE assistant_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (assistant_id, max(1, min(int(limit or 100), 300))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["before"] = _load(item.pop("before_json"), {})
        item["after"] = _load(item.pop("after_json"), {})
        result.append(item)
    return result


def _audit(conn, event: dict, action: str, before: dict, after: dict, actor_type: str, actor_ref: str, reason: str) -> None:
    conn.execute(
        """INSERT INTO virtual_life_event_audits(
            id,event_id,assistant_id,action,actor_type,actor_ref,reason,before_json,after_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (uuid.uuid4().hex, event["id"], event["assistant_id"], action, actor_type,
         _clip(actor_ref, 120), _clip(reason, 500), _json(before), _json(after), utc_now()),
    )


def generate_events(conn: sqlite3.Connection, payload: dict, *, idempotency_key: str) -> dict:
    if not _feature_enabled(conn):
        raise ValueError("virtual_life_feature_disabled")
    assistant_id = _active_assistant_id(conn)
    profile_result = get_profile(conn)
    profile = profile_result["profile"]
    if not profile["enabled"] or profile["share_policy"] == "disabled":
        raise ValueError("virtual_life_profile_disabled")
    zone = _zone(profile["timezone"])
    try:
        target = date.fromisoformat(_clip(payload.get("date"), 10)) if payload.get("date") else datetime.now(zone).date()
    except ValueError as exc:
        raise ValueError("virtual_life_date_invalid") from exc
    templates = [
        item for item in list_templates(conn)
        if item["enabled"] and target.weekday() in item["active_days"]
        and item["category"] not in set(profile["blocked_categories"])
    ]
    if not templates:
        raise ValueError("virtual_life_template_unavailable")
    normalized = {"assistant_id": assistant_id, "date": target.isoformat(), "profile_version": profile["version"]}
    fingerprint = _request_hash(normalized)
    if cached := _idempotent_get(conn, "virtual_life_generate", idempotency_key, fingerprint):
        return cached
    weighted = [item for item in templates for _ in range(item["weight"])]
    seed = hashlib.sha256(f"{assistant_id}|{target}|{profile['version']}|{','.join(item['id']+':'+str(item['version']) for item in templates)}".encode()).digest()
    template = weighted[int.from_bytes(seed[:8], "big") % len(weighted)]
    start_clock = datetime.strptime(template["window_start"], "%H:%M").time()
    end_clock = datetime.strptime(template["window_end"], "%H:%M").time()
    start = datetime.combine(target, start_clock, tzinfo=zone)
    end = datetime.combine(target, end_clock, tzinfo=zone)
    if end <= start:
        end += timedelta(days=1)
    span = max(1, int((end - start).total_seconds() // 60))
    start += timedelta(minutes=int.from_bytes(seed[8:12], "big") % span)
    duration = 30 + int.from_bytes(seed[12:16], "big") % 91
    end = min(start + timedelta(minutes=duration), end)
    place = template["virtual_place"] or (profile["virtual_places"][0] if profile["virtual_places"] else "虚拟居所")
    title = template["title_template"].replace("{place}", place)
    description = template["description_template"].replace("{place}", place)
    content_hash = hashlib.sha256(_json({"title": title, "description": description, "start": start.isoformat()}).encode()).hexdigest()
    existing = conn.execute(
        "SELECT * FROM virtual_life_events WHERE assistant_id=? AND content_sha256=?", (assistant_id, content_hash),
    ).fetchone()
    if existing:
        event = _present_event(existing)
    else:
        event_id, now = uuid.uuid4().hex, utc_now()
        conn.execute(
            """INSERT INTO virtual_life_events(
                id,assistant_id,template_id,starts_at,ends_at,category,title,description,
                virtual_place,fact_boundary,share_level,source,status,version,content_sha256,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'virtual',?,'deterministic_generator','active',1,?,?,?)""",
            (event_id, assistant_id, template["id"], start.astimezone(timezone.utc).isoformat(),
             end.astimezone(timezone.utc).isoformat(), template["category"], title, description, place,
             template["share_level"], content_hash, now, now),
        )
        event = _present_event(conn.execute("SELECT * FROM virtual_life_events WHERE id=?", (event_id,)).fetchone())
        _audit(conn, event, "create", {}, event, "system", "deterministic_generator", "daily_visible_generation")
    result = {"event": event, "delivery_eligible": False, "fact_boundary": "virtual"}
    _idempotent_save(conn, "virtual_life_generate", idempotency_key, fingerprint, result)
    return result


def event_action(conn: sqlite3.Connection, payload: dict, *, idempotency_key: str) -> dict:
    assistant_id = _active_assistant_id(conn)
    event_id, action = _clip(payload.get("event_id"), 80), _clip(payload.get("action"), 20)
    row = conn.execute(
        "SELECT * FROM virtual_life_events WHERE id=? AND assistant_id=?", (event_id, assistant_id),
    ).fetchone()
    if not row:
        raise ValueError("virtual_life_event_not_found")
    before = _present_event(row)
    expected = int(payload.get("expected_version", -1))
    if expected != int(before["version"]):
        raise ValueError("stale_virtual_life_event_version")
    if action not in {"update", "delete", "restore"}:
        raise ValueError("virtual_life_event_action_invalid")
    normalized = {"event_id": event_id, "action": action, "expected_version": expected,
                  "title": _clip(payload.get("title"), 200), "description": _clip(payload.get("description"), 1000),
                  "reason": _clip(payload.get("reason"), 500)}
    fingerprint = _request_hash(normalized)
    if cached := _idempotent_get(conn, "virtual_life_event_action", idempotency_key, fingerprint):
        return cached
    status, title, description = before["status"], before["title"], before["description"]
    if action == "delete":
        status = "deleted"
    elif action == "restore":
        status = "active"
    else:
        title = _validate_template_text(payload.get("title", title), "event_title", 200)
        description = _virtual_text(payload.get("description", description), 1000)
    now, version = utc_now(), expected + 1
    content_hash = hashlib.sha256(_json({"title": title, "description": description, "start": before["starts_at"]}).encode()).hexdigest()
    conn.execute(
        "UPDATE virtual_life_events SET title=?,description=?,status=?,version=?,content_sha256=?,updated_at=? WHERE id=?",
        (title, description, status, version, content_hash, now, event_id),
    )
    after = _present_event(conn.execute("SELECT * FROM virtual_life_events WHERE id=?", (event_id,)).fetchone())
    _audit(conn, after, action, before, after, "admin", "web", normalized["reason"])
    result = {"event": after}
    _idempotent_save(conn, "virtual_life_event_action", idempotency_key, fingerprint, result)
    return result


__all__ = [
    "event_action", "generate_events", "get_profile", "list_event_audits", "list_events",
    "list_templates", "set_virtual_life_feature", "update_profile", "upsert_template",
]
