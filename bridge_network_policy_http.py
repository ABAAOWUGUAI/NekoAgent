#!/usr/bin/env python3
"""Admin HTTP adapter for the network access policy."""

from __future__ import annotations

from typing import Callable

from bridge_network_policy import (
    get_network_policy,
    list_network_policy_events,
    set_network_policy,
)


class NetworkPolicyHttpApi:
    PATH = "/assistant/network-policy"

    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @classmethod
    def matches_post(cls, path: str) -> bool:
        return path == cls.PATH

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path != self.PATH:
            return False
        try:
            limit = int(query.get("limit", ["20"])[0])
            with self._db_connect() as conn:
                policy = get_network_policy(conn)
                events = list_network_policy_events(conn, limit=limit)
        except Exception as exc:
            self._json_response(
                request,
                400,
                {"ok": False, "error": str(exc) or type(exc).__name__},
            )
            return True
        self._json_response(
            request,
            200,
            {"ok": True, "policy": policy, "events": events},
        )
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path != self.PATH:
            return False
        try:
            with self._db_connect() as conn:
                policy = set_network_policy(
                    conn,
                    base_mode=payload.get("base_mode"),
                    owner_web_search_enabled=(
                        payload.get("owner_web_search_enabled")
                        if "owner_web_search_enabled" in payload
                        else None
                    ),
                    ttl_minutes=payload.get("ttl_minutes"),
                    expected_version=payload.get("version"),
                    actor_ref="owner",
                    channel="web",
                )
                events = list_network_policy_events(conn, limit=20)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            status = 409 if error.endswith("_conflict") else 400
            self._json_response(
                request,
                status,
                {"ok": False, "error": error},
            )
            return True
        self._json_response(
            request,
            200,
            {"ok": True, "policy": policy, "events": events},
        )
        return True


__all__ = ["NetworkPolicyHttpApi"]
