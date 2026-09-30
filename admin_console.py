#!/usr/bin/env python3
"""组装管理后台的固定、版本化资源。"""

from __future__ import annotations

import hashlib
from pathlib import Path


_ADMIN_DIR = Path(__file__).with_name("admin")


def _read_admin_resource(name: str) -> str:
    return (_ADMIN_DIR / name).read_text(encoding="utf-8")


def _compact_html_lines(value: str) -> str:
    """Remove authoring indentation while preserving one separator per HTML line."""

    compact = "\n".join(line.strip() for line in value.splitlines() if line.strip())
    return compact.replace(">\n<", "><")


_ADMIN_CSS_NAMES = (
    "admin-v4-product.css",
)
_ADMIN_JS_NAMES = (
    "v4-product-contract.js",
    "v4-product-attention.js",
    "v4-product-adapters.js",
    "v4-product-app.js",
)
_ADMIN_VENDOR_ASSETS = ()
_ADMIN_EXTRA_ASSETS = ()
_ADMIN_TEMPLATE = _compact_html_lines(_read_admin_resource("index.html"))
_ADMIN_ASSETS = {
    **{name: (_ADMIN_DIR / name).read_bytes() for name in _ADMIN_CSS_NAMES},
    **{name: (_ADMIN_DIR / name).read_bytes() for name in _ADMIN_JS_NAMES},
    **{name: (_ADMIN_DIR / name).read_bytes() for name in _ADMIN_VENDOR_ASSETS},
    **{name: (_ADMIN_DIR / name).read_bytes() for name in _ADMIN_EXTRA_ASSETS},
}
_ADMIN_ASSETS["admin.bundle.js"] = b"\n".join(_ADMIN_ASSETS[name] for name in _ADMIN_JS_NAMES)
ADMIN_ASSET_VERSION = hashlib.sha256(
    b"\0".join(
        _ADMIN_ASSETS[name]
        for name in (*_ADMIN_CSS_NAMES, *_ADMIN_JS_NAMES, *_ADMIN_VENDOR_ASSETS, *_ADMIN_EXTRA_ASSETS)
    )
).hexdigest()[:16]

if "__ADMIN_ASSET_VERSION__" not in _ADMIN_TEMPLATE:
    raise RuntimeError("管理后台模板缺少静态资源版本占位符")

ADMIN_HTML = _ADMIN_TEMPLATE.replace("__ADMIN_ASSET_VERSION__", ADMIN_ASSET_VERSION)


def admin_asset(name: str) -> tuple[bytes, str, str] | None:
    """Return a versioned public console asset without exposing arbitrary files."""

    payload = _ADMIN_ASSETS.get(name)
    if payload is None:
        return None
    if name.endswith(".css"):
        content_type = "text/css; charset=utf-8"
    elif name.endswith(".svg"):
        content_type = "image/svg+xml; charset=utf-8"
    elif name.endswith(".png"):
        content_type = "image/png"
    elif name.endswith(".webp"):
        content_type = "image/webp"
    elif name.endswith(".json"):
        content_type = "application/json; charset=utf-8"
    elif name.endswith(".md"):
        content_type = "text/plain; charset=utf-8"
    else:
        content_type = "text/javascript; charset=utf-8"
    etag = hashlib.sha256(payload).hexdigest()
    return payload, content_type, etag
