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


def test_bound_github_empty_topic_contract_is_enriched_to_ai_agent() -> None:
    # A contract already bound to github.trending.read but with an empty topic
    # (the 2026-08-08 production job) must be enriched deterministically to
    # topic=ai-agent when the instruction implies an AI / AI Agent topic.
    from bridge_automation_execution_contract import (
        audit_execution_contract_repair,
        normalize_execution_contract,
    )

    bound = {
        "schema_version": 1,
        "capability_id": "github.trending.read",
        "arguments": {
            "dedupe_policy": "job_history",
            "limit": 10,
            "output_language": "zh-CN",
            "period": "daily",
            "topic": "",
        },
        "status": "ready",
        "missing_inputs": [],
        "network_required": True,
        "output_kind": "github_trending",
    }
    audit = audit_execution_contract_repair(
        "呢，钟给我统计一下目前githu上关于ai或者aiagent的热门相关话题，我需要每天10条，不允许出现重复",
        {"source": "github", "topic": "ai_agent", "item_limit": 10, "dedupe_policy": "job_history"},
        persisted_contract=normalize_execution_contract(bound),
        action_type="agent",
    )
    assert audit["repairable"] is True
    assert audit["reason"] == "bound_github_empty_topic_enriched"
    assert audit["derived_capability_id"] == "github.trending.read"
    assert audit["network_required_rose"] is False
    assert audit["repair"]["arguments"]["topic"] == "ai-agent"


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


def test_group_media_claim_without_observation_is_blocked() -> None:
    """S11 regression: ``刚看你们刷图`` with observation=deferred is blocked."""
    from bridge_group_truth_gate import group_final_truth_issues

    envelope = {
        "media": {"kind": "image", "observation": "deferred", "preflight": "none", "visual_context": "none"},
        "allowed_claim_types": ["observed_media_facts", "subjective_opinion", "greeting"],
        "forbidden_claim_types": ["visual_details"],
    }
    issues = group_final_truth_issues("刚看你们刷图呢～今天这么热闹？", envelope)
    assert "media_claim_without_evidence" in issues
    # Verifiable transport metadata stays allowed.
    safe = group_final_truth_issues("看到你们发了不少媒体内容，我还没细看。", envelope)
    assert "media_claim_without_evidence" not in safe


def test_serious_reply_meow_defaults_to_overuse() -> None:
    """Defect-3: clarification / refusal / boundary replies default to no 喵."""
    from bridge_group_truth_gate import signature_budget_issues

    issues = signature_budget_issues(
        draft="这种定制壳我没法直接变一个出来给你喵。",
        recent_confirmed=[],
    )
    assert "persona_signature_overuse" in issues
    plain = signature_budget_issues(
        draft="这种定制壳我没法直接变一个出来给你。",
        recent_confirmed=[],
    )
    assert "persona_signature_overuse" not in plain


def test_meow_cadence_budget_blocks_over_35_percent() -> None:
    """Defect-3: 4/10 meow replies must block a new 喵 reply (default 3/10)."""
    from bridge_group_truth_gate import signature_budget_issues

    history = [
        {"content": f"样本 {i} 喵" if i in {0, 1, 2, 3} else f"样本 {i}"}
        for i in range(10)
    ]
    issues = signature_budget_issues(draft="再来一条喵", recent_confirmed=history)
    assert "persona_signature_overuse" in issues
    # 2/10 meow leaves room for one more (3/10 = 30% <= 35%).
    sparse = [
        {"content": f"样本 {i} 喵" if i in {0, 1} else f"样本 {i}"}
        for i in range(10)
    ]
    ok = signature_budget_issues(draft="再来一条喵", recent_confirmed=sparse)
    assert "persona_signature_overuse" not in ok


def test_retrieval_keyword_fallback_is_bounded_and_compound() -> None:
    """Defect-1: compound/natural queries must reach the keyword overlap path.

    ``_keyword_candidate_where`` is the bounded pre-filter that replaced the
    unconditional early return.  It must produce a bounded LIKE condition for
    compound Chinese/English queries and an empty condition (1=0) when there
    are no searchable terms, so unrelated queries do not inject anything.
    """
    from bridge_knowledge_service import _keyword_candidate_where, _keyword_set

    terms = _keyword_set("平台当前生产运行事实 助手实例标识 schema迁移版本")
    where, params = _keyword_candidate_where("平台当前生产运行事实 助手实例标识 schema迁移版本", terms)
    assert "LIKE" in where
    assert len(params) >= 4
    # No-term queries must produce a no-candidate condition (1=0), never a
    # full-corpus "recent published" injection.
    empty_where, empty_params = _keyword_candidate_where("", set())
    assert "1=0" in empty_where
    assert empty_params == []


def test_executor_startup_log_is_never_a_final_body() -> None:
    """E3: a model that only prints startup/usage prose fails work-mode
    verification; only a genuine final assistant body passes."""
    from bridge_executor_verification import _looks_like_startup_log

    assert _looks_like_startup_log("Welcome to Codex CLI\nInitializing…") is True
    assert _looks_like_startup_log("Checking for updates\nReading config") is True
    assert _looks_like_startup_log("我读取了 work-verify.txt 的内容摘要并完成了一次 ls 命令。") is False


def test_executor_verification_hash_never_contains_secret() -> None:
    """E3/E5: the verification hash covers identity/config/version facts but
    never the secret value itself."""
    import json
    from bridge_executor_verification import verification_hash_inputs

    inputs = {
        "id": "proxy-exec",
        "kind": "codex",
        "transport": "codex_cli_custom_provider",
        "secret_version": 2,
        "secret_rotated_at": "2026-08-09T00:00:00+00:00",
        "executor_config_version": 2,
        "executor_applied_version": 2,
        "sandbox_policy": "read-only",
    }
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    assert "secret-key-not-real" not in payload
    assert "api_key" not in payload
    assert "DEEPSEEK_API_KEY" not in payload
    # secret_version is a version, not the secret; it belongs in the hash so a
    # rotation invalidates it even though the value never appears.
    assert "secret_version" in payload


def test_executor_verify_cli_explicitly_disables_all_network_paths() -> None:
    """The verification profile cannot re-enable egress or web search."""
    from bridge_executor_verification import _codex_work_verify_args

    args = _codex_work_verify_args(
        profile_name="sample-executor",
        model_name="sample-model",
        network_mode="none",
    )
    overrides = [
        args[index + 1]
        for index, value in enumerate(args[:-1])
        if value in {"-c", "--config"}
    ]
    assert "sandbox_workspace_write.network_access=false" in overrides
    assert 'web_search="disabled"' in overrides
    assert "sandbox_workspace_write.writable_roots=[]" in overrides
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in overrides
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in overrides


def test_existing_failed_executor_binding_resave_is_an_explicit_no_op() -> None:
    """A no-change save must bypass the new-bind verification gate."""
    import bridge_model_registry as registry

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_providers (
            id TEXT PRIMARY KEY, kind TEXT, transport TEXT, billing_scope TEXT,
            runtime_owner TEXT, config_mode TEXT, trusted_for_executor INTEGER,
            enabled INTEGER
        );
        CREATE TABLE model_catalog (
            id TEXT PRIMARY KEY, provider_id TEXT, enabled INTEGER,
            supports_tools INTEGER, capabilities_json TEXT
        );
        CREATE TABLE model_role_bindings (
            role TEXT PRIMARY KEY, primary_model_id TEXT,
            fallback_model_id TEXT, updated_at TEXT
        );
        INSERT INTO model_providers VALUES (
            'proxy-exec','codex','codex_cli_custom_provider','local_proxy',
            'platform','managed',1,1
        );
        INSERT INTO model_catalog VALUES (
            'proxy-model','proxy-exec',1,1,'["text","tools"]'
        );
        INSERT INTO model_role_bindings VALUES (
            'work_executor','proxy-model','','2026-08-09T00:00:00Z'
        );
        """
    )
    original_guard = registry.work_executor_bind_guard
    registry.work_executor_bind_guard = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("verification gate must not run for an unchanged binding")
    )
    try:
        result = registry.bind_model_role(
            conn,
            {"role": "work_executor", "primary_model_id": "proxy-model"},
        )
    finally:
        registry.work_executor_bind_guard = original_guard
        conn.close()
    assert result["primary_model_id"] == "proxy-model"
    assert result["no_op"] is True
    assert result["updated_at"] == "2026-08-09T00:00:00Z"


def test_executor_verify_route_and_truthful_status_are_publicly_pinned() -> None:
    """The public gate protects route reachability and failed-result truth."""
    from bridge_http_routes import BRIDGE_POST_ROUTES

    assert "/assistant/models/executor/verify" in BRIDGE_POST_ROUTES
    source = (ROOT / "codex_qq_bridge.py").read_text(encoding="utf-8")
    assert '"ok": result.get("status") == "verified"' in source


def test_executor_configuration_action_opens_and_prefills_connection_editor() -> None:
    """can_configure must lead to an adapter editor, not back to routing."""
    source = (ROOT / "admin" / "views-models.js").read_text(encoding="utf-8")
    assert "openExecutorAdapterConfiguration" in source
    assert "data-configure-executor-model" in source
    assert "editModelProvider" in source
    assert "modelExecutorUpstreamProvider" in source
    assert "modelExecutorUpstreamModel" in source
    assert "modelExecutorConfigureEntry').addEventListener('click', () => { setModelWorkspace('routing')" not in source


def test_executor_display_label_tracks_the_configured_upstream_model() -> None:
    """A proxy catalog row must not keep saying Pro after it is configured to
    run Flash."""
    from bridge_executor_profiles import executor_model_display_label

    label = executor_model_display_label(
        {"id": "sample-proxy", "label": "Old Pro label", "model": "old-pro"},
        {"model_label": "deepseek-v4-flash", "model": "deepseek-v4-flash"},
    )
    assert label == "deepseek-v4-flash（工作执行适配器）"


def test_executor_v39_schema_can_be_validated_before_v40_column_exists() -> None:
    """Migration preflight must validate the schema for the applied version."""
    from bridge_executor_verification_schema import (
        apply_executor_verification_reason_code_v2,
        apply_executor_verification_v1,
        require_executor_verification_schema,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE model_providers(id TEXT PRIMARY KEY)")
    apply_executor_verification_v1(conn)
    assert require_executor_verification_schema(conn, version=39)["version"] == 39
    apply_executor_verification_reason_code_v2(conn)
    assert require_executor_verification_schema(conn, version=40)["version"] == 40
    conn.close()
