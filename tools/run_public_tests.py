#!/usr/bin/env python3
"""Run the public release's dependency-free contract tests.

The source release intentionally keeps its verification path independent from
an unpinned test framework.  This runner is used by GitHub Actions and by the
independent-clone Gate.  It never starts a service, creates runtime state, or
contacts a Provider.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"test_module_load_failed:{relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compile_public_python() -> int:
    compiled = 0
    candidates = list(ROOT.glob("*.py"))
    for directory_name in ("admin", "remote-plugin", "tests"):
        directory = ROOT / directory_name
        if directory.is_dir():
            candidates.extend(directory.rglob("*.py"))
    for relative in ("tools/install_starter_pack.py", "tools/run_public_tests.py"):
        path = ROOT / relative
        if path.is_file():
            candidates.append(path)
    for path in sorted(set(candidates)):
        if "__pycache__" in path.parts:
            continue
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        compiled += 1
    return compiled


def main() -> int:
    try:
        starter_pack_tests = _load("public_starter_pack_tests", "tests/test_starter_pack.py")
        smoke_tests = _load("public_release_smoke_tests", "tests/test_public_release_smoke.py")
        starter_pack_tests.test_xiaofei_pack_dry_run_has_no_runtime_state()
        smoke_tests.test_bridge_imports_without_runtime_bootstrap()
        smoke_tests.test_deployment_and_protection_documents_are_present()
        smoke_tests.test_automation_conversation_contract_is_public_and_versioned()
        smoke_tests.test_automation_followup_actions_are_context_bound_and_fail_closed()
        smoke_tests.test_owner_private_voice_input_is_local_bounded_and_opt_in()
        smoke_tests.test_continuity_terminal_outcomes_settle_plan_and_empty_skill_state()
        smoke_tests.test_owner_private_voice_output_is_policy_owned_and_opt_in()
        tests = 8
        reliability_test_path = ROOT / "tests" / "test_reliability_regression_public.py"
        if reliability_test_path.is_file():
            reliability_tests = _load("public_reliability_regression_tests", "tests/test_reliability_regression_public.py")
            reliability_tests.test_knowledge_worker_passes_connection_and_surfaces_fatal_connect_failure()
            reliability_tests.test_knowledge_worker_surfaces_error_persistence_failure_as_fatal()
            reliability_tests.test_knowledge_worker_per_source_failure_is_visible_not_swallowed()
            reliability_tests.test_automation_business_verdict_blocks_off_topic_ai_agent_results()
            reliability_tests.test_automation_business_verdict_passes_on_topic_ai_agent_results()
            reliability_tests.test_superseded_delivery_projects_terminal_not_pending()
            reliability_tests.test_bound_github_empty_topic_contract_is_enriched_to_ai_agent()
            reliability_tests.test_group_media_claim_without_observation_is_blocked()
            reliability_tests.test_serious_reply_meow_defaults_to_overuse()
            reliability_tests.test_meow_cadence_budget_blocks_over_35_percent()
            reliability_tests.test_retrieval_keyword_fallback_is_bounded_and_compound()
            reliability_tests.test_executor_startup_log_is_never_a_final_body()
            reliability_tests.test_executor_verification_hash_never_contains_secret()
            reliability_tests.test_executor_verify_cli_explicitly_disables_all_network_paths()
            reliability_tests.test_existing_failed_executor_binding_resave_is_an_explicit_no_op()
            reliability_tests.test_executor_verify_route_and_truthful_status_are_publicly_pinned()
            reliability_tests.test_executor_configuration_action_opens_and_prefills_connection_editor()
            reliability_tests.test_executor_display_label_tracks_the_configured_upstream_model()
            reliability_tests.test_executor_v39_schema_can_be_validated_before_v40_column_exists()
            tests += 19
        exporter_test_path = ROOT / "tests" / "test_open_source_release_export.py"
        if exporter_test_path.is_file():
            export_tests = _load("public_export_tests", "tests/test_open_source_release_export.py")
            export_tests.test_sanitizer_removes_private_instance_and_pet_identifiers()
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_accepts_exported_dotfiles_and_examples(Path(directory))
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_allows_declared_public_starter_pack_assets(Path(directory))
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_does_not_allow_private_identity_outside_documented_pack_paths(Path(directory))
            export_tests.test_source_allowlist_includes_starter_pack_installer()
            export_tests.test_required_public_files_include_deployment_and_protection_docs()
            export_tests.test_sanitizer_removes_private_voice_runtime_defaults()
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_requires_license_ci_and_public_tests(Path(directory))
            tests += 8
        print(json.dumps({"ok": True, "compiled_python_files": _compile_public_python(), "tests": tests}))
        return 0
    except Exception as exc:  # The runner must surface the exact Gate failure.
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
