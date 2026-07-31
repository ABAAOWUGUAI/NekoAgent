#!/usr/bin/env python3
from __future__ import annotations

def codex_model_args(settings: dict) -> list[str]:
    model = str(settings.get("codex_model") or "").strip()
    if not model:
        return []
    return ["--model", model]
