#!/usr/bin/env python3
"""Build a portable, reviewable archive for the current V4.1 Foundation slices."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PREFIX = "NekoAgent-V4.1-Foundation-Slices-2026-08-11-r2"
FOUNDATION_FILES = (
    "admin/index.html",
    "admin_console.py",
    "admin/core.js",
    "admin/admin-v4-shell.css",
    "admin/v4-shell.js",
    "admin/admin-v4-artifact-surface.css",
    "admin/v4-artifact-surface.js",
    "admin/admin-v4-ai-chat-surface.css",
    "admin/v4-ai-chat-surface.js",
    "admin/views-workbench.js",
    "bridge_inbound_idempotency.py",
    "codex_qq_bridge.py",
    "tests/test_reliability_regression_public.py",
    "tests/test_v4_shell_phase1.py",
    "tests/test_v4_ai_chat_slice.py",
    "tests/test_v4_foundation_hardening.py",
    "tools/run_public_tests.py",
    "tools/run_v4_artifact_browser_rehearsal.cjs",
    "tools/run_v4_ai_chat_browser_rehearsal.cjs",
    "tools/run_v4_workbench_dispatch_browser_rehearsal.cjs",
    "docs/V4_1_RECONSTRUCTION_IMPLEMENTATION_2026-08-11.md",
    "docs/V4_1_AI_CHAT_FIRST_SLICE_CONTRACT_2026-08-11.md",
    "docs/V4_1_AI_CHAT_FIRST_SLICE_REVIEW_2026-08-11.md",
    "docs/V4_FOUNDATION_RELIABILITY_HARDENING_2026-08-11.md",
    "tools/build_v4_1_reconstruction_patch.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_provenance() -> dict[str, object]:
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
            encoding="utf-8",
        )
        if Path(root.stdout.strip()).resolve() != ROOT.resolve():
            return {
                "git_checkout_available": False,
                "git_base_revision": None,
                "working_tree_clean": None,
            }
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
            encoding="utf-8",
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {
            "git_checkout_available": False,
            "git_base_revision": None,
            "working_tree_clean": None,
        }
    return {
        "git_checkout_available": True,
        "git_base_revision": result.stdout.strip(),
        "working_tree_clean": not bool(status.stdout.strip()),
    }


def content_manifest_sha256(records: list[dict[str, str]]) -> str:
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_bytes(relative: str, provenance: dict[str, object]) -> bytes:
    revision = provenance.get("git_base_revision")
    if provenance.get("git_checkout_available") and provenance.get("working_tree_clean") and isinstance(revision, str):
        try:
            return subprocess.run(
                ["git", "show", f"{revision}:{relative}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"tracked_source_read_failed:{relative}") from exc
    return (ROOT / relative).read_bytes()


def build(output: Path) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing archive: {output}")
    provenance = source_provenance()
    records: list[dict[str, str]] = []
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in FOUNDATION_FILES:
            source = ROOT / relative
            if not source.is_file():
                raise FileNotFoundError(f"required patch file missing: {relative}")
            archive_name = f"{PACKAGE_PREFIX}/{relative.replace(chr(92), '/') }"
            if "\\" in archive_name:
                raise ValueError(f"non-portable ZIP entry: {archive_name}")
            contents = source_bytes(relative, provenance)
            archive.writestr(archive_name, contents)
            records.append({
                "path": relative.replace("\\", "/"),
                "sha256": hashlib.sha256(contents).hexdigest(),
            })
        manifest = {
            "schema_version": 1,
            "purpose": "current V4.1 Foundation shell, Artifact and AI Chat slices; not a deployment artifact",
            **provenance,
            "content_manifest_sha256": content_manifest_sha256(records),
            "entries_use_forward_slashes": True,
            "files": records,
        }
        archive.writestr(f"{PACKAGE_PREFIX}/PATCH_MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        if any("\\" in name for name in names):
            raise AssertionError("archive contains Windows-only path separators")
    return {
        "output": str(output),
        "entries": len(records) + 1,
        **provenance,
        "content_manifest_sha256": content_manifest_sha256(records),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT.parents[1] / "release-artifacts" / "NekoAgent-V4.1-Foundation-Slices-2026-08-11-r2.zip",
    )
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
