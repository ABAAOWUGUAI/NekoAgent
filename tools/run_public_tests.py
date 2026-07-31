#!/usr/bin/env python3
"""Run the public release's dependency-free contract tests.

The source release intentionally keeps its verification path independent from
an unpinned test framework.  This runner is used by GitHub Actions and by the
independent-clone Gate.  It never starts a service, creates runtime state, or
contacts a Provider.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"test_module_load_failed:{relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compile_public_python() -> int:
    compiled = 0
    candidates = list(ROOT.glob("*.py"))
    for directory_name in ("admin", "remote-plugin", "tests"):
        directory = ROOT / directory_name
        if directory.is_dir():
            candidates.extend(directory.rglob("*.py"))
    for relative in ("tools/install_starter_pack.py", "tools/run_public_tests.py"):
        path = ROOT / relative
        if path.is_file():
            candidates.append(path)
    for path in sorted(set(candidates)):
        if "__pycache__" in path.parts:
            continue
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        compiled += 1
    return compiled


def main() -> int:
    try:
        starter_pack_tests = _load("public_starter_pack_tests", "tests/test_starter_pack.py")
        smoke_tests = _load("public_release_smoke_tests", "tests/test_public_release_smoke.py")
        starter_pack_tests.test_xiaofei_pack_dry_run_has_no_runtime_state()
        smoke_tests.test_bridge_imports_without_runtime_bootstrap()
        smoke_tests.test_deployment_and_protection_documents_are_present()
        tests = 3
        exporter_test_path = ROOT / "tests" / "test_open_source_release_export.py"
        if exporter_test_path.is_file():
            export_tests = _load("public_export_tests", "tests/test_open_source_release_export.py")
            export_tests.test_sanitizer_removes_private_instance_and_pet_identifiers()
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_accepts_exported_dotfiles_and_examples(Path(directory))
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_allows_declared_public_starter_pack_assets(Path(directory))
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_does_not_allow_private_identity_outside_documented_pack_paths(Path(directory))
            export_tests.test_source_allowlist_includes_starter_pack_installer()
            export_tests.test_required_public_files_include_deployment_and_protection_docs()
            with tempfile.TemporaryDirectory() as directory:
                export_tests.test_audit_requires_license_ci_and_public_tests(Path(directory))
            tests += 7
        print(json.dumps({"ok": True, "compiled_python_files": _compile_public_python(), "tests": tests}))
        return 0
    except Exception as exc:  # The runner must surface the exact Gate failure.
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
