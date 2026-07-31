#!/usr/bin/env python3
"""Admin-only HTTP adapter for curated shared knowledge."""

from __future__ import annotations

from typing import Callable
from urllib.parse import unquote

from bridge_continuity_service import list_memory_candidates, review_memory_candidate
from bridge_knowledge_service import (
    create_knowledge,
    create_relation,
    get_knowledge_lints,
    get_knowledge_revisions,
    get_knowledge_workspace,
    get_retrieval_audit,
    list_knowledge,
    list_relations,
    promote_memory,
    review_knowledge,
    update_knowledge,
)


class KnowledgeHttpApi:
    def __init__(self, db_connect: Callable, json_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response

    def matches_post(self, path: str) -> bool:
        return (
            path in {"/assistant/knowledge", "/assistant/knowledge/relations"}
            or (path.startswith("/assistant/knowledge/") and path.endswith(("/review", "/edit")))
            or (path.startswith("/assistant/memories/") and path.endswith("/promote"))
            or (path.startswith("/assistant/memory-candidates/") and path.endswith("/review"))
        )

    def _failure(self, request, exc: Exception) -> bool:
        message = str(exc) or type(exc).__name__
        status = 404 if message.endswith("not_found") else 409 if message.endswith("conflict") else 400
        self._json_response(request, status, {"ok": False, "error": message})
        return True

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path == "/assistant/memory-candidates":
            try:
                status = str(query.get("status", ["pending"])[0] or "").strip()
                limit = int(query.get("limit", ["100"])[0])
                with self._db_connect() as conn:
                    result = list_memory_candidates(conn, status=status, limit=limit)
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, "result": result})
            return True
        if path == "/assistant/knowledge/workspace":
            try:
                with self._db_connect() as conn:
                    result = get_knowledge_workspace(conn)
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, "result": result})
            return True
        if path == "/assistant/knowledge/revisions":
            try:
                item_id = str(query.get("item_id", [""])[0] or "").strip()
                limit = int(query.get("limit", ["50"])[0])
                with self._db_connect() as conn:
                    result = get_knowledge_revisions(conn, item_id=item_id, limit=limit)
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, "result": result})
            return True
        if path == "/assistant/knowledge/retrieval-audit":
            try:
                limit = int(query.get("limit", ["50"])[0])
                with self._db_connect() as conn:
                    result = get_retrieval_audit(conn, limit=limit)
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, "result": result})
            return True
        if path == "/assistant/knowledge/lint":
            try:
                limit = int(query.get("limit", ["30"])[0])
                with self._db_connect() as conn:
                    result = get_knowledge_lints(conn, limit=limit)
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, "result": result})
            return True
        if path == "/assistant/knowledge/relations":
            try:
                item_id = str(query.get("item_id", [""])[0] or "").strip()
                with self._db_connect() as conn:
                    result = list_relations(conn, item_id=item_id)
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, "result": result})
            return True
        if path != "/assistant/knowledge":
            return False
        try:
            status = str(query.get("status", [""])[0] or "").strip()
            kind = str(query.get("kind", [""])[0] or "").strip()
            search = str(query.get("q", [""])[0] or "").strip()
            limit = int(query.get("limit", ["100"])[0])
            with self._db_connect() as conn:
                result = list_knowledge(conn, status=status, kind=kind, query=search, limit=limit)
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if not self.matches_post(path):
            return False
        try:
            with self._db_connect() as conn:
                if path == "/assistant/knowledge":
                    result = create_knowledge(conn, payload)
                elif path == "/assistant/knowledge/relations":
                    result = create_relation(conn, payload)
                elif path.startswith("/assistant/memory-candidates/"):
                    candidate_id = unquote(path.split("/")[3]).strip()
                    result = review_memory_candidate(conn, candidate_id, payload)
                elif path.startswith("/assistant/memories/"):
                    memory_id = unquote(path.split("/")[3]).strip()
                    result = promote_memory(conn, memory_id, payload)
                else:
                    item_id = unquote(path.split("/")[3]).strip()
                    result = (
                        review_knowledge(conn, item_id, payload)
                        if path.endswith("/review")
                        else update_knowledge(conn, item_id, payload)
                    )
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, "result": result})
        return True


__all__ = ["KnowledgeHttpApi"]
