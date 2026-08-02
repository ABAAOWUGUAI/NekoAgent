#!/usr/bin/env python3
"""Immutable Artifact storage, validation, task capture and reconciliation."""

from __future__ import annotations

import base64
import hashlib
import html.parser
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping

from bridge_artifact_repository import ArtifactError, ArtifactRepository


MAX_FILES = 500
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PATH_DEPTH = 12
MAX_PATH_LENGTH = 240
MANIFEST_NAME = ".agent-artifact-manifest.json"
INTERNAL_MANIFEST = ".artifact-internal-manifest.json"

CANONICAL_MEDIA_TYPES = {
    ".wav": "audio/wav",
}

ALLOWED_PREVIEW_MEDIA = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}

ARTIFACT_MANIFEST_INSTRUCTION = """
如果本任务生成了用户需要下载、预览或继续修改的成品，请在工作目录根部创建
.agent-artifact-manifest.json。它必须是 UTF-8 JSON 对象：
{"schema_version":1,"task_id":"由任务指令提供的 ID","generated_at":"ISO-8601 时间","title":"成品名称","kind":"file|report|presentation|image|archive|static_site","summary":"简述","entrypoint":"静态站点入口或空字符串","files":["相对路径"],"preview_days":7}
只列出本任务明确交付的文件；禁止绝对路径、..、符号链接、凭据和项目无关文件。若没有成品，不创建该文件。
""".strip()


class _StaticHtmlAudit(html.parser.HTMLParser):
    blocked_tags = {"base", "iframe", "object", "embed", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self._script_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag in self.blocked_tags:
            self.errors.append("blocked_tag:" + tag)
        if any(key.startswith("on") for key in values):
            self.errors.append("inline_event_handler")
        if tag == "script":
            self._script_depth += 1
            src = values.get("src", "")
            if not src or not _local_reference(src):
                self.errors.append("script_must_be_local_external")
        if tag == "link":
            rel = values.get("rel", "").lower().split()
            href = values.get("href", "")
            if "manifest" in rel:
                self.errors.append("manifest_forbidden")
            if href and not _local_reference(href):
                self.errors.append("external_subresource_forbidden")
        if tag in {"img", "source", "audio", "video"}:
            for key in ("src", "srcset", "poster"):
                value = values.get(key, "")
                if value and not (_local_reference(value) or value.startswith("data:image/")):
                    self.errors.append("external_subresource_forbidden")
        if tag == "a":
            href = values.get("href", "")
            if href and not _local_reference(href):
                self.errors.append("external_anchor_forbidden")
        if tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            self.errors.append("meta_refresh_forbidden")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_depth:
            self._script_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._script_depth and data.strip():
            self.errors.append("inline_script_forbidden")


def _local_reference(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return True
    if value.startswith(("#", "./", "../")):
        return not value.startswith("../")
    return not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value) and not value.startswith("//")


def normalize_relative_path(value: object) -> str:
    raw = str(value or "")
    if not raw or len(raw) > MAX_PATH_LENGTH or "\\" in raw or "\x00" in raw:
        raise ArtifactError("artifact_path_invalid")
    if any(ord(char) < 32 for char in raw) or any(char in raw for char in ("\u2215", "\u2044", "\uff0f")):
        raise ArtifactError("artifact_path_invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactError("artifact_path_invalid")
    if len(path.parts) > MAX_PATH_DEPTH or path.name in {MANIFEST_NAME, INTERNAL_MANIFEST}:
        raise ArtifactError("artifact_path_invalid")
    return str(path)


def _safe_source(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    parts = PurePosixPath(relative_path).parts
    lexical = root
    for part in parts:
        lexical = lexical / part
        if lexical.is_symlink():
            raise ArtifactError("artifact_file_type_forbidden")
    candidate = lexical.resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArtifactError("artifact_path_escape") from exc
    stat = candidate.stat()
    if candidate.is_symlink() or not candidate.is_file() or getattr(stat, "st_nlink", 1) != 1:
        raise ArtifactError("artifact_file_type_forbidden")
    return candidate


def _media_type(path: str, payload: bytes, *, preview: bool) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if preview:
        media = ALLOWED_PREVIEW_MEDIA.get(suffix)
        if not media:
            raise ArtifactError("artifact_preview_media_forbidden")
        if suffix in {".svg", ".xml"}:
            raise ArtifactError("artifact_preview_media_forbidden")
        if suffix in {".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt"}:
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArtifactError("artifact_text_not_utf8") from exc
        if suffix in {".png"} and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ArtifactError("artifact_signature_mismatch")
        if suffix in {".jpg", ".jpeg"} and not payload.startswith(b"\xff\xd8\xff"):
            raise ArtifactError("artifact_signature_mismatch")
        if suffix == ".gif" and not payload.startswith((b"GIF87a", b"GIF89a")):
            raise ArtifactError("artifact_signature_mismatch")
        if suffix == ".webp" and not (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"):
            raise ArtifactError("artifact_signature_mismatch")
        return media
    guessed = CANONICAL_MEDIA_TYPES.get(suffix) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    return guessed + ("; charset=utf-8" if guessed.startswith("text/") else "")


def _audit_preview_text(path: str, payload: bytes) -> None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".html", ".htm"}:
        audit = _StaticHtmlAudit()
        audit.feed(payload.decode("utf-8"))
        if audit.errors:
            raise ArtifactError("artifact_html_unsafe:" + sorted(set(audit.errors))[0])
    elif suffix == ".css":
        text = payload.decode("utf-8")
        if re.search(r"@import\s|url\s*\(\s*['\"]?(?:https?:|//)", text, flags=re.I):
            raise ArtifactError("artifact_css_external_resource")


class ArtifactService:
    def __init__(self, connect: Callable, storage_root: Path) -> None:
        self._connect = connect
        self.root = Path(storage_root).resolve()
        self.staging = self.root / "staging"
        self.published = self.root / "published"
        self.quarantine = self.root / "quarantine"

    def ensure_storage(self) -> None:
        for path in (self.root, self.staging, self.published, self.quarantine):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o750)
            except OSError:
                pass

    def _validate_files(self, source_root: Path, names: Iterable[object], *, preview: bool) -> list[dict]:
        normalized = [normalize_relative_path(value) for value in names]
        if not normalized or len(normalized) > MAX_FILES or len(set(normalized)) != len(normalized):
            raise ArtifactError("artifact_file_set_invalid")
        result: list[dict] = []
        total = 0
        for relative in sorted(normalized):
            source = _safe_source(source_root, relative)
            size = source.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ArtifactError("artifact_file_too_large")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ArtifactError("artifact_total_too_large")
            payload = source.read_bytes()
            media = _media_type(relative, payload, preview=preview)
            if preview:
                _audit_preview_text(relative, payload)
            result.append({
                "relative_path": relative,
                "storage_name": relative,
                "media_type": media,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "payload": payload,
            })
        return result

    def import_from_directory(
        self,
        *,
        source_root: Path,
        owner_id: str,
        origin_assistant_id: str,
        source_goal_id: str,
        source_run_id: str,
        title: str,
        kind: str,
        summary: str,
        file_names: Iterable[object],
        entrypoint: str = "",
        artifact_id: str = "",
        expected_base_version_id: str = "",
        retention_days: int = 30,
        preview_days: int = 7,
    ) -> dict:
        self.ensure_storage()
        preview = str(kind) == "static_site"
        entrypoint = normalize_relative_path(entrypoint) if entrypoint else ""
        files = self._validate_files(Path(source_root), file_names, preview=preview)
        if preview and (not entrypoint or entrypoint not in {item["relative_path"] for item in files}):
            raise ArtifactError("artifact_entrypoint_invalid")
        storage_key = secrets_token()
        retention_expires = (
            datetime.now(timezone.utc) + timedelta(days=max(1, min(int(retention_days), 365)))
        ).isoformat()
        with self._connect() as conn:
            repo = ArtifactRepository(conn)
            if artifact_id:
                artifact = repo.get_artifact(artifact_id)
                if not artifact or artifact["owner_id"] != str(owner_id):
                    raise ArtifactError("artifact_not_found")
                if artifact["kind"] != str(kind):
                    raise ArtifactError("artifact_kind_immutable")
            else:
                artifact = repo.create_artifact(
                    owner_id=owner_id,
                    origin_assistant_id=origin_assistant_id,
                    source_goal_id=source_goal_id,
                    source_run_id=source_run_id,
                    kind=kind,
                    title=title,
                    summary=summary,
                )
            version = repo.create_preparing_version(
                artifact["id"],
                source_run_id=source_run_id,
                storage_key=storage_key,
                entrypoint_path=entrypoint,
                retention_expires_at=retention_expires,
                expected_current_version_id=expected_base_version_id,
            )

        stage = self.staging / (version["id"] + "-" + secrets_token())
        final = self.published / storage_key
        try:
            stage.mkdir(parents=True, exist_ok=False)
            public_files = []
            for item in files:
                target = stage / Path(*PurePosixPath(item["storage_name"]).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item["payload"])
                public_files.append({key: item[key] for key in (
                    "relative_path", "storage_name", "media_type", "size_bytes", "sha256",
                )})
            manifest_payload = {
                "schema_version": 1,
                "artifact_id": artifact["id"],
                "version_id": version["id"],
                "storage_key": storage_key,
                "entrypoint_path": entrypoint,
                "files": public_files,
            }
            manifest_bytes = json.dumps(
                manifest_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            (stage / INTERNAL_MANIFEST).write_bytes(manifest_bytes)
            self._fsync_tree(stage)
            for path in stage.rglob("*"):
                if path.is_file():
                    path.chmod(0o440)
            os.replace(stage, final)
            self._fsync_directory(self.published)
            try:
                final.chmod(0o550)
            except OSError:
                pass
            with self._connect() as conn:
                repo = ArtifactRepository(conn)
                published = repo.publish_version(
                    version["id"],
                    manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                    files=public_files,
                )
                publication = repo.create_publication(
                    published["id"], ttl_seconds=max(300, min(int(preview_days), 30) * 86400),
                ) if preview else None
                return {
                    "artifact": repo.get_artifact(artifact["id"]),
                    "version": published,
                    "publication": publication,
                }
        except Exception as exc:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if not final.exists():
                with self._connect() as conn:
                    try:
                        ArtifactRepository(conn).fail_version(version["id"], str(exc))
                    except Exception:
                        pass
            raise

    @staticmethod
    def _fsync_tree(root: Path) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())
        if os.name != "nt":
            for directory in sorted(
                (path for path in root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts), reverse=True,
            ):
                ArtifactService._fsync_directory(directory)
            ArtifactService._fsync_directory(root)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def capture_task_manifest(self, task: Mapping[str, object], *, origin_assistant_id: str) -> dict | None:
        root = Path(str(task.get("cwd") or "")).resolve()
        manifest_path = root / MANIFEST_NAME
        if not manifest_path.exists():
            return None
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 65536:
            raise ArtifactError("artifact_manifest_invalid")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactError("artifact_manifest_invalid") from exc
        if not isinstance(manifest, dict) or int(manifest.get("schema_version") or 0) != 1:
            raise ArtifactError("artifact_manifest_invalid")
        allowed = {
            "schema_version", "title", "kind", "summary", "entrypoint", "files",
            "artifact_id", "retention_days", "preview_days", "task_id", "generated_at",
        }
        if set(manifest) - allowed or not isinstance(manifest.get("files"), list):
            raise ArtifactError("artifact_manifest_invalid")
        if str(task.get("sandbox") or "") != "workspace-write":
            raise ArtifactError("artifact_manifest_sandbox_forbidden")
        if str(manifest.get("task_id") or "") != str(task.get("id") or ""):
            raise ArtifactError("artifact_manifest_task_mismatch")
        generated_at = str(manifest.get("generated_at") or "")
        try:
            generated = datetime.fromisoformat(generated_at)
            created = datetime.fromisoformat(str(task.get("created_at") or ""))
        except ValueError as exc:
            raise ArtifactError("artifact_manifest_time_invalid") from exc
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if generated.astimezone(timezone.utc) < created.astimezone(timezone.utc) or generated.astimezone(timezone.utc) > now + timedelta(minutes=5):
            raise ArtifactError("artifact_manifest_stale")
        expected_artifact_id = str(task.get("artifact_revision_id") or "").strip()
        expected_base_version_id = str(
            task.get("artifact_revision_base_version_id") or ""
        ).strip()
        manifest_artifact_id = str(manifest.get("artifact_id") or "").strip()
        if expected_artifact_id and manifest_artifact_id not in {"", expected_artifact_id}:
            raise ArtifactError("artifact_revision_target_mismatch")
        result = self.import_from_directory(
            source_root=root,
            owner_id=str(task.get("user_id") or "admin"),
            origin_assistant_id=str(origin_assistant_id or ""),
            source_goal_id=str(task.get("goal_id") or ""),
            source_run_id=str(task.get("run_id") or ""),
            title=str(manifest.get("title") or task.get("summary") or "未命名成品"),
            kind=str(manifest.get("kind") or "file"),
            summary=str(manifest.get("summary") or ""),
            file_names=manifest["files"],
            entrypoint=str(manifest.get("entrypoint") or ""),
            artifact_id=expected_artifact_id or manifest_artifact_id,
            expected_base_version_id=expected_base_version_id,
            retention_days=int(manifest.get("retention_days") or 30),
            preview_days=int(manifest.get("preview_days") or 7),
        )
        consumed = root / (MANIFEST_NAME + ".consumed")
        os.replace(manifest_path, consumed)
        return result

    def file_payload(self, version_id: str, relative_path: str, *, owner_id: str) -> tuple[bytes, str, str]:
        relative = normalize_relative_path(relative_path)
        with self._connect() as conn:
            repo = ArtifactRepository(conn)
            version = repo.require_accessible_version(version_id, owner_id=owner_id, include_storage=True)
            row = next((item for item in version["files"] if item["relative_path"] == relative), None)
            if not row:
                raise ArtifactError("artifact_file_not_found")
            root = (self.published / version["storage_key"]).resolve()
            path = (root / Path(*PurePosixPath(str(row["storage_name"])).parts)).resolve(strict=True)
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ArtifactError("artifact_path_escape") from exc
            payload = path.read_bytes()
            if len(payload) != int(row["size_bytes"]) or hashlib.sha256(payload).hexdigest() != row["sha256"]:
                raise ArtifactError("artifact_file_integrity_failed")
            return payload, str(row["media_type"]), relative

    def download_payload(self, version_id: str, *, owner_id: str) -> tuple[bytes, str, str]:
        with self._connect() as conn:
            version = ArtifactRepository(conn).require_accessible_version(
                version_id, owner_id=owner_id, include_storage=True,
            )
        if len(version["files"]) == 1:
            item = version["files"][0]
            payload, media, name = self.file_payload(version_id, str(item["relative_path"]), owner_id=owner_id)
            result = (payload, media, PurePosixPath(name).name)
            self._record_download(version, owner_id=owner_id)
            return result
        with tempfile.SpooledTemporaryFile(max_size=MAX_TOTAL_BYTES + 1024) as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for item in sorted(version["files"], key=lambda value: value["relative_path"]):
                    payload, _, name = self.file_payload(
                        version_id, str(item["relative_path"]), owner_id=owner_id,
                    )
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100440 << 16
                    archive.writestr(info, payload)
            stream.seek(0)
            result = (stream.read(), "application/zip", f"artifact-{version_id}.zip")
        self._record_download(version, owner_id=owner_id)
        return result

    def _record_download(self, version: Mapping[str, object], *, owner_id: str) -> None:
        with self._connect() as conn:
            ArtifactRepository(conn).record_event(
                str(version["artifact_id"]), "artifact.downloaded",
                version_id=str(version["id"]), detail={"actor": str(owner_id)},
            )

    def materialize_version(self, version_id: str, target_root: Path, *, owner_id: str) -> dict:
        """Create a writable revision workspace from verified immutable bytes."""
        target = Path(target_root).resolve()
        if target.exists():
            raise ArtifactError("artifact_revision_workspace_exists")
        with self._connect() as conn:
            version = ArtifactRepository(conn).require_accessible_version(version_id, owner_id=owner_id)
        target.mkdir(parents=True, exist_ok=False)
        try:
            for item in version["files"]:
                payload, _, relative = self.file_payload(
                    version_id, str(item["relative_path"]), owner_id=owner_id,
                )
                destination = target / Path(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
            return version
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def delete_artifact(self, artifact_id: str, *, expected_version: int, owner_id: str) -> dict:
        """Deny access transactionally, then move immutable bytes out of the served tree."""
        self.ensure_storage()
        with self._connect() as conn:
            repo = ArtifactRepository(conn)
            artifact = repo.get_artifact(artifact_id)
            if not artifact or artifact["owner_id"] != str(owner_id):
                raise ArtifactError("artifact_not_found")
            versions = repo.list_versions(artifact_id, include_storage=True)
            deleted = repo.delete_artifact(artifact_id, expected_version=expected_version)
        moved: list[str] = []
        failed: list[str] = []
        for version in versions:
            source = self.published / str(version.get("storage_key") or "")
            if not source.exists():
                continue
            target = self.quarantine / ("deleted-" + str(version["id"]))
            if target.exists():
                target = self.quarantine / ("deleted-" + str(version["id"]) + "-" + secrets_token())
            try:
                os.replace(source, target)
                moved.append(str(version["id"]))
            except OSError:
                failed.append(str(version["id"]))
        return {"artifact": deleted, "storage": {"quarantined": moved, "failed": failed}}

    def _verify_published_tree(self, version: Mapping[str, object], root: Path) -> list[dict]:
        if root.is_symlink() or not root.is_dir():
            raise ArtifactError("artifact_storage_invalid")
        manifest_path = root / INTERNAL_MANIFEST
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ArtifactError("artifact_internal_manifest_missing")
        manifest_bytes = manifest_path.read_bytes()
        if str(version.get("manifest_sha256") or "") and hashlib.sha256(manifest_bytes).hexdigest() != str(version["manifest_sha256"]):
            raise ArtifactError("artifact_internal_manifest_hash_mismatch")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactError("artifact_internal_manifest_invalid") from exc
        if manifest.get("version_id") != version.get("id") or manifest.get("storage_key") != version.get("storage_key"):
            raise ArtifactError("artifact_reconcile_manifest_mismatch")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ArtifactError("artifact_internal_manifest_invalid")
        expected_names: set[str] = set()
        expected_relatives: set[str] = set()
        verified: list[dict] = []
        for raw in files:
            if not isinstance(raw, dict):
                raise ArtifactError("artifact_internal_manifest_invalid")
            relative = normalize_relative_path(raw.get("relative_path"))
            storage_name = normalize_relative_path(raw.get("storage_name"))
            if relative in expected_relatives or storage_name in expected_names:
                raise ArtifactError("artifact_internal_manifest_invalid")
            expected_relatives.add(relative)
            expected_names.add(storage_name)
            path = (root / Path(*PurePosixPath(storage_name).parts)).resolve(strict=True)
            try:
                path.relative_to(root.resolve())
            except ValueError as exc:
                raise ArtifactError("artifact_path_escape") from exc
            if path.is_symlink() or not path.is_file():
                raise ArtifactError("artifact_file_type_forbidden")
            payload = path.read_bytes()
            if len(payload) != int(raw.get("size_bytes") or -1) or hashlib.sha256(payload).hexdigest() != str(raw.get("sha256") or ""):
                raise ArtifactError("artifact_file_integrity_failed")
            verified.append({
                "relative_path": relative,
                "storage_name": storage_name,
                "media_type": str(raw.get("media_type") or "application/octet-stream"),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        actual_names: set[str] = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ArtifactError("artifact_file_type_forbidden")
            if path.is_file() and path.name != INTERNAL_MANIFEST:
                actual_names.add(path.relative_to(root).as_posix())
        if actual_names != expected_names:
            raise ArtifactError("artifact_file_set_mismatch")
        database_files = version.get("files") or []
        if database_files:
            expected_database = {
                (str(item["relative_path"]), str(item["storage_name"]), int(item["size_bytes"]), str(item["sha256"]))
                for item in database_files
            }
            actual_database = {
                (item["relative_path"], item["storage_name"], item["size_bytes"], item["sha256"])
                for item in verified
            }
            if actual_database != expected_database:
                raise ArtifactError("artifact_database_manifest_mismatch")
        return verified

    def _quarantine_tree(self, root: Path, label: str) -> None:
        if not root.exists():
            return
        target = self.quarantine / (str(label) + "-" + root.name)
        if target.exists():
            target = self.quarantine / (str(label) + "-" + root.name + "-" + secrets_token())
        os.replace(root, target)

    def reconcile(self, *, grace_seconds: int = 300) -> dict:
        self.ensure_storage()
        recovered: list[str] = []
        failed: list[str] = []
        quarantined: list[str] = []
        with self._connect() as conn:
            repo = ArtifactRepository(conn)
            known = {item["storage_key"]: item for item in repo.list_storage_versions()}
        for storage_key, version in known.items():
            final = self.published / storage_key
            retention = str(version.get("retention_expires_at") or "")
            expired = bool(retention and datetime.fromisoformat(retention).astimezone(timezone.utc) <= datetime.now(timezone.utc))
            if version["deleted_at"] or expired:
                if expired and not version["deleted_at"]:
                    with self._connect() as conn:
                        ArtifactRepository(conn).invalidate_version(version["id"], reason="artifact_retention_expired")
                if final.exists():
                    self._quarantine_tree(final, "retired")
                    quarantined.append(storage_key)
                continue
            if version["state"] == "preparing":
                if final.exists():
                    try:
                        data = (final / INTERNAL_MANIFEST).read_bytes()
                        verified = self._verify_published_tree(version, final)
                        with self._connect() as conn:
                            ArtifactRepository(conn).publish_version(
                                version["id"], manifest_sha256=hashlib.sha256(data).hexdigest(), files=verified,
                            )
                        recovered.append(version["id"])
                    except Exception as exc:
                        with self._connect() as conn:
                            ArtifactRepository(conn).fail_version(version["id"], str(exc))
                        self._quarantine_tree(final, "invalid")
                        quarantined.append(storage_key)
                        failed.append(version["id"])
                else:
                    try:
                        created = datetime.fromisoformat(str(version.get("created_at") or ""))
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                    except ValueError:
                        created = datetime.min.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
                    if age >= max(0, int(grace_seconds)):
                        with self._connect() as conn:
                            ArtifactRepository(conn).fail_version(
                                version["id"], "artifact_preparing_storage_missing",
                            )
                        failed.append(version["id"])
            elif version["state"] == "available":
                try:
                    if not final.exists():
                        raise ArtifactError("artifact_storage_missing")
                    self._verify_published_tree(version, final)
                except Exception as exc:
                    with self._connect() as conn:
                        ArtifactRepository(conn).invalidate_version(version["id"], reason=str(exc))
                    if final.exists():
                        self._quarantine_tree(final, "invalid")
                        quarantined.append(storage_key)
                    failed.append(version["id"])
        for final in self.published.iterdir():
            if final.is_dir() and final.name not in known:
                target = self.quarantine / (final.name + "-orphan")
                if target.exists():
                    target = self.quarantine / (final.name + "-orphan-" + secrets_token())
                os.replace(final, target)
                quarantined.append(final.name)
        return {"ok": not failed, "recovered": recovered, "failed": failed, "quarantined": quarantined}


def secrets_token() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=").lower()


__all__ = [
    "ALLOWED_PREVIEW_MEDIA", "ARTIFACT_MANIFEST_INSTRUCTION", "ArtifactService",
    "INTERNAL_MANIFEST", "MANIFEST_NAME", "normalize_relative_path",
]
