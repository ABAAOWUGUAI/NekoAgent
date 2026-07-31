#!/usr/bin/env python3
"""Small Bridge runtime helpers for Gate C1 access enforcement."""

from __future__ import annotations

import sqlite3

from bridge_migrations import MigrationError
from bridge_qq_access_service import check_qq_access, get_qq_access_settings


def diagnostic_access_snapshot(connect) -> tuple[dict, list[str]]:
    try:
        with connect() as conn:
            access = get_qq_access_settings(conn)
    except (sqlite3.Error, MigrationError, ValueError):
        return {}, []
    principals = [
        *(access.get("administrators") or []),
        *(access.get("private_allowlist") or []),
    ]
    allowed_ids = sorted(
        {
            str(item.get("qq_id") or "").strip()
            for item in principals
            if str(item.get("qq_id") or "").strip()
        },
    )
    return access, allowed_ids


def super_admin_ids(connect) -> set[str]:
    access, _ = diagnostic_access_snapshot(connect)
    return {
        str(item.get("qq_id") or "").strip()
        for item in access.get("administrators") or []
        if item.get("enabled") and item.get("role") == "super_admin"
    }


def channel_runtime_enabled(connect) -> bool:
    access, _ = diagnostic_access_snapshot(connect)
    settings = access.get("settings") or {}
    return bool(access.get("feature_enabled") and settings.get("channel_enabled"))


def private_access(connect, sender_id: str, action: str) -> dict:
    with connect() as conn:
        return check_qq_access(
            conn,
            {
                "sender_id": sender_id,
                "event_type": "private",
                "requested_action": action,
            },
        )


def private_access_http_error(connect, sender_id: str, action: str):
    try:
        access = private_access(connect, sender_id, action)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if access.get("allowed"):
        return None
    return 403, {
        "ok": False,
        "error": "qq_access_denied",
        "reason": access.get("reason"),
        "config_version": access.get("config_version"),
    }


def group_access(connect, sender_id: str, group_id: str) -> dict:
    with connect() as conn:
        return check_qq_access(
            conn,
            {
                "sender_id": sender_id,
                "event_type": "group",
                "group_id": group_id,
                "requested_action": "group_message",
            },
        )


__all__ = [
    "channel_runtime_enabled",
    "diagnostic_access_snapshot",
    "group_access",
    "private_access",
    "private_access_http_error",
    "super_admin_ids",
]
