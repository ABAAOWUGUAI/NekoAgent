"""Contract tests for public optional Starter Packs."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "tools" / "install_starter_pack.py"
SPEC = importlib.util.spec_from_file_location("starter_pack_installer", MODULE_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def test_xiaofei_pack_dry_run_has_no_runtime_state() -> None:
    pack_dir, manifest = INSTALLER.load_pack(ROOT / "starter-packs" / "xiaofei")

    result = INSTALLER.preview(pack_dir, manifest)

    assert result["ok"] is True
    assert result["pack_id"] == "xiaofei-starter-pack"
    assert result["default_activation"] is False
    assert result["state_imported"] == []
    assert result["meme_count"] == 5
    assert manifest["install_contract"]["never_imports"]
