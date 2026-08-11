#!/usr/bin/env python3
"""Dependency-free public contract checks for the V4.1 AI Chat first slice."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import admin_console


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_v4_ai_chat_assets_are_versioned_and_allowlisted() -> None:
    assert "/admin/static/admin-v4-ai-chat-surface.css?v=" in admin_console.ADMIN_HTML
    assert "/admin/static/v4-ai-chat-surface.js?v=" in admin_console.ADMIN_HTML
    assert admin_console.admin_asset("admin-v4-ai-chat-surface.css") is not None
    assert admin_console.admin_asset("v4-ai-chat-surface.js") is not None


def test_v4_ai_chat_uses_the_existing_dispatch_contract_without_model_controls() -> None:
    source = _source("admin/v4-ai-chat-surface.js")
    assert "const dispatchEndpoint = '/assistant/dispatch';" in source
    assert "user_id: 'web-console'" in source
    assert "source: 'web-console'" in source
    assert "force: 'auto'" in source
    assert "'X-QQ-Message-ID': request.id" in source
    assert "'X-QQ-Actor-ID': 'web-console'" in source
    assert "window.loadTask" in source
    assert "approval_required" in source
    assert "model selector" not in source.lower()
    assert "attachment" not in source.lower()


def test_v4_ai_chat_preserves_lifecycle_and_duplicate_dispatch_controls() -> None:
    source = _source("admin/v4-ai-chat-surface.js")
    css = _source("admin/admin-v4-ai-chat-surface.css")
    assert "root.inert = true" in source
    assert "root.inert = false" in source
    assert "requestVersion += 1" in source
    assert "submitting" in source
    assert "retryableRequest" in source
    assert "nekoagent:v4-experience-disable" in source
    assert "nekoagent:v4-route-change" in source
    assert 'body[data-v4-experience="active"][data-v4-active-view="chat"]' in css
    assert ".v4-ai-chat-surface { display: none; }" in css
    assert "data-v4-chat-legacy-mode" in css


if __name__ == "__main__":
    test_v4_ai_chat_assets_are_versioned_and_allowlisted()
    test_v4_ai_chat_uses_the_existing_dispatch_contract_without_model_controls()
    test_v4_ai_chat_preserves_lifecycle_and_duplicate_dispatch_controls()
