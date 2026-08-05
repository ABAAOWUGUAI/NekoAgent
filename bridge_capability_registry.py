#!/usr/bin/env python3
"""Plugin inventory and task Skill registry for the QQ agent bridge."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - production image already provides PyYAML
    yaml = None


BRIDGE_DIR = Path(os.environ.get("ASSISTANT_PLATFORM_BRIDGE_DIR", "/opt/agent-stack/codex-qq-bridge"))
SKILL_ROOT = Path(os.environ.get("ASSISTANT_PLATFORM_SKILL_ROOT", str(BRIDGE_DIR / "skills")))
ASTRBOT_PLUGIN_ROOT = Path(os.environ.get("ASTRBOT_PLUGIN_ROOT", "/opt/agent-stack/astrbot/data/plugins"))
ASTRBOT_DB_PATH = Path(os.environ.get("ASTRBOT_DB_PATH", "/opt/agent-stack/astrbot/data/data_v4.db"))
ASTRBOT_CONTAINER = os.environ.get("ASTRBOT_CONTAINER", "astrbot")
PROTECTED_PLUGINS = {"codex_agent"}
SECRET_PATTERNS = (
    "BEGIN OPENSSH PRIVATE KEY",
    "sk-proj-",
    "Authorization: Bearer",
)

BUILTIN_SKILLS = (
    {
        "id": "server-diagnostics",
        "name": "服务器故障诊断",
        "description": "用于服务器、Docker、QQ 链路、代理、日志和资源异常的只读诊断。",
        "scope": "ops",
        "triggers": "服务器,docker,容器,日志,掉线,代理,端口,内存,磁盘,qq",
        "instructions": """# 服务器故障诊断

先建立时间线，再区分现象、直接证据、推断和仍未知的信息。

- 优先做只读检查：服务状态、容器状态、近期日志、资源和网络连通性。
- 不把重启当作诊断；只有证据指向且用户已授权时才重启。
- 输出必须包含：发现、证据、根因置信度、已执行操作、仍需观察项。
- 日志中的密码、令牌、Cookie、私钥和二维码内容不得出现在回复中。
""",
    },
    {
        "id": "project-change",
        "name": "项目改动与上线",
        "description": "用于代码修改、重构、测试、部署和回归验证。",
        "scope": "code",
        "triggers": "代码,开发,修复,重构,测试,部署,上线,项目,ui,前端,后端",
        "instructions": """# 项目改动与上线

先阅读项目约定和现有结构，再做范围清晰的修改。

- 改动前确认用户目标、主要流程和验收标准。
- 优先沿用现有模式，并把新增职责放入独立模块。
- 行为变化要有针对性测试；部署后检查服务、日志和真实接口。
- 汇报需求理解、改动文件、验证结果和剩余风险，不声称未验证的结果。
""",
    },
    {
        "id": "source-research",
        "name": "开源项目研究",
        "description": "用于参考 GitHub、官方文档和开源实现并形成可落地方案。",
        "scope": "research",
        "triggers": "github,开源,参考,文档,资料,调研,麦麦,maibot",
        "instructions": """# 开源项目研究

优先阅读官方仓库、官方文档和一手源码。

- 区分可直接复用、只能借鉴概念和受许可证限制的内容。
- 不只罗列功能，要说明它解决的用户问题、运行机制和迁移成本。
- 给出适合当前项目规模的最小实现，并在落地后做二次可行性验证。
""",
    },
)

# A Skill is procedural guidance; these allowlisted mappings connect it to the
# server-owned capability adapters without letting a document execute code.
SKILL_CAPABILITY_REQUIREMENTS = {
    "server-diagnostics": ("platform.health.read",),
    "project-change": ("codex.sandbox",),
    "source-research": ("github.trending.read",),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "开启"}


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_id(value: object, fallback: str = "skill") -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    text = text or fallback
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", text):
        raise ValueError("invalid_skill_id")
    return text


def ensure_capability_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_registry (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'all',
            triggers TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 5,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT NOT NULL DEFAULT '',
            validation_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    existing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(skill_registry)")
    }
    for name, declaration in (
        ("success_count", "INTEGER NOT NULL DEFAULT 0"),
        ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_outcome_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE skill_registry ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_registry_enabled ON skill_registry(enabled, scope, priority)")


def _skill_path(skill_id: str) -> Path:
    root = SKILL_ROOT.resolve()
    path = (root / skill_id / "SKILL.md").resolve()
    if root not in path.parents:
        raise ValueError("invalid_skill_path")
    return path


def _skill_document(item: dict) -> str:
    return "\n".join(
        [
            "---",
            f"name: {item['name']}",
            f"description: {item['description']}",
            f"scope: {item['scope']}",
            f"triggers: {item['triggers']}",
            "---",
            "",
            str(item["instructions"]).strip(),
            "",
        ],
    )


def _validate_skill_content(content: str) -> None:
    if not content.strip():
        raise ValueError("skill_instructions_required")
    if len(content) > 12000:
        raise ValueError("skill_instructions_too_long")
    if any(pattern.lower() in content.lower() for pattern in SECRET_PATTERNS):
        raise ValueError("skill_contains_possible_secret")


def seed_builtin_skills(conn: sqlite3.Connection) -> None:
    SKILL_ROOT.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    for raw in BUILTIN_SKILLS:
        item = dict(raw)
        _validate_skill_content(item["instructions"])
        path = _skill_path(item["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(_skill_document(item), encoding="utf-8")
        conn.execute(
            """
            INSERT OR IGNORE INTO skill_registry(
                id, name, description, scope, triggers, source_path, enabled,
                priority, use_count, last_used_at, validation_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 8, 0, '', '', ?, ?)
            """,
            (
                item["id"],
                item["name"],
                item["description"],
                item["scope"],
                item["triggers"],
                str(path),
                now,
                now,
            ),
        )


def upsert_skill(conn: sqlite3.Connection, payload: dict) -> dict:
    skill_id = _safe_id(payload.get("id") or payload.get("name"))
    name = _clip(payload.get("name"), 100)
    description = _clip(payload.get("description"), 500)
    instructions = str(payload.get("instructions") or "").strip()
    if not name:
        raise ValueError("skill_name_required")
    _validate_skill_content(instructions)
    scope = str(payload.get("scope") or "all").strip().lower()
    if scope not in {"all", "ops", "code", "research", "analysis", "memory", "chat"}:
        raise ValueError("invalid_skill_scope")
    try:
        priority = max(1, min(int(payload.get("priority") or 5), 20))
    except (TypeError, ValueError):
        priority = 5
    item = {
        "id": skill_id,
        "name": name,
        "description": description,
        "scope": scope,
        "triggers": _clip(payload.get("triggers"), 800),
        "instructions": instructions,
    }
    path = _skill_path(skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_skill_document(item), encoding="utf-8")
    now = utc_now()
    conn.execute(
        """
        INSERT INTO skill_registry(
            id, name, description, scope, triggers, source_path, enabled,
            priority, use_count, last_used_at, validation_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            scope = excluded.scope,
            triggers = excluded.triggers,
            source_path = excluded.source_path,
            enabled = excluded.enabled,
            priority = excluded.priority,
            validation_error = '',
            updated_at = excluded.updated_at
        """,
        (
            skill_id,
            name,
            description,
            scope,
            item["triggers"],
            str(path),
            1 if truthy(payload.get("enabled", "1")) else 0,
            priority,
            now,
            now,
        ),
    )
    return dict(conn.execute("SELECT * FROM skill_registry WHERE id = ?", (skill_id,)).fetchone())


def set_skill_enabled(conn: sqlite3.Connection, skill_id: str, enabled: bool) -> dict | None:
    now = utc_now()
    conn.execute(
        "UPDATE skill_registry SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, now, str(skill_id or "").strip()),
    )
    row = conn.execute("SELECT * FROM skill_registry WHERE id = ?", (str(skill_id or "").strip(),)).fetchone()
    return dict(row) if row else None


def _read_skill(path_text: object, limit: int = 12000) -> tuple[str, str]:
    try:
        path = Path(str(path_text or "")).resolve()
        root = SKILL_ROOT.resolve()
        if root not in path.parents:
            return "", "skill_path_outside_root"
        if not path.is_file():
            return "", "skill_file_missing"
        content = path.read_text(encoding="utf-8", errors="replace")[:limit]
        _validate_skill_content(content)
        return content, ""
    except Exception as exc:
        return "", str(exc)


def _frontmatter(content: str) -> dict[str, str]:
    text = str(content or "")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}
    result: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


def discover_local_skills(conn: sqlite3.Connection) -> list[dict]:
    """Discover validated SKILL.md files added under the controlled Skill root."""

    root = SKILL_ROOT.resolve()
    if not root.is_dir():
        return []
    discovered: list[dict] = []
    now = utc_now()
    for path in sorted(root.glob("*/SKILL.md")):
        try:
            resolved = path.resolve()
            if root not in resolved.parents:
                continue
            content = resolved.read_text(encoding="utf-8", errors="replace")
            _validate_skill_content(content)
            metadata = _frontmatter(content)
            skill_id = _safe_id(metadata.get("id") or resolved.parent.name)
            name = _clip(metadata.get("name") or skill_id, 100)
            description = _clip(metadata.get("description"), 500)
            scope = str(metadata.get("scope") or "all").strip().lower()
            if scope not in {"all", "ops", "code", "research", "analysis", "memory", "chat"}:
                continue
            triggers = _clip(metadata.get("triggers"), 800)
            conn.execute(
                """
                INSERT INTO skill_registry(
                    id,name,description,scope,triggers,source_path,enabled,priority,
                    use_count,last_used_at,validation_error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,1,5,0,'','',?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,description=excluded.description,scope=excluded.scope,
                    triggers=excluded.triggers,source_path=excluded.source_path,
                    validation_error='',updated_at=excluded.updated_at
                """,
                (skill_id, name, description, scope, triggers, str(resolved), now, now),
            )
            discovered.append({"id": skill_id, "name": name, "source_path": str(resolved)})
        except (OSError, ValueError, sqlite3.Error):
            continue
    return discovered


def list_skills(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM skill_registry ORDER BY enabled DESC, priority DESC, name",
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        content, error = _read_skill(item.get("source_path"))
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) == 3:
                content = parts[2].strip()
        item["instructions"] = content[:12000]
        item["healthy"] = not error
        item["validation_error"] = error or item.get("validation_error") or ""
        result.append(item)
    return result


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    latin = set(re.findall(r"[a-z0-9_+-]{2,}", lowered))
    chinese = set(re.findall(r"[\u4e00-\u9fff]{2,6}", lowered))
    return latin | chinese


def build_skill_context(
    conn: sqlite3.Connection,
    *,
    message: str,
    intent: str,
    limit: int = 3,
    max_chars: int = 4200,
) -> tuple[str, list[dict]]:
    rows = conn.execute(
        """
        SELECT * FROM skill_registry
        WHERE enabled = 1 AND (scope = ? OR scope = 'all')
        ORDER BY priority DESC, use_count ASC, updated_at DESC
        """,
        (str(intent or "analysis").strip(),),
    ).fetchall()
    query = _tokens(f"{message} {intent}")
    scored: list[tuple[int, dict]] = []
    for row in rows:
        item = dict(row)
        haystack = _tokens(f"{item.get('name')} {item.get('description')} {item.get('triggers')} {item.get('scope')}")
        overlap = len(query & haystack)
        score = overlap * 100 + int(item.get("priority") or 0)
        if overlap or item.get("scope") in {intent, "all"}:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], -int(pair[1].get("use_count") or 0)), reverse=True)
    selected: list[dict] = []
    blocks: list[str] = []
    size = 0
    for _, item in scored[: max(1, min(int(limit or 3), 5))]:
        content, error = _read_skill(item.get("source_path"))
        if error:
            conn.execute(
                "UPDATE skill_registry SET validation_error = ?, updated_at = ? WHERE id = ?",
                (_clip(error, 500), utc_now(), item["id"]),
            )
            continue
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) == 3:
                content = parts[2].strip()
        item["source_digest"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        block = f"### Skill: {item['name']}\n{content.strip()}"
        if blocks and size + len(block) > max_chars:
            break
        blocks.append(block)
        size += len(block)
        selected.append(item)
    return "\n\n".join(blocks), selected


def discover_skill_plan(
    conn: sqlite3.Connection,
    *,
    message: str,
    intent: str,
    capability_ids: tuple[str, ...] | list[str] | set[str] | None = None,
    allowed_capability_ids: tuple[str, ...] | list[str] | set[str] | None = None,
) -> dict:
    """Return a deterministic, auditable Skill selection and capability gate."""

    discovered = discover_local_skills(conn)
    context, selected = build_skill_context(conn, message=message, intent=intent)
    required = sorted(
        {
            capability_id
            for item in selected
            for capability_id in SKILL_CAPABILITY_REQUIREMENTS.get(str(item.get("id") or ""), ())
        },
    )
    # ``None`` means the caller did not provide a capability catalog and only
    # wants discovery/selection.  An explicitly empty catalog means nothing is
    # admitted, so a selected Skill with requirements must fail closed.
    available = None if capability_ids is None else {str(item) for item in capability_ids}
    missing = [
        item for item in required
        if available is not None and item not in available
    ]
    allowed = None if allowed_capability_ids is None else {
        str(item).strip() for item in allowed_capability_ids if str(item).strip()
    }
    scope_missing = sorted(set(required) - (allowed or set())) if allowed is not None else []
    scope_unexpected = sorted((allowed or set()) - set(required)) if allowed is not None else []
    status = (
        "skill_contract_mismatch" if scope_missing
        else "missing_capability" if missing
        else "ready" if selected
        else "no_match"
    )
    return {
        "status": status,
        "intent": str(intent or "analysis"),
        "message_hash": hashlib.sha256(str(message or "").encode("utf-8")).hexdigest()[:16],
        "discovered_skill_count": len(discovered),
        "selected_skills": [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "source_digest": str(item.get("source_digest") or ""),
            }
            for item in selected
        ],
        "required_capabilities": required,
        "missing_capabilities": sorted(set(missing) | set(scope_missing)),
        "unexpected_capabilities": scope_unexpected,
        "allowed_capabilities": sorted(allowed) if allowed is not None else None,
        "context": context,
    }


def validate_skill_contract(contract: Mapping[str, object], skill_plan: Mapping[str, object]) -> dict:
    """Ensure selected Skill requirements stay inside the server Action contract."""

    raw_allowed = contract.get("allowed_capability_ids")
    if isinstance(raw_allowed, (list, tuple, set)):
        allowed = {str(item).strip() for item in raw_allowed if str(item).strip()}
    else:
        capability_id = str(contract.get("capability_id") or "").strip()
        allowed = {capability_id} if capability_id else set()
    raw_required = skill_plan.get("required_capabilities")
    required = {
        str(item).strip()
        for item in (raw_required if isinstance(raw_required, (list, tuple, set)) else [])
        if str(item).strip()
    }
    missing = sorted(required - allowed)
    unexpected = sorted(allowed - required)
    return {"ok": not missing, "missing": missing, "unexpected": unexpected}


def record_skill_outcomes(
    conn: sqlite3.Connection,
    skill_ids: list[str] | tuple[str, ...] | set[str],
    *,
    succeeded: bool,
    occurred_at: str = "",
) -> int:
    """Count admitted Skill use only after the associated work has an outcome."""

    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(skill_registry)")
    }
    required = {"success_count", "failure_count", "last_outcome_at"}
    if not required.issubset(columns):
        return 0
    ids = sorted({str(item or "").strip() for item in skill_ids if str(item or "").strip()})
    if not ids:
        return 0
    now = occurred_at or utc_now()
    field = "success_count" if succeeded else "failure_count"
    updated = 0
    for skill_id in ids:
        cursor = conn.execute(
            f"""
            UPDATE skill_registry
            SET use_count=use_count+1,{field}={field}+1,
                last_used_at=?,last_outcome_at=?,validation_error=''
            WHERE id=?
            """,
            (now, now, skill_id),
        )
        updated += max(0, int(cursor.rowcount or 0))
    return updated


def _metadata(path: Path) -> tuple[dict, str]:
    try:
        if yaml is not None:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            if isinstance(parsed, dict):
                return parsed, ""
        result: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if ":" not in line or line.startswith((" ", "\t", "#")):
                continue
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip('"\'')
        return result, ""
    except Exception as exc:
        return {}, str(exc)


def _inactive_plugin_paths() -> set[str]:
    if not ASTRBOT_DB_PATH.is_file():
        return set()
    try:
        with sqlite3.connect(str(ASTRBOT_DB_PATH), timeout=5) as conn:
            row = conn.execute(
                """
                SELECT value FROM preferences
                WHERE scope = 'global' AND scope_id = 'global' AND key = 'inactivated_plugins'
                LIMIT 1
                """,
            ).fetchone()
        if not row:
            return set()
        payload = json.loads(str(row[0] or "{}"))
        values = payload.get("val", []) if isinstance(payload, dict) else []
        return {str(value) for value in values if str(value).strip()}
    except Exception:
        return set()


def _recent_astrbot_logs(lines: int = 600) -> str:
    if os.name == "posix" and os.environ.get("OPS_BROKER_EXECUTOR") != "1":
        try:
            from bridge_ops_broker_client import OpsBrokerClient

            response = OpsBrokerClient().request({
                "action": "container_logs",
                "target": "astrbot",
                "args": {"lines": max(50, min(lines, 500)), "timeout_seconds": 12},
            })
            return str((response.get("data") or {}).get("output") or "")[-200000:]
        except Exception:
            return ""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(max(50, min(lines, 2000))), ASTRBOT_CONTAINER],
            text=True,
            capture_output=True,
            timeout=12,
        )
        return ((result.stdout or "") + (result.stderr or ""))[-200000:]
    except Exception:
        return ""


def list_plugins() -> list[dict]:
    inactive = _inactive_plugin_paths()
    logs = _recent_astrbot_logs()
    result = []
    if not ASTRBOT_PLUGIN_ROOT.is_dir():
        return result
    for plugin_dir in sorted(path for path in ASTRBOT_PLUGIN_ROOT.iterdir() if path.is_dir()):
        metadata_path = plugin_dir / "metadata.yaml"
        data, error = _metadata(metadata_path) if metadata_path.is_file() else ({}, "metadata_missing")
        plugin_id = str(data.get("name") or plugin_dir.name).strip()
        module_path = f"data.plugins.{plugin_dir.name}.main"
        enabled = module_path not in inactive
        loaded_marker = f"Plugin {plugin_id} "
        failed_markers = (f"Loading plugin {plugin_dir.name}", f"插件 {plugin_id} 加载失败")
        loaded = enabled and (loaded_marker in logs or f"Loading plugin {plugin_dir.name}" in logs)
        failed = any(marker in logs and "failed" in logs[logs.rfind(marker) : logs.rfind(marker) + 500].lower() for marker in failed_markers)
        result.append(
            {
                "id": plugin_id,
                "dir_name": plugin_dir.name,
                "module_path": module_path,
                "display_name": str(data.get("display_name") or plugin_id),
                "description": str(data.get("desc") or data.get("description") or ""),
                "version": str(data.get("version") or ""),
                "author": str(data.get("author") or ""),
                "repo": str(data.get("repo") or ""),
                "enabled": enabled,
                "loaded": loaded,
                "healthy": enabled and loaded and not failed and not error,
                "protected": plugin_id in PROTECTED_PLUGINS,
                "validation_error": error,
            },
        )
    return result


def set_plugin_enabled(plugin_id: str, enabled: bool, *, idempotency_key: str = "") -> dict:
    if os.name == "posix" and os.environ.get("OPS_BROKER_EXECUTOR") != "1":
        from bridge_ops_actions import broker_write

        return broker_write(
            "astrbot_plugin_set_enabled",
            "astrbot",
            {"plugin_id": str(plugin_id or "").strip(), "enabled": bool(enabled)},
            idempotency_key=idempotency_key,
        )
    plugin = next((item for item in list_plugins() if item["id"] == str(plugin_id or "").strip()), None)
    if not plugin:
        raise ValueError("plugin_not_found")
    if plugin.get("protected") and not enabled:
        raise ValueError("protected_plugin_cannot_be_disabled")
    if not ASTRBOT_DB_PATH.is_file():
        raise ValueError("astrbot_database_missing")
    module_path = str(plugin["module_path"])
    now = utc_now()
    with sqlite3.connect(str(ASTRBOT_DB_PATH), timeout=10) as conn:
        row = conn.execute(
            """
            SELECT value FROM preferences
            WHERE scope = 'global' AND scope_id = 'global' AND key = 'inactivated_plugins'
            LIMIT 1
            """,
        ).fetchone()
        try:
            payload = json.loads(str(row[0] or "{}")) if row else {}
        except json.JSONDecodeError:
            payload = {}
        inactive = [str(value) for value in payload.get("val", [])] if isinstance(payload, dict) else []
        inactive = [value for value in inactive if value != module_path]
        if not enabled:
            inactive.append(module_path)
        value = json.dumps({"val": sorted(set(inactive))}, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO preferences(created_at, updated_at, scope, scope_id, key, value)
            VALUES (?, ?, 'global', 'global', 'inactivated_plugins', ?)
            ON CONFLICT(scope, scope_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (now, now, value),
        )
    restart = subprocess.run(
        ["docker", "restart", "--time", "20", ASTRBOT_CONTAINER],
        text=True,
        capture_output=True,
        timeout=50,
    )
    if restart.returncode != 0:
        raise RuntimeError((restart.stderr or restart.stdout or "astrbot_restart_failed")[-1000:])
    return {"ok": True, "plugin_id": plugin["id"], "enabled": bool(enabled), "astrbot_restarted": True}


def reload_plugins(*, idempotency_key: str = "") -> dict:
    if os.name == "posix" and os.environ.get("OPS_BROKER_EXECUTOR") != "1":
        try:
            from bridge_ops_actions import broker_write

            result = broker_write("container_restart", "astrbot", idempotency_key=idempotency_key)
            return {"ok": True, "astrbot_restarted": bool(result.get("restarted")), "error": ""}
        except Exception as exc:
            return {"ok": False, "astrbot_restarted": False, "error": str(exc)[:500]}
    result = subprocess.run(
        ["docker", "restart", "--time", "20", ASTRBOT_CONTAINER],
        text=True,
        capture_output=True,
        timeout=50,
    )
    return {
        "ok": result.returncode == 0,
        "astrbot_restarted": result.returncode == 0,
        "error": "" if result.returncode == 0 else (result.stderr or result.stdout or "restart_failed")[-1000:],
    }
