#!/usr/bin/env python3
"""Authenticated HTTP adapter for the meme asset domain.

The Bridge owns authentication and transport primitives.  This module owns
the meme routes, validation-to-status mapping, and domain calls so adding a
capability does not keep extending the monolithic request handler.
"""

from __future__ import annotations

import hashlib
from typing import Callable
from urllib.parse import unquote

from bridge_meme_discovery import (
    candidate_asset,
    discovery_state,
    review_meme_candidate,
    run_meme_discovery,
)
from bridge_meme_social import (
    list_meme_assets,
    mark_meme_delivery,
    save_uploaded_meme,
    upsert_meme_asset,
)
from bridge_meme_selection import meme_pool_health


class MemeHttpApi:
    """Small domain router called only after the Bridge auth gate."""

    def __init__(
        self,
        db_connect: Callable,
        json_response: Callable,
        binary_response: Callable,
    ) -> None:
        self._db_connect = db_connect
        self._json_response = json_response
        self._binary_response = binary_response

    @staticmethod
    def _int_query(query: dict, name: str, default: int) -> int:
        try:
            return int(query.get(name, [str(default)])[0])
        except (TypeError, ValueError, IndexError):
            return default

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path == "/assistant/memes":
            enabled = (query.get("enabled", [""])[0] or "").strip()
            limit = self._int_query(query, "limit", 80)
            with self._db_connect() as conn:
                result = {
                    "ok": True,
                    "memes": list_meme_assets(conn, enabled=enabled, limit=limit),
                    "health": meme_pool_health(conn),
                }
            self._json_response(request, 200, result)
            return True

        if path == "/assistant/memes/discovery":
            job_limit = self._int_query(query, "job_limit", 20)
            candidate_limit = self._int_query(query, "candidate_limit", 120)
            with self._db_connect() as conn:
                result = discovery_state(conn, job_limit=job_limit, candidate_limit=candidate_limit)
            self._json_response(request, 200, result)
            return True

        if path.startswith("/assistant/memes/discovery/candidate/"):
            filename = unquote(path.rsplit("/", 1)[-1])
            asset = candidate_asset(filename)
            if asset is None:
                self._json_response(request, 404, {"ok": False, "error": "meme_candidate_not_found"})
                return True
            payload, mime = asset
            self._binary_response(
                request,
                200,
                payload,
                mime,
                cache_control="private, max-age=300",
                etag=hashlib.sha256(payload).hexdigest(),
            )
            return True
        return False

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if path == "/assistant/memes":
            try:
                with self._db_connect() as conn:
                    meme = upsert_meme_asset(conn, payload)
                    memes = list_meme_assets(conn)
                    health = meme_pool_health(conn)
            except Exception as exc:
                self._json_response(request, 400, {"ok": False, "error": str(exc)})
                return True
            self._json_response(
                request, 200,
                {"ok": True, "meme": meme, "memes": memes, "health": health},
            )
            return True

        if path == "/assistant/memes/upload":
            try:
                with self._db_connect() as conn:
                    meme = save_uploaded_meme(conn, payload)
                    memes = list_meme_assets(conn)
                    health = meme_pool_health(conn)
            except Exception as exc:
                self._json_response(request, 400, {"ok": False, "error": str(exc)})
                return True
            self._json_response(
                request, 201, {"ok": True, "meme": meme, "memes": memes, "health": health},
            )
            return True

        if path == "/assistant/memes/mark":
            with self._db_connect() as conn:
                history = mark_meme_delivery(
                    conn,
                    str(payload.get("selection_id") or payload.get("id") or "").strip(),
                    status=str(payload.get("status") or "sent").strip(),
                    error=str(payload.get("error") or "").strip(),
                )
            self._json_response(
                request,
                200 if history else 404,
                {"ok": bool(history), "history": history, "error": "" if history else "selection_not_found"},
            )
            return True

        if path == "/assistant/memes/discovery/search":
            try:
                with self._db_connect() as conn:
                    result = run_meme_discovery(conn, payload)
            except Exception as exc:
                self._json_response(request, 400, {"ok": False, "error": str(exc)})
                return True
            # Provider failures are durable discovery jobs, not HTTP transport
            # failures; the client renders the job-level evidence and error.
            self._json_response(request, 200, result)
            return True

        if path == "/assistant/memes/discovery/review":
            try:
                with self._db_connect() as conn:
                    result = review_meme_candidate(conn, payload)
                    result["memes"] = list_meme_assets(conn)
                    result["health"] = meme_pool_health(conn)
            except Exception as exc:
                self._json_response(request, 400, {"ok": False, "error": str(exc)})
                return True
            self._json_response(request, 200, result)
            return True
        return False


__all__ = ["MemeHttpApi"]
