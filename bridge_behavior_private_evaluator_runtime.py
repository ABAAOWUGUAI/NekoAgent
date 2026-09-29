#!/usr/bin/env python3
"""Private-process runtime for BE-4 frozen benchmark evaluation.

This module deliberately has no SQLite, HTTP, Delivery, Knowledge, Memory,
Task, Approval or Bridge dependency.  It may load a private Vault and an
Ed25519 signing key only inside the evaluator process.  Its public results are
the body-free plans and signed aggregate receipts accepted by the Bridge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

try:  # Dependency absence must keep the evaluator closed instead of falling back to a shared secret.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # pragma: no cover - exercised by deployment preflight.
    serialization = None  # type: ignore[assignment]
    Ed25519PrivateKey = None  # type: ignore[assignment,misc]

from bridge_behavior_benchmark_contract import PrivateBenchmarkVault
from bridge_behavior_evolution_contract import make_policy_bundle_candidate
from bridge_behavior_private_evaluator import (
    PrivateBehaviorEvaluatorError,
    run_private_candidate_evaluation_receipt,
    run_private_reference_evaluation,
)


_CONFIG_FIELDS = frozenset(
    {"schema_version", "assistant_id", "evaluator_ref", "signing_key_ref", "private_key_path", "vault_path"}
)
_VAULT_FIELDS = frozenset(
    {
        "schema_version", "assistant_id", "owner_approval_ref", "provider_ref", "model_ref", "thinking_ref",
        "reference_policy_ref", "campaign_budget", "private_cases",
    }
)
_BODY_FIELD_RE = re.compile(r"(?:^|_)(?:scenario|gold|rubric|prompt|message|content|private)(?:_|$)|^output$")
_ASSISTANT_ID_RE = re.compile(r"^(?:assistant-[0-9a-f]{32}|assistant:[A-Za-z0-9][A-Za-z0-9_-]*)$")


class PrivateEvaluatorRuntimeError(RuntimeError):
    """The private evaluator configuration, Vault or request was not safe to run."""


def _ref(value: object, *, error: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(char.isspace() for char in text):
        raise PrivateEvaluatorRuntimeError(error)
    return text


def _assistant_id(value: object) -> str:
    text = _ref(value, error="private_evaluator_assistant_invalid")
    if not _ASSISTANT_ID_RE.fullmatch(text):
        raise PrivateEvaluatorRuntimeError("private_evaluator_assistant_invalid")
    return text


def _utc(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PrivateEvaluatorRuntimeError("private_evaluator_time_invalid") from exc
    if parsed.tzinfo is None:
        raise PrivateEvaluatorRuntimeError("private_evaluator_time_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def _regular_private_path(value: object, *, error: str) -> Path:
    path = Path(str(value or "")).expanduser().resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise PrivateEvaluatorRuntimeError(error) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise PrivateEvaluatorRuntimeError(error)
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise PrivateEvaluatorRuntimeError("private_evaluator_private_file_permissions_invalid")
    return path


def _load_json_private(value: object, *, error: str) -> dict[str, object]:
    path = _regular_private_path(value, error=error)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrivateEvaluatorRuntimeError(error) from exc
    if not isinstance(parsed, dict):
        raise PrivateEvaluatorRuntimeError(error)
    return parsed


def _key(value: object) -> object:
    if Ed25519PrivateKey is None:
        raise PrivateEvaluatorRuntimeError("private_evaluator_crypto_unavailable")
    path = _regular_private_path(value, error="private_evaluator_signing_key_invalid")
    try:
        raw = path.read_bytes()
        key = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        raise PrivateEvaluatorRuntimeError("private_evaluator_signing_key_invalid") from exc
    return key


def _assert_body_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            name = str(key or "").lower()
            if _BODY_FIELD_RE.search(name):
                raise PrivateEvaluatorRuntimeError("private_evaluator_body_free_violation")
            _assert_body_free(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_body_free(nested)


class PrivateEvaluatorRuntime:
    """One evaluator-only Vault/key binding, with a body-free public interface."""

    def __init__(
        self,
        config: Mapping[str, object],
        *,
        model_runner: Callable[[str, str, Mapping[str, str]], Mapping[str, object]] | None,
    ) -> None:
        if not isinstance(config, Mapping) or set(config) != _CONFIG_FIELDS or config.get("schema_version") != 1:
            raise PrivateEvaluatorRuntimeError("private_evaluator_config_invalid")
        self._assistant_id = _assistant_id(config.get("assistant_id"))
        self._evaluator_ref = _ref(config.get("evaluator_ref"), error="private_evaluator_config_invalid")
        self._signing_key_ref = _ref(config.get("signing_key_ref"), error="private_evaluator_config_invalid")
        if not callable(model_runner):
            raise PrivateEvaluatorRuntimeError("private_evaluator_model_runner_unavailable")
        vault_config = _load_json_private(config.get("vault_path"), error="private_evaluator_vault_invalid")
        if set(vault_config) != _VAULT_FIELDS or vault_config.get("schema_version") != 1:
            raise PrivateEvaluatorRuntimeError("private_evaluator_vault_invalid")
        if _assistant_id(vault_config.get("assistant_id")) != self._assistant_id:
            raise PrivateEvaluatorRuntimeError("private_evaluator_vault_assistant_mismatch")
        try:
            self._vault = PrivateBenchmarkVault.from_cases(
                owner_approval_ref=vault_config["owner_approval_ref"],
                provider_ref=vault_config["provider_ref"],
                model_ref=vault_config["model_ref"],
                thinking_ref=vault_config["thinking_ref"],
                reference_policy_ref=vault_config["reference_policy_ref"],
                campaign_budget=vault_config["campaign_budget"],
                private_cases=vault_config["private_cases"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PrivateEvaluatorRuntimeError("private_evaluator_vault_invalid") from exc
        self._key = _key(config.get("private_key_path"))
        self._model_runner = model_runner

    def _require_assistant(self, assistant_id: object) -> str:
        selected = _assistant_id(assistant_id)
        if selected != self._assistant_id:
            raise PrivateEvaluatorRuntimeError("private_evaluator_assistant_mismatch")
        return selected

    def _public_key_fingerprint(self) -> str:
        if serialization is None:
            raise PrivateEvaluatorRuntimeError("private_evaluator_crypto_unavailable")
        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def _reference_plan(self, *, run_ref: str = "") -> dict[str, object]:
        frozen = self._vault.frozen
        result: dict[str, object] = {
            "assistant_id": self._assistant_id,
            "benchmark_ref": frozen.benchmark_ref,
            "benchmark_hash": frozen.benchmark_hash,
            "reference_policy_ref": frozen.reference_policy_ref,
            "evaluator_ref": self._evaluator_ref,
            "signing_key_ref": self._signing_key_ref,
            "public_key_fingerprint": self._public_key_fingerprint(),
            "provider_ref": frozen.provider_ref,
            "model_ref": frozen.model_ref,
            "thinking_ref": frozen.thinking_ref,
            "case_count": frozen.case_count,
            "campaign_budget": dict(frozen.campaign_budget),
        }
        if run_ref:
            result["run_ref"] = _ref(run_ref, error="private_evaluator_run_ref_invalid")
        _assert_body_free(result)
        return result

    def reference_plan(self, assistant_id: object) -> dict[str, object]:
        self._require_assistant(assistant_id)
        return self._reference_plan()

    def run_reference(self, assistant_id: object, run_ref: object, completed_at: object) -> dict[str, object]:
        self._require_assistant(assistant_id)
        try:
            receipt = run_private_reference_evaluation(
                self._vault,
                self._model_runner,
                evaluator_ref=self._evaluator_ref,
                run_ref=_ref(run_ref, error="private_evaluator_run_ref_invalid"),
                signing_key_ref=self._signing_key_ref,
                signing_key=self._key,
                completed_at=_utc(completed_at),
            )
        except (PrivateBehaviorEvaluatorError, ValueError) as exc:
            raise PrivateEvaluatorRuntimeError("private_evaluator_reference_failed") from exc
        _assert_body_free(receipt)
        return receipt

    def candidate_plan(self, assistant_id: object, candidate: Mapping[str, object], run_ref: object) -> dict[str, object]:
        self._require_assistant(assistant_id)
        try:
            normalized = make_policy_bundle_candidate(candidate)
        except ValueError as exc:
            raise PrivateEvaluatorRuntimeError("private_evaluator_candidate_invalid") from exc
        if normalized["reference_policy_ref"] != self._vault.frozen.reference_policy_ref:
            raise PrivateEvaluatorRuntimeError("private_evaluator_candidate_reference_mismatch")
        result = {
            **self._reference_plan(run_ref=_ref(run_ref, error="private_evaluator_run_ref_invalid")),
            "candidate_ref": normalized["candidate_id"],
            "policy_bundle_hash": normalized["policy_bundle_hash"],
        }
        _assert_body_free(result)
        return result

    def run_candidate(
        self,
        assistant_id: object,
        candidate: Mapping[str, object],
        owner_authorization_ref: object,
        run_ref: object,
        completed_at: object,
    ) -> dict[str, object]:
        self._require_assistant(assistant_id)
        try:
            normalized = make_policy_bundle_candidate(candidate)
        except ValueError as exc:
            raise PrivateEvaluatorRuntimeError("private_evaluator_candidate_invalid") from exc
        if normalized["reference_policy_ref"] != self._vault.frozen.reference_policy_ref:
            raise PrivateEvaluatorRuntimeError("private_evaluator_candidate_reference_mismatch")
        try:
            receipt = run_private_candidate_evaluation_receipt(
                self._vault,
                self._model_runner,
                normalized,
                assistant_id=self._assistant_id,
                owner_authorization_ref=_ref(owner_authorization_ref, error="private_evaluator_authorization_invalid"),
                evaluator_ref=self._evaluator_ref,
                run_ref=_ref(run_ref, error="private_evaluator_run_ref_invalid"),
                signing_key_ref=self._signing_key_ref,
                signing_key=self._key,
                completed_at=_utc(completed_at),
            )
        except (PrivateBehaviorEvaluatorError, ValueError) as exc:
            raise PrivateEvaluatorRuntimeError("private_evaluator_candidate_failed") from exc
        _assert_body_free(receipt)
        return receipt


__all__ = ["PrivateEvaluatorRuntime", "PrivateEvaluatorRuntimeError"]
