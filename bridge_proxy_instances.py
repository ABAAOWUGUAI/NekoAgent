#!/usr/bin/env python3
"""User-owned proxy subscription lifecycle on top of the Mihomo store.

Subscriptions are the only editable proxy assets.  Providers, groups and
nodes are materialized runtime data and are rebuilt from those subscriptions.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import yaml

from bridge_proxy_service import (
    MANAGED_FILE_MODE,
    ManagedSubscriptionStore,
    _atomic_write,
    classify_subscription_payload,
    provider_yaml_from_payload,
    safe_subscription_fetch_error,
    safe_subscription_key,
    validate_subscription_url,
)


_SAFE_SOURCE_ERRORS = frozenset({
    "subscription_payload_empty",
    "subscription_payload_too_large",
    "subscription_payload_not_utf8",
    "unsupported_subscription_format",
    "subscription_converter_input_invalid",
    "subscription_converter_config_missing",
    "subscription_converter_permission_failed",
    "subscription_converter_execution_failed",
    "subscription_converter_failed",
    "subscription_converter_output_invalid",
    "subscription_has_no_proxies",
    "pyyaml_missing",
    "subscription_payload_invalid",
    "subscription_payload_parse_failed",
    "subscription_download_failed",
})
_SAFE_SUBSCRIPTION_ROLLBACK_ERRORS = frozenset({
    "mihomo_reload_failed",
    "mihomo_restore_reload_failed",
    "subscription_rollback_failed",
})


def safe_subscription_rollback_error(error: object) -> str:
    """Reduce a subscription compensation failure to an allowlisted code."""

    if error is None or (isinstance(error, str) and not error.strip()):
        return ""
    code = str(error).strip()
    if code in _SAFE_SUBSCRIPTION_ROLLBACK_ERRORS:
        return code
    return "subscription_rollback_failed"


def _safe_source_error(error: Exception, *, stage: str) -> str:
    code = str(error).strip()
    if stage in {"source_validation", "source_fetch"}:
        return safe_subscription_fetch_error(error)
    if code in _SAFE_SOURCE_ERRORS:
        return code
    if stage == "source_parse" and isinstance(error, (ValueError, yaml.YAMLError)):
        return "subscription_payload_parse_failed"
    return "subscription_download_failed"


def _source_failure(error: Exception, *, stage: str, current_revision: int) -> dict:
    error_kind = _safe_source_error(error, stage=stage)
    return {
        "ok": False,
        "error": error_kind,
        "error_kind": error_kind,
        "current_revision": current_revision,
        "transaction_stage": stage,
    }


class UserSubscriptionStore(ManagedSubscriptionStore):
    """Transactional subscription CRUD with one optimistic management revision."""

    def _state_document(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            value = {}
        if isinstance(value, list):
            return {"management_revision": 0, "subscriptions": value}
        if not isinstance(value, dict):
            return {"management_revision": 0, "subscriptions": []}
        return value

    def _state(self) -> list[dict]:
        records = super()._state()
        for item in records:
            item["enabled"] = bool(item.get("enabled", True))
            item["active"] = bool(item.get("active"))
        return records

    def _state_revision(self) -> int:
        value = self._state_document().get("management_revision", 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _write_state(self, records: list[dict], *, revision: int | None = None) -> None:
        payload = {
            "management_revision": self._state_revision() if revision is None else int(revision),
            "subscriptions": records,
        }
        _atomic_write(
            self.state_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            mode=MANAGED_FILE_MODE,
        )

    def _revision_gate(self, expected_revision: int | None) -> tuple[int, dict | None]:
        current = self._state_revision()
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or int(expected_revision) != current
        ):
            return current, {
                "ok": False,
                "error": "subscription_revision_changed",
                "current_revision": current,
                "transaction_stage": "revision_check",
            }
        return current, None

    @staticmethod
    def _safe_record(record: dict) -> dict:
        return {
            key: value
            for key, value in record.items()
            if key != "url"
        }

    def _active_key(self, records: list[dict] | None = None) -> str:
        items = records if records is not None else self._state()
        active = next((item for item in items if item.get("active") and item.get("enabled", True)), None)
        return str((active or {}).get("key") or "")

    def _normalize_runtime(
        self,
        active_key: str,
        *,
        records: list[dict] | None = None,
        revision: int | None = None,
        backup_key: str = "",
        backup_provider_path: Path | None = None,
        provider_bytes: bytes | None = None,
        remove_provider_path: Path | None = None,
    ) -> dict:
        """Rebuild runtime from the candidate records and commit only after validate/reload."""

        records = [dict(item) for item in (records if records is not None else self._state())]
        for item in records:
            item["enabled"] = bool(item.get("enabled", True))
            item["active"] = False
            item["group"] = "Proxies"
        active = next(
            (item for item in records if item.get("key") == active_key and item.get("enabled")),
            None,
        )
        if active_key and active is None:
            return {
                "ok": False,
                "error": "subscription_not_found",
                "transaction_stage": "candidate_validation",
            }
        try:
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            config = {}
        if not isinstance(config, dict):
            return {
                "ok": False,
                "error": "mihomo_config_not_mapping",
                "transaction_stage": "candidate_validation",
            }

        enabled_records = [item for item in records if item.get("enabled")]
        managed_providers = {str(item.get("provider") or "") for item in enabled_records}
        managed_groups = {str(item.get("group") or "") for item in enabled_records}
        managed_providers.discard("")
        managed_groups.discard("")
        available_providers = config.get("proxy-providers") or {}
        if not isinstance(available_providers, dict):
            available_providers = {}
        normalized_providers = {
            key: value
            for key, value in available_providers.items()
            if key in managed_providers and isinstance(value, dict)
        }
        for record in enabled_records:
            provider_key = str(record.get("provider") or "")
            if not provider_key or provider_key in normalized_providers:
                continue
            normalized_providers[provider_key] = {
                "type": "file",
                "path": f"./proxy-providers/{provider_key}.yaml",
                "health-check": {
                    "enable": True,
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 600,
                    "timeout": 5000,
                    "lazy": True,
                },
            }
        config["proxy-providers"] = normalized_providers

        groups = [item for item in (config.get("proxy-groups") or []) if isinstance(item, dict)]
        known_names = {str(item.get("name") or "") for item in groups}
        rule_targets = {
            str(rule).rsplit(",", 1)[-1].strip()
            for rule in (config.get("rules") or [])
            if isinstance(rule, str) and "," in rule
        }
        blocked = sorted((rule_targets & known_names) - managed_groups - {"Proxies"})
        if blocked:
            return {
                "ok": False,
                "error": "legacy_group_still_referenced",
                "groups": blocked[:12],
                "transaction_stage": "candidate_validation",
            }

        primary = {
            "name": "Proxies",
            "type": "select",
            "proxies": ["DIRECT"],
            "use": [str(active.get("provider") or "")] if active else [],
        }
        config["proxy-groups"] = [primary]
        config.pop("proxies", None)

        provider_path = backup_provider_path
        if provider_path is None:
            provider_name = str((active or {}).get("provider") or "runtime-only")
            provider_path = self.provider_dir / f"{provider_name}.yaml"
        provider_existed = provider_path.is_file()
        state_existed = self.state_path.is_file()
        backup = None
        stage = "backup_prepare"
        try:
            backup = self._backup(
                backup_key or active_key or "runtime",
                provider_path,
            )
            stage = "write_candidate"
            if provider_bytes is not None:
                _atomic_write(provider_path, provider_bytes, mode=MANAGED_FILE_MODE)
            _atomic_write(
                self.config_path,
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"),
                mode=MANAGED_FILE_MODE,
            )
            stage = "mihomo_config_test"
            tested, _ = self.config_test()
            if not tested:
                raise RuntimeError("mihomo_config_test_failed")
            stage = "mihomo_reload"
            reloaded, _ = self.reload_config()
            if not reloaded:
                raise RuntimeError("mihomo_reload_failed")
            if remove_provider_path is not None and remove_provider_path.is_file():
                stage = "provider_cleanup"
                remove_provider_path.unlink()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for item in records:
                item["active"] = bool(active and item.get("key") == active_key)
                if item["active"]:
                    item["activated_at"] = now
            stage = "state_commit"
            self._write_state(records, revision=revision)
        except Exception:
            rolled_back = False
            rollback_error = ""
            if backup is not None:
                try:
                    _atomic_write(
                        self.config_path,
                        (backup / "config.yaml").read_bytes(),
                        mode=MANAGED_FILE_MODE,
                    )
                    state_backup = backup / "codex-subscriptions.json"
                    if state_existed and state_backup.is_file():
                        _atomic_write(
                            self.state_path,
                            state_backup.read_bytes(),
                            mode=MANAGED_FILE_MODE,
                        )
                    elif not state_existed:
                        try:
                            self.state_path.unlink()
                        except FileNotFoundError:
                            pass
                    provider_backup = backup / provider_path.name
                    if provider_existed and provider_backup.is_file():
                        _atomic_write(
                            provider_path,
                            provider_backup.read_bytes(),
                            mode=MANAGED_FILE_MODE,
                        )
                    elif not provider_existed:
                        try:
                            provider_path.unlink()
                        except FileNotFoundError:
                            pass
                    restored, restore_output = self.reload_config()
                    if not restored:
                        raise RuntimeError(restore_output or "mihomo_restore_reload_failed")
                    rolled_back = True
                except Exception as exc:
                    rollback_error = safe_subscription_rollback_error(exc)
            errors = {
                "backup_prepare": "subscription_backup_prepare_failed",
                "write_candidate": "subscription_candidate_write_failed",
                "mihomo_config_test": "mihomo_config_test_failed",
                "mihomo_reload": "mihomo_reload_failed",
                "provider_cleanup": "provider_cleanup_failed",
                "state_commit": "subscription_state_commit_failed",
            }
            return {
                "ok": False,
                "error": errors.get(stage, "subscription_transaction_failed"),
                "rolled_back": rolled_back,
                "rollback_error": rollback_error,
                "backup_id": backup.name if backup is not None else "",
                "transaction_stage": stage,
            }
        return {
            "ok": True,
            "active_key": active_key,
            "backup_id": backup.name,
            "revision": self._state_revision(),
            "transaction_stage": "committed",
        }

    def _upsert_and_reconcile(
        self,
        name: str,
        url: str,
        *,
        key: str,
        enabled: bool,
        current_revision: int,
    ) -> dict:
        before = self._state()
        active_before = self._active_key(before)
        effective_key = safe_subscription_key(key) or safe_subscription_key(name)
        clean_name = (name or "").strip()
        clean_url = (url or "").strip()
        if not clean_name:
            return {
                "ok": False,
                "error": "subscription_name_required",
                "transaction_stage": "source_validation",
            }
        if not effective_key:
            return {
                "ok": False,
                "error": "subscription_name_invalid",
                "transaction_stage": "source_validation",
            }
        validation = validate_subscription_url(clean_url)
        if not validation.get("ok"):
            return _source_failure(
                ValueError(str(validation.get("error") or "invalid_subscription_url")),
                stage="source_validation",
                current_revision=current_revision,
            )
        try:
            payload, content_type = self.fetcher(clean_url)
        except Exception as exc:
            return _source_failure(
                exc,
                stage="source_fetch",
                current_revision=current_revision,
            )
        try:
            classification = classify_subscription_payload(payload, content_type)
            if not classification.get("ok"):
                raise ValueError(str(classification.get("error") or "unsupported_subscription_format"))
        except Exception as exc:
            return _source_failure(
                exc,
                stage="source_parse",
                current_revision=current_revision,
            )
        if classification.get("needs_converter"):
            try:
                payload = self.converter(payload)
                content_type = "application/yaml"
            except Exception as exc:
                return _source_failure(
                    exc,
                    stage="source_convert",
                    current_revision=current_revision,
                )
        try:
            provider_document, _ = provider_yaml_from_payload(payload, content_type)
            provider_bytes = yaml.safe_dump(
                provider_document,
                allow_unicode=True,
                sort_keys=False,
            ).encode("utf-8")
        except Exception as exc:
            return _source_failure(
                exc,
                stage="source_parse",
                current_revision=current_revision,
            )
        provider_path = self.provider_dir / f"agent-{effective_key}.yaml"
        previous = next((item for item in before if item.get("key") == effective_key), {})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target_record = {
            "key": effective_key,
            "name": clean_name,
            "url": clean_url,
            "provider": f"agent-{effective_key}",
            "group": "Proxies",
            "format": classification["format"],
            "node_count": classification["node_count"],
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
            "last_status": "ready",
            "last_error": "",
            "enabled": bool(enabled),
            "active": bool(previous.get("active")),
        }
        candidate = [dict(item) for item in before if item.get("key") != effective_key]
        candidate.append(target_record)
        if active_before and any(
            item.get("key") == active_before and item.get("enabled") for item in candidate
        ):
            active_target = active_before
        elif enabled:
            active_target = effective_key
        else:
            active_target = next(
                (str(item.get("key") or "") for item in candidate if item.get("enabled")),
                "",
            )
        normalized = self._normalize_runtime(
            active_target,
            records=candidate,
            revision=current_revision + 1,
            backup_key=effective_key,
            backup_provider_path=provider_path,
            provider_bytes=provider_bytes,
        )
        if not normalized.get("ok"):
            return normalized
        normalized["subscription"] = self._safe_record(target_record)
        normalized["changed"] = True
        normalized["node_count"] = target_record.get("node_count", 0)
        normalized["refreshed_at"] = target_record.get("updated_at", "")
        return normalized

    def create(
        self,
        name: str,
        url: str,
        *,
        enabled: bool,
        expected_revision: int | None,
    ) -> dict:
        current, conflict = self._revision_gate(expected_revision)
        if conflict:
            return conflict
        name = (name or "").strip()
        records = self._state()
        key = safe_subscription_key(name)
        if any(
            item.get("key") == key
            or str(item.get("name") or "").strip().casefold() == name.casefold()
            for item in records
        ):
            return {
                "ok": False,
                "error": "subscription_already_exists",
                "current_revision": current,
                "transaction_stage": "request_validation",
            }
        return self._upsert_and_reconcile(
            name,
            (url or "").strip(),
            key="",
            enabled=bool(enabled),
            current_revision=current,
        )

    def update(
        self,
        key: str,
        name: str,
        url: str = "",
        *,
        url_update_present: bool,
        enabled: bool,
        expected_revision: int | None,
    ) -> dict:
        current, conflict = self._revision_gate(expected_revision)
        if conflict:
            return conflict
        key = safe_subscription_key(key)
        records = self._state()
        previous = next((item for item in records if item.get("key") == key), None)
        if previous is None:
            return {
                "ok": False,
                "error": "subscription_not_found",
                "transaction_stage": "request_validation",
            }
        clean_name = (name or "").strip()
        if not clean_name:
            return {
                "ok": False,
                "error": "subscription_name_required",
                "transaction_stage": "request_validation",
            }
        if any(
            item.get("key") != key
            and str(item.get("name") or "").strip().casefold() == clean_name.casefold()
            for item in records
        ):
            return {
                "ok": False,
                "error": "subscription_name_conflict",
                "transaction_stage": "request_validation",
            }
        if url_update_present:
            return self._upsert_and_reconcile(
                clean_name,
                (url or "").strip(),
                key=key,
                enabled=bool(enabled),
                current_revision=current,
            )

        candidate = [dict(item) for item in records]
        target = next(item for item in candidate if item.get("key") == key)
        target["name"] = clean_name
        target["enabled"] = bool(enabled)
        target["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        active_before = self._active_key(records)
        if active_before == key and not enabled:
            active_target = next(
                (str(item.get("key") or "") for item in candidate if item.get("enabled") and item.get("key") != key),
                "",
            )
        elif active_before:
            active_target = active_before
        elif enabled:
            active_target = key
        else:
            active_target = ""
        provider_path = self.provider_dir / f"{target.get('provider')}.yaml"
        result = self._normalize_runtime(
            active_target,
            records=candidate,
            revision=current + 1,
            backup_key=key,
            backup_provider_path=provider_path,
        )
        if result.get("ok"):
            result["changed"] = True
            result["subscription"] = self._safe_record(target)
            result["node_count"] = target.get("node_count", 0)
        return result

    def refresh(self, key: str, *, expected_revision: int | None = None) -> dict:
        current, conflict = self._revision_gate(expected_revision)
        if conflict:
            return conflict
        key = safe_subscription_key(key)
        record = next((item for item in self._state() if item.get("key") == key), None)
        if record is None:
            return {
                "ok": False,
                "error": "subscription_not_found",
                "transaction_stage": "request_validation",
            }
        result = self._upsert_and_reconcile(
            str(record.get("name") or key),
            str(record.get("url") or ""),
            key=key,
            enabled=bool(record.get("enabled", True)),
            current_revision=current,
        )
        if not result.get("ok"):
            result.setdefault("error", "subscription_download_failed")
            result["current_revision"] = current
        return result

    def enable(self, key: str, *, expected_revision: int | None = None) -> dict:
        current, conflict = self._revision_gate(expected_revision)
        if conflict:
            return conflict
        key = safe_subscription_key(key)
        records = self._state()
        target = next((item for item in records if item.get("key") == key), None)
        if target is None:
            return {"ok": False, "error": "subscription_not_found", "transaction_stage": "request_validation"}
        if target.get("enabled") and target.get("active"):
            return {"ok": True, "changed": False, "revision": current, "active_key": key}
        candidate = [dict(item) for item in records]
        next(item for item in candidate if item.get("key") == key)["enabled"] = True
        active_target = self._active_key(records) or key
        result = self._normalize_runtime(
            active_target,
            records=candidate,
            revision=current + 1,
            backup_key=key,
            backup_provider_path=self.provider_dir / f"{target.get('provider')}.yaml",
        )
        if result.get("ok"):
            result["changed"] = True
        return result

    def disable(self, key: str, *, expected_revision: int | None = None) -> dict:
        current, conflict = self._revision_gate(expected_revision)
        if conflict:
            return conflict
        key = safe_subscription_key(key)
        records = self._state()
        target = next((item for item in records if item.get("key") == key), None)
        if target is None:
            return {"ok": False, "error": "subscription_not_found", "transaction_stage": "request_validation"}
        if not target.get("enabled"):
            return {"ok": True, "changed": False, "revision": current, "active_key": self._active_key(records)}
        candidate = [dict(item) for item in records]
        next(item for item in candidate if item.get("key") == key)["enabled"] = False
        active_before = self._active_key(records)
        active_target = active_before
        if active_before == key:
            active_target = next(
                (str(item.get("key") or "") for item in candidate if item.get("enabled") and item.get("key") != key),
                "",
            )
        result = self._normalize_runtime(
            active_target,
            records=candidate,
            revision=current + 1,
            backup_key=key,
            backup_provider_path=self.provider_dir / f"{target.get('provider')}.yaml",
        )
        if result.get("ok"):
            result["changed"] = True
        return result

    def delete(self, key: str, *, expected_revision: int | None = None) -> dict:
        current, conflict = self._revision_gate(expected_revision)
        if conflict:
            return conflict
        key = safe_subscription_key(key)
        records = self._state()
        target = next((item for item in records if item.get("key") == key), None)
        if target is None:
            return {"ok": False, "error": "subscription_not_found", "transaction_stage": "request_validation"}
        candidate = [dict(item) for item in records if item.get("key") != key]
        active_before = self._active_key(records)
        active_target = active_before if active_before != key else next(
            (str(item.get("key") or "") for item in candidate if item.get("enabled")),
            "",
        )
        provider_path = self.provider_dir / f"{target.get('provider')}.yaml"
        result = self._normalize_runtime(
            active_target,
            records=candidate,
            revision=current + 1,
            backup_key=key,
            backup_provider_path=provider_path,
            remove_provider_path=provider_path,
        )
        if result.get("ok"):
            result.update({"changed": True, "deleted": key})
        return result

    def switch(self, key: str, *, expected_revision: int | None = None) -> dict:
        current, conflict = self._revision_gate(expected_revision)
        if conflict:
            return conflict
        key = safe_subscription_key(key)
        records = self._state()
        target = next((item for item in records if item.get("key") == key), None)
        if target is None:
            return {"ok": False, "error": "subscription_not_found", "transaction_stage": "request_validation"}
        candidate = [dict(item) for item in records]
        next(item for item in candidate if item.get("key") == key)["enabled"] = True
        result = self._normalize_runtime(
            key,
            records=candidate,
            revision=current + 1,
            backup_key=key,
            backup_provider_path=self.provider_dir / f"{target.get('provider')}.yaml",
        )
        if result.get("ok"):
            result["changed"] = True
        return result
