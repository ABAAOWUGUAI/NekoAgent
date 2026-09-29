#!/usr/bin/env python3
"""Public checks for the currently served V4 product Web conversation."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import admin_console


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_v4_ai_chat_assets_are_versioned_and_allowlisted() -> None:
    served = (
        "admin-v4-product.css", "v4-product-contract.js",
        "v4-product-attention.js", "v4-product-adapters.js", "v4-product-app.js",
    )
    for name in served:
        assert f"/admin/static/{name}?v=" in admin_console.ADMIN_HTML
        assert admin_console.admin_asset(name) is not None
    assert admin_console.admin_asset("v4-ai-chat-surface.js") is None


def test_v4_ai_chat_uses_existing_receipt_and_build_contracts() -> None:
    adapter = _source("admin/v4-product-adapters.js")
    assert "dispatch: (prompt, fixedRequestId = '') => post('/assistant/dispatch'" in adapter
    assert "source: 'web-console'" in adapter
    assert "force: 'auto'" in adapter
    assert "'Idempotency-Key': clientRequestId" in adapter
    assert "'X-Request-ID': clientRequestId" in adapter
    assert "fixedRequestId ? { requestId: fixedRequestId } : {}" in adapter
    assert "'X-QQ-Message-ID': clientRequestId" not in adapter
    assert "'X-Admin-Build': renderedConsoleBuild()" in adapter
    assert "build: renderedConsoleBuild()" in adapter


def test_v4_ai_chat_keeps_read_errors_and_stale_thread_results_separate() -> None:
    app = _source("admin/v4-product-app.js")
    assert "const requestedThread = selectedThread" in app
    assert "requestEpoch !== messageRequestEpoch || requestedThread !== selectedThread" in app
    assert "loadError = '无法读取这段 Web 对话。'" in app
    assert "notice.textContent = userError(error)" in app
    assert "form.reset()" in app


if __name__ == "__main__":
    test_v4_ai_chat_assets_are_versioned_and_allowlisted()
    test_v4_ai_chat_uses_existing_receipt_and_build_contracts()
    test_v4_ai_chat_keeps_read_errors_and_stale_thread_results_separate()
