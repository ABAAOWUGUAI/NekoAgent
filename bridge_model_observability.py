#!/usr/bin/env python3
"""Persist and aggregate provider-neutral model usage facts."""

from __future__ import annotations

import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_model_usage_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_usage_events (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            trace_id TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            provider_id TEXT NOT NULL DEFAULT '',
            provider_kind TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            error_kind TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            usage_reported INTEGER NOT NULL DEFAULT 0,
            duration_seconds REAL,
            estimated_cost REAL,
            currency TEXT NOT NULL DEFAULT 'USD',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_usage_created
        ON model_usage_events(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_model_usage_model
        ON model_usage_events(model_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_model_usage_role
        ON model_usage_events(role, created_at DESC);
        """
    )


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def record_model_usage(
    conn: sqlite3.Connection,
    settings: dict,
    result: dict,
    *,
    source: str,
    user_id: str = "",
    trace_id: str = "",
) -> dict:
    ensure_model_usage_tables(conn)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    input_tokens = _int_or_none(usage.get("input_tokens", usage.get("prompt_tokens")))
    output_tokens = _int_or_none(usage.get("output_tokens", usage.get("completion_tokens")))
    total_tokens = _int_or_none(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    usage_reported = any(value is not None for value in (input_tokens, output_tokens, total_tokens))

    input_price = settings.get("model_input_price_per_million")
    output_price = settings.get("model_output_price_per_million")
    estimated_cost = None
    if input_tokens is not None and output_tokens is not None and input_price is not None and output_price is not None:
        try:
            estimated_cost = round(
                (input_tokens * float(input_price) + output_tokens * float(output_price)) / 1_000_000,
                8,
            )
        except (TypeError, ValueError):
            estimated_cost = None

    event_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO model_usage_events(
               id, source, user_id, trace_id, role, provider_id, provider_kind,
               model_id, model_name, status, error_kind, input_tokens, output_tokens,
               total_tokens, usage_reported, duration_seconds, estimated_cost, currency, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            str(source or "")[:80],
            str(user_id or "")[:80],
            str(trace_id or "")[:120],
            str(settings.get("model_role") or "")[:80],
            str(settings.get("model_registry_provider_id") or "")[:80],
            str(result.get("provider") or settings.get("chat_provider") or "")[:80],
            str(settings.get("model_registry_id") or "")[:80],
            str(result.get("model") or settings.get("chat_model") or settings.get("codex_model") or "")[:200],
            "success" if result.get("ok") else "failed",
            str(result.get("error_kind") or "")[:80],
            input_tokens,
            output_tokens,
            total_tokens,
            1 if usage_reported else 0,
            result.get("duration"),
            estimated_cost,
            str(settings.get("model_price_currency") or "USD")[:12].upper(),
            utc_now(),
        ),
    )
    row = conn.execute("SELECT * FROM model_usage_events WHERE id=?", (event_id,)).fetchone()
    return dict(row)


def usage_report(conn: sqlite3.Connection, *, days: int = 7, limit: int = 50) -> dict:
    ensure_model_usage_tables(conn)
    days = max(1, min(int(days or 7), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM model_usage_events WHERE created_at>=? ORDER BY created_at DESC",
            (since,),
        ).fetchall()
    ]
    durations = sorted(float(row["duration_seconds"]) for row in rows if row.get("duration_seconds") is not None)
    p95 = durations[max(0, math.ceil(len(durations) * 0.95) - 1)] if durations else None
    by_model: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    for row in rows:
        model_key = row.get("model_id") or row.get("model_name") or row.get("provider_kind") or "unknown"
        model = by_model.setdefault(model_key, {"key": model_key, "calls": 0, "success": 0, "input_tokens": 0, "output_tokens": 0, "known_token_calls": 0, "estimated_cost": 0.0, "currency": row.get("currency") or "USD"})
        day_key = str(row.get("created_at") or "")[:10]
        day = by_day.setdefault(day_key, {"date": day_key, "calls": 0, "success": 0, "total_tokens": 0})
        for bucket in (model, day):
            bucket["calls"] += 1
            bucket["success"] += 1 if row.get("status") == "success" else 0
        if row.get("usage_reported"):
            model["known_token_calls"] += 1
        model["input_tokens"] += row.get("input_tokens") or 0
        model["output_tokens"] += row.get("output_tokens") or 0
        model["estimated_cost"] = round(model["estimated_cost"] + (row.get("estimated_cost") or 0), 8)
        day["total_tokens"] += row.get("total_tokens") or 0
    calls = len(rows)
    successes = sum(1 for row in rows if row.get("status") == "success")
    known = sum(1 for row in rows if row.get("usage_reported"))
    return {
        "range_days": days,
        "summary": {
            "calls": calls,
            "success_rate": round(successes / calls * 100, 1) if calls else None,
            "input_tokens": sum(row.get("input_tokens") or 0 for row in rows),
            "output_tokens": sum(row.get("output_tokens") or 0 for row in rows),
            "known_token_calls": known,
            "unknown_token_calls": calls - known,
            "average_duration": round(sum(durations) / len(durations), 3) if durations else None,
            "p95_duration": round(p95, 3) if p95 is not None else None,
            "estimated_cost": round(sum(row.get("estimated_cost") or 0 for row in rows), 8),
        },
        "by_model": sorted(by_model.values(), key=lambda item: (-item["calls"], item["key"])),
        "by_day": [by_day[key] for key in sorted(by_day)],
        "events": rows[: max(1, min(int(limit or 50), 200))],
    }

