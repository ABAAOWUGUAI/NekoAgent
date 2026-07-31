"""Safe smoke checks that are included in the public source release."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_bridge_imports_without_runtime_bootstrap() -> None:
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    bridge = importlib.import_module("codex_qq_bridge")

    assert bridge.LISTEN_HOST == "127.0.0.1"
    assert bridge.LISTEN_PORT == 18777


def test_deployment_and_protection_documents_are_present() -> None:
    expected_documents = (
        "README.zh-CN.md",
        "SECURITY.zh-CN.md",
        "docs/DEPLOYMENT.md",
        "docs/OPERATIONS.md",
        "docs/REPOSITORY_PROTECTION.md",
        "docs/zh-CN/ARCHITECTURE.md",
        "docs/zh-CN/INTEGRATIONS.md",
        "docs/zh-CN/DEPLOYMENT.md",
        "docs/zh-CN/OPERATIONS.md",
        "docs/zh-CN/REPOSITORY_PROTECTION.md",
        ".github/CODEOWNERS",
    )

    template_root = ROOT / "open-source-template"
    for relative in expected_documents:
        # The source tree keeps public-facing files in open-source-template;
        # an exported candidate places the same files directly at its root.
        # Check the authoritative location in either layout so the contract is
        # enforced before and after export.
        document = ROOT / relative
        if not document.is_file():
            document = template_root / relative
        assert document.is_file(), f"missing_public_document:{relative}"
        assert document.read_text(encoding="utf-8").strip(), f"empty_public_document:{relative}"


def test_automation_conversation_contract_is_public_and_versioned() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    contracts = importlib.import_module("bridge_automation_contracts")
    interaction = importlib.import_module("bridge_interaction_contract")

    contract = contracts.normalize_output_contract(contracts.DEFAULT_OUTPUT_CONTRACT)
    assert contract["schema_version"] == 1
    assert contract["scope"] == "current_automation_job"
    assert contract["hide_internal_metadata"] is True
    assert len(contracts.output_contract_hash(contract)) == 64
    assert interaction.PLAN_SCHEMA_VERSION == 2
    for relative in (
        "bridge_automation_conversation.py",
        "bridge_automation_conversation_schema.py",
        "bridge_automation_reference_runtime.py",
    ):
        assert (ROOT / relative).is_file(), f"missing_automation_contract_module:{relative}"


def test_automation_followup_actions_are_context_bound_and_fail_closed() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    conversation = importlib.import_module("bridge_automation_conversation")
    action_truth = importlib.import_module("bridge_action_truth")
    action_gate = importlib.import_module("bridge_interaction_action_gate")

    contextual = conversation._initial_plan(
        "立即触发一次呢？我要进行检验",
        [{"role": "assistant", "content": "已修改最近一次匹配的定时任务。"}],
        {},
    )
    unrelated = conversation._initial_plan(
        "触发啊",
        [{"role": "assistant", "content": "图片已经准备好了。"}],
        {},
    )
    assert contextual["actions"][0]["type"] == "automation.schedule.run_now"
    assert unrelated is None
    assert action_truth.has_ungrounded_action_claim(
        "好，我现在就触发它跑一次，结果马上单独发给你看。",
    )
    assert action_gate.planned_automation_action_types(
        {"interaction_plan": {"actions": [{"type": "automation.schedule.run_now"}]}},
    ) == ["automation.schedule.run_now"]
