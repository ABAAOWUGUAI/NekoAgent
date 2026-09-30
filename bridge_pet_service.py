#!/usr/bin/env python3
"""Validated PetPack storage and settings for the private admin console."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


BUILTIN_PACK_ID = "assistant-placeholder"
ASSISTANT_CORE_NAMESPACE = "assistant-core"
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_PORTRAIT_BYTES = 200 * 1024
ASSET_ROOT = Path(os.environ.get("AGENT_PET_ASSET_ROOT", "/opt/agent-stack/codex-qq-bridge/assets/pets"))
BUILTIN_ASSET_PATH = Path(__file__).with_name("admin") / "pet-placeholder.svg"
BUILTIN_MANIFEST_PATH = Path(__file__).with_name("admin") / "pet-placeholder.svg"
ALLOWED_DOCKS = {"bottom-right", "bottom-left", "free"}
ALLOWED_MOTION = {"auto", "reduced", "off"}
ALLOWED_ANIMATION_STATES = {
    "idle", "running-right", "running-left", "waving", "jumping",
    "failed", "waiting", "running", "review",
}
MIME_EXTENSIONS = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )


def _assistant_identity_migrated(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'",
    ).fetchone()
    if not table:
        return False
    return bool(
        conn.execute(
            "SELECT 1 FROM schema_migrations WHERE namespace=? AND version>=3",
            (ASSISTANT_CORE_NAMESPACE,),
        ).fetchone(),
    )


def ensure_pet_tables(conn: sqlite3.Connection) -> None:
    _ensure_settings_table(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pet_packs (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          author TEXT NOT NULL DEFAULT '',
          license TEXT NOT NULL DEFAULT '',
          asset_name TEXT NOT NULL,
          mime_type TEXT NOT NULL,
          manifest_json TEXT NOT NULL DEFAULT '{}',
          built_in INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          owner_actor_id TEXT NOT NULL DEFAULT 'owner-local',
          status TEXT NOT NULL DEFAULT 'active',
          source_type TEXT NOT NULL DEFAULT 'user_import',
          deleted_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS pet_pack_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          pack_id TEXT NOT NULL,
          action TEXT NOT NULL,
          detail_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        """
    )
    # Once Gate 2 owns the resource row, startup must never recreate a deleted
    # private appearance. The packaged file remains only as an optional source.
    # The public edition ships no preinstalled persona or PetPack.
    # Deployers may import assets they are entitled to distribute.
    return
    now = _utc_now()
    manifest = json.loads(BUILTIN_MANIFEST_PATH.read_text(encoding="utf-8"))
    values = (
        BUILTIN_PACK_ID,
        "当前助手 · 动态私有默认形象",
        "Agent Control",
        "private-use / unofficial",
        "builtin:pet-placeholder.svg",
        "image/webp",
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        now,
        now,
    )
    pet_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(pet_packs)")}
    ownership_columns = {"owner_actor_id", "status", "source_type", "deleted_at"}
    if ownership_columns.issubset(pet_columns):
        conn.execute(
            """
            INSERT INTO pet_packs
              (id, name, author, license, asset_name, mime_type, manifest_json, built_in,
               created_at, updated_at, owner_actor_id, status, source_type, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'owner-local', 'active', 'legacy_private', '')
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, author=excluded.author, license=excluded.license,
              asset_name=excluded.asset_name, mime_type=excluded.mime_type,
              manifest_json=excluded.manifest_json, built_in=1,
              updated_at=CASE
                WHEN pet_packs.asset_name <> excluded.asset_name OR pet_packs.manifest_json <> excluded.manifest_json
                THEN excluded.updated_at ELSE pet_packs.updated_at END
            """,
            values,
        )
    else:
        conn.execute(
            """
            INSERT INTO pet_packs(
              id,name,author,license,asset_name,mime_type,manifest_json,built_in,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,1,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, author=excluded.author, license=excluded.license,
              asset_name=excluded.asset_name, mime_type=excluded.mime_type,
              manifest_json=excluded.manifest_json, built_in=1,
              updated_at=CASE
                WHEN pet_packs.asset_name <> excluded.asset_name OR pet_packs.manifest_json <> excluded.manifest_json
                THEN excluded.updated_at ELSE pet_packs.updated_at END
            """,
            values,
        )


def _setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else default


def _write_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "updated_at" in columns:
        conn.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, _utc_now()),
        )
        return
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _pack_dict(row: sqlite3.Row | tuple, *, identity_migrated: bool) -> dict:
    values = dict(row) if isinstance(row, sqlite3.Row) else {
        "id": row[0], "name": row[1], "author": row[2], "license": row[3],
        "asset_name": row[4], "mime_type": row[5], "manifest_json": row[6],
        "built_in": row[7], "created_at": row[8], "updated_at": row[9],
        "owner_actor_id": row[10], "status": row[11], "source_type": row[12],
        "deleted_at": row[13],
    }
    try:
        manifest = json.loads(values.get("manifest_json") or "{}")
    except json.JSONDecodeError:
        manifest = {}
    return {
        "id": values["id"],
        "name": values["name"],
        "author": values["author"],
        "license": values["license"],
        "mime_type": values["mime_type"],
        "manifest": manifest,
        "built_in": bool(values["built_in"]),
        "preinstalled_source": bool(values["built_in"]),
        "owner_actor_id": values.get("owner_actor_id") or "owner-local",
        "status": values.get("status") or "active",
        "source_type": values.get("source_type") or "user_import",
        "deletable": (
            (values.get("status") or "active") == "active"
            and (identity_migrated or not bool(values["built_in"]))
        ),
        "asset_url": f"/assistant/pets/assets/{values['id']}",
        "portrait": _portrait_descriptor(values, manifest),
        "created_at": values["created_at"],
        "updated_at": values["updated_at"],
}


def list_pet_packs(conn: sqlite3.Connection) -> list[dict]:
    ensure_pet_tables(conn)
    identity_migrated = _assistant_identity_migrated(conn)
    rows = conn.execute(
        "SELECT id,name,author,license,asset_name,mime_type,manifest_json,built_in,"
        "created_at,updated_at,owner_actor_id,status,source_type,deleted_at "
        "FROM pet_packs WHERE status='active' ORDER BY built_in DESC, name COLLATE NOCASE"
    ).fetchall()
    return [_pack_dict(row, identity_migrated=identity_migrated) for row in rows]


def pet_state(conn: sqlite3.Connection, *, integrity_scope: str = "identity") -> dict:
    packs = list_pet_packs(conn)
    pack_ids = {item["id"] for item in packs}
    selected = _setting(conn, "admin_pet_pack_id", BUILTIN_PACK_ID)
    identity_enabled = False
    if _assistant_identity_migrated(conn):
        from bridge_assistant_identity import current_assistant, identity_feature_enabled

        identity_enabled = identity_feature_enabled(conn)
        if identity_enabled:
            assistant = current_assistant(conn, integrity_scope=integrity_scope)
            selected = str((((assistant or {}).get("appearance") or {}).get("id")) or "")
    if selected not in pack_ids:
        selected = "" if identity_enabled else (BUILTIN_PACK_ID if BUILTIN_PACK_ID in pack_ids else "")
    try:
        scale = float(_setting(conn, "admin_pet_scale", "1"))
    except ValueError:
        scale = 1.0
    try:
        position_x = float(_setting(conn, "admin_pet_position_x", "0.82"))
        position_y = float(_setting(conn, "admin_pet_position_y", "0.72"))
    except ValueError:
        position_x, position_y = 0.82, 0.72
    return {
        "enabled": bool(selected) and _setting(conn, "admin_pet_enabled", "1") == "1",
        "pack_id": selected,
        "scale": max(0.5, min(scale, 1.8)),
        "dock": _setting(conn, "admin_pet_dock", "bottom-right") if _setting(conn, "admin_pet_dock", "bottom-right") in ALLOWED_DOCKS else "bottom-right",
        "motion": _setting(conn, "admin_pet_motion", "auto") if _setting(conn, "admin_pet_motion", "auto") in ALLOWED_MOTION else "auto",
        "position_x": max(0.0, min(position_x, 1.0)),
        "position_y": max(0.0, min(position_y, 1.0)),
        "packs": packs,
    }


def save_pet_settings(conn: sqlite3.Connection, payload: dict) -> dict:
    if "portrait" in payload:
        return _save_pet_portrait(conn, payload)
    state = pet_state(conn, integrity_scope="database")
    pack_id = str(payload.get("pack_id") or state["pack_id"]).strip()
    if not pack_id or not conn.execute(
        "SELECT 1 FROM pet_packs WHERE id=? AND status='active'",
        (pack_id,),
    ).fetchone():
        raise ValueError("pet_pack_not_found")
    try:
        scale = float(payload.get("scale", state["scale"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_pet_scale") from exc
    if not 0.5 <= scale <= 1.8:
        raise ValueError("invalid_pet_scale")
    dock = str(payload.get("dock") or state["dock"]).strip()
    motion = str(payload.get("motion") or state["motion"]).strip()
    if dock not in ALLOWED_DOCKS:
        raise ValueError("invalid_pet_dock")
    if motion not in ALLOWED_MOTION:
        raise ValueError("invalid_pet_motion")
    try:
        position_x = float(payload.get("position_x", state["position_x"]))
        position_y = float(payload.get("position_y", state["position_y"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_pet_position") from exc
    if not 0.0 <= position_x <= 1.0 or not 0.0 <= position_y <= 1.0:
        raise ValueError("invalid_pet_position")
    enabled = bool(payload.get("enabled", state["enabled"]))
    if _assistant_identity_migrated(conn):
        from bridge_assistant_identity import (
            current_assistant,
            identity_feature_enabled,
            update_assistant,
        )

        if identity_feature_enabled(conn):
            assistant = current_assistant(conn)
            if assistant is None:
                raise ValueError("active_assistant_missing")
            update_assistant(
                conn,
                assistant["id"],
                {"appearance_pack_id": pack_id},
                channel="web-pet-settings",
            )
    for key, value in {
        "admin_pet_enabled": "1" if enabled else "0",
        "admin_pet_pack_id": pack_id,
        "admin_pet_scale": f"{scale:.2f}",
        "admin_pet_dock": dock,
        "admin_pet_motion": motion,
        "admin_pet_position_x": f"{position_x:.4f}",
        "admin_pet_position_y": f"{position_y:.4f}",
    }.items():
        _write_setting(conn, key, value)
    conn.execute(
        "INSERT INTO pet_pack_events(pack_id,action,detail_json,created_at) VALUES(?,?,?,?)",
        (
            pack_id,
            "settings",
            json.dumps({"enabled": enabled, "dock": dock, "motion": motion, "position_x": position_x, "position_y": position_y}),
            _utc_now(),
        ),
    )
    return pet_state(conn, integrity_scope="database")


def _image_dimensions(data: bytes, mime: str) -> tuple[int, int] | None:
    if mime == "image/png" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if mime == "image/gif" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if mime != "image/webp" or len(data) < 20:
        return None
    offset = 12
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        chunk = data[offset + 8:offset + 8 + size]
        if kind == b"VP8X" and len(chunk) >= 10:
            return 1 + int.from_bytes(chunk[4:7], "little"), 1 + int.from_bytes(chunk[7:10], "little")
        if kind == b"VP8 " and len(chunk) >= 10 and chunk[3:6] == b"\x9d\x01\x2a":
            return int.from_bytes(chunk[6:8], "little") & 0x3FFF, int.from_bytes(chunk[8:10], "little") & 0x3FFF
        if kind == b"VP8L" and len(chunk) >= 5 and chunk[0] == 0x2F:
            bits = int.from_bytes(chunk[1:5], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        offset += 8 + size + (size & 1)
    return None


def _portrait_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    if not data or len(data) > MAX_PORTRAIT_BYTES:
        raise ValueError("invalid_portrait_size")
    if mime == "image/webp":
        if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP" or int.from_bytes(data[4:8], "little") + 8 != len(data):
            raise ValueError("invalid_portrait_image")
        offset = 12
        while offset + 8 <= len(data):
            kind = data[offset:offset + 4]
            size = int.from_bytes(data[offset + 4:offset + 8], "little")
            if offset + 8 + size > len(data):
                raise ValueError("invalid_portrait_image")
            if kind in {b"ANIM", b"ANMF"} or (kind == b"VP8X" and size and data[offset + 8] & 2):
                raise ValueError("portrait_must_be_static")
            offset += 8 + size + (size & 1)
        if offset != len(data):
            raise ValueError("invalid_portrait_image")
    elif mime == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
            raise ValueError("invalid_portrait_image")
        offset = 8
        while offset + 12 <= len(data):
            size = int.from_bytes(data[offset:offset + 4], "big")
            if data[offset + 4:offset + 8] == b"acTL":
                raise ValueError("portrait_must_be_static")
            offset += 12 + size
        if offset != len(data):
            raise ValueError("invalid_portrait_image")
    else:
        raise ValueError("unsupported_portrait_type")
    dimensions = _image_dimensions(data, mime)
    if not dimensions or any(value <= 0 or value > 8192 for value in dimensions) or dimensions[0] * dimensions[1] > 16_777_216:
        raise ValueError("invalid_portrait_dimensions")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("portrait_decoder_unavailable")
    try:
        decoded = subprocess.run(
            [executable, "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode", "-threads", "1", "-i", "pipe:0", "-map", "0:v:0", "-f", "null", "-"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("portrait_decode_failed") from exc
    if decoded.returncode != 0:
        raise ValueError("portrait_decode_failed")
    return dimensions


def _portrait_resource(values: dict, manifest: object) -> tuple[bytes, dict] | None:
    if values.get("status", "active") != "active" or values.get("deleted_at") or values.get("owner_actor_id", "owner-local") != "owner-local":
        return None
    item = manifest.get("portrait") if isinstance(manifest, dict) else None
    if not isinstance(item, dict):
        return None
    version = item.get("content_version")
    mime = item.get("mime_type")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version) or mime not in {"image/webp", "image/png"}:
        return None
    expected_name = f"portrait-{version}{MIME_EXTENSIONS[mime]}"
    if item.get("asset_name") != expected_name:
        return None
    root = ASSET_ROOT.resolve()
    path = (root / expected_name).resolve()
    try:
        if not path.is_relative_to(root) or not 0 < path.stat().st_size <= MAX_PORTRAIT_BYTES:
            return None
        data = path.read_bytes()
        width, height = _portrait_dimensions(data, mime)
    except (OSError, ValueError):
        return None
    if hashlib.sha256(data).hexdigest() != version or any(type(item.get(key)) is not int or item[key] != value for key, value in (("width", width), ("height", height), ("bytes", len(data)))):
        return None
    descriptor = {"pack_id": values["id"], "url": f"/assistant/pets/assets/{values['id']}/portrait?v={version[:16]}",
                  "mime_type": mime, "width": width, "height": height, "bytes": len(data), "content_version": version}
    return data, descriptor


def _portrait_descriptor(values: dict, manifest: object) -> dict | None:
    resource = _portrait_resource(values, manifest)
    return resource[1] if resource else None


def _save_pet_portrait(conn: sqlite3.Connection, payload: dict) -> dict:
    if set(payload) != {"pack_id", "portrait"} or not isinstance(payload["portrait"], dict):
        raise ValueError("invalid_portrait_request")
    state = pet_state(conn, integrity_scope="database")
    pack_id = str(payload["pack_id"])
    if not pack_id or pack_id != state["pack_id"]:
        raise ValueError("portrait_requires_current_pack")
    row = conn.execute("SELECT manifest_json FROM pet_packs WHERE id=? AND owner_actor_id='owner-local' AND status='active' AND deleted_at=''", (pack_id,)).fetchone()
    if row is None:
        raise ValueError("pet_pack_not_found")
    raw = str(row[0])
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("invalid_pet_manifest")
    request = payload["portrait"]
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if request.get("expected_manifest_sha256") != digest:
        raise ValueError("portrait_manifest_conflict")
    if request.get("clear") is True and set(request) == {"clear", "expected_manifest_sha256"}:
        manifest.pop("portrait", None)
        event = {"cleared": True}
    else:
        if set(request) != {"expected_manifest_sha256", "mime_type", "asset_base64"}:
            raise ValueError("invalid_portrait_request")
        encoded = request["asset_base64"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PORTRAIT_BYTES + 2) // 3):
            raise ValueError("invalid_portrait_size")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid_portrait_base64") from exc
        mime = str(request["mime_type"])
        width, height = _portrait_dimensions(data, mime)
        version = hashlib.sha256(data).hexdigest()
        name = f"portrait-{version}{MIME_EXTENSIONS[mime]}"
        root = ASSET_ROOT.resolve(); root.mkdir(parents=True, exist_ok=True)
        destination = (root / name).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("unsafe_pet_asset_path")
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != version:
                raise ValueError("portrait_asset_conflict")
        else:
            temporary = root / (name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(data); os.chmod(temporary, 0o600); os.replace(temporary, destination)
        manifest["portrait"] = {"asset_name": name, "mime_type": mime, "width": width, "height": height, "bytes": len(data), "content_version": version}
        event = {"content_version": version, "bytes": len(data)}
    now = _utc_now()
    changed = conn.execute("UPDATE pet_packs SET manifest_json=?,updated_at=? WHERE id=? AND manifest_json=? AND status='active'",
                           (json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), now, pack_id, raw))
    if changed.rowcount != 1:
        raise ValueError("portrait_manifest_conflict")
    conn.execute("INSERT INTO pet_pack_events(pack_id,action,detail_json,created_at) VALUES(?,?,?,?)", (pack_id, "portrait", json.dumps(event), now))
    return pet_state(conn, integrity_scope="database")


def pet_portrait_asset(conn: sqlite3.Connection, pack_id: str, version: str) -> tuple[bytes, str] | None:
    state = pet_state(conn)
    if not pack_id or pack_id != state["pack_id"]:
        return None
    pack = next((item for item in state["packs"] if item["id"] == pack_id), None)
    resource = _portrait_resource(pack, pack["manifest"]) if pack else None
    if resource is None or resource[1]["content_version"][:16] != version:
        return None
    return resource[0], resource[1]["mime_type"]


def _validated_manifest(raw: object, data: bytes, mime: str) -> dict:
    if raw in (None, "", {}):
        return {
            "schema_version": 1,
            "renderer": "image",
            "states": {"idle": {"animation": "native"}},
            "default_scale": 1.0,
            "anchor": "bottom",
        }
    try:
        manifest = json.loads(raw) if isinstance(raw, str) else dict(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_pet_manifest") from exc
    renderer = str(manifest.get("renderer") or "image")
    if renderer == "image":
        return {
            "schema_version": 1,
            "renderer": "image",
            "states": {"idle": {"animation": "native"}},
            "default_scale": float(manifest.get("default_scale", 1.0)),
            "anchor": "bottom",
        }
    if renderer != "spritesheet" or mime not in {"image/png", "image/webp"}:
        raise ValueError("invalid_pet_manifest")
    atlas = manifest.get("atlas")
    states = manifest.get("states")
    if not isinstance(atlas, dict) or not isinstance(states, dict) or "idle" not in states:
        raise ValueError("invalid_pet_manifest")
    try:
        width = int(atlas["width"])
        height = int(atlas["height"])
        columns = int(atlas["columns"])
        rows = int(atlas["rows"])
        cell_width = int(atlas["cell_width"])
        cell_height = int(atlas["cell_height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid_pet_manifest") from exc
    actual_dimensions = _image_dimensions(data, mime)
    if (
        min(width, height, columns, rows, cell_width, cell_height) <= 0
        or columns > 16 or rows > 16
        or width != columns * cell_width or height != rows * cell_height
        or actual_dimensions != (width, height)
    ):
        raise ValueError("pet_manifest_dimensions_mismatch")
    clean_states: dict[str, dict] = {}
    for name, spec in states.items():
        if name not in ALLOWED_ANIMATION_STATES or not isinstance(spec, dict):
            raise ValueError("invalid_pet_manifest")
        try:
            row = int(spec["row"])
            frames = int(spec["frames"])
            fps = float(spec.get("fps", 4))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid_pet_manifest") from exc
        if not 0 <= row < rows or not 1 <= frames <= columns or not 1 <= fps <= 24:
            raise ValueError("invalid_pet_manifest")
        clean_states[name] = {"row": row, "frames": frames, "fps": fps, "loop": bool(spec.get("loop", True))}
    return {
        "schema_version": 2,
        "renderer": "spritesheet",
        "atlas": {
            "width": width, "height": height, "columns": columns, "rows": rows,
            "cell_width": cell_width, "cell_height": cell_height,
        },
        "states": clean_states,
        "default_state": str(manifest.get("default_state") or "idle"),
        "reduced_motion_frame": manifest.get("reduced_motion_frame") or {"state": "idle", "frame": 0},
        "default_scale": max(0.5, min(float(manifest.get("default_scale", 1.0)), 1.8)),
        "anchor": "bottom",
    }


def _validated_asset(payload: dict) -> tuple[bytes, str, str, dict]:
    mime = str(payload.get("mime_type") or "").strip().lower()
    if mime not in MIME_EXTENSIONS:
        raise ValueError("unsupported_pet_asset_type")
    encoded = str(payload.get("asset_base64") or "").strip()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid_pet_asset_base64") from exc
    if not data or len(data) > MAX_ASSET_BYTES:
        raise ValueError("invalid_pet_asset_size")
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
    }
    if not signatures[mime]:
        raise ValueError("pet_asset_signature_mismatch")
    return data, mime, MIME_EXTENSIONS[mime], _validated_manifest(payload.get("manifest"), data, mime)


def import_pet_pack(conn: sqlite3.Connection, payload: dict) -> dict:
    ensure_pet_tables(conn)
    name = re.sub(r"\s+", " ", str(payload.get("name") or "").strip())[:80]
    if len(name) < 2:
        raise ValueError("invalid_pet_pack_name")
    author = re.sub(r"\s+", " ", str(payload.get("author") or "自定义").strip())[:80]
    license_note = re.sub(r"\s+", " ", str(payload.get("license") or "private-use").strip())[:160]
    data, mime, extension, manifest = _validated_asset(payload)
    pack_id = "pet-" + uuid.uuid4().hex
    root = ASSET_ROOT.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / f"{pack_id}{extension}").resolve()
    if not destination.is_relative_to(root):
        raise ValueError("unsafe_pet_asset_path")
    temp = destination.with_suffix(destination.suffix + ".tmp")
    temp.write_bytes(data)
    os.chmod(temp, 0o600)
    os.replace(temp, destination)
    now = _utc_now()
    try:
        conn.execute(
            """
            INSERT INTO pet_packs(
                id,name,author,license,asset_name,mime_type,manifest_json,built_in,
                created_at,updated_at,owner_actor_id,status,source_type,deleted_at
            ) VALUES(?,?,?,?,?,?,?,0,?,?,'owner-local','active','user_import','')
            """,
            (
                pack_id,
                name,
                author,
                license_note,
                destination.name,
                mime,
                json.dumps(manifest),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO pet_pack_events(pack_id,action,detail_json,created_at) VALUES(?,?,?,?)",
            (pack_id, "import", json.dumps({"bytes": len(data), "mime": mime}), now),
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"pack": next(item for item in list_pet_packs(conn) if item["id"] == pack_id), "state": pet_state(conn, integrity_scope="database")}


def delete_pet_pack(conn: sqlite3.Connection, payload: dict) -> dict:
    pack_id = str(payload.get("pack_id") or "").strip()
    if payload.get("confirm") is not True:
        raise ValueError("pet_delete_confirmation_required")
    row = conn.execute(
        "SELECT asset_name,built_in,status FROM pet_packs WHERE id=?",
        (pack_id,),
    ).fetchone()
    if not row:
        raise ValueError("pet_pack_not_found")
    identity_migrated = _assistant_identity_migrated(conn)
    if bool(row[1]) and not identity_migrated:
        raise ValueError("builtin_pet_pack_protected")
    asset_name = str(row[0])
    if str(row[2]) != "active":
        raise ValueError("pet_pack_not_found")
    if identity_migrated:
        from bridge_assistant_resources import replace_or_unbind_appearance

        replace_or_unbind_appearance(
            conn,
            pack_id,
            replacement_pack_id=str(payload.get("replacement_pack_id") or "").strip(),
            unbind=payload.get("unbind") is True,
        )
    elif _setting(conn, "admin_pet_pack_id", BUILTIN_PACK_ID) == pack_id:
        _write_setting(conn, "admin_pet_pack_id", BUILTIN_PACK_ID)
    conn.execute("DELETE FROM pet_packs WHERE id = ?", (pack_id,))
    conn.execute(
        "INSERT INTO pet_pack_events(pack_id,action,detail_json,created_at) VALUES(?,?,?,?)",
        (pack_id, "delete", "{}", _utc_now()),
    )
    root = ASSET_ROOT.resolve()
    target = (root / asset_name).resolve()
    if not bool(row[1]) and target.is_relative_to(root):
        target.unlink(missing_ok=True)
    return pet_state(conn, integrity_scope="database")


def pet_asset(conn: sqlite3.Connection, pack_id: str) -> tuple[bytes, str] | None:
    ensure_pet_tables(conn)
    row = conn.execute("SELECT asset_name,mime_type,built_in FROM pet_packs WHERE id = ?", (pack_id,)).fetchone()
    if not row:
        return None
    if bool(row[2]):
        path = BUILTIN_ASSET_PATH
    else:
        root = ASSET_ROOT.resolve()
        path = (root / str(row[0])).resolve()
        if not path.is_relative_to(root):
            return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data, str(row[1])


__all__ = [
    "BUILTIN_PACK_ID", "delete_pet_pack", "ensure_pet_tables", "import_pet_pack",
    "list_pet_packs", "pet_asset", "pet_state", "save_pet_settings",
]
