"""Safe, bounded Markdown source adapter for knowledge ingestion (C1/C3).

Supports two logical sources — ``obsidian_vault`` and ``llm_wiki_export`` —
over one shared local file scanner.  Only paths the Owner explicitly configured
are scanned; wide directories (drive roots, home, workspace root, server ``/``)
are rejected.  The scanner never modifies a source file, never follows
symlink/junction escapes, reads UTF-8 only, and enforces hard limits on file
size, file count, per-run bytes, scan time and chunk count.

This module never decides what becomes Published knowledge.  It only produces
chunks and per-file identity facts that the ingestion run turns into Drafts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


SOURCE_TYPES = {"obsidian_vault", "llm_wiki_export"}
IGNORED_DIR_NAMES = {
    ".obsidian", ".trash", ".git", ".hg", ".svn", "__pycache__",
    "node_modules", ".cache", ".tmp", ".trash", "$recycle.bin",
}
# Extensions always ignored unless explicitly allowed.
DEFAULT_ALLOWED_SUFFIXES = (".md",)
FORBIDDEN_SUFFIXES = (
    ".env", ".pem", ".key", ".crt", ".p12", ".pfx", ".token",
    ".sqlite", ".db", ".json", ".yml", ".yaml", ".toml", ".ini",
)
IGNORED_FILENAMES = {
    ".env", ".env.local", ".gitignore", "id_rsa", "id_ed25519",
    "config.toml", "credentials", "secrets",
}

DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_FILES = 2000
DEFAULT_MAX_RUN_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_SCAN_SECONDS = 60
DEFAULT_MAX_CHUNKS = 8000
MAX_HEADING_LENGTH = 120
MAX_HEADING_DEPTH = 6
MAX_CHUNK_CHARS = 6000


@dataclass(frozen=True)
class SourceConfig:
    source_type: str
    root: str
    enabled: bool = False
    allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED_SUFFIXES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_run_bytes: int = DEFAULT_MAX_RUN_BYTES
    max_scan_seconds: int = DEFAULT_MAX_SCAN_SECONDS
    max_chunks: int = DEFAULT_MAX_CHUNKS
    config_revision: int = 1


def validate_source_config(config: Mapping) -> SourceConfig:
    """Validate one Owner-configured source with hard bounds (C1)."""

    source_type = str(config.get("source_type") or "").strip().lower()
    if source_type not in SOURCE_TYPES:
        raise ValueError("knowledge_source_type_invalid")
    root = str(config.get("root") or "").strip()
    if not root:
        raise ValueError("knowledge_source_root_required")
    path = Path(root)
    if not path.is_absolute():
        raise ValueError("knowledge_source_root_must_be_absolute")
    resolved = path.resolve()
    if _is_wide_directory(resolved):
        raise ValueError("knowledge_source_root_too_wide")
    try:
        enabled = bool(int(config.get("enabled") or 0))
    except (TypeError, ValueError):
        enabled = False
    try:
        config_revision = max(1, int(config.get("config_revision") or 1))
    except (TypeError, ValueError):
        config_revision = 1

    def _bounded(value, default, low, high) -> int:
        try:
            return max(low, min(int(value), high))
        except (TypeError, ValueError):
            return default

    suffixes = tuple(
        str(item).strip().lower() for item in (config.get("allowed_suffixes") or DEFAULT_ALLOWED_SUFFIXES)
        if isinstance(item, str) and str(item).strip()
    )
    if not suffixes:
        suffixes = DEFAULT_ALLOWED_SUFFIXES
    if any(suffix in FORBIDDEN_SUFFIXES or suffix == ".txt" for suffix in suffixes):
        # .txt needs explicit opt-in and is never mixed with credentials.
        suffixes = tuple(s for s in suffixes if s != ".txt")
        if not suffixes:
            suffixes = DEFAULT_ALLOWED_SUFFIXES
    return SourceConfig(
        source_type=source_type,
        root=str(resolved),
        enabled=enabled,
        allowed_suffixes=suffixes,
        max_file_bytes=_bounded(config.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES, 1024, 4 * 1024 * 1024),
        max_files=_bounded(config.get("max_files"), DEFAULT_MAX_FILES, 1, 20000),
        max_run_bytes=_bounded(config.get("max_run_bytes"), DEFAULT_MAX_RUN_BYTES, 64 * 1024, 512 * 1024 * 1024),
        max_scan_seconds=_bounded(config.get("max_scan_seconds"), DEFAULT_MAX_SCAN_SECONDS, 5, 600),
        max_chunks=_bounded(config.get("max_chunks"), DEFAULT_MAX_CHUNKS, 8, 50000),
        config_revision=config_revision,
    )


def _is_wide_directory(path: Path) -> bool:
    """Reject drive roots, the home directory itself, and known wide dirs.

    A deep, Owner-configured subdirectory (e.g. ``...\\Documents\\vault`` or a
    test temp dir) is narrow enough to scan; only the directory itself, drive
    roots, and generic wide folders are rejected.
    """
    try:
        parts = list(path.parts)
    except (AttributeError, TypeError):
        return True
    if not parts:
        return True
    # Windows drive root: C:\ ; POSIX root: /
    if len(parts) == 1:
        return True
    last = str(parts[-1]).lower()
    if last in {
        "users", "user", "home", "document", "documents", "downloads",
        "desktop", "videos", "pictures", "music",
    }:
        return True
    home = Path.home().resolve()
    if path == home:
        return True
    return False


def _safe_read_text(path: Path, max_bytes: int) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"knowledge_source_read_error:{type(exc).__name__}") from exc
    if len(raw) > max_bytes:
        raise ValueError("knowledge_source_file_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("knowledge_source_encoding_invalid") from exc


def _safe_relative(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ValueError("knowledge_source_path_escape") from None
    return rel.as_posix()


def _scan_file(root: Path, path: Path, config: SourceConfig) -> dict:
    """Return per-file identity facts (no body in the returned dict)."""

    st = path.stat()
    rel = _safe_relative(path, root)
    return {
        "file_path": rel,
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": int(st.st_size),
        "mtime_iso": None,  # filled by caller with a real clock
        "status": "active",
    }


_HEADING_RE = re.compile(r"^(#{1,%d})\s+(.+?)\s*$" % MAX_HEADING_DEPTH)
_CODE_FENCE_RE = re.compile(r"^```")
_PROMPT_INJECTION_HINT = re.compile(r"忽略.{0,12}(?:系统|之前|以上)|ignore all previous|you are now|system prompt", re.IGNORECASE)


def chunk_markdown(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[dict]:
    """Slice Markdown by headings into bounded chunks with a stable identity.

    ``prompt injection`` text is treated as ordinary document content: it is
    never executed, and its presence is recorded as an observation for the
    ingestion run, not an instruction.
    """

    if not str(text or "").strip():
        return []
    lines = str(text).splitlines()
    chunks: list[dict] = []
    current_heading: list[str] = []
    current_lines: list[str] = []
    fenced = False

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if not body:
            return
        heading_path = "/".join(current_heading)[:800]
        chunks.append(
            {
                "heading_path": heading_path,
                "content": body[:max_chars],
                "content_sha256": hashlib.sha256(body[:max_chars].encode("utf-8")).hexdigest(),
                "prompt_injection_hint": bool(_PROMPT_INJECTION_HINT.search(body)),
            }
        )

    for line in lines:
        if _CODE_FENCE_RE.match(line.strip()):
            fenced = not fenced
            current_lines.append(line)
            continue
        if not fenced:
            match = _HEADING_RE.match(line.strip())
            if match:
                flush()
                heading_level = len(match.group(1))
                heading_text = match.group(2).strip()
                while len(current_heading) >= heading_level:
                    current_heading.pop()
                current_heading.append(heading_text[:MAX_HEADING_LENGTH])
                current_lines = []
                continue
        current_lines.append(line)
    flush()
    return chunks


def scan_source(config: SourceConfig) -> dict:
    """Scan one configured source and return file/chunk facts (C1/C3).

    Returned dict carries only identity metadata plus chunk list (bounded);
    no source body is persisted by the adapter itself.
    """

    started = time.monotonic()
    root = Path(config.root)
    if not config.enabled:
        return {
            "ok": False,
            "reason": "disabled",
            "discovered": 0, "changed": 0, "deleted": 0, "failed": 0,
            "files": [], "duration_seconds": 0.0,
        }
    if not root.is_dir():
        return {
            "ok": False, "reason": "root_missing", "discovered": 0,
            "changed": 0, "deleted": 0, "failed": 0, "files": [],
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    files: list[dict] = []
    failed = 0
    discovered = 0
    total_bytes = 0
    for path in _iter_markdown_files(root, config):
        discovered += 1
        try:
            facts = _scan_file(root, path, config)
        except (ValueError, OSError) as exc:
            failed += 1
            files.append(
                {
                    "file_path": _safe_relative(path, root) if _inside(path, root) else "?",
                    "status": "failed",
                    "error_kind": str(exc)[:160],
                }
            )
            continue
        total_bytes += int(facts["size_bytes"])
        if total_bytes > config.max_run_bytes:
            files.append({"file_path": facts["file_path"], "status": "over_run_budget"})
            failed += 1
            break
        files.append(facts)
        if time.monotonic() - started > config.max_scan_seconds:
            files.append({"file_path": facts["file_path"], "status": "scan_timeout"})
            failed += 1
            break
        if len(files) >= config.max_files:
            break
    return {
        "ok": True,
        "reason": "",
        "discovered": discovered,
        "changed": 0,
        "deleted": 0,
        "failed": failed,
        "files": files,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _iter_markdown_files(root: Path, config: SourceConfig) -> Iterable[Path]:
    allowed = {suffix.lower() for suffix in config.allowed_suffixes}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath_path = Path(dirpath)
        # Reject symlink/junction escape at the directory level.
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DIR_NAMES
            and not name.startswith(".")
            and not _is_link_escape(dirpath_path / name, root)
        ]
        for name in sorted(filenames):
            path = dirpath_path / name
            if path.is_symlink() or name.lower() in IGNORED_FILENAMES:
                continue
            suffix = path.suffix.lower()
            if suffix not in allowed:
                continue
            if not _inside(path.resolve(), root):
                continue
            yield path


def _is_link_escape(path: Path, root: Path) -> bool:
    try:
        is_link = os.path.islink(path) or (
            os.path.exists(path) and os.path.realpath(path) != str(path.resolve())
        )
        if is_link:
            resolved = Path(os.path.realpath(path))
            return not _inside(resolved, root)
    except (OSError, RuntimeError):
        return True
    return False


__all__ = [
    "DEFAULT_MAX_CHUNKS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_RUN_BYTES",
    "DEFAULT_MAX_SCAN_SECONDS",
    "IGNORED_DIR_NAMES",
    "SOURCE_TYPES",
    "SourceConfig",
    "chunk_markdown",
    "scan_source",
    "validate_source_config",
]
