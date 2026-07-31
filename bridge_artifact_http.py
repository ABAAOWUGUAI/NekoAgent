#!/usr/bin/env python3
"""Authenticated Artifact Center HTTP adapter; active content is attachment-only."""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import quote, unquote

from bridge_artifact_cutover import artifact_preview_feature_enabled
from bridge_artifact_repository import ArtifactError, ArtifactRepository


def _error_status(message: str) -> int:
    if "not_found" in message:
        return 404
    if "expired" in message:
        return 410
    if any(marker in message for marker in ("conflict", "disabled", "not_active", "stale")):
        return 409
    if "too_large" in message:
        return 413
    return 400


def _identifier(value: str) -> str:
    result = unquote(str(value or "")).strip()
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,120}", result):
        raise ArtifactError("artifact_identifier_invalid")
    return result


def _attachment_response(request, payload: bytes, content_type: str, filename: str) -> None:
    fallback = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(filename or "artifact"))[:120] or "artifact"
    encoded = quote(str(filename or fallback), safe="")
    request.send_response(200)
    request.send_header("Content-Type", str(content_type or "application/octet-stream"))
    request.send_header("Content-Length", str(len(payload)))
    request.send_header("Content-Disposition", f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}")
    request.send_header("Cache-Control", "private, no-store")
    request.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    request.send_header("Cross-Origin-Resource-Policy", "same-origin")
    request.send_header("X-Content-Type-Options", "nosniff")
    request.send_header("Referrer-Policy", "no-referrer")
    request.send_header("Connection", "close")
    request.end_headers()
    if request.command != "HEAD":
        request.wfile.write(payload)


class ArtifactHttpApi:
    def __init__(
        self,
        assistant_connect: Callable,
        task_connect: Callable,
        json_response: Callable,
        artifact_service,
        *,
        preview_base_url: str,
        revision_task: Callable[[dict, dict], dict],
        cutover_plan: Callable[[], dict],
    ) -> None:
        self._assistant_connect = assistant_connect
        self._task_connect = task_connect
        self._json_response = json_response
        self._service = artifact_service
        self._preview_base_url = str(preview_base_url or "").rstrip("/")
        self._revision_task = revision_task
        self._cutover_plan = cutover_plan

    @staticmethod
    def matches_post(path: str) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["assistant", "artifacts"] and parts[3] in {"revise", "delete"}:
            return True
        return (
            len(parts) == 4 and parts[:2] == ["assistant", "preview-publications"]
            and parts[3] in {"grant", "stop", "restore", "extend", "delete"}
        )

    def _failure(self, request, exc: Exception) -> bool:
        message = str(exc) or type(exc).__name__
        public = message if (
            isinstance(exc, (ArtifactError, ValueError))
            and re.fullmatch(r"[a-z0-9_.:-]{1,200}", message)
        ) else "artifact_internal_error"
        self._json_response(request, _error_status(public), {"ok": False, "error": public})
        return True

    def _require_enabled(self) -> None:
        with self._assistant_connect() as conn:
            if not artifact_preview_feature_enabled(conn):
                raise ArtifactError("artifact_preview_disabled")

    def handle_get(self, request, path: str, query: dict) -> bool:
        if path == "/assistant/artifact/cutover-plan":
            try:
                result = self._cutover_plan()
            except Exception as exc:
                return self._failure(request, exc)
            self._json_response(request, 200, {"ok": True, "result": result})
            return True
        if not path.startswith("/assistant/artifacts"):
            return False
        try:
            self._require_enabled()
            parts = path.strip("/").split("/")
            with self._task_connect() as conn:
                repo = ArtifactRepository(conn)
                if path == "/assistant/artifacts":
                    limit = int(query.get("limit", ["30"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                    result = {"items": repo.list_artifacts(owner_id="admin", limit=limit, offset=offset)}
                elif len(parts) == 3:
                    artifact = repo.get_artifact(_identifier(parts[2]))
                    if not artifact or artifact["owner_id"] != "admin":
                        raise ArtifactError("artifact_not_found")
                    result = {"artifact": artifact}
                elif len(parts) == 4 and parts[3] == "versions":
                    artifact_id = _identifier(parts[2])
                    artifact = repo.get_artifact(artifact_id)
                    if not artifact or artifact["owner_id"] != "admin":
                        raise ArtifactError("artifact_not_found")
                    result = {"items": repo.list_versions(artifact_id)}
                elif len(parts) == 4 and parts[3] == "events":
                    artifact_id = _identifier(parts[2])
                    artifact = repo.get_artifact(artifact_id)
                    if not artifact or artifact["owner_id"] != "admin":
                        raise ArtifactError("artifact_not_found")
                    result = {"items": repo.list_events(artifact_id, limit=int(query.get("limit", ["200"])[0]))}
                elif len(parts) == 5 and parts[2] == "versions" and parts[4] == "download":
                    payload, media, filename = self._service.download_payload(
                        _identifier(parts[3]), owner_id="admin",
                    )
                    _attachment_response(request, payload, media, filename)
                    return True
                else:
                    return False
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, 200, {"ok": True, **result})
        return True

    def handle_post(self, request, path: str, payload: dict) -> bool:
        if not self.matches_post(path):
            return False
        try:
            self._require_enabled()
            parts = path.strip("/").split("/")
            identifier = _identifier(parts[2])
            action = parts[3]
            if parts[1] == "artifacts":
                with self._task_connect() as conn:
                    repo = ArtifactRepository(conn)
                    artifact = repo.get_artifact(identifier)
                    if not artifact or artifact["owner_id"] != "admin":
                        raise ArtifactError("artifact_not_found")
                    if action == "delete":
                        result = self._service.delete_artifact(
                            identifier,
                            expected_version=int(payload.get("expected_version") or 0),
                            owner_id="admin",
                        )
                        status = 200
                    else:
                        result = {"task": self._revision_task(artifact, payload)}
                        status = 202
            else:
                with self._task_connect() as conn:
                    repo = ArtifactRepository(conn)
                    repo.require_owned_publication(identifier, owner_id="admin")
                    if action == "grant":
                        if not self._preview_base_url:
                            raise ArtifactError("artifact_preview_base_url_missing")
                        grant = repo.create_grant(identifier, created_by="admin")
                        result = {
                            "grant": {
                                "id": grant["id"],
                                "publication_id": grant["publication_id"],
                                "expires_at": grant["expires_at"],
                                "activation_url": self._preview_base_url + "/activate/" + grant["token"],
                            },
                        }
                        status = 201
                    else:
                        result = {"publication": repo.change_publication(
                            identifier, action,
                            expected_version=int(payload.get("expected_version") or 0),
                            expected_generation=int(payload.get("expected_generation") or 0),
                            ttl_seconds=int(payload.get("ttl_seconds") or 86400),
                        )}
                        status = 200
        except Exception as exc:
            return self._failure(request, exc)
        self._json_response(request, status, {"ok": True, **result})
        return True


__all__ = ["ArtifactHttpApi"]
