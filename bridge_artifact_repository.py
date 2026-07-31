"""Artifact, immutable version and preview authorization repository."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Mapping

from bridge_artifact_schema import require_artifact_schema
from bridge_artifact_lifecycle import ArtifactLifecycleMixin
from bridge_artifact_preview_repository import ArtifactPreviewRepositoryMixin
from bridge_migrations import utc_after, utc_now


ARTIFACT_KINDS = {"file", "report", "presentation", "image", "archive", "static_site"}


class ArtifactError(ValueError):
    """Stable Artifact domain error."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _public_artifact(row: Mapping[str, object]) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "owner_id": str(row.get("owner_id") or ""),
        "origin_assistant_id": str(row.get("origin_assistant_id") or ""),
        "source_goal_id": str(row.get("source_goal_id") or ""),
        "source_run_id": str(row.get("source_run_id") or ""),
        "kind": str(row.get("kind") or "file"),
        "title": str(row.get("title") or ""),
        "summary": str(row.get("summary") or ""),
        "current_version_id": str(row.get("current_version_id") or ""),
        "version": int(row.get("row_version") or 0),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "deleted_at": str(row.get("deleted_at") or ""),
    }


def _public_version(row: Mapping[str, object]) -> dict:
    state = str(row.get("state") or "")
    deleted_at = str(row.get("deleted_at") or "")
    retention = str(row.get("retention_expires_at") or "")
    if deleted_at:
        state = "deleted"
    elif state == "available" and retention and _time(retention) <= datetime.now(timezone.utc):
        state = "expired"
    return {
        "id": str(row.get("id") or ""),
        "artifact_id": str(row.get("artifact_id") or ""),
        "version_number": int(row.get("version_number") or 0),
        "source_run_id": str(row.get("source_run_id") or ""),
        "entrypoint_path": str(row.get("entrypoint_path") or ""),
        "manifest_sha256": str(row.get("manifest_sha256") or ""),
        "file_count": int(row.get("file_count") or 0),
        "total_bytes": int(row.get("total_bytes") or 0),
        "state": state,
        "retention_expires_at": retention,
        "failure_reason": str(row.get("failure_reason") or ""),
        "created_at": str(row.get("created_at") or ""),
        "published_at": str(row.get("published_at") or ""),
        "deleted_at": deleted_at,
    }


class ArtifactRepository(ArtifactLifecycleMixin, ArtifactPreviewRepositoryMixin):
    error_type = ArtifactError
    def __init__(self, conn: sqlite3.Connection, *, validate_schema: bool = True) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        if validate_schema:
            require_artifact_schema(self.conn)

    @contextmanager
    def _write(self):
        if self.conn.in_transaction:
            name = "artifact_" + uuid.uuid4().hex
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
                raise
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _event(
        self,
        artifact_id: str,
        event_type: str,
        *,
        version_id: str = "",
        publication_id: str = "",
        detail: Mapping[str, object] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO artifact_events(
              artifact_id,version_id,publication_id,event_type,detail_json,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (artifact_id, version_id, publication_id, event_type, _json(dict(detail or {})), utc_now()),
        )

    def record_event(
        self,
        artifact_id: str,
        event_type: str,
        *,
        version_id: str = "",
        publication_id: str = "",
        detail: Mapping[str, object] | None = None,
    ) -> None:
        with self._write():
            self._event(
                str(artifact_id), str(event_type), version_id=str(version_id),
                publication_id=str(publication_id), detail=detail,
            )

    def create_artifact(
        self,
        *,
        owner_id: str,
        origin_assistant_id: str,
        source_goal_id: str,
        source_run_id: str,
        kind: str,
        title: str,
        summary: str = "",
    ) -> dict:
        kind = str(kind or "").strip()
        title = str(title or "").strip()
        owner_id = str(owner_id or "").strip()
        if kind not in ARTIFACT_KINDS:
            raise ArtifactError("artifact_kind_invalid")
        if not owner_id or not title or len(title) > 240:
            raise ArtifactError("artifact_identity_invalid")
        artifact_id = "artifact-" + uuid.uuid4().hex
        now = utc_now()
        with self._write():
            self.conn.execute(
                """
                INSERT INTO artifacts(
                  id,owner_id,origin_assistant_id,source_goal_id,source_run_id,kind,title,
                  summary,current_version_id,row_version,created_at,updated_at,deleted_at
                ) VALUES(?,?,?,?,?,?,?,?,NULL,1,?,?, '')
                """,
                (
                    artifact_id, owner_id, str(origin_assistant_id or ""), str(source_goal_id or ""),
                    str(source_run_id or ""), kind, title, str(summary or "")[:2000], now, now,
                ),
            )
            self._event(artifact_id, "artifact.created")
        return self.get_artifact(artifact_id) or {}

    def get_artifact(self, artifact_id: str, *, include_deleted: bool = False) -> dict | None:
        clause = "" if include_deleted else " AND deleted_at=''"
        rows = _rows(self.conn.execute(f"SELECT * FROM artifacts WHERE id=?{clause}", (str(artifact_id),)))
        if not rows:
            return None
        item = _public_artifact(rows[0])
        if item["current_version_id"]:
            version = self.get_version(item["current_version_id"], include_storage=False)
            item["current_version"] = version
            if version:
                item["publication"] = self.get_publication_for_version(version["id"])
        return item

    def list_artifacts(self, *, owner_id: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        params: list[object] = []
        where = "WHERE deleted_at=''"
        if owner_id:
            where += " AND owner_id=?"
            params.append(str(owner_id))
        params.extend([max(1, min(int(limit or 50), 100)), max(0, int(offset or 0))])
        rows = _rows(self.conn.execute(
            f"SELECT * FROM artifacts {where} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ))
        return [self.get_artifact(str(row["id"])) or _public_artifact(row) for row in rows]

    def create_preparing_version(
        self,
        artifact_id: str,
        *,
        source_run_id: str,
        storage_key: str,
        entrypoint_path: str,
        retention_expires_at: str = "",
        expected_current_version_id: str = "",
    ) -> dict:
        now = utc_now()
        version_id = "av-" + uuid.uuid4().hex
        with self._write():
            artifact = self.conn.execute(
                "SELECT * FROM artifacts WHERE id=? AND deleted_at=''", (str(artifact_id),),
            ).fetchone()
            if not artifact:
                raise ArtifactError("artifact_not_found")
            expected_current_version_id = str(expected_current_version_id or "")
            if (
                expected_current_version_id
                and str(artifact["current_version_id"] or "") != expected_current_version_id
            ):
                raise ArtifactError("artifact_revision_base_conflict")
            if expected_current_version_id:
                preparing = self.conn.execute(
                    """
                    SELECT id FROM artifact_versions
                    WHERE artifact_id=? AND state='preparing' AND deleted_at=''
                    LIMIT 1
                    """,
                    (str(artifact_id),),
                ).fetchone()
                if preparing:
                    raise ArtifactError("artifact_revision_in_progress")
            number = int(self.conn.execute(
                "SELECT coalesce(max(version_number),0)+1 FROM artifact_versions WHERE artifact_id=?",
                (str(artifact_id),),
            ).fetchone()[0])
            self.conn.execute(
                """
                INSERT INTO artifact_versions(
                  id,artifact_id,version_number,source_run_id,storage_key,entrypoint_path,
                  state,retention_expires_at,created_at
                ) VALUES(?,?,?,?,?,?,'preparing',?,?)
                """,
                (
                    version_id, str(artifact_id), number, str(source_run_id or ""), str(storage_key),
                    str(entrypoint_path or ""), str(retention_expires_at or ""), now,
                ),
            )
            self._event(str(artifact_id), "artifact.version_preparing", version_id=version_id)
        return self.get_version(version_id, include_storage=True) or {}

    def publish_version(
        self,
        version_id: str,
        *,
        manifest_sha256: str,
        files: Iterable[Mapping[str, object]],
    ) -> dict:
        file_items = [dict(item) for item in files]
        now = utc_now()
        with self._write():
            row = self.conn.execute("SELECT * FROM artifact_versions WHERE id=?", (str(version_id),)).fetchone()
            if not row:
                raise ArtifactError("artifact_version_not_found")
            if str(row["state"]) == "available":
                if str(row["manifest_sha256"]) != str(manifest_sha256):
                    raise ArtifactError("artifact_version_immutable")
                return self.get_version(str(version_id), include_storage=True) or {}
            if str(row["state"]) != "preparing":
                raise ArtifactError("artifact_version_not_preparing")
            total = sum(int(item.get("size_bytes") or 0) for item in file_items)
            for item in file_items:
                self.conn.execute(
                    """
                    INSERT INTO artifact_version_files(
                      version_id,relative_path,storage_name,media_type,size_bytes,sha256
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        str(version_id), str(item["relative_path"]), str(item["storage_name"]),
                        str(item["media_type"]), int(item["size_bytes"]), str(item["sha256"]),
                    ),
                )
            self.conn.execute(
                """
                UPDATE artifact_versions SET manifest_sha256=?,file_count=?,total_bytes=?,
                  state='available',published_at=?,failure_reason=''
                WHERE id=? AND state='preparing'
                """,
                (str(manifest_sha256), len(file_items), total, now, str(version_id)),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ArtifactError("artifact_version_publish_conflict")
            artifact_id = str(row["artifact_id"])
            current = self.conn.execute("SELECT row_version,updated_at FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if not current:
                raise ArtifactError("artifact_not_found")
            updated_at = utc_after(str(current["updated_at"]))
            self.conn.execute(
                """
                UPDATE artifacts SET current_version_id=?,row_version=row_version+1,updated_at=?
                WHERE id=? AND row_version=? AND EXISTS (
                  SELECT 1 FROM artifact_versions WHERE id=? AND artifact_id=? AND state='available'
                )
                """,
                (str(version_id), updated_at, artifact_id, int(current["row_version"]), str(version_id), artifact_id),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ArtifactError("artifact_current_version_conflict")
            self._event(artifact_id, "artifact.version_published", version_id=str(version_id), detail={
                "manifest_sha256": str(manifest_sha256), "file_count": len(file_items), "total_bytes": total,
            })
        return self.get_version(str(version_id), include_storage=True) or {}

    def fail_version(self, version_id: str, reason: str) -> dict:
        now = utc_now()
        with self._write():
            row = self.conn.execute("SELECT artifact_id FROM artifact_versions WHERE id=?", (str(version_id),)).fetchone()
            if not row:
                raise ArtifactError("artifact_version_not_found")
            artifact_id = str(row[0])
            self.conn.execute(
                "UPDATE artifact_versions SET state='failed',failure_reason=? WHERE id=? AND state<>'available'",
                (str(reason or "artifact_version_failed")[:500], str(version_id)),
            )
            self._event(artifact_id, "artifact.version_failed", version_id=str(version_id), detail={"reason": str(reason)[:200]})
            self.conn.execute("UPDATE artifacts SET updated_at=?,row_version=row_version+1 WHERE id=?", (now, artifact_id))
        return self.get_version(str(version_id), include_storage=True) or {}

    def get_version(self, version_id: str, *, include_storage: bool = False) -> dict | None:
        rows = _rows(self.conn.execute("SELECT * FROM artifact_versions WHERE id=?", (str(version_id),)))
        if not rows:
            return None
        item = _public_version(rows[0])
        if include_storage:
            item["storage_key"] = str(rows[0].get("storage_key") or "")
        item["files"] = _rows(self.conn.execute(
            """
            SELECT relative_path,storage_name,media_type,size_bytes,sha256
            FROM artifact_version_files WHERE version_id=? ORDER BY relative_path
            """,
            (str(version_id),),
        ))
        return item

    def require_accessible_version(
        self,
        version_id: str,
        *,
        owner_id: str = "",
        include_storage: bool = False,
    ) -> dict:
        row = self.conn.execute(
            """
            SELECT v.*,a.owner_id,a.deleted_at AS artifact_deleted_at
            FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id
            WHERE v.id=?
            """,
            (str(version_id),),
        ).fetchone()
        if not row or (owner_id and str(row["owner_id"]) != str(owner_id)):
            raise ArtifactError("artifact_version_not_found")
        if (
            str(row["state"]) != "available"
            or str(row["deleted_at"])
            or str(row["artifact_deleted_at"])
        ):
            raise ArtifactError("artifact_version_not_available")
        retention = str(row["retention_expires_at"] or "")
        if retention and _time(retention) <= datetime.now(timezone.utc):
            raise ArtifactError("artifact_version_expired")
        return self.get_version(str(version_id), include_storage=include_storage) or {}

    def list_versions(self, artifact_id: str, *, include_storage: bool = False) -> list[dict]:
        rows = _rows(self.conn.execute(
            "SELECT id FROM artifact_versions WHERE artifact_id=? ORDER BY version_number DESC", (str(artifact_id),),
        ))
        return [self.get_version(str(row["id"]), include_storage=include_storage) or {} for row in rows]

    def list_storage_versions(self) -> list[dict]:
        """Return every version for filesystem reconciliation, including deleted artifacts."""
        rows = _rows(self.conn.execute(
            "SELECT id FROM artifact_versions ORDER BY artifact_id,version_number",
        ))
        return [self.get_version(str(row["id"]), include_storage=True) or {} for row in rows]

    def list_events(self, artifact_id: str, *, limit: int = 200) -> list[dict]:
        rows = _rows(self.conn.execute(
            """
            SELECT id,artifact_id,version_id,publication_id,event_type,detail_json,created_at
            FROM artifact_events WHERE artifact_id=? ORDER BY id DESC LIMIT ?
            """,
            (str(artifact_id), max(1, min(int(limit or 200), 500))),
        ))
        for row in rows:
            try:
                row["detail"] = json.loads(str(row.pop("detail_json") or "{}"))
            except json.JSONDecodeError:
                row["detail"] = {}
        return rows


__all__ = ["ARTIFACT_KINDS", "ArtifactError", "ArtifactRepository"]
