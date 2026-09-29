"""Versioned control metadata for the existing AIClient2API proxy authority.

The metadata lives inside AIClient2API's native config document. It is not a
second proxy inventory: subscription identity is always revalidated against
Mihomo's authoritative managed-subscription state before use.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any


AICLIENT_CONFIG_PATH = Path(os.environ.get("AICLIENT_CONFIG_PATH", "/var/lib/aiclient2api/configs/config.json"))
MIHOMO_SUBSCRIPTION_STATE_PATH = Path(os.environ.get("MIHOMO_SUBSCRIPTION_STATE_PATH", "/etc/mihomo/codex-subscriptions.json"))
CONTROL_KEY = "NEKOAGENT_PROXY_CONTROL"
CONTROL_SCHEMA_VERSION = 1
TARGET_PROXY_URL = "http://mihomo:7890"
UPSTREAM_PROVIDER = "gemini-antigravity"
PROVIDER_ID = "aiclient2api-gemini-antigravity"
MODEL_ID = "aiclient2api-gemini-2-5-flash"
ALLOWED_SELECTION_FIELDS = frozenset({
    "enabled", "subscription_key", "group", "node", "apply_intent",
    "form_version", "expected_revision",
})
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_document_not_object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _locked():
    with _LOCK:
        yield


def subscription_inventory(state_path: Path = MIHOMO_SUBSCRIPTION_STATE_PATH) -> dict[str, Any]:
    path = Path(state_path)
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except Exception:
        raw = b""
        document = {}
    records = document if isinstance(document, list) else document.get("subscriptions", [])
    if not isinstance(records, list):
        records = []
    managed = []
    for item in records:
        if not isinstance(item, dict):
            continue
        managed.append({
            "key": str(item.get("key") or ""),
            "name": str(item.get("name") or item.get("key") or ""),
            "provider": str(item.get("provider") or ""),
            "group": str(item.get("group") or ""),
            "format": str(item.get("format") or "unknown"),
            "node_count": int(item.get("node_count") or 0),
            "last_status": str(item.get("last_status") or "unknown"),
            "last_error": str(item.get("last_error") or "")[:120],
            "updated_at": str(item.get("updated_at") or ""),
            "enabled": bool(item.get("enabled", True)),
            "active": bool(item.get("active")),
        })
    return {
        "revision": hashlib.sha256(raw).hexdigest() if raw else "missing",
        "active_key": next((item["key"] for item in managed if item["active"] and item["enabled"]), ""),
        "managed": managed,
    }


def _default_control() -> dict[str, Any]:
    return {"schema_version": CONTROL_SCHEMA_VERSION, "revision": 0, "draft": {}, "latest_test": {}, "applied": {}, "previous_controlled": {}}


def _control(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get(CONTROL_KEY)
    if not isinstance(value, dict) or value.get("schema_version") != CONTROL_SCHEMA_VERSION:
        return _default_control()
    return {**_default_control(), **value}


def _required_text(value: Any, label: str, maximum: int = 128) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label}_required")
    if len(text) > maximum or any(char in text for char in "\x00\r\n"):
        raise ValueError(f"{label}_invalid")
    return text


def _normalize_selection(payload: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - ALLOWED_SELECTION_FIELDS)
    if unknown:
        raise ValueError(f"selection_field_forbidden:{','.join(unknown)}")
    if not isinstance(payload.get("enabled"), bool):
        raise ValueError("enabled_boolean_required")
    key = _required_text(payload.get("subscription_key"), "subscription_key")
    record = next((item for item in inventory["managed"] if item["key"] == key), None)
    if record is None:
        raise ValueError("managed_subscription_not_found")
    if not record.get("active") or not record.get("enabled") or key != inventory.get("active_key"):
        raise ValueError("managed_subscription_not_active")
    group = _required_text(payload.get("group"), "group")
    if group != "Proxies":
        raise ValueError("canonical_proxy_group_required")
    intent = _required_text(payload.get("apply_intent"), "apply_intent", 40)
    if intent != "controlled_mihomo":
        raise ValueError("apply_intent_forbidden")
    selection = {
        "enabled": payload["enabled"],
        "subscription_key": key,
        "group": "Proxies",
        "node": _required_text(payload.get("node"), "node", 192),
        "apply_intent": intent,
        "form_version": _required_text(payload.get("form_version"), "form_version", 96),
        "subscription_revision": inventory["revision"],
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "upstream_provider": UPSTREAM_PROVIDER,
    }
    encoded = json.dumps(selection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    selection["selection_fingerprint"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return selection


def save_draft(config_path: Path, state_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    with _locked():
        config = _load_json(Path(config_path))
        control = _control(config)
        expected = payload.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != int(control["revision"]):
            return {"ok": False, "error": "consumer_revision_conflict", "revision": int(control["revision"])}
        try:
            draft = _normalize_selection(payload, subscription_inventory(Path(state_path)))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        control["revision"] = int(control["revision"]) + 1
        control["draft"] = draft
        control["saved_at"] = _now()
        config[CONTROL_KEY] = control
        _atomic_json(Path(config_path), config)
        return {"ok": True, "revision": control["revision"], "draft": draft}


def record_test_receipt(
    config_path: Path,
    state_path: Path,
    *,
    expected_revision: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    with _locked():
        config = _load_json(Path(config_path))
        control = _control(config)
        revision = int(control["revision"])
        if expected_revision != revision:
            return {"ok": False, "error": "consumer_revision_conflict", "revision": revision}
        selection = control.get("draft") if isinstance(control.get("draft"), dict) else {}
        if not selection:
            return {"ok": False, "error": "consumer_draft_not_saved", "revision": revision}
        inventory = subscription_inventory(Path(state_path))
        if selection.get("subscription_revision") != inventory["revision"]:
            return {"ok": False, "error": "subscription_revision_changed", "revision": revision}
        receipt = {
            "receipt_id": hashlib.sha256(f"{selection['selection_fingerprint']}:{_now()}".encode()).hexdigest()[:24],
            "tested_at": _now(),
            "consumer_revision": revision,
            "selection": dict(selection),
            "selection_fingerprint": selection["selection_fingerprint"],
            "subscription_revision": selection["subscription_revision"],
            "form_version": selection["form_version"],
            "provider_id": PROVIDER_ID,
            "model_id": MODEL_ID,
            "google": dict(result.get("google") or {}),
            "text": dict(result.get("text") or {}),
            "vision": dict(result.get("vision") or {}),
            "auth": dict(result.get("auth") or {}),
        }
        receipt["passed"] = all(bool(receipt[key].get("ok")) for key in ("google", "text", "vision", "auth"))
        control["latest_test"] = receipt
        config[CONTROL_KEY] = control
        _atomic_json(Path(config_path), config)
        return {"ok": receipt["passed"], "receipt": receipt, "error": "" if receipt["passed"] else "candidate_test_failed"}


def evaluate_apply_gate(config_path: Path, state_path: Path, runtime: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
    config = _load_json(Path(config_path))
    control = _control(config)
    inventory = subscription_inventory(Path(state_path))
    draft = control.get("draft") if isinstance(control.get("draft"), dict) else {}
    receipt = control.get("latest_test") if isinstance(control.get("latest_test"), dict) else {}
    blockers: list[str] = []
    if expected_revision is not None and expected_revision != int(control["revision"]):
        blockers.append("consumer_revision_conflict")
    if not draft or not draft.get("enabled"):
        blockers.append("consumer_not_enabled")
    active = next((item for item in inventory["managed"] if item["key"] == draft.get("subscription_key")), None)
    if active is None:
        blockers.append("managed_subscription_not_found")
    elif not active.get("active"):
        blockers.append("managed_subscription_not_active")
    if draft.get("subscription_revision") != inventory["revision"]:
        blockers.append("subscription_revision_changed")
    if not receipt or receipt.get("selection_fingerprint") != draft.get("selection_fingerprint"):
        blockers.append("test_receipt_stale")
    elif receipt.get("subscription_revision") != inventory["revision"]:
        blockers.append("subscription_revision_changed")
    else:
        for key, code in (("google", "google_reachability_failed"), ("text", "gemini_text_test_failed"), ("vision", "gemini_vision_test_failed"), ("auth", "aiclient_auth_test_failed")):
            if not bool((receipt.get(key) or {}).get("ok")):
                blockers.append(code)
    for key, code in (("network_ready", "proxy_network_unhealthy"), ("mihomo_healthy", "mihomo_unhealthy"), ("aiclient_healthy", "aiclient_unhealthy"), ("credential_configured", "credential_not_configured")):
        if not bool(runtime.get(key)):
            blockers.append(code)
    if bool(runtime.get("direct_allowed")):
        blockers.append("direct_not_fail_closed")
    if bool(runtime.get("fallback_allowed")):
        blockers.append("fallback_not_fail_closed")
    if bool(runtime.get("provider_fallback", runtime.get("provider_fallback_allowed"))):
        blockers.append("provider_fallback_enabled")
    if bool(runtime.get("model_fallback", runtime.get("model_fallback_allowed"))):
        blockers.append("model_fallback_enabled")
    blockers = list(dict.fromkeys(blockers))
    return {"eligible": not blockers, "blockers": blockers, "revision": int(control["revision"])}


def apply_controlled_config(config_path: Path, state_path: Path, runtime: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
    with _locked():
        gate = evaluate_apply_gate(config_path, state_path, runtime, expected_revision=expected_revision)
        if not gate["eligible"]:
            return {"ok": False, "error": "apply_gate_blocked", **gate}
        config = _load_json(Path(config_path))
        control = _control(config)
        current_applied = control.get("applied") if isinstance(control.get("applied"), dict) else {}
        if current_applied:
            control["previous_controlled"] = {
                "selection": current_applied.get("selection") or {},
                "runtime": {
                    "PROXY_URL": str(config.get("PROXY_URL") or ""),
                    "PROXY_ENABLED_PROVIDERS": list(config.get("PROXY_ENABLED_PROVIDERS") or []),
                    "MODEL_FALLBACK_ENABLED": bool(config.get("MODEL_FALLBACK_ENABLED")),
                    "providerFallbackChain": dict(config.get("providerFallbackChain") or {}),
                    "modelFallbackMapping": dict(config.get("modelFallbackMapping") or {}),
                },
            }
        config["PROXY_URL"] = TARGET_PROXY_URL
        config["PROXY_ENABLED_PROVIDERS"] = [UPSTREAM_PROVIDER]
        config["MODEL_FALLBACK_ENABLED"] = False
        config["providerFallbackChain"] = {}
        config["modelFallbackMapping"] = {}
        control["applied"] = {"selection": dict(control["draft"]), "applied_at": _now()}
        control["revision"] = int(control["revision"]) + 1
        config[CONTROL_KEY] = control
        _atomic_json(Path(config_path), config)
        return {"ok": True, "revision": control["revision"], "applied": control["applied"]}


def rollback_controlled_config(config_path: Path, *, expected_revision: int) -> dict[str, Any]:
    with _locked():
        config = _load_json(Path(config_path))
        control = _control(config)
        if expected_revision != int(control["revision"]):
            return {"ok": False, "error": "consumer_revision_conflict", "revision": int(control["revision"])}
        previous = control.get("previous_controlled") if isinstance(control.get("previous_controlled"), dict) else {}
        runtime = previous.get("runtime") if isinstance(previous.get("runtime"), dict) else {}
        selection = previous.get("selection") if isinstance(previous.get("selection"), dict) else {}
        if not runtime or not selection:
            return {"ok": False, "error": "previous_controlled_config_not_found", "revision": int(control["revision"])}
        config["PROXY_URL"] = str(runtime.get("PROXY_URL") or "")
        config["PROXY_ENABLED_PROVIDERS"] = list(runtime.get("PROXY_ENABLED_PROVIDERS") or [])
        config["MODEL_FALLBACK_ENABLED"] = bool(runtime.get("MODEL_FALLBACK_ENABLED"))
        config["providerFallbackChain"] = dict(runtime.get("providerFallbackChain") or {})
        config["modelFallbackMapping"] = dict(runtime.get("modelFallbackMapping") or {})
        control["applied"] = {"selection": selection, "applied_at": _now(), "rolled_back": True}
        control["previous_controlled"] = {}
        control["revision"] = int(control["revision"]) + 1
        config[CONTROL_KEY] = control
        _atomic_json(Path(config_path), config)
        return {"ok": True, "revision": control["revision"], "applied": control["applied"]}


def control_surface_status(config_path: Path = AICLIENT_CONFIG_PATH, state_path: Path = MIHOMO_SUBSCRIPTION_STATE_PATH) -> dict[str, Any]:
    try:
        config = _load_json(Path(config_path))
        control = _control(config)
    except Exception as exc:
        return {"revision": 0, "draft": {}, "latest_test": {}, "applied": {}, "rollback_available": False, "error": str(exc)[:120]}
    inventory = subscription_inventory(Path(state_path))
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "revision": int(control["revision"]),
        "draft": dict(control.get("draft") or {}),
        "latest_test": dict(control.get("latest_test") or {}),
        "applied": dict(control.get("applied") or {}),
        "rollback_available": bool(control.get("previous_controlled")),
        "subscription_revision": inventory["revision"],
        "subscription_active_key": inventory["active_key"],
    }


__all__ = [
    "AICLIENT_CONFIG_PATH", "ALLOWED_SELECTION_FIELDS", "CONTROL_KEY",
    "MIHOMO_SUBSCRIPTION_STATE_PATH", "TARGET_PROXY_URL", "apply_controlled_config",
    "control_surface_status", "evaluate_apply_gate", "record_test_receipt",
    "rollback_controlled_config", "save_draft", "subscription_inventory",
]
