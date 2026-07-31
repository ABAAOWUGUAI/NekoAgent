#!/usr/bin/env python3
"""User-owned proxy subscription lifecycle on top of the Mihomo store.

Subscriptions are the only editable proxy assets.  Providers, groups and
nodes are materialized runtime data and are rebuilt from those subscriptions.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

import yaml

from bridge_proxy_service import ManagedSubscriptionStore, _atomic_write, safe_subscription_key


class UserSubscriptionStore(ManagedSubscriptionStore):
    """Add edit/switch semantics and remove legacy inline proxy inventory."""

    def _write_state(self, records: list[dict]) -> None:
        payload = json.dumps({"subscriptions": records}, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(self.state_path, payload.encode("utf-8"))

    def _active_key(self, records: list[dict] | None = None) -> str:
        items = records if records is not None else self._state()
        active = next((item for item in items if item.get("active")), None)
        return str((active or {}).get("key") or "")

    def _normalize_runtime(self, active_key: str) -> dict:
        """Make managed subscriptions the sole source of runtime proxy nodes.

        The rewrite happens only after a subscription payload has passed the
        existing download, conversion and Mihomo validation transaction.
        """

        records = self._state()
        active = next((item for item in records if item.get("key") == active_key), None)
        if active is None:
            return {"ok": False, "error": "subscription_not_found"}
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            return {"ok": False, "error": "mihomo_config_not_mapping"}

        managed_providers = {str(item.get("provider") or "") for item in records}
        managed_groups = {str(item.get("group") or "") for item in records}
        managed_providers.discard("")
        managed_groups.discard("")
        available_providers = config.get("proxy-providers") or {}
        config["proxy-providers"] = {
            key: value for key, value in available_providers.items()
            if key in managed_providers and isinstance(value, dict)
        }

        groups = [item for item in (config.get("proxy-groups") or []) if isinstance(item, dict)]
        known_names = {str(item.get("name") or "") for item in groups}
        rule_targets = {
            str(rule).rsplit(",", 1)[-1].strip()
            for rule in (config.get("rules") or [])
            if isinstance(rule, str) and "," in rule
        }
        blocked = sorted((rule_targets & known_names) - managed_groups - {"Proxies"})
        if blocked:
            return {"ok": False, "error": "legacy_group_still_referenced", "groups": blocked[:12]}

        materialized = []
        for record in records:
            group_name = str(record.get("group") or "")
            provider_key = str(record.get("provider") or "")
            if group_name and provider_key:
                materialized.append({"name": group_name, "type": "select", "use": [provider_key]})
        primary = {
            "name": "Proxies",
            "type": "select",
            "proxies": ["DIRECT"],
            "use": [str(active.get("provider") or "")],
        }
        config["proxy-groups"] = [primary, *[item for item in materialized if item["name"] != "Proxies"]]
        config.pop("proxies", None)

        provider_path = self.provider_dir / f"{active.get('provider')}.yaml"
        provider_existed = provider_path.is_file()
        backup = self._backup(active_key, provider_path)
        try:
            _atomic_write(
                self.config_path,
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"),
            )
            tested, _ = self.config_test()
            if not tested:
                raise RuntimeError("mihomo_config_test_failed")
            reloaded, _ = self.reload_config()
            if not reloaded:
                raise RuntimeError("mihomo_reload_failed")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for item in records:
                item["active"] = item.get("key") == active_key
                if item["active"]:
                    item["activated_at"] = now
            self._write_state(records)
        except Exception as exc:
            self._restore(backup, provider_path, provider_existed)
            return {
                "ok": False,
                "error": str(exc)[:240],
                "rolled_back": True,
                "backup_id": backup.name,
            }
        return {"ok": True, "active_key": active_key, "backup_id": backup.name}

    def save(self, name: str, url: str, *, key: str = "") -> dict:
        existing = self._state()
        stable_key = safe_subscription_key(key)
        previous = next((item for item in existing if item.get("key") == stable_key), None)
        if stable_key and previous is None:
            return {"ok": False, "error": "subscription_not_found"}
        source_url = (url or "").strip() or str((previous or {}).get("url") or "")
        active_before = self._active_key(existing)
        effective_key = stable_key or safe_subscription_key(name)
        provider_path = self.provider_dir / f"agent-{effective_key}.yaml"
        provider_existed = provider_path.is_file()
        result = self.upsert(name, source_url, key_override=stable_key)
        if not result.get("ok"):
            return result
        target = active_before or str(result.get("subscription", {}).get("key") or "")
        normalized = self._normalize_runtime(target)
        if not normalized.get("ok"):
            base_backup = self.backup_root / str(result.get("backup_id") or "")
            if base_backup.is_dir():
                self._restore(base_backup, provider_path, provider_existed)
                if not (base_backup / "codex-subscriptions.json").is_file():
                    try:
                        self.state_path.unlink()
                    except FileNotFoundError:
                        pass
                normalized["rolled_back"] = True
            return normalized
        result.update(normalized)
        return result

    def refresh(self, key: str) -> dict:
        key = safe_subscription_key(key)
        record = next((item for item in self._state() if item.get("key") == key), None)
        if record is None:
            return {"ok": False, "error": "subscription_not_found"}
        return self.save(str(record.get("name") or key), str(record.get("url") or ""), key=key)

    def switch(self, key: str) -> dict:
        started = time.monotonic()
        normalized = self._normalize_runtime(safe_subscription_key(key))
        normalized["duration"] = round(time.monotonic() - started, 2)
        return normalized

    def delete(self, key: str) -> dict:
        key = safe_subscription_key(key)
        before = self._state()
        record = next((item for item in before if item.get("key") == key), None)
        was_active = self._active_key(before) == key
        provider_path = self.provider_dir / f"{(record or {}).get('provider') or ('agent-' + key)}.yaml"
        provider_existed = provider_path.is_file()
        result = super().delete(key)
        if not result.get("ok"):
            return result
        remaining = self._state()
        if was_active and remaining:
            normalized = self._normalize_runtime(str(remaining[0].get("key") or ""))
            if not normalized.get("ok"):
                base_backup = self.backup_root / str(result.get("backup_id") or "")
                if base_backup.is_dir():
                    self._restore(base_backup, provider_path, provider_existed)
                    normalized["rolled_back"] = True
                return normalized
            result.update(normalized)
        elif not remaining:
            result["active_key"] = ""
        return result
