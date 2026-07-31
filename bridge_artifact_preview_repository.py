"""Preview publication, one-time grant and session repository mixin."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

from bridge_migrations import utc_now


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat()


def _bounded_expiry(seconds: int, ceiling: str = "", error_type=ValueError) -> str:
    now = datetime.now(timezone.utc)
    candidate = now + timedelta(seconds=max(1, int(seconds)))
    if ceiling:
        limit = _time(ceiling)
        if limit <= now:
            raise error_type("artifact_version_expired")
        candidate = min(candidate, limit)
    return candidate.isoformat()


def _public_publication(row: Mapping[str, object]) -> dict:
    expires_at = str(row.get("preview_expires_at") or "")
    status = str(row.get("status") or "")
    effective = "expired" if status == "active" and _time(expires_at) <= datetime.now(timezone.utc) else status
    return {
        "id": str(row.get("id") or ""),
        "version_id": str(row.get("version_id") or ""),
        "generation": int(row.get("generation") or 0),
        "status": effective,
        "stored_status": status,
        "preview_expires_at": expires_at,
        "version": int(row.get("row_version") or 0),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "stopped_at": str(row.get("stopped_at") or ""),
    }


class ArtifactPreviewRepositoryMixin:
    """Preview-only behavior kept out of the core Artifact repository."""

    def create_publication(self, version_id: str, *, ttl_seconds: int = 86400) -> dict:
        now = utc_now()
        with self._write():
            version = self.conn.execute(
                """
                SELECT v.artifact_id,v.state,v.entrypoint_path,v.retention_expires_at,
                       v.deleted_at,a.deleted_at AS artifact_deleted_at
                FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id
                WHERE v.id=?
                """,
                (str(version_id),),
            ).fetchone()
            if (
                not version
                or str(version["state"]) != "available"
                or str(version["deleted_at"])
                or str(version["artifact_deleted_at"])
                or not str(version["entrypoint_path"])
            ):
                raise self.error_type("artifact_version_not_previewable")
            expires = _bounded_expiry(
                max(300, min(int(ttl_seconds), 30 * 86400)),
                str(version["retention_expires_at"] or ""), self.error_type,
            )
            existing = self.conn.execute(
                "SELECT * FROM preview_publications WHERE version_id=?", (str(version_id),),
            ).fetchone()
            if existing:
                if _time(str(existing["preview_expires_at"])) > _time(expires):
                    self.conn.execute(
                        "UPDATE preview_publications SET preview_expires_at=?,updated_at=?,row_version=row_version+1 WHERE id=?",
                        (expires, now, str(existing["id"])),
                    )
                return self.get_publication(str(existing["id"])) or {}
            publication_id = "publication-" + uuid.uuid4().hex
            self.conn.execute(
                """
                INSERT INTO preview_publications(
                  id,version_id,generation,status,preview_expires_at,row_version,created_at,updated_at
                ) VALUES(?,?,1,'active',?,1,?,?)
                """,
                (publication_id, str(version_id), expires, now, now),
            )
            self._event(
                str(version["artifact_id"]), "preview.published",
                version_id=str(version_id), publication_id=publication_id,
            )
        return self.get_publication(publication_id) or {}

    def get_publication(self, publication_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM preview_publications WHERE id=?", (str(publication_id),),
        ).fetchone()
        return _public_publication(dict(row)) if row else None

    def get_publication_for_version(self, version_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM preview_publications WHERE version_id=?", (str(version_id),),
        ).fetchone()
        return _public_publication(dict(row)) if row else None

    def require_owned_publication(self, publication_id: str, *, owner_id: str) -> dict:
        row = self.conn.execute(
            """
            SELECT p.* FROM preview_publications p
            JOIN artifact_versions v ON v.id=p.version_id
            JOIN artifacts a ON a.id=v.artifact_id
            WHERE p.id=? AND a.owner_id=? AND a.deleted_at=''
            """,
            (str(publication_id), str(owner_id)),
        ).fetchone()
        if not row:
            raise self.error_type("preview_publication_not_found")
        return _public_publication(dict(row))

    def create_grant(self, publication_id: str, *, created_by: str, ttl_seconds: int = 900) -> dict:
        token = secrets.token_urlsafe(32)
        now = utc_now()
        grant_id = "grant-" + uuid.uuid4().hex
        with self._write():
            row = self.conn.execute(
                """
                SELECT p.*,v.state AS version_state,v.deleted_at AS version_deleted_at,
                       v.retention_expires_at,a.owner_id,a.deleted_at AS artifact_deleted_at
                FROM preview_publications p
                JOIN artifact_versions v ON v.id=p.version_id
                JOIN artifacts a ON a.id=v.artifact_id
                WHERE p.id=?
                """,
                (str(publication_id),),
            ).fetchone()
            if not row or str(row["status"]) != "active" or _time(str(row["preview_expires_at"])) <= datetime.now(timezone.utc):
                raise self.error_type("preview_publication_not_active")
            if str(row["owner_id"]) != str(created_by):
                raise self.error_type("preview_publication_not_found")
            if str(row["version_state"]) != "available" or row["version_deleted_at"] or row["artifact_deleted_at"]:
                raise self.error_type("artifact_version_not_available")
            expires = _bounded_expiry(
                max(60, min(int(ttl_seconds), 3600)),
                min(
                    (value for value in (str(row["preview_expires_at"]), str(row["retention_expires_at"] or "")) if value),
                    key=_time,
                ),
                self.error_type,
            )
            self.conn.execute(
                """
                INSERT INTO preview_access_grants(
                  id,publication_id,generation,token_hash,status,created_by,created_at,expires_at
                ) VALUES(?,?,?,?,'issued',?,?,?)
                """,
                (grant_id, str(publication_id), int(row["generation"]), _hash(token), str(created_by), now, expires),
            )
            version = self.conn.execute(
                """
                SELECT v.artifact_id,v.id FROM artifact_versions v
                JOIN preview_publications p ON p.version_id=v.id WHERE p.id=?
                """,
                (str(publication_id),),
            ).fetchone()
            self._event(
                str(version["artifact_id"]), "preview.grant_created",
                version_id=str(version["id"]), publication_id=str(publication_id),
            )
        return {"id": grant_id, "publication_id": str(publication_id), "token": token, "expires_at": expires}

    def issue_challenge(self, raw_token: str, *, ttl_seconds: int = 120) -> dict:
        challenge = secrets.token_urlsafe(24)
        now = utc_now()
        expires = _future(max(30, min(int(ttl_seconds), 300)))
        with self._write():
            row = self.conn.execute(
                "SELECT * FROM preview_access_grants WHERE token_hash=?", (_hash(str(raw_token or "")),),
            ).fetchone()
            self._validate_grant(row)
            self.conn.execute(
                "UPDATE preview_access_grants SET challenge_hash=?,challenge_expires_at=? WHERE id=? AND status='issued'",
                (_hash(challenge), expires, str(row["id"])),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise self.error_type("preview_grant_conflict")
        return {"challenge": challenge, "challenge_expires_at": expires, "grant_id": str(row["id"]), "issued_at": now}

    def _validate_grant(self, row) -> None:
        if not row:
            raise self.error_type("preview_grant_not_found")
        if str(row["status"]) != "issued":
            raise self.error_type("preview_grant_not_active")
        if _time(str(row["expires_at"])) <= datetime.now(timezone.utc):
            raise self.error_type("preview_grant_expired")
        publication = self.conn.execute(
            """
            SELECT p.*,v.state AS version_state,v.deleted_at AS version_deleted_at,
                   v.retention_expires_at,a.deleted_at AS artifact_deleted_at
            FROM preview_publications p
            JOIN artifact_versions v ON v.id=p.version_id
            JOIN artifacts a ON a.id=v.artifact_id
            WHERE p.id=?
            """,
            (str(row["publication_id"]),),
        ).fetchone()
        if not publication or str(publication["status"]) != "active":
            raise self.error_type("preview_publication_not_active")
        if int(publication["generation"]) != int(row["generation"]):
            raise self.error_type("preview_grant_generation_stale")
        if _time(str(publication["preview_expires_at"])) <= datetime.now(timezone.utc):
            raise self.error_type("preview_publication_expired")
        if (
            str(publication["version_state"]) != "available"
            or publication["version_deleted_at"]
            or publication["artifact_deleted_at"]
        ):
            raise self.error_type("artifact_version_not_available")
        retention = str(publication["retention_expires_at"] or "")
        if retention and _time(retention) <= datetime.now(timezone.utc):
            raise self.error_type("artifact_version_expired")

    def activate(self, raw_token: str, challenge: str, *, session_ttl_seconds: int = 1800) -> dict:
        now = utc_now()
        session_token = secrets.token_urlsafe(32)
        session_id = "session-" + uuid.uuid4().hex
        with self._write():
            row = self.conn.execute(
                "SELECT * FROM preview_access_grants WHERE token_hash=?", (_hash(str(raw_token or "")),),
            ).fetchone()
            self._validate_grant(row)
            expected = str(row["challenge_hash"] or "")
            if not expected or not hmac.compare_digest(expected, _hash(str(challenge or ""))):
                raise self.error_type("preview_challenge_invalid")
            if _time(str(row["challenge_expires_at"])) <= datetime.now(timezone.utc):
                raise self.error_type("preview_challenge_expired")
            publication = self.conn.execute(
                """
                SELECT p.*,v.retention_expires_at FROM preview_publications p
                JOIN artifact_versions v ON v.id=p.version_id WHERE p.id=?
                """,
                (str(row["publication_id"]),),
            ).fetchone()
            session_limits = [
                datetime.now(timezone.utc) + timedelta(seconds=max(60, min(int(session_ttl_seconds), 3600))),
                _time(str(publication["preview_expires_at"])),
            ]
            if str(publication["retention_expires_at"] or ""):
                session_limits.append(_time(str(publication["retention_expires_at"])))
            session_expiry = min(session_limits).isoformat()
            self.conn.execute(
                """
                UPDATE preview_access_grants SET status='consumed',consumed_at=?
                WHERE id=? AND status='issued' AND generation=?
                """,
                (now, str(row["id"]), int(publication["generation"])),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise self.error_type("preview_grant_conflict")
            self.conn.execute(
                """
                INSERT INTO preview_sessions(
                  id,publication_id,generation,session_hash,status,created_at,expires_at,last_used_at
                ) VALUES(?,?,?,?, 'active',?,?,?)
                """,
                (
                    session_id, str(publication["id"]), int(publication["generation"]),
                    _hash(session_token), now, session_expiry, now,
                ),
            )
            version = self.conn.execute(
                "SELECT artifact_id,id FROM artifact_versions WHERE id=?", (str(publication["version_id"]),),
            ).fetchone()
            self._event(
                str(version["artifact_id"]), "preview.session_activated",
                version_id=str(version["id"]), publication_id=str(publication["id"]),
                detail={"session_id": session_id},
            )
        return {
            "session": session_token,
            "session_id": session_id,
            "publication_id": str(publication["id"]),
            "generation": int(publication["generation"]),
            "expires_at": session_expiry,
        }

    def authorize(self, raw_session: str, publication_id: str) -> dict:
        row = self.conn.execute(
            """
            SELECT s.*,p.status AS publication_status,p.generation AS publication_generation,
                   p.preview_expires_at,v.id AS version_id,v.storage_key,v.entrypoint_path,
                   v.state AS version_state,v.deleted_at AS version_deleted_at,
                   v.retention_expires_at,a.deleted_at AS artifact_deleted_at
            FROM preview_sessions s
            JOIN preview_publications p ON p.id=s.publication_id
            JOIN artifact_versions v ON v.id=p.version_id
            JOIN artifacts a ON a.id=v.artifact_id
            WHERE s.session_hash=? AND s.publication_id=?
            """,
            (_hash(str(raw_session or "")), str(publication_id)),
        ).fetchone()
        if not row:
            raise self.error_type("preview_session_not_found")
        if str(row["status"]) != "active" or str(row["publication_status"]) != "active":
            raise self.error_type("preview_session_not_active")
        if int(row["generation"]) != int(row["publication_generation"]):
            raise self.error_type("preview_session_generation_stale")
        if _time(str(row["expires_at"])) <= datetime.now(timezone.utc):
            raise self.error_type("preview_session_expired")
        if _time(str(row["preview_expires_at"])) <= datetime.now(timezone.utc):
            raise self.error_type("preview_publication_expired")
        if str(row["retention_expires_at"] or "") and _time(str(row["retention_expires_at"])) <= datetime.now(timezone.utc):
            raise self.error_type("artifact_version_expired")
        if str(row["version_state"]) != "available" or row["version_deleted_at"] or row["artifact_deleted_at"]:
            raise self.error_type("artifact_version_not_available")
        return {
            "publication_id": str(publication_id),
            "generation": int(row["generation"]),
            "version_id": str(row["version_id"]),
            "storage_key": str(row["storage_key"]),
            "entrypoint_path": str(row["entrypoint_path"]),
            "expires_at": str(row["expires_at"]),
        }

    def change_publication(
        self,
        publication_id: str,
        action: str,
        *,
        expected_version: int,
        expected_generation: int,
        ttl_seconds: int = 86400,
    ) -> dict:
        action = str(action or "")
        if action not in {"stop", "restore", "extend", "delete"}:
            raise self.error_type("preview_publication_action_invalid")
        now = utc_now()
        with self._write():
            row = self.conn.execute(
                """
                SELECT p.*,v.retention_expires_at,v.state AS version_state,
                       v.deleted_at AS version_deleted_at,a.deleted_at AS artifact_deleted_at
                FROM preview_publications p
                JOIN artifact_versions v ON v.id=p.version_id
                JOIN artifacts a ON a.id=v.artifact_id
                WHERE p.id=?
                """,
                (str(publication_id),),
            ).fetchone()
            if not row:
                raise self.error_type("preview_publication_not_found")
            if int(row["row_version"]) != int(expected_version) or int(row["generation"]) != int(expected_generation):
                raise self.error_type("preview_publication_version_conflict")
            old_generation = int(row["generation"])
            new_generation = old_generation
            status = str(row["status"])
            expires = str(row["preview_expires_at"])
            stopped_at = str(row["stopped_at"])
            deleted_at = str(row["deleted_at"])
            if action == "stop":
                status, stopped_at, new_generation = "stopped", now, old_generation + 1
            elif action == "restore":
                if status == "deleted":
                    raise self.error_type("preview_publication_deleted")
                if str(row["version_state"]) != "available" or row["version_deleted_at"] or row["artifact_deleted_at"]:
                    raise self.error_type("artifact_version_not_available")
                status, stopped_at, new_generation = "active", "", old_generation + 1
                expires = _bounded_expiry(
                    max(300, min(int(ttl_seconds), 30 * 86400)),
                    str(row["retention_expires_at"] or ""), self.error_type,
                )
            elif action == "extend":
                if status != "active":
                    raise self.error_type("preview_publication_not_active")
                if _time(expires) <= datetime.now(timezone.utc):
                    raise self.error_type("preview_publication_expired_restore_required")
                retention = str(row["retention_expires_at"] or "")
                if retention and _time(retention) <= datetime.now(timezone.utc):
                    raise self.error_type("artifact_version_expired")
                candidate = _time(expires) + timedelta(seconds=max(300, min(int(ttl_seconds), 30 * 86400)))
                if retention:
                    candidate = min(candidate, _time(retention))
                expires = candidate.isoformat()
            else:
                status, deleted_at, stopped_at, new_generation = "deleted", now, now, old_generation + 1
            self.conn.execute(
                """
                UPDATE preview_publications SET generation=?,status=?,preview_expires_at=?,
                  row_version=row_version+1,updated_at=?,stopped_at=?,deleted_at=?
                WHERE id=? AND row_version=? AND generation=?
                """,
                (
                    new_generation, status, expires, now, stopped_at, deleted_at,
                    str(publication_id), int(expected_version), old_generation,
                ),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise self.error_type("preview_publication_version_conflict")
            if new_generation != old_generation:
                self.conn.execute(
                    "UPDATE preview_sessions SET status='revoked',revoked_at=? WHERE publication_id=? AND status='active'",
                    (now, str(publication_id)),
                )
                self.conn.execute(
                    "UPDATE preview_access_grants SET status='revoked',revoked_at=? WHERE publication_id=? AND status='issued'",
                    (now, str(publication_id)),
                )
            version = self.conn.execute(
                """
                SELECT v.artifact_id,v.id FROM artifact_versions v
                JOIN preview_publications p ON p.version_id=v.id WHERE p.id=?
                """,
                (str(publication_id),),
            ).fetchone()
            self._event(
                str(version["artifact_id"]), "preview." + action,
                version_id=str(version["id"]), publication_id=str(publication_id),
                detail={"old_generation": old_generation, "generation": new_generation},
            )
        return self.get_publication(str(publication_id)) or {}


__all__ = ["ArtifactPreviewRepositoryMixin"]
