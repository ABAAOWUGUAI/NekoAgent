"""Fail-closed composition of registry-backed runtime role settings."""

from __future__ import annotations

import sqlite3

from bridge_model_registry import runtime_settings_for_role


def runtime_settings_for_role_safe(db_connect, role: str, fallback_settings: dict) -> dict:
    """Resolve one role and preserve a typed fallback when registry reads fail."""

    settings = dict(fallback_settings)
    try:
        with db_connect() as conn:
            return runtime_settings_for_role(conn, role, settings)
    except (sqlite3.Error, ValueError):
        settings.update({"model_role": role, "model_registry_fallback": True})
        return settings


__all__ = ["runtime_settings_for_role_safe"]
