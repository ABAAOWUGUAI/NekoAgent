"""Public-safe regression tests for the 2026-08-08 reliability repair follow-up.

These tests are dependency-free: they import only the public bridge modules
they exercise and never touch a database, channel, model, or private runtime.
They pin the three defects closed by the follow-up so the public-source-gate
actually executes the regression, not just the smoke contract checks:

1. The Knowledge ingestion worker hands ``run_ingestion`` a real connection and
   surfaces connector failures as ``fatal`` instead of silently swallowing them.
2. The Automation business verdict enforces an ``ai-agent`` topic: off-topic
   trending results are blocked before any delivery.
3. A superseded delivery projects as a terminal state, never "pending".
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_knowledge_worker_passes_connection_and_surfaces_fatal_connect_failure() -> None:
    from bridge_knowledge_ingestion_worker import maybe_run_knowledge_ingestion

    def broken_connect():
        raise sqlite3.OperationalError("cannot open database")

    summary = maybe_run_knowledge_ingestion(broken_connect, run_ingestion=lambda conn, config: {"ok": True})
    # A connect failure is fatal, not a pseudo-normal empty run.
    assert summary["fatal"] == "source_listing_failed"
    assert summary["error_kind"] == "knowledge_source_listing_failed"
    assert summary["ran"] == 0
    # The summary is JSON-serialisable for caller persistence.
    assert "source_listing_failed" in summary["summary_json"]


def test_knowledge_worker_surfaces_error_persistence_failure_as_fatal() -> None:
    from bridge_knowledge_ingestion_worker import maybe_run_knowledge_ingestion

    attempts = {"n": 0}

    def flaky_connect():
        attempts["n"] += 1
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        if attempts["n"] >= 2:
            conn.close()
            raise sqlite3.OperationalError("database is locked")
        # First connection must have the metadata table for the listing query.
        conn.executescript(
            """
            CREATE TABLE assistant_knowledge_sources (
                id TEXT PRIMARY KEY, source_type TEXT, root_path TEXT,
                enabled INTEGER, config_revision INTEGER, config_json TEXT
            );
            INSERT INTO assistant_knowledge_sources VALUES (
                'src-1','llm_wiki_export','/tmp/vault',1,1,'{}'
            );
            """
        )
        conn.commit()
        return conn

    def broken_runner(conn, config):
        raise RuntimeError("connector_broken")

    events = []
    summary = maybe_run_knowledge_ingestion(
        flaky_connect,
        run_ingestion=broken_runner,
        log_event=events.append,
    )
    # The runner failure is recorded; persistence failure is fatal + logged.
    assert len(summary["errors"]) == 1
    assert summary["fatal"] == "error_persistence_failed"
    assert summary["error_kind"] == "knowledge_worker_error_persistence_failed"
    assert len(events) == 1
    assert events[0]["stage"] == "error_persistence"


def test_knowledge_worker_per_source_failure_is_visible_not_swallowed() -> None:
    import tempfile
    from bridge_knowledge_ingestion_worker import maybe_run_knowledge_ingestion

    db_path = tempfile.mkstemp(suffix=".sqlite3")[1]

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assistant_knowledge_sources (
                id TEXT PRIMARY KEY, source_type TEXT, root_path TEXT,
                enabled INTEGER, config_revision INTEGER, config_json TEXT
            );
            INSERT OR IGNORE INTO assistant_knowledge_sources VALUES (
                'src-1','llm_wiki_export','/tmp/vault',1,1,'{}'
            );
            CREATE TABLE IF NOT EXISTS assistant_knowledge_ingestion_runs (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, config_revision INTEGER NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '',
                duration_seconds REAL NOT NULL DEFAULT 0, discovered INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
                chunks INTEGER NOT NULL DEFAULT 0, candidates INTEGER NOT NULL DEFAULT 0,
                drafts INTEGER NOT NULL DEFAULT 0, conflicts INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0, stop_reason TEXT NOT NULL DEFAULT '',
                error_kind TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.commit()
        return conn

    def broken_runner(conn, config):
        raise RuntimeError("connector_broken")

    summary = maybe_run_knowledge_ingestion(connect, run_ingestion=broken_runner)
    assert summary["fatal"] == ""
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["source_id"] == "src-1"
    assert "connector_broken" in summary["errors"][0]["error"]
    # The per-source error is persisted as an observable run row.
    with connect() as conn:
        error_rows = conn.execute(
            "SELECT error_kind, stop_reason, failed FROM assistant_knowledge_ingestion_runs"
        ).fetchall()
    assert len(error_rows) == 1
    assert error_rows[0]["stop_reason"] == "worker_error"
    assert error_rows[0]["failed"] == 1


def test_automation_business_verdict_blocks_off_topic_ai_agent_results() -> None:
    from bridge_automation_business_gate import evaluate_automation_business_verdict

    items = [
        {
            "repo": f"owner/cooking-{index}",
            "url": f"https://github.com/owner/cooking-{index}",
            "description": "A recipe collection.",
        }
        for index in range(10)
    ]
    verdict = evaluate_automation_business_verdict(
        capability_id="github.trending.read",
        result={"output": {"items": items}, "evidence": [{"source_id": "github-trending"}]},
        contract_arguments={"limit": 10, "topic": "ai-agent"},
    )
    assert verdict["passed"] is False
    assert verdict["status"] == "blocked"
    assert verdict["error_kind"] == "github_trending_topic_mismatch"


def test_automation_business_verdict_passes_on_topic_ai_agent_results() -> None:
    from bridge_automation_business_gate import evaluate_automation_business_verdict

    items = [
        {
            "repo": f"owner/agent-{index}",
            "url": f"https://github.com/owner/agent-{index}",
            "description": "An AI agent framework.",
        }
        for index in range(10)
    ]
    verdict = evaluate_automation_business_verdict(
        capability_id="github.trending.read",
        result={"output": {"items": items}, "evidence": [{"source_id": "github-trending"}]},
        contract_arguments={"limit": 10, "topic": "ai-agent"},
    )
    assert verdict["passed"] is True
    assert verdict["error_kind"] == ""


def test_superseded_delivery_projects_terminal_not_pending() -> None:
    from bridge_conversation_participation import build_media_delivery_trace

    trace = build_media_delivery_trace(
        engagement_decision_id="dec-1",
        delivery_id="delivery-1",
        media_kind="none",
        media_preflight_state="none",
        visual_context_state="none",
        media_observation_decision="none",
        delivery_state="superseded",
        ack_state="pending",
    )
    assert trace["delivery_state"] == "superseded"
    assert trace["outcome_category"] == "delivery_superseded"
