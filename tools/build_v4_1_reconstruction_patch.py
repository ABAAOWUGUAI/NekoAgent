#!/usr/bin/env python3
"""Build a portable, reviewable archive for the V4.1 clean reconstruction patch."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH_FILES = (
    "admin/index.html",
    "admin_console.py",
    "admin/admin-v4-shell.css",
    "admin/v4-shell.js",
    "admin/admin-v4-artifact-surface.css",
    "admin/v4-artifact-surface.js",
    "tests/test_v4_shell_phase1.py",
    "tools/run_v4_artifact_browser_rehearsal.cjs",
    "docs/V4_1_RECONSTRUCTION_IMPLEMENTATION_2026-08-11.md",
    "tools/build_v4_1_reconstruction_patch.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def build(output: Path) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing archive: {output}")
    records: list[dict[str, str]] = []
    prefix = "NekoAgent-V4.1-Reconstruction-Patch-2026-08-11-r5"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in PATCH_FILES:
            source = ROOT / relative
            if not source.is_file():
                raise FileNotFoundError(f"required patch file missing: {relative}")
            archive_name = f"{prefix}/{relative.replace(chr(92), '/') }"
            if "\\" in archive_name:
                raise ValueError(f"non-portable ZIP entry: {archive_name}")
            archive.writestr(archive_name, source.read_bytes())
            records.append({"path": relative.replace("\\", "/"), "sha256": sha256(source)})
        manifest = {
            "schema_version": 1,
            "purpose": "clean local V4.1 Phase 1 correction and Phase 2 Artifact slice; not a deployment artifact",
            "base_revision": source_revision(),
            "entries_use_forward_slashes": True,
            "files": records,
        }
        archive.writestr(f"{prefix}/PATCH_MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        if any("\\" in name for name in names):
            raise AssertionError("archive contains Windows-only path separators")
    return {"output": str(output), "entries": len(records) + 1, "base_revision": source_revision()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT.parents[1] / "release-artifacts" / "NekoAgent-V4.1-Reconstruction-Patch-2026-08-11-r5.zip",
    )
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
