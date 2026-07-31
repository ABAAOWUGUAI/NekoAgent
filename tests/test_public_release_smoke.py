"""Safe smoke checks that are included in the public source release."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_bridge_imports_without_runtime_bootstrap() -> None:
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    bridge = importlib.import_module("codex_qq_bridge")

    assert bridge.LISTEN_HOST == "127.0.0.1"
    assert bridge.LISTEN_PORT == 18777
