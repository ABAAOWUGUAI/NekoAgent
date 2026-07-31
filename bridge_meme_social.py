#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import random
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BRIDGE_DIR = Path(os.environ.get("ASSISTANT_PLATFORM_BRIDGE_DIR", "/opt/agent-stack/codex-qq-bridge"))
ASSET_DIR=Path(os.environ.get("AGENT_SOCIAL_ASSET_ROOT",str(BRIDGE_DIR/"assets")))
MEME_DIR = ASSET_DIR / "memes"
DEFAULT_SAMPLE_SOURCE = ASSET_DIR / "sample-background.jpg"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        if name in {"UTC", "Etc/UTC"}:
            return timezone.utc
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        raise
EMOTION_KEYWORDS = {
    "happy": ("开心", "好", "棒", "通过", "完成", "ok", "成功", "笑", "哈哈"),
    "comfort": ("难受", "掉线", "失败", "崩", "骂", "烦", "累", "冷冰冰"),
    "work": ("任务", "代码", "服务器", "代理", "上线", "修", "验证", "日志"),
    "daily": ("聊天", "在吗", "当前助手", "想你", "日常", "主动"),
}
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_UPLOAD_BYTES = 3 * 1024 * 1024
OFFICIAL_SAMPLE_OPUS = ""
BUNDLED_SAMPLE_STICKERS = ()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_social_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meme_assets (
            id TEXT PRIMARY KEY,
            pack TEXT NOT NULL DEFAULT 'default',
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            description_method TEXT NOT NULL DEFAULT '',
            emotion TEXT NOT NULL DEFAULT 'daily',
            tags TEXT NOT NULL DEFAULT '',
            creator TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            license_note TEXT NOT NULL DEFAULT '',
            license_url TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            public_url TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            weight INTEGER NOT NULL DEFAULT 5,
            usage_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT NOT NULL DEFAULT '',
            file_hash TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            file_size INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'approved',
            cooldown_minutes INTEGER NOT NULL DEFAULT 60,
            max_daily INTEGER NOT NULL DEFAULT 3,
            source_kind TEXT NOT NULL DEFAULT 'local',
            rights_status TEXT NOT NULL DEFAULT 'unknown',
            usage_scope TEXT NOT NULL DEFAULT 'private_owner',
            category TEXT NOT NULL DEFAULT 'daily',
            intent TEXT NOT NULL DEFAULT 'reaction',
            intensity INTEGER NOT NULL DEFAULT 2,
            source_page_url TEXT NOT NULL DEFAULT '',
            media_url TEXT NOT NULL DEFAULT '',
            attribution_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meme_send_history (
            id TEXT PRIMARY KEY,
            meme_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            session TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'daily',
            emotion TEXT NOT NULL DEFAULT 'daily',
            status TEXT NOT NULL DEFAULT 'selected',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proactive_plans (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            interval_minutes INTEGER NOT NULL DEFAULT 360,
            quiet_start TEXT NOT NULL DEFAULT '23:30',
            quiet_end TEXT NOT NULL DEFAULT '09:00',
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            include_meme INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'scheduled',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL DEFAULT '',
            last_sent_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            next_due_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qq_sessions (
            user_id TEXT PRIMARY KEY,
            session TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meme_assets_enabled ON meme_assets(enabled, emotion)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meme_history_user ON meme_send_history(user_id, status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meme_history_asset ON meme_send_history(meme_id, status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proactive_due ON proactive_plans(enabled, next_due_at)")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(proactive_plans)").fetchall()}
    migrations = {
        "timezone": "TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
        "include_meme": "INTEGER NOT NULL DEFAULT 0",
        "state": "TEXT NOT NULL DEFAULT 'scheduled'",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "sent_count": "INTEGER NOT NULL DEFAULT 0",
        "failed_count": "INTEGER NOT NULL DEFAULT 0",
        "last_attempt_at": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "lease_until": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in migrations.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE proactive_plans ADD COLUMN {column} {definition}")
    meme_existing = {row[1] for row in conn.execute("PRAGMA table_info(meme_assets)").fetchall()}
    meme_migrations = {
        "description": "TEXT NOT NULL DEFAULT ''",
        "description_method": "TEXT NOT NULL DEFAULT ''",
        "creator": "TEXT NOT NULL DEFAULT ''",
        "license_url": "TEXT NOT NULL DEFAULT ''",
        "last_used_at": "TEXT NOT NULL DEFAULT ''",
        "file_hash": "TEXT NOT NULL DEFAULT ''",
        "mime_type": "TEXT NOT NULL DEFAULT ''",
        "file_size": "INTEGER NOT NULL DEFAULT 0",
        "review_status": "TEXT NOT NULL DEFAULT 'approved'",
        "cooldown_minutes": "INTEGER NOT NULL DEFAULT 60",
        "max_daily": "INTEGER NOT NULL DEFAULT 3",
        "source_kind": "TEXT NOT NULL DEFAULT 'local'",
        "rights_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "usage_scope": "TEXT NOT NULL DEFAULT 'private_owner'",
        "category": "TEXT NOT NULL DEFAULT 'daily'",
        "intent": "TEXT NOT NULL DEFAULT 'reaction'",
        "intensity": "INTEGER NOT NULL DEFAULT 2",
        "source_page_url": "TEXT NOT NULL DEFAULT ''",
        "media_url": "TEXT NOT NULL DEFAULT ''",
        "attribution_required": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in meme_migrations.items():
        if column not in meme_existing:
            conn.execute(f"ALTER TABLE meme_assets ADD COLUMN {column} {definition}")
    conn.execute(
        """
        UPDATE meme_assets SET enabled = 0, review_status = 'pending'
        WHERE id = 'sample-background'
        """,
    )
    conn.execute(
        """
        UPDATE meme_assets SET enabled = 0, review_status = 'pending'
        WHERE pack = 'sample-candidates' AND file_size = 0
        """,
    )


def _asset_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:12]


def seed_default_memes(conn: sqlite3.Connection) -> None:
    MEME_DIR.mkdir(parents=True, exist_ok=True)
    for filename, name, emotion, tags in BUNDLED_SAMPLE_STICKERS:
        path = MEME_DIR / filename
        if not path.exists():
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        now = utc_now()
        conn.execute(
            """INSERT OR IGNORE INTO meme_assets(
                   id, pack, name, emotion, tags, source, license_note,
                   file_path, public_url, enabled, weight, usage_count,
                   last_used_at, file_hash, mime_type, file_size, review_status,
                   cooldown_minutes, max_daily, created_at, updated_at
               ) VALUES (?, 'sample-official', ?, ?, ?, ?, ?, ?, ?, 1, 5, 0,
                         '', ?, 'image/webp', ?, 'approved', 90, 3, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   pack=excluded.pack, name=excluded.name, emotion=excluded.emotion,
                   tags=excluded.tags, source=excluded.source, license_note=excluded.license_note,
                   file_path=excluded.file_path, public_url=excluded.public_url,
                   enabled=1, file_hash=excluded.file_hash, mime_type=excluded.mime_type,
                   file_size=excluded.file_size, review_status='approved', updated_at=excluded.updated_at""",
            (
                digest[:12],
                name,
                emotion,
                tags,
                OFFICIAL_SAMPLE_OPUS,
                "来自永雏示例官方 Bilibili 动态公开展示的表情素材，仅用于本机器人私有 QQ 聊天，不作二次分发。",
                str(path),
                f"/memes/assets/{filename}",
                digest,
                len(data),
                now,
                now,
            ),
        )
    if DEFAULT_SAMPLE_SOURCE.exists():
        target = MEME_DIR / "sample-background.jpg"
        if not target.exists():
            shutil.copy2(DEFAULT_SAMPLE_SOURCE, target)
        now = utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO meme_assets(
                id, pack, name, emotion, tags, source, license_note,
                file_path, public_url, enabled, weight, usage_count, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 5, 0, 'pending', ?, ?)
            """,
            (
                "sample-background",
                "sample",
                "永雏示例默认图",
                "daily",
                "sample,永雏示例,默认,日常",
                "local-project-asset",
                "来自当前项目已有资源，仅用于本机器人私有环境。",
                str(target),
                "/memes/assets/sample-background.jpg",
                now,
                now,
            ),
        )
    now = utc_now()
    for title, url in (
        ("永雏示例表情包候选搜索", "https://www.bing.com/images/search?q=%E6%B0%B8%E9%9B%8F%E5%A1%94%E8%8F%B2%20%E8%A1%A8%E6%83%85%E5%8C%85"),
        ("永雏示例图片候选搜索", "https://www.bing.com/images/search?q=%E6%B0%B8%E9%9B%8F%E5%A1%94%E8%8F%B2%20%E5%9B%BE%E7%89%87"),
    ):
        conn.execute(
            """
            INSERT OR IGNORE INTO meme_assets(
                id, pack, name, emotion, tags, source, license_note,
                file_path, public_url, enabled, weight, usage_count, review_status, created_at, updated_at
            ) VALUES (?, 'sample-candidates', ?, 'daily', 'sample,候选,待审核', ?, ?, '', ?, 0, 1, 0, 'pending', ?, ?)
            """,
            (
                _asset_id(url),
                title,
                url,
                "外部图片来源与授权未确认，默认不进入自动发送。",
                url,
                now,
                now,
            ),
        )


def update_qq_session(conn: sqlite3.Connection, user_id: str, session: str) -> None:
    user_id = (user_id or "").strip()
    session = (session or "").strip()
    if not user_id or not session:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO qq_sessions(user_id, session, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET session = excluded.session, updated_at = excluded.updated_at
        """,
        (user_id, session, now),
    )


def list_meme_assets(conn: sqlite3.Connection, *, enabled: str = "", limit: int = 80) -> list[dict]:
    where = []
    params: list[object] = []
    if enabled in {"0", "1"}:
        where.append("enabled = ?")
        params.append(int(enabled))
    sql = "SELECT * FROM meme_assets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY enabled DESC, pack, usage_count ASC, name LIMIT ?"
    params.append(max(1, min(int(limit or 80), 200)))
    rows = conn.execute(sql, params).fetchall()
    assets = []
    for row in rows:
        item = dict(row)
        stats = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                MAX(CASE WHEN status = 'sent' THEN sent_at ELSE '' END) AS last_sent_at
            FROM meme_send_history WHERE meme_id = ?
            """,
            (item["id"],),
        ).fetchone()
        item["delivery"] = dict(stats) if stats else {"sent_count": 0, "failed_count": 0, "last_sent_at": ""}
        assets.append(item)
    return assets


def upsert_meme_asset(conn: sqlite3.Connection, payload: dict) -> dict:
    now = utc_now()
    name = str(payload.get("name") or "").strip()
    public_url = str(payload.get("public_url") or payload.get("url") or "").strip()
    file_path = str(payload.get("file_path") or "").strip()
    if not name:
        raise ValueError("name_required")
    if not public_url and not file_path:
        raise ValueError("asset_path_required")
    asset_id = str(payload.get("id") or "").strip() or _asset_id(name + public_url + file_path)
    enabled = 1 if str(payload.get("enabled", "1")).lower() in {"1", "true", "yes", "on"} else 0
    review_status = str(payload.get("review_status") or "approved").strip().lower()
    if review_status not in {"pending", "approved", "rejected"}:
        raise ValueError("invalid_review_status")
    if review_status != "approved":
        enabled = 0
    conn.execute(
        """
        INSERT INTO meme_assets(
            id, pack, name, description, description_method, emotion, tags, creator,
            source, license_note, license_url, file_path, public_url, enabled, weight,
            usage_count, review_status, cooldown_minutes, max_daily, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            pack = excluded.pack,
            name = excluded.name,
            description = excluded.description,
            description_method = excluded.description_method,
            emotion = excluded.emotion,
            tags = excluded.tags,
            creator = excluded.creator,
            source = excluded.source,
            license_note = excluded.license_note,
            license_url = excluded.license_url,
            file_path = excluded.file_path,
            public_url = excluded.public_url,
            enabled = excluded.enabled,
            weight = excluded.weight,
            review_status = excluded.review_status,
            cooldown_minutes = excluded.cooldown_minutes,
            max_daily = excluded.max_daily,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            str(payload.get("pack") or "default").strip() or "default",
            name,
            str(payload.get("description") or "").strip()[:500],
            str(payload.get("description_method") or "").strip()[:40],
            str(payload.get("emotion") or "daily").strip() or "daily",
            str(payload.get("tags") or "").strip(),
            str(payload.get("creator") or "").strip()[:160],
            str(payload.get("source") or "").strip(),
            str(payload.get("license_note") or "").strip(),
            str(payload.get("license_url") or "").strip()[:1000],
            file_path,
            public_url,
            enabled,
            max(1, min(int(payload.get("weight") or 5), 20)),
            review_status,
            max(0, min(int(payload.get("cooldown_minutes") or 60), 10080)),
            max(1, min(int(payload.get("max_daily") or 3), 100)),
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM meme_assets WHERE id = ?", (asset_id,)).fetchone()
    return dict(row)


def _detect_image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("unsupported_image_type")


def save_uploaded_meme(conn: sqlite3.Connection, payload: dict) -> dict:
    raw = str(payload.get("data_base64") or payload.get("data") or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError("invalid_image_base64") from exc
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image_size_invalid")
    mime, suffix = _detect_image_type(data)
    digest = hashlib.sha256(data).hexdigest()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name_required")
    MEME_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{digest[:20]}{suffix}"
    target = (MEME_DIR / filename).resolve()
    if MEME_DIR.resolve() not in target.parents:
        raise ValueError("invalid_asset_path")
    if not target.exists():
        target.write_bytes(data)
    item_payload = dict(payload)
    item_payload.update(
        {
            "id": str(payload.get("id") or digest[:12]),
            "file_path": str(target),
            "public_url": f"/memes/assets/{filename}",
            "review_status": str(payload.get("review_status") or "approved"),
            "source": str(payload.get("source") or "admin-upload"),
        },
    )
    item = upsert_meme_asset(conn, item_payload)
    conn.execute(
        """
        UPDATE meme_assets
        SET file_hash = ?, mime_type = ?, file_size = ?, updated_at = ?
        WHERE id = ?
        """,
        (digest, mime, len(data), utc_now(), item["id"]),
    )
    return dict(conn.execute("SELECT * FROM meme_assets WHERE id = ?", (item["id"],)).fetchone())

def choose_emotion(text: str, mode: str = "daily", intent: str = "chat") -> str:
    joined = f"{text or ''} {mode or ''} {intent or ''}".lower()
    if intent in {"ops", "code", "research", "analysis"} or mode == "work":
        return "work"
    scores = {}
    for emotion, words in EMOTION_KEYWORDS.items():
        scores[emotion] = sum(1 for word in words if word.lower() in joined)
    best = max(scores, key=scores.get)
    return best if scores[best] else "daily"

def choose_meme(
    conn: sqlite3.Connection,
    *,
    text: str = "",
    mode: str = "daily",
    intent: str = "chat",
    increment_usage: bool = True,
    user_id: str = "",
    session: str = "",
    emotion_hint: str = "", allow_recent_reuse: bool = False,
) -> dict | None:
    emotion = str(emotion_hint or "").strip().lower() or choose_emotion(text, mode=mode, intent=intent)
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = conn.execute(
        """
        SELECT a.*,
               (SELECT COUNT(*) FROM meme_send_history h
                WHERE h.meme_id = a.id AND h.status = 'sent' AND h.sent_at >= ?) AS sent_today
        FROM meme_assets a
        WHERE a.enabled = 1 AND a.review_status = 'approved'
          AND (a.emotion = ? OR a.emotion = 'daily' OR a.tags LIKE ?)
        ORDER BY a.usage_count ASC, a.weight DESC, a.updated_at DESC
        LIMIT 40
        """,
        (day_start, emotion, f"%{emotion}%"),
    ).fetchall()
    if not rows:
        return None
    recent = set() if allow_recent_reuse else {
        str(row["meme_id"])
        for row in conn.execute(
            """
            SELECT meme_id FROM meme_send_history
            WHERE user_id = ? AND status = 'sent' ORDER BY sent_at DESC LIMIT 3
            """,
            (str(user_id or ""),),
        ).fetchall()
    }
    weighted = []
    for row in rows:
        item = dict(row)
        if int(item.get("sent_today") or 0) >= int(item.get("max_daily") or 3):
            continue
        if item["id"] in recent:
            continue
        last_used = None
        try:
            last_used = datetime.fromisoformat(str(item.get("last_used_at") or ""))
            if last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=timezone.utc)
        except ValueError:
            last_used = None
        if last_used and now - last_used < timedelta(minutes=max(0, int(item.get("cooldown_minutes") or 60))):
            continue
        bonus = 4 if item.get("emotion") == emotion else 1
        inverse_usage = max(1, 8 - min(int(item.get("usage_count") or 0), 7))
        count = max(1, min(int(item.get("weight") or 1) + bonus + inverse_usage, 30))
        weighted.extend([item] * count)
    if not weighted:
        return None
    selected = random.choice(weighted)
    selection_id = uuid.uuid4().hex[:16]
    conn.execute(
        """
        INSERT INTO meme_send_history(
            id, meme_id, user_id, session, mode, emotion, status, error, created_at, sent_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'selected', '', ?, '')
        """,
        (
            selection_id,
            selected["id"],
            str(user_id or "").strip(),
            str(session or "").strip(),
            str(mode or "daily").strip(),
            emotion,
            utc_now(),
        ),
    )
    if increment_usage:
        mark_meme_delivery(conn, selection_id, status="sent")
    selected["selected_emotion"] = emotion
    selected["selection_id"] = selection_id
    return selected


def mark_meme_delivery(conn: sqlite3.Connection, selection_id: str, *, status: str, error: str = "") -> dict | None:
    row = conn.execute("SELECT * FROM meme_send_history WHERE id = ?", (str(selection_id or "").strip(),)).fetchone()
    if not row:
        return None
    normalized = "sent" if status == "sent" else "failed"
    now = utc_now()
    conn.execute(
        """
        UPDATE meme_send_history SET status = ?, error = ?, sent_at = ? WHERE id = ?
        """,
        (normalized, str(error or "")[:1000], now if normalized == "sent" else "", row["id"]),
    )
    if normalized == "sent" and row["status"] != "sent":
        conn.execute(
            """
            UPDATE meme_assets
            SET usage_count = usage_count + 1, last_used_at = ?, updated_at = ? WHERE id = ?
            """,
            (now, now, row["meme_id"]),
        )
    return dict(conn.execute("SELECT * FROM meme_send_history WHERE id = ?", (row["id"],)).fetchone())


def public_asset(path_name: str) -> tuple[bytes, str] | None:
    name = (path_name or "").strip().lstrip("/")
    if not name or "/" in name or "\\" in name:
        return None
    target = (MEME_DIR / name).resolve()
    if not str(target).startswith(str(MEME_DIR.resolve())) or not target.is_file():
        return None
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return target.read_bytes(), mime


def list_proactive_plans(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.*, s.session AS known_session
        FROM proactive_plans p
        LEFT JOIN qq_sessions s ON s.user_id = p.user_id
        ORDER BY p.enabled DESC, p.next_due_at ASC, p.updated_at DESC
        LIMIT ?
        """,
        (max(1, min(int(limit or 50), 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_proactive_plan(conn: sqlite3.Connection, payload: dict) -> dict:
    user_id = str(payload.get("user_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    if not user_id:
        raise ValueError("user_id_required")
    if not title:
        raise ValueError("title_required")
    if not prompt:
        raise ValueError("prompt_required")
    now = utc_now()
    interval = max(15, min(int(payload.get("interval_minutes") or 360), 10080))
    plan_id = str(payload.get("id") or "").strip() or _asset_id(user_id + title + prompt)
    enabled = 1 if str(payload.get("enabled", "1")).lower() in {"1", "true", "yes", "on"} else 0
    include_meme = 1 if str(payload.get("include_meme", "0")).lower() in {"1", "true", "yes", "on"} else 0
    timezone_name = str(payload.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        _timezone(timezone_name)
    except Exception as exc:
        raise ValueError("invalid_timezone") from exc
    session = str(payload.get("session") or "").strip()
    if not session:
        row = conn.execute("SELECT session FROM qq_sessions WHERE user_id = ?", (user_id,)).fetchone()
        session = str(row["session"]) if row else ""
    next_due = str(payload.get("next_due_at") or "").strip()
    if not next_due:
        next_due = (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat()
    conn.execute(
        """
        INSERT INTO proactive_plans(
            id, user_id, session, title, prompt, enabled, interval_minutes,
            quiet_start, quiet_end, timezone, include_meme, state,
            last_sent_at, next_due_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', '', ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            user_id = excluded.user_id,
            session = excluded.session,
            title = excluded.title,
            prompt = excluded.prompt,
            enabled = excluded.enabled,
            interval_minutes = excluded.interval_minutes,
            quiet_start = excluded.quiet_start,
            quiet_end = excluded.quiet_end,
            timezone = excluded.timezone,
            include_meme = excluded.include_meme,
            state = CASE WHEN excluded.enabled = 1 THEN 'scheduled' ELSE 'disabled' END,
            lease_until = '',
            next_due_at = excluded.next_due_at,
            updated_at = excluded.updated_at
        """,
        (
            plan_id,
            user_id,
            session,
            title,
            prompt,
            enabled,
            interval,
            str(payload.get("quiet_start") or "23:30").strip() or "23:30",
            str(payload.get("quiet_end") or "09:00").strip() or "09:00",
            timezone_name,
            include_meme,
            next_due,
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM proactive_plans WHERE id = ?", (plan_id,)).fetchone()
    return dict(row)


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quiet_end(now_utc: datetime, item: dict) -> datetime | None:
    try:
        zone = _timezone(str(item.get("timezone") or DEFAULT_TIMEZONE))
        start_hour, start_minute = [int(part) for part in str(item.get("quiet_start") or "23:30").split(":", 1)]
        end_hour, end_minute = [int(part) for part in str(item.get("quiet_end") or "09:00").split(":", 1)]
    except (ValueError, TypeError):
        return None
    local = now_utc.astimezone(zone)
    minute = local.hour * 60 + local.minute
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return None
    inside = start <= minute < end if start < end else minute >= start or minute < end
    if not inside:
        return None
    target = local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if start >= end and minute >= start:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def due_proactive_plans(conn: sqlite3.Connection, limit: int = 3) -> list[dict]:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    rows = conn.execute(
        """
        SELECT p.*, COALESCE(NULLIF(p.session, ''), s.session, '') AS send_session
        FROM proactive_plans p
        LEFT JOIN qq_sessions s ON s.user_id = p.user_id
        WHERE p.enabled = 1 AND p.next_due_at <= ?
        ORDER BY p.next_due_at ASC
        LIMIT 50
        """,
        (now,),
    ).fetchall()
    plans = []
    wanted = max(1, min(int(limit or 3), 10))
    for row in rows:
        item = dict(row)
        if len(plans) >= wanted:
            break
        lease_until = _parse_iso(item.get("lease_until"))
        if lease_until and lease_until > now_dt:
            continue
        if not str(item.get("send_session") or "").strip():
            retry_at = now_dt + timedelta(minutes=15)
            conn.execute(
                "UPDATE proactive_plans SET state = 'pending_session', next_due_at = ?, updated_at = ? WHERE id = ?",
                (retry_at.isoformat(), now, item["id"]),
            )
            continue
        quiet_until = _quiet_end(now_dt, item)
        if quiet_until:
            conn.execute(
                "UPDATE proactive_plans SET state = 'quiet', next_due_at = ?, updated_at = ? WHERE id = ?",
                (quiet_until.isoformat(), now, item["id"]),
            )
            continue
        lease = now_dt + timedelta(minutes=3)
        claimed = conn.execute(
            """
            UPDATE proactive_plans
            SET state = 'sending', lease_until = ?, last_attempt_at = ?, updated_at = ?
            WHERE id = ? AND enabled = 1 AND (lease_until = '' OR lease_until <= ?)
            """,
            (lease.isoformat(), now, now, item["id"], now),
        )
        if claimed.rowcount != 1:
            continue
        item["state"] = "sending"
        item["lease_until"] = lease.isoformat()
        item["message"] = compose_proactive_text(item)
        plans.append(item)
    return plans


def mark_proactive_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    status: str = "sent",
    *,
    error: str = "",
    meme_id: str = "",
) -> dict | None:
    row = conn.execute("SELECT * FROM proactive_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    now_dt = datetime.now(timezone.utc)
    interval = max(15, int(item.get("interval_minutes") or 360))
    normalized = "sent" if status == "sent" else "failed"
    if normalized == "sent":
        next_due = now_dt + timedelta(minutes=interval)
        conn.execute(
            """
            UPDATE proactive_plans
            SET state = 'scheduled', consecutive_failures = 0,
                sent_count = sent_count + 1, last_attempt_at = ?, last_sent_at = ?,
                last_error = '', lease_until = '', next_due_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_dt.isoformat(), now_dt.isoformat(), next_due.isoformat(), now_dt.isoformat(), plan_id),
        )
        if meme_id:
            conn.execute(
                "UPDATE meme_assets SET usage_count = usage_count + 1, updated_at = ? WHERE id = ?",
                (now_dt.isoformat(), meme_id),
            )
    else:
        failures = int(item.get("consecutive_failures") or 0) + 1
        backoff_minutes = min(interval, 5 * (2 ** min(failures - 1, 6)))
        next_due = now_dt + timedelta(minutes=backoff_minutes)
        conn.execute(
            """
            UPDATE proactive_plans
            SET state = 'retry_wait', consecutive_failures = ?,
                failed_count = failed_count + 1, last_attempt_at = ?, last_error = ?,
                lease_until = '', next_due_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (failures, now_dt.isoformat(), str(error or "")[:1000], next_due.isoformat(), now_dt.isoformat(), plan_id),
        )
    updated = dict(conn.execute("SELECT * FROM proactive_plans WHERE id = ?", (plan_id,)).fetchone())
    updated["mark_status"] = normalized
    return updated


def compose_proactive_text(plan: dict) -> str:
    prompt = str(plan.get("prompt") or "").strip()
    title = str(plan.get("title") or "提醒").strip()
    if prompt:
        return f"{title}：{prompt}"
    return f"{title}：我来看看你现在需不需要我帮忙。"
