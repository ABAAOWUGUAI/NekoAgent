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
        "docs/VOICE_INPUT.md",
        "docs/VOICE_OUTPUT.md",
        "docs/zh-CN/ARCHITECTURE.md",
        "docs/zh-CN/INTEGRATIONS.md",
        "docs/zh-CN/DEPLOYMENT.md",
        "docs/zh-CN/OPERATIONS.md",
        "docs/zh-CN/REPOSITORY_PROTECTION.md",
        "docs/zh-CN/VOICE_INPUT.md",
        "docs/zh-CN/VOICE_OUTPUT.md",
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


def test_owner_private_voice_input_is_local_bounded_and_opt_in() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    schema = importlib.import_module("bridge_voice_message_schema")
    source = importlib.import_module("bridge_voice_message_source")

    assert schema.VOICE_INPUT_FEATURE_FLAG == "voice_input_v1"
    assert source.MAX_QQ_VOICE_BYTES == 10 * 1024 * 1024
    validated = source.validate_qq_private_record_source(
        {
            "schema_version": 1,
            "source_kind": "llbot_onebot_record",
            "channel_type": "qq",
            "scope_type": "private",
            "external_message_id": "public-smoke-message",
            "attachment_index": 0,
            "file_handle_sha256": "a" * 64,
            "transport_url": "https://media.example.test/voice.amr?signature=ephemeral",
        },
        allowed_host_suffixes=("example.test",),
    )
    receipt = source.qq_record_receipt_metadata(validated)
    assert validated["transport_url"].startswith("https://")
    assert "transport_url" not in receipt
    for relative in (
        "bridge_voice_dispatch.py",
        "bridge_voice_input.py",
        "bridge_voice_input_http.py",
        "bridge_voice_input_runtime.py",
        "bridge_voice_media_fetch.py",
        "remote-plugin/voice_input_fetch.py",
        "remote-plugin/voice_message_source.py",
        "tools/set_voice_input.py",
        "docs/VOICE_INPUT.md",
        "docs/zh-CN/VOICE_INPUT.md",
    ):
        path = ROOT / relative
        if not path.is_file():
            path = ROOT / "open-source-template" / relative
        assert path.is_file(), f"missing_voice_input_file:{relative}"


def test_continuity_terminal_outcomes_settle_plan_and_empty_skill_state() -> None:
    kernel_source = (ROOT / "bridge_continuity_kernel.py").read_text(encoding="utf-8")
    outcome_source = (ROOT / "bridge_continuity_outcomes.py").read_text(encoding="utf-8")
    reconciliation_source = (ROOT / "bridge_continuity_reconciliation.py").read_text(encoding="utf-8")

    assert 'else "not_applied"' in kernel_source
    assert '"succeeded": "completed"' in outcome_source
    assert "_settle_interaction_plan" in outcome_source
    assert "projection_reconciled" in reconciliation_source


def test_owner_private_voice_output_is_policy_owned_and_opt_in() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    output_schema = importlib.import_module("bridge_voice_output_schema")
    artifact_service = importlib.import_module("bridge_artifact_service")
    output = importlib.import_module("bridge_voice_output")
    policy = importlib.import_module("bridge_voice_response_policy")
    tts = importlib.import_module("bridge_voice_tts")

    assert output_schema.VOICE_OUTPUT_FEATURE_FLAG == "voice_output_v1"
    assert output_schema.VOICE_DELIVERY_FEATURE_FLAG == "voice_delivery_v1"
    assert output.VOICE_ARTIFACT_KIND == "file"
    assert artifact_service.CANONICAL_MEDIA_TYPES[".wav"] == "audio/wav"
    assert policy.VOICE_RESPONSE_MODES == {
        "text_only", "explicit_only", "emotion_auto", "always",
    }
    assert policy.explicit_voice_request("请用语音回复我") is True
    assert policy.negative_voice_request("不要用语音") is True
    assert callable(policy.release_voice_response_reservation)
    synthesizer = tts.PiperSynthesizer(
        command_prefix=("python", "-m", "piper"),
        model="model.onnx",
    )
    assert synthesizer.max_attempts == 2
    voice_root = ROOT
    if not (voice_root / "docs" / "VOICE_OUTPUT.md").is_file():
        voice_root = ROOT / "open-source-template"
    voice_output_en = (voice_root / "docs" / "VOICE_OUTPUT.md").read_text(encoding="utf-8")
    voice_output_zh = (voice_root / "docs" / "zh-CN" / "VOICE_OUTPUT.md").read_text(
        encoding="utf-8"
    )
    for source in (voice_output_en, voice_output_zh):
        assert "ProtectProc=invisible" in source
        assert "ProcSubset=all" in source
        assert "ProcSubset=pid" in source
    for relative in (
        "bridge_voice_delivery.py",
        "bridge_voice_output.py",
        "bridge_voice_response_policy.py",
        "bridge_voice_response_policy_schema.py",
        "bridge_voice_tts.py",
        "remote-plugin/voice_media.py",
        "docs/VOICE_OUTPUT.md",
        "docs/zh-CN/VOICE_OUTPUT.md",
    ):
        path = ROOT / relative
        if not path.is_file():
            path = ROOT / "open-source-template" / relative
        assert path.is_file(), f"missing_voice_output_file:{relative}"
