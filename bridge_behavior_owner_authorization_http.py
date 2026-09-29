#!/usr/bin/env python3
"""Owner-authenticated HTTP surface for the body-free authorization registry."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from typing import Callable

from bridge_assistant_identity import current_assistant
from bridge_behavior_owner_authorization import (
    BehaviorOwnerAuthorizationError,
    behavior_owner_authorization_plan,
    create_behavior_owner_authorization,
    require_behavior_owner_authorization_schema,
    revoke_behavior_owner_authorization,
)
from bridge_behavior_owner_authorization_schema import BEHAVIOR_OWNER_AUTHORIZATION_TABLE


AUTHORIZATION_BASE_PATH = "/assistant/behavior-growth/authorizations"
AUTHORIZATION_PLAN_PATH = AUTHORIZATION_BASE_PATH + "/plan"
AUTHORIZATION_CREATE_PATH = AUTHORIZATION_BASE_PATH + "/create"
AUTHORIZATION_REVOKE_PATH = AUTHORIZATION_BASE_PATH + "/revoke"
_POST_PATHS = frozenset(
    {AUTHORIZATION_PLAN_PATH, AUTHORIZATION_CREATE_PATH, AUTHORIZATION_REVOKE_PATH},
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_string(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _binding_has_strict_scalar_types(binding: object) -> bool:
    if type(binding) is not dict:
        return False
    for field, value in binding.items():
        if type(field) is not str:
            return False
        if field == "scope_refs":
            if type(value) is not list or not value or any(not _strict_string(item) for item in value):
                return False
        elif not _strict_string(value):
            return False
    return True


def _row(cursor) -> dict | None:
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in cursor.description]
    return dict(row) if hasattr(row, "keys") else dict(zip(columns, row))


class BehaviorOwnerAuthorizationHttpApi:
    """Thin Owner control-plane adapter; authentication remains in Bridge."""

    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path in _POST_PATHS

    @staticmethod
    def _active_context(conn) -> tuple[str, str]:
        assistant = current_assistant(conn) or {}
        assistant_id = str(assistant.get("id") or "").strip()
        owner_actor_id = str(assistant.get("owner_actor_id") or "").strip()
        if not assistant_id or not owner_actor_id:
            raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_assistant_unavailable")
        owner_digest = hashlib.sha256(owner_actor_id.encode("utf-8")).hexdigest()
        return assistant_id, "owner:sha256-" + owner_digest

    @staticmethod
    def _binding_for_current_assistant(binding: object, assistant_id: str) -> dict:
        if not _binding_has_strict_scalar_types(binding):
            raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_payload_invalid")
        value = dict(binding)
        if value.get("assistant_id") != assistant_id:
            raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_assistant_mismatch")
        return value

    @staticmethod
    def _project(conn, record: Mapping[str, object], *, now: str) -> dict:
        binding = record.get("binding")
        if binding is None:
            binding = json.loads(str(record.get("binding_json") or ""))
        if not isinstance(binding, Mapping):
            raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_projection_invalid")
        plan = behavior_owner_authorization_plan(
            conn,
            purpose=record.get("purpose"),
            binding=dict(binding),
            now=now,
        )
        state = str(record.get("state") or "")
        expires_at = str(record.get("expires_at") or "")
        if state == "active" and expires_at and expires_at <= now:
            state = "expired"
        projected = {
            "authorization_ref": str(record.get("authorization_ref") or ""),
            "assistant_id": str(record.get("assistant_id") or ""),
            "purpose": str(record.get("purpose") or ""),
            "binding_hash": str(record.get("binding_hash") or ""),
            "state": state,
            "issued_at": str(record.get("issued_at") or ""),
            "expires_at": expires_at,
            "revoked_at": record.get("revoked_at"),
            "consumed_at": record.get("consumed_at"),
            "plan_checksum": str(plan["plan_checksum"]),
        }
        fingerprint = binding.get("public_key_fingerprint")
        if _strict_string(fingerprint):
            projected["public_key_fingerprint"] = str(fingerprint)
        scopes = binding.get("scope_refs")
        if isinstance(scopes, list):
            projected["scope_count"] = len(scopes)
        return projected

    def _failure(self, request, exc: Exception) -> bool:
        if isinstance(exc, BehaviorOwnerAuthorizationError):
            error = str(exc) or "behavior_owner_authorization_invalid"
            if "not_found" in error:
                status = 404
            elif error == "behavior_owner_authorization_assistant_mismatch":
                status = 400
            elif "stale" in error or "not_active" in error or "mismatch" in error:
                status = 409
            else:
                status = 400
            self._json_response(request, status, {"ok": False, "error": error})
            return True
        self._json_response(
            request,
            503,
            {"ok": False, "error": "behavior_owner_authorization_unavailable"},
        )
        return True

    def handle_get(self, request, path: str) -> bool:
        if path != AUTHORIZATION_BASE_PATH:
            return False
        timestamp = _now()
        try:
            with self._db_connect() as conn:
                require_behavior_owner_authorization_schema(conn)
                assistant_id, _owner_ref = self._active_context(conn)
                cursor = conn.execute(
                    f"""SELECT authorization_ref,assistant_id,purpose,binding_json,binding_hash,state,
                               issued_at,expires_at,revoked_at,consumed_at
                          FROM {BEHAVIOR_OWNER_AUTHORIZATION_TABLE}
                         WHERE assistant_id=? ORDER BY issued_at DESC,authorization_ref DESC LIMIT 100""",
                    (assistant_id,),
                )
                rows = []
                while True:
                    record = _row(cursor)
                    if record is None:
                        break
                    rows.append(self._project(conn, record, now=timestamp))
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": {"items": rows}})
        return True

    def handle_post(self, request, path: str, payload: object) -> bool:
        if path not in _POST_PATHS:
            return False
        try:
            if type(payload) is not dict:
                raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_payload_invalid")
            expected = {
                AUTHORIZATION_PLAN_PATH: {"purpose", "binding"},
                AUTHORIZATION_CREATE_PATH: {"purpose", "binding", "plan_checksum", "expires_at"},
                AUTHORIZATION_REVOKE_PATH: {"authorization_ref", "plan_checksum"},
            }[path]
            if set(payload) != expected:
                raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_payload_invalid")
            timestamp = _now()
            with self._db_connect() as conn:
                assistant_id, owner_ref = self._active_context(conn)
                if path in {AUTHORIZATION_PLAN_PATH, AUTHORIZATION_CREATE_PATH}:
                    if not _strict_string(payload["purpose"]):
                        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_payload_invalid")
                    binding = self._binding_for_current_assistant(payload["binding"], assistant_id)
                    if path == AUTHORIZATION_PLAN_PATH:
                        plan = behavior_owner_authorization_plan(
                            conn,
                            purpose=payload["purpose"],
                            binding=binding,
                            now=timestamp,
                        )
                        result = {
                            "purpose": plan["purpose"],
                            "assistant_id": plan["assistant_id"],
                            "binding_hash": plan["binding_hash"],
                            "planned_at": plan["planned_at"],
                            "plan_checksum": plan["plan_checksum"],
                            "preconditions": {"state": "ready", "reason": "ready"},
                        }
                        fingerprint = binding.get("public_key_fingerprint")
                        if _strict_string(fingerprint):
                            result["public_key_fingerprint"] = fingerprint
                        if isinstance(binding.get("scope_refs"), list):
                            result["scope_count"] = len(binding["scope_refs"])
                    else:
                        if not _strict_string(payload["plan_checksum"]) or not _strict_string(payload["expires_at"]):
                            raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_payload_invalid")
                        record = create_behavior_owner_authorization(
                            conn,
                            purpose=payload["purpose"],
                            binding=binding,
                            plan_checksum=payload["plan_checksum"],
                            issuer_ref=owner_ref,
                            expires_at=payload["expires_at"],
                            now=timestamp,
                        )
                        result = self._project(conn, record, now=timestamp)
                else:
                    if not _strict_string(payload["authorization_ref"]) or not _strict_string(payload["plan_checksum"]):
                        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_payload_invalid")
                    record = _row(
                        conn.execute(
                            f"SELECT * FROM {BEHAVIOR_OWNER_AUTHORIZATION_TABLE} WHERE authorization_ref=? AND assistant_id=?",
                            (payload["authorization_ref"], assistant_id),
                        ),
                    )
                    if record is None:
                        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_not_found")
                    projected = self._project(conn, record, now=timestamp)
                    if payload["plan_checksum"] != projected["plan_checksum"]:
                        raise BehaviorOwnerAuthorizationError("behavior_owner_authorization_plan_checksum_stale")
                    revoked = revoke_behavior_owner_authorization(
                        conn,
                        authorization_ref=payload["authorization_ref"],
                        assistant_id=assistant_id,
                        issuer_ref=owner_ref,
                        now=timestamp,
                    )
                    result = self._project(conn, revoked, now=timestamp)
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = [
    "AUTHORIZATION_BASE_PATH",
    "AUTHORIZATION_PLAN_PATH",
    "AUTHORIZATION_CREATE_PATH",
    "AUTHORIZATION_REVOKE_PATH",
    "BehaviorOwnerAuthorizationHttpApi",
]
