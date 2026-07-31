#!/usr/bin/env python3
"""Verified SQLite backup helpers used by production migration gates."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Mapping


def sqlite_integrity(path: Path) -> str:
    path = path.resolve(strict=True)
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite_database(source: Path, backup_root: Path, change_id: str) -> Path:
    """Create one verified backup with SQLite's online backup API."""

    source = source.resolve(strict=True)
    backup_root = backup_root.resolve()
    if not source.is_file() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}", change_id or ""):
        raise ValueError("invalid_database_backup_request")
    if sqlite_integrity(source).lower() != "ok":
        raise sqlite3.DatabaseError("source_integrity_check_failed")
    destination_dir = backup_root / change_id
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination = destination_dir / source.name
    try:
        with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(destination))) as dst:
            src.backup(dst)
        os.chmod(destination, 0o600)
        if sqlite_integrity(destination).lower() != "ok":
            raise sqlite3.DatabaseError("backup_integrity_check_failed")
    except Exception:
        if destination.exists():
            destination.unlink()
        try:
            destination_dir.rmdir()
        except OSError:
            pass
        raise
    return destination


def backup_sqlite_bundle(
    sources: Mapping[str, Path],
    backup_root: Path,
    change_id: str,
) -> dict[str, Path]:
    """Back up multiple databases into one all-verified change directory."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}", change_id or ""):
        raise ValueError("invalid_database_backup_request")
    resolved: dict[str, Path] = {}
    for label, source in sources.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", label or ""):
            raise ValueError("invalid_database_backup_label")
        path = source.resolve(strict=True)
        if not path.is_file() or sqlite_integrity(path).lower() != "ok":
            raise sqlite3.DatabaseError(f"source_integrity_check_failed:{label}")
        resolved[label] = path
    destination_dir = backup_root.resolve() / change_id
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    destinations: dict[str, Path] = {}
    try:
        used_names: set[str] = set()
        for label, source in resolved.items():
            name = source.name
            if name in used_names:
                name = f"{label}-{name}"
            used_names.add(name)
            destination = destination_dir / name
            with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(destination))) as dst:
                src.backup(dst)
            os.chmod(destination, 0o600)
            if sqlite_integrity(destination).lower() != "ok":
                raise sqlite3.DatabaseError(f"backup_integrity_check_failed:{label}")
            destinations[label] = destination
    except Exception:
        for path in destination_dir.glob("*"):
            if path.is_file():
                path.unlink()
        try:
            destination_dir.rmdir()
        except OSError:
            pass
        raise
    return destinations
