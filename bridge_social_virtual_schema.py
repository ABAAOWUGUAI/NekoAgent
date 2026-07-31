#!/usr/bin/env python3
"""Assistant Core v24 schema for SocialOpportunity and Virtual Life V1."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from bridge_migrations import MigrationDriftError


SOCIAL_OPPORTUNITY_FEATURE_FLAG = "social_opportunity_v1"
VIRTUAL_LIFE_FEATURE_FLAG = "virtual_life_v1"

TABLE_COLUMNS = {
    "social_opportunities": (
        "id", "assistant_id", "kind", "subject_type", "subject_id",
        "thread_id", "trigger_type", "trigger_ref", "default_action",
        "status", "policy_snapshot_json", "relationship_version",
        "created_at", "expires_at", "decided_at",
    ),
    "social_topic_candidates": (
        "id", "opportunity_id", "source_type", "source_id", "scope_type",
        "scope_id", "summary", "freshness", "why_relevant", "risk",
        "evidence_sha256", "eligible", "created_at",
    ),
    "social_feedback_events": (
        "id", "assistant_id", "opportunity_id", "decision_ref",
        "subject_type", "subject_id", "topic_candidate_id", "approach",
        "signal", "source", "detail_json", "created_at",
    ),
    "virtual_life_profiles": (
        "assistant_id", "enabled", "timezone", "active_start", "active_end",
        "virtual_places_json", "blocked_categories_json", "share_policy",
        "retention_days", "generation_mode", "version", "created_at", "updated_at",
    ),
    "virtual_activity_templates": (
        "id", "assistant_id", "category", "title_template", "description_template",
        "virtual_place", "active_days_json", "window_start", "window_end",
        "weight", "share_level", "enabled", "version", "created_at", "updated_at",
    ),
    "virtual_life_events": (
        "id", "assistant_id", "template_id", "starts_at", "ends_at", "category",
        "title", "description", "virtual_place", "fact_boundary", "share_level",
        "source", "status", "version", "content_sha256", "created_at", "updated_at",
    ),
    "virtual_life_event_audits": (
        "id", "event_id", "assistant_id", "action", "actor_type", "actor_ref",
        "reason", "before_json", "after_json", "created_at",
    ),
}

INDEXES = (
    "idx_social_opportunities_subject",
    "idx_social_opportunities_status",
    "idx_social_topic_candidates_opportunity",
    "idx_social_feedback_subject",
    "idx_virtual_templates_assistant",
    "idx_virtual_events_assistant_time",
    "idx_virtual_event_audits_event",
)

PROACTIVE_EVENT_COLUMNS = {
    "opportunity_id": "TEXT NOT NULL DEFAULT ''",
    "topic_candidate_id": "TEXT NOT NULL DEFAULT ''",
    "why_now": "TEXT NOT NULL DEFAULT ''",
    "approach": "TEXT NOT NULL DEFAULT ''",
    "meme_intent": "TEXT NOT NULL DEFAULT 'none'",
    "evidence_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
    "feedback_state": "TEXT NOT NULL DEFAULT ''",
}


def _contract_payload() -> str:
    return json.dumps(
        {
            "flags": [SOCIAL_OPPORTUNITY_FEATURE_FLAG, VIRTUAL_LIFE_FEATURE_FLAG],
            "tables": {key: list(value) for key, value in TABLE_COLUMNS.items()},
            "indexes": list(INDEXES),
            "proactive_event_columns": PROACTIVE_EVENT_COLUMNS,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


SOCIAL_VIRTUAL_MIGRATION_CHECKSUM = hashlib.sha256(
    _contract_payload().encode("utf-8"),
).hexdigest()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_social_virtual_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE social_opportunities (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            kind TEXT NOT NULL CHECK(kind IN ('reply','join','start')),
            subject_type TEXT NOT NULL CHECK(subject_type IN ('private_user','qq_group','web_admin')),
            subject_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            trigger_type TEXT NOT NULL,
            trigger_ref TEXT NOT NULL DEFAULT '',
            default_action TEXT NOT NULL CHECK(default_action IN ('reply','silent')),
            status TEXT NOT NULL CHECK(status IN ('open','decided','expired','cancelled')),
            policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
            relationship_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_social_opportunities_subject
        ON social_opportunities(assistant_id,subject_type,subject_id,created_at DESC);
        CREATE INDEX idx_social_opportunities_status
        ON social_opportunities(status,expires_at,created_at);

        CREATE TABLE social_topic_candidates (
            id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL REFERENCES social_opportunities(id) ON DELETE RESTRICT,
            source_type TEXT NOT NULL CHECK(source_type IN (
                'inbound_message','follow_up','reminder','project','goal','conversation',
                'memory','knowledge','relationship','virtual_life'
            )),
            source_id TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            freshness TEXT NOT NULL DEFAULT '',
            why_relevant TEXT NOT NULL,
            risk TEXT NOT NULL CHECK(risk IN ('low','medium','high','blocked')),
            evidence_sha256 TEXT NOT NULL,
            eligible INTEGER NOT NULL DEFAULT 1 CHECK(eligible IN (0,1)),
            created_at TEXT NOT NULL,
            UNIQUE(opportunity_id,evidence_sha256)
        );
        CREATE INDEX idx_social_topic_candidates_opportunity
        ON social_topic_candidates(opportunity_id,eligible,created_at);

        CREATE TABLE social_feedback_events (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            opportunity_id TEXT NOT NULL DEFAULT '',
            decision_ref TEXT NOT NULL DEFAULT '',
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            topic_candidate_id TEXT NOT NULL DEFAULT '',
            approach TEXT NOT NULL DEFAULT '',
            signal TEXT NOT NULL CHECK(signal IN (
                'replied','ignored','corrected','muted','delivery_failed','ambiguous'
            )),
            source TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_social_feedback_subject
        ON social_feedback_events(assistant_id,subject_type,subject_id,created_at DESC);

        CREATE TABLE virtual_life_profiles (
            assistant_id TEXT PRIMARY KEY REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            active_start TEXT NOT NULL DEFAULT '08:00',
            active_end TEXT NOT NULL DEFAULT '23:00',
            virtual_places_json TEXT NOT NULL DEFAULT '[]',
            blocked_categories_json TEXT NOT NULL DEFAULT '[]',
            share_policy TEXT NOT NULL DEFAULT 'private_preview_only'
                CHECK(share_policy IN ('private_preview_only','private_reviewable','disabled')),
            retention_days INTEGER NOT NULL DEFAULT 90 CHECK(retention_days BETWEEN 1 AND 3650),
            generation_mode TEXT NOT NULL DEFAULT 'manual_or_daily_visible'
                CHECK(generation_mode IN ('manual_only','manual_or_daily_visible')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE virtual_activity_templates (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            category TEXT NOT NULL,
            title_template TEXT NOT NULL,
            description_template TEXT NOT NULL DEFAULT '',
            virtual_place TEXT NOT NULL DEFAULT '',
            active_days_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
            window_start TEXT NOT NULL DEFAULT '09:00',
            window_end TEXT NOT NULL DEFAULT '22:00',
            weight INTEGER NOT NULL DEFAULT 1 CHECK(weight BETWEEN 1 AND 100),
            share_level TEXT NOT NULL DEFAULT 'private'
                CHECK(share_level IN ('private','reviewable')),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_virtual_templates_assistant
        ON virtual_activity_templates(assistant_id,enabled,category,updated_at DESC);

        CREATE TABLE virtual_life_events (
            id TEXT PRIMARY KEY,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            template_id TEXT NOT NULL DEFAULT '',
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            virtual_place TEXT NOT NULL DEFAULT '',
            fact_boundary TEXT NOT NULL DEFAULT 'virtual' CHECK(fact_boundary='virtual'),
            share_level TEXT NOT NULL DEFAULT 'private'
                CHECK(share_level IN ('private','reviewable')),
            source TEXT NOT NULL CHECK(source IN ('deterministic_generator','admin')),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_virtual_events_assistant_time
        ON virtual_life_events(assistant_id,status,starts_at DESC);

        CREATE TABLE virtual_life_event_audits (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES virtual_life_events(id) ON DELETE RESTRICT,
            assistant_id TEXT NOT NULL REFERENCES assistant_instances(id) ON DELETE RESTRICT,
            action TEXT NOT NULL CHECK(action IN ('create','update','delete','restore')),
            actor_type TEXT NOT NULL CHECK(actor_type IN ('admin','system')),
            actor_ref TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_virtual_event_audits_event
        ON virtual_life_event_audits(event_id,created_at DESC);
        """,
    )
    event_columns = _columns(conn, "proactive_events")
    if not event_columns:
        raise MigrationDriftError("social_virtual_base_table_missing:proactive_events")
    for name, definition in PROACTIVE_EVENT_COLUMNS.items():
        if name not in event_columns:
            conn.execute(f"ALTER TABLE proactive_events ADD COLUMN {name} {definition}")
    now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO assistant_feature_flags(name,enabled,updated_at) VALUES(?,0,?)",
        ((SOCIAL_OPPORTUNITY_FEATURE_FLAG, now), (VIRTUAL_LIFE_FEATURE_FLAG, now)),
    )


def require_social_virtual_schema(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_tables = sorted(set(TABLE_COLUMNS) - tables)
    missing_columns = {}
    for table, required in TABLE_COLUMNS.items():
        if table in tables and (missing := sorted(set(required) - _columns(conn, table))):
            missing_columns[table] = missing
    proactive_missing = []
    if "proactive_events" in tables:
        proactive_missing = sorted(set(PROACTIVE_EVENT_COLUMNS) - _columns(conn, "proactive_events"))
    flags = {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM assistant_feature_flags WHERE name IN (?,?)",
            (SOCIAL_OPPORTUNITY_FEATURE_FLAG, VIRTUAL_LIFE_FEATURE_FLAG),
        )
    }
    missing_flags = sorted(
        {SOCIAL_OPPORTUNITY_FEATURE_FLAG, VIRTUAL_LIFE_FEATURE_FLAG} - flags,
    )
    missing_indexes = sorted(set(INDEXES) - indexes)
    if missing_tables or missing_columns or proactive_missing or missing_flags or missing_indexes:
        raise MigrationDriftError(
            "social_virtual_schema_drift:"
            + json.dumps(
                {
                    "tables": missing_tables,
                    "columns": missing_columns,
                    "proactive_events": proactive_missing,
                    "flags": missing_flags,
                    "indexes": missing_indexes,
                },
                sort_keys=True,
            ),
        )
    return {"ok": True, "contract_checksum": SOCIAL_VIRTUAL_MIGRATION_CHECKSUM}


__all__ = [
    "SOCIAL_OPPORTUNITY_FEATURE_FLAG",
    "SOCIAL_VIRTUAL_MIGRATION_CHECKSUM",
    "VIRTUAL_LIFE_FEATURE_FLAG",
    "apply_social_virtual_v1",
    "require_social_virtual_schema",
]
