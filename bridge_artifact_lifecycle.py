#!/usr/bin/env python3
"""Artifact retirement and global preview revocation lifecycle mixin."""

from __future__ import annotations

from bridge_migrations import utc_now


def _rows(cursor) -> list[dict]:
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


class ArtifactLifecycleMixin:
    """Requires ``conn``, ``_write``, ``_event`` and preview mutation methods."""

    error_type = ValueError

    def revoke_all_preview_access(self, *, reason: str = "feature_disabled") -> dict:
        now = utc_now()
        with self._write():
            publications = _rows(self.conn.execute(
                """
                SELECT p.id,p.version_id,v.artifact_id FROM preview_publications p
                JOIN artifact_versions v ON v.id=p.version_id
                WHERE p.status='active'
                """,
            ))
            self.conn.execute(
                """
                UPDATE preview_publications SET generation=generation+1,row_version=row_version+1,
                  updated_at=? WHERE status='active'
                """,
                (now,),
            )
            self.conn.execute(
                "UPDATE preview_sessions SET status='revoked',revoked_at=? WHERE status='active'",
                (now,),
            )
            self.conn.execute(
                "UPDATE preview_access_grants SET status='revoked',revoked_at=? WHERE status='issued'",
                (now,),
            )
            for publication in publications:
                self._event(
                    str(publication["artifact_id"]), "preview.access_revoked",
                    version_id=str(publication["version_id"]),
                    publication_id=str(publication["id"]),
                    detail={"reason": str(reason or "feature_disabled")[:100]},
                )
        return {"ok": True, "publication_count": len(publications)}

    def invalidate_version(self, version_id: str, *, reason: str) -> dict:
        now = utc_now()
        with self._write():
            row = self.conn.execute(
                "SELECT artifact_id FROM artifact_versions WHERE id=?",
                (str(version_id),),
            ).fetchone()
            if not row:
                raise self.error_type("artifact_version_not_found")
            publication = self.conn.execute(
                "SELECT row_version,generation,id FROM preview_publications WHERE version_id=? AND status<>'deleted'",
                (str(version_id),),
            ).fetchone()
            if publication:
                self.change_publication(
                    str(publication["id"]), "delete",
                    expected_version=int(publication["row_version"]),
                    expected_generation=int(publication["generation"]),
                )
            self.conn.execute(
                "UPDATE artifact_versions SET deleted_at=?,failure_reason=? WHERE id=? AND deleted_at=''",
                (now, str(reason or "artifact_version_invalid")[:500], str(version_id)),
            )
            self._event(
                str(row["artifact_id"]), "artifact.version_invalidated",
                version_id=str(version_id), detail={"reason": str(reason or "")[:200]},
            )
        return self.get_version(str(version_id), include_storage=True) or {}

    def delete_artifact(self, artifact_id: str, *, expected_version: int) -> dict:
        now = utc_now()
        with self._write():
            row = self.conn.execute(
                "SELECT row_version FROM artifacts WHERE id=?",
                (str(artifact_id),),
            ).fetchone()
            if not row:
                raise self.error_type("artifact_not_found")
            if int(row["row_version"]) != int(expected_version):
                raise self.error_type("artifact_version_conflict")
            publication_ids = [str(item[0]) for item in self.conn.execute(
                """
                SELECT p.id FROM preview_publications p JOIN artifact_versions v ON v.id=p.version_id
                WHERE v.artifact_id=? AND p.status<>'deleted'
                """,
                (str(artifact_id),),
            ).fetchall()]
            for publication_id in publication_ids:
                publication = self.conn.execute(
                    "SELECT row_version,generation FROM preview_publications WHERE id=?",
                    (publication_id,),
                ).fetchone()
                self.change_publication(
                    publication_id, "delete",
                    expected_version=int(publication["row_version"]),
                    expected_generation=int(publication["generation"]),
                )
            self.conn.execute(
                "UPDATE artifact_versions SET deleted_at=? WHERE artifact_id=? AND deleted_at=''",
                (now, str(artifact_id)),
            )
            self.conn.execute(
                """
                UPDATE artifacts SET deleted_at=?,updated_at=?,row_version=row_version+1
                WHERE id=? AND row_version=?
                """,
                (now, now, str(artifact_id), int(expected_version)),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise self.error_type("artifact_version_conflict")
            self._event(str(artifact_id), "artifact.deleted")
        return self.get_artifact(str(artifact_id), include_deleted=True) or {}


__all__ = ["ArtifactLifecycleMixin"]
