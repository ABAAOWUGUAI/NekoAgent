#!/usr/bin/env python3
"""HTTP adapter for the Owner-only body-free evidence collection bundle."""

from __future__ import annotations

from typing import Callable

from bridge_behavior_evidence_collection_runtime import (
    EVIDENCE_COLLECTION_CUTOVER_PATH,
    BehaviorEvidenceCollectionError,
    behavior_evidence_collection_plan,
    set_behavior_evidence_collection_cutover,
)


class BehaviorEvidenceCollectionHttpApi:
    """Checksum-bound control plane; Bridge authentication stays outside it.

    The QQ channel receives no route allow-list entry for this path.  This
    adapter cannot enable optimizer, delivery, Canary, Stable, or any formal
    write because its payload owns only the evidence collection bundle.
    """

    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    @staticmethod
    def matches_post(path: str) -> bool:
        return path == EVIDENCE_COLLECTION_CUTOVER_PATH

    def handle_get(self, request, path: str) -> bool:
        if path != EVIDENCE_COLLECTION_CUTOVER_PATH:
            return False
        try:
            with self._db_connect() as conn:
                result = behavior_evidence_collection_plan(conn)
        except Exception as exc:
            self._json_response(request, 503, {"ok": False, "error": str(exc) or type(exc).__name__})
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path != EVIDENCE_COLLECTION_CUTOVER_PATH:
            return False
        try:
            if not isinstance(payload, dict) or set(payload) != {"enabled", "plan_checksum"}:
                raise BehaviorEvidenceCollectionError("behavior_evidence_collection_cutover_payload_invalid")
            with self._db_connect() as conn:
                result = set_behavior_evidence_collection_cutover(
                    conn,
                    enabled=payload["enabled"],
                    expect_plan_checksum=payload["plan_checksum"],
                )
        except BehaviorEvidenceCollectionError as exc:
            error = str(exc) or "behavior_evidence_collection_cutover_invalid"
            status = 409 if error.startswith("stale_") or error.startswith("behavior_evidence_collection_prerequisite_required") else 400
            self._json_response(request, status, {"ok": False, "error": error})
            return True
        except Exception as exc:
            self._json_response(request, 503, {"ok": False, "error": str(exc) or type(exc).__name__})
            return True
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["BehaviorEvidenceCollectionHttpApi"]
