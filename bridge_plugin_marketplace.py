#!/usr/bin/env python3
"""Curated plugin marketplace for the Agent Control capability center.

The marketplace deliberately keeps third-party code inside the AstrBot runtime.
The bridge owns discovery, consent, backups and an operation audit trail, while
AstrBot's own CLI remains responsible for dependency and compatibility handling.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from bridge_capability_registry import ASTRBOT_CONTAINER, ASTRBOT_PLUGIN_ROOT, list_plugins


DEFAULT_MARKET_SOURCE = "https://api.soulter.top/astrbot/plugins"
FALLBACK_MARKET_SOURCE = (
    "https://github.com/AstrBotDevs/AstrBot_Plugins_Collection/"
    "raw/refs/heads/main/plugin_cache_original.json"
)
BRIDGE_DIR = Path(os.environ.get("ASSISTANT_PLATFORM_BRIDGE_DIR", "/opt/agent-stack/codex-qq-bridge"))
MARKET_CACHE_DIR = Path(os.environ.get("PLUGIN_MARKET_CACHE_DIR", str(BRIDGE_DIR / "cache" / "plugin-market")))
PLUGIN_BACKUP_ROOT = Path(
    os.environ.get("PLUGIN_MARKET_BACKUP_ROOT", "/opt/agent-stack/backups/plugin-market")
)
MARKET_CACHE_TTL_SECONDS = max(60, int(os.environ.get("PLUGIN_MARKET_CACHE_TTL", "900")))
ASTRBOT_ROOT = os.environ.get("ASTRBOT_ROOT", "/AstrBot")
MAX_MARKET_BYTES = 12 * 1024 * 1024
MARKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
MARKET_OPERATION_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_plugin_market_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_market_sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            runtime TEXT NOT NULL DEFAULT 'astrbot',
            enabled INTEGER NOT NULL DEFAULT 1,
            trusted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_market_operations (
            id TEXT PRIMARY KEY,
            plugin_id TEXT NOT NULL,
            plugin_name TEXT NOT NULL DEFAULT '',
            runtime TEXT NOT NULL DEFAULT 'astrbot',
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            backup_path TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plugin_market_operations_started "
        "ON plugin_market_operations(started_at DESC)",
    )
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO plugin_market_sources(
            id, name, url, runtime, enabled, trusted, created_at, updated_at
        ) VALUES ('astrbot-official', 'AstrBot 官方市场', ?, 'astrbot', 1, 1, ?, ?)
        """,
        (DEFAULT_MARKET_SOURCE, now, now),
    )


def list_market_sources(conn: sqlite3.Connection) -> list[dict]:
    ensure_plugin_market_tables(conn)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM plugin_market_sources ORDER BY trusted DESC, name",
        ).fetchall()
    ]


def list_market_operations(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    ensure_plugin_market_tables(conn)
    safe_limit = max(1, min(int(limit or 30), 100))
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM plugin_market_operations ORDER BY started_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    ]


def _fetch_json(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Agent-Control-Plugin-Market/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_MARKET_BYTES:
            raise ValueError("plugin_market_catalog_too_large")
        payload = response.read(MAX_MARKET_BYTES + 1)
    if len(payload) > MAX_MARKET_BYTES:
        raise ValueError("plugin_market_catalog_too_large")
    return json.loads(payload.decode("utf-8-sig", errors="replace"))


def _cache_path() -> Path:
    return MARKET_CACHE_DIR / "astrbot-official.json"


def _write_cache(payload: object) -> None:
    MARKET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path()
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _read_cache(*, allow_stale: bool) -> tuple[object | None, bool]:
    path = _cache_path()
    if not path.is_file():
        return None, False
    age = max(0.0, time.time() - path.stat().st_mtime)
    if age > MARKET_CACHE_TTL_SECONDS and not allow_stale:
        return None, False
    try:
        return json.loads(path.read_text(encoding="utf-8")), age > MARKET_CACHE_TTL_SECONDS
    except (OSError, json.JSONDecodeError):
        return None, False


def _safe_text(value: object, limit: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def _version_tuple(value: object) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(value or ""))[:4])


def _category(item: dict) -> str:
    haystack = " ".join(
        [
            _safe_text(item.get("display_name"), 200),
            _safe_text(item.get("desc") or item.get("description"), 800),
            " ".join(str(tag) for tag in item.get("tags", []) if tag),
        ],
    ).lower()
    groups = (
        ("效率工具", ("todo", "task", "calendar", "remind", "translate", "weather", "工具", "提醒", "翻译")),
        ("内容与媒体", ("music", "video", "image", "rss", "bilibili", "媒体", "图片", "音乐", "视频")),
        ("娱乐互动", ("game", "meme", "emoji", "抽签", "游戏", "表情", "娱乐")),
        ("平台集成", ("github", "qq", "api", "webhook", "home assistant", "集成", "连接")),
        ("管理运维", ("admin", "docker", "server", "monitor", "log", "管理", "运维", "监控")),
    )
    for label, tokens in groups:
        if any(token in haystack for token in tokens):
            return label
    return "其他"


def _catalog_entries(payload: object) -> list[tuple[str, dict]]:
    if isinstance(payload, dict):
        return [(str(key), value) for key, value in payload.items() if isinstance(value, dict)]
    if isinstance(payload, list):
        return [
            (str(value.get("id") or value.get("name") or index), value)
            for index, value in enumerate(payload)
            if isinstance(value, dict)
        ]
    raise ValueError("invalid_plugin_market_catalog")


def _normalize_catalog(payload: object, installed: list[dict]) -> list[dict]:
    installed_by_id: dict[str, dict] = {}
    for plugin in installed:
        keys = {
            str(plugin.get("id") or "").lower(),
            str(plugin.get("dir_name") or "").lower(),
            str(plugin.get("repo") or "").rstrip("/").lower(),
        }
        for key in keys:
            if key:
                installed_by_id[key] = plugin

    result: list[dict] = []
    for raw_id, raw in _catalog_entries(payload):
        plugin_id = _safe_text(raw.get("name") or raw_id, 128)
        if not MARKET_ID_PATTERN.fullmatch(plugin_id):
            continue
        repo = _safe_text(raw.get("repo") or raw.get("repository"), 500)
        if repo and not repo.startswith("https://"):
            continue
        match = installed_by_id.get(plugin_id.lower()) or installed_by_id.get(repo.rstrip("/").lower())
        version = _safe_text(raw.get("version"), 64)
        installed_version = _safe_text((match or {}).get("version"), 64)
        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        item = {
            "id": plugin_id,
            "display_name": _safe_text(raw.get("display_name") or raw.get("title") or plugin_id, 160),
            "description": _safe_text(raw.get("desc") or raw.get("description"), 1000),
            "author": _safe_text(raw.get("author"), 160),
            "version": version,
            "repo": repo,
            "tags": [_safe_text(tag, 60) for tag in tags[:12] if _safe_text(tag, 60)],
            "category": _category(raw),
            "runtime": "astrbot",
            "runtime_label": "AstrBot 兼容",
            "compatibility": _safe_text(raw.get("astrbot_version") or "由 AstrBot 安装器校验", 120),
            "source_id": "astrbot-official",
            "source_label": "AstrBot 官方市场",
            "trust_level": "community",
            "risk_level": "review_required",
            "permissions": ["QQ 消息与事件", "插件独立数据目录", "外部网络（取决于插件实现）"],
            "installed": bool(match),
            "installed_id": _safe_text((match or {}).get("id"), 128),
            "installed_version": installed_version,
            "protected": bool((match or {}).get("protected")),
            "healthy": bool((match or {}).get("healthy")) if match else None,
            "update_available": bool(
                match and version and installed_version and _version_tuple(version) > _version_tuple(installed_version)
            ),
        }
        result.append(item)
    result.sort(
        key=lambda item: (
            not item["installed"],
            not item["update_available"],
            item["display_name"].lower(),
        ),
    )
    return result


def get_marketplace(
    conn: sqlite3.Connection,
    *,
    force_refresh: bool = False,
    fetcher: Callable[[str], object] | None = None,
) -> dict:
    ensure_plugin_market_tables(conn)
    fetch = fetcher or _fetch_json
    payload: object | None = None
    stale = False
    source_url = DEFAULT_MARKET_SOURCE
    errors: list[str] = []
    if not force_refresh:
        payload, stale = _read_cache(allow_stale=False)
    if payload is None:
        for url in (DEFAULT_MARKET_SOURCE, FALLBACK_MARKET_SOURCE):
            try:
                payload = fetch(url)
                source_url = url
                _write_cache(payload)
                stale = False
                break
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
                errors.append(f"{url}: {type(exc).__name__}")
    if payload is None:
        payload, stale = _read_cache(allow_stale=True)
    if payload is None:
        return {
            "ok": False,
            "plugins": [],
            "sources": list_market_sources(conn),
            "operations": list_market_operations(conn),
            "stale": False,
            "error": "plugin_market_unavailable",
            "diagnostic": "; ".join(errors)[:500],
        }
    plugins = _normalize_catalog(payload, list_plugins())
    return {
        "ok": True,
        "plugins": plugins,
        "sources": list_market_sources(conn),
        "operations": list_market_operations(conn),
        "stale": stale,
        "source_url": source_url,
        "fetched_at": utc_now(),
        "counts": {
            "available": len(plugins),
            "installed": sum(1 for item in plugins if item["installed"]),
            "updates": sum(1 for item in plugins if item["update_available"]),
        },
    }


def _record_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    plugin_id: str,
    plugin_name: str,
    action: str,
    status: str,
    message: str = "",
    backup_path: str = "",
    finished: bool = False,
) -> None:
    ensure_plugin_market_tables(conn)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO plugin_market_operations(
            id, plugin_id, plugin_name, runtime, action, status, message,
            backup_path, started_at, finished_at
        ) VALUES (?, ?, ?, 'astrbot', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status = excluded.status,
            message = excluded.message,
            backup_path = excluded.backup_path,
            finished_at = excluded.finished_at
        """,
        (
            operation_id,
            plugin_id,
            plugin_name,
            action,
            status,
            _safe_text(message, 2000),
            backup_path,
            now,
            now if finished else "",
        ),
    )
    conn.commit()


def _safe_backup(plugin: dict, operation_id: str) -> Path | None:
    plugin_dir = (ASTRBOT_PLUGIN_ROOT / str(plugin.get("dir_name") or "")).resolve()
    root = ASTRBOT_PLUGIN_ROOT.resolve()
    if not plugin_dir.is_dir() or root not in plugin_dir.parents:
        return None
    operation_root = (PLUGIN_BACKUP_ROOT / operation_id).resolve()
    backup_root = PLUGIN_BACKUP_ROOT.resolve()
    if operation_root != backup_root and backup_root not in operation_root.parents:
        raise ValueError("invalid_plugin_backup_path")
    operation_root.mkdir(parents=True, exist_ok=False)
    target = operation_root / plugin_dir.name
    shutil.copytree(plugin_dir, target)
    return target


def _restore_backup(backup_path: Path | None, plugin: dict) -> bool:
    if not backup_path or not backup_path.is_dir():
        return False
    plugin_dir = (ASTRBOT_PLUGIN_ROOT / str(plugin.get("dir_name") or "")).resolve()
    root = ASTRBOT_PLUGIN_ROOT.resolve()
    if root not in plugin_dir.parents:
        return False
    temporary = plugin_dir.with_name(f".{plugin_dir.name}.failed-{uuid.uuid4().hex[:8]}")
    if plugin_dir.exists():
        plugin_dir.replace(temporary)
    try:
        shutil.copytree(backup_path, plugin_dir)
    except Exception:
        if temporary.exists() and not plugin_dir.exists():
            temporary.replace(plugin_dir)
        return False
    if temporary.exists():
        shutil.rmtree(temporary)
    return True


def _plugin_dirs() -> set[str]:
    if not ASTRBOT_PLUGIN_ROOT.is_dir():
        return set()
    return {path.name for path in ASTRBOT_PLUGIN_ROOT.iterdir() if path.is_dir()}


def _cleanup_new_plugin_dirs(before: set[str]) -> bool:
    root = ASTRBOT_PLUGIN_ROOT.resolve()
    cleaned = False
    for name in _plugin_dirs() - before:
        path = (root / name).resolve()
        if root in path.parents and path.is_dir():
            shutil.rmtree(path)
            cleaned = True
    return cleaned


def _operation_verified(
    action: str,
    plugin_id: str,
    catalog_item: dict | None,
    installed_item: dict | None,
    before_dirs: set[str],
) -> bool:
    current = list_plugins()
    repo = str((catalog_item or {}).get("repo") or "").rstrip("/").lower()
    match = next(
        (
            item
            for item in current
            if item.get("id") == plugin_id
            or item.get("dir_name") == plugin_id
            or (repo and str(item.get("repo") or "").rstrip("/").lower() == repo)
        ),
        None,
    )
    if action == "install":
        return bool(match or (_plugin_dirs() - before_dirs))
    if action == "update":
        expected_dir = str((installed_item or {}).get("dir_name") or "")
        return bool(match or (expected_dir and expected_dir in _plugin_dirs()))
    expected_dir = str((installed_item or {}).get("dir_name") or "")
    return not match and (not expected_dir or expected_dir not in _plugin_dirs())


def _ensure_astrbot_cli_root() -> None:
    if not str(ASTRBOT_ROOT).startswith("/") or ".." in Path(ASTRBOT_ROOT).parts:
        raise ValueError("invalid_astrbot_root")
    probe = subprocess.run(
        ["docker", "exec", ASTRBOT_CONTAINER, "test", "-f", f"{ASTRBOT_ROOT}/pyproject.toml"],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if probe.returncode != 0:
        raise RuntimeError("astrbot_cli_root_missing")
    marker = subprocess.run(
        ["docker", "exec", ASTRBOT_CONTAINER, "touch", f"{ASTRBOT_ROOT}/.astrbot"],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if marker.returncode != 0:
        raise RuntimeError((marker.stderr or marker.stdout or "astrbot_cli_root_marker_failed")[-1000:])


def _operate_market_plugin(conn: sqlite3.Connection, payload: dict) -> dict:
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"install", "update", "uninstall"}:
        raise ValueError("invalid_plugin_market_action")
    if str(payload.get("confirm_risk") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise ValueError("plugin_market_risk_confirmation_required")
    plugin_id = str(payload.get("plugin_id") or payload.get("id") or "").strip()
    if not MARKET_ID_PATTERN.fullmatch(plugin_id):
        raise ValueError("invalid_plugin_market_id")

    marketplace = get_marketplace(conn)
    catalog_item = next((item for item in marketplace.get("plugins", []) if item["id"] == plugin_id), None)
    installed = list_plugins()
    installed_item = next(
        (item for item in installed if item["id"] == plugin_id or item["dir_name"] == plugin_id),
        None,
    )
    if action == "install" and not catalog_item:
        raise ValueError("plugin_not_in_trusted_market")
    if action in {"update", "uninstall"} and not installed_item:
        raise ValueError("plugin_not_installed")
    if action == "uninstall" and installed_item and installed_item.get("protected"):
        raise ValueError("protected_plugin_cannot_be_uninstalled")

    _ensure_astrbot_cli_root()
    plugin_name = str((catalog_item or installed_item or {}).get("display_name") or plugin_id)
    operation_id = f"op_{uuid.uuid4().hex}"
    backup_path: Path | None = None
    if action in {"update", "uninstall"} and installed_item:
        backup_path = _safe_backup(installed_item, operation_id)
    _record_operation(
        conn,
        operation_id=operation_id,
        plugin_id=plugin_id,
        plugin_name=plugin_name,
        action=action,
        status="running",
        backup_path=str(backup_path or ""),
    )

    before_dirs = _plugin_dirs()
    cli_action = "remove" if action == "uninstall" else action
    result = subprocess.run(
        [
            "docker", "exec", "--workdir", ASTRBOT_ROOT, ASTRBOT_CONTAINER,
            "astrbot", "plug", cli_action, plugin_id,
        ],
        text=True,
        capture_output=True,
        timeout=240,
    )
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[-1800:]
    restart_ok = False
    verified = False
    if result.returncode == 0:
        restart = subprocess.run(
            ["docker", "restart", "--time", "20", ASTRBOT_CONTAINER],
            text=True,
            capture_output=True,
            timeout=60,
        )
        restart_ok = restart.returncode == 0
        if not restart_ok:
            output = (output + "\n" + (restart.stderr or restart.stdout or "astrbot_restart_failed"))[-1800:]
        else:
            for attempt in range(4):
                if _operation_verified(action, plugin_id, catalog_item, installed_item, before_dirs):
                    verified = True
                    break
                if attempt < 3:
                    time.sleep(1)
            if not verified:
                output = (output + "\nplugin_installation_verification_failed")[-1800:]

    if result.returncode != 0 or not restart_ok or not verified:
        restored = _restore_backup(backup_path, installed_item or {})
        cleaned = False
        if action == "install" and not restored:
            cleaned = _cleanup_new_plugin_dirs(before_dirs)
        message = output or "plugin_market_operation_failed"
        if restored:
            subprocess.run(
                ["docker", "restart", "--time", "20", ASTRBOT_CONTAINER],
                text=True,
                capture_output=True,
                timeout=60,
            )
            message = f"{message}\n已从备份恢复。"
        elif cleaned:
            message = f"{message}\n已清理未通过验证的插件目录。"
        _record_operation(
            conn,
            operation_id=operation_id,
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            action=action,
            status="rolled_back" if restored else "failed",
            message=message,
            backup_path=str(backup_path or ""),
            finished=True,
        )
        return {
            "ok": False,
            "operation_id": operation_id,
            "status": "rolled_back" if restored else "failed",
            "error": "plugin_market_operation_failed",
            "message": _safe_text(message, 1800),
        }

    _record_operation(
        conn,
        operation_id=operation_id,
        plugin_id=plugin_id,
        plugin_name=plugin_name,
        action=action,
        status="succeeded",
        message=output or "operation_succeeded",
        backup_path=str(backup_path or ""),
        finished=True,
    )
    return {
        "ok": True,
        "operation_id": operation_id,
        "status": "succeeded",
        "plugin_id": plugin_id,
        "action": action,
        "astrbot_restarted": True,
        "plugins": list_plugins(),
        "operations": list_market_operations(conn),
    }


def operate_market_plugin(conn: sqlite3.Connection, payload: dict) -> dict:
    if os.name == "posix" and os.environ.get("OPS_BROKER_EXECUTOR") != "1":
        from bridge_ops_actions import broker_write

        action = str(payload.get("action") or "").strip().lower()
        plugin_id = str(payload.get("plugin_id") or payload.get("id") or "").strip()
        if action not in {"install", "update", "uninstall"}:
            raise ValueError("invalid_plugin_market_action")
        if str(payload.get("confirm_risk") or "").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError("plugin_market_risk_confirmation_required")
        return broker_write(
            "astrbot_plugin_operate",
            "astrbot",
            {"operation": action, "plugin_id": plugin_id},
            idempotency_key=str(payload.get("_idempotency_key") or ""),
        )
    if not MARKET_OPERATION_LOCK.acquire(blocking=False):
        raise ValueError("plugin_market_operation_in_progress")
    try:
        return _operate_market_plugin(conn, payload)
    finally:
        MARKET_OPERATION_LOCK.release()
