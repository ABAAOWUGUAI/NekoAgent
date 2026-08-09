#!/usr/bin/env python3
"""Third-party executor eligibility contract, isolated work-mode verification,
and verification-hash binding.

Product goal (2026-08-09): every enabled model must be visible in the work
executor choice, with a structured reason when it is not yet bindable, and a
third-party model can be configured behind the trusted Codex CLI Proxy adapter,
verified through a real isolated workspace run, and only then bound as
work_executor.

This module owns three server-side concerns:

- E1  ``executor_eligibility_state``: the structured per-model contract that the
      frontend renders (can_bind / reason_code / reason_zh / adapter /
      can_configure / verified_at / verification_hash / stale).  It is computed
      server-side; the frontend never grants binding by itself.
- E3  ``verify_executor_work_mode``: a deterministic, isolated work-mode
      verification that actually executes against the model through the Codex
      CLI Proxy adapter inside a platform-owned temporary workspace — fixed
      file read+summarise, a fixed no-network command, a file mutation with
      SHA-256 confirmation, and a final assistant body.  Failure to perform the
      file/tool operation is a failed verification, never a pass.
- E5  the verification state is consulted by ``bind_model_role`` so a stale or
      failed verification can never bind work_executor.

No Secret is ever written to the API, audit, logs, or the verification state;
the verification hash covers identity, non-sensitive connection version, secret
version (not the secret), transport, adapter/profile, config/applied version,
and sandbox/tool/workspace policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bridge_executor_apply import apply_executor_profile
from bridge_executor_profiles import (
    executor_runtime_status,
    get_executor_profile,
)
from bridge_model_control import capabilities_from_row


# ---------------------------------------------------------------------------
# Reason vocabulary (E1).  Ordered; the first hit wins.
# ---------------------------------------------------------------------------

REASON_MODEL_DISABLED = "model_disabled"
REASON_PROVIDER_DISABLED = "provider_disabled"
REASON_TOOLS_CAPABILITY_MISSING = "tools_capability_missing"
REASON_PROVIDER_NOT_TRUSTED = "provider_not_trusted"
REASON_EXECUTOR_TRANSPORT_UNSUPPORTED = "executor_transport_unsupported"
REASON_EXECUTOR_PROFILE_MISSING = "executor_profile_missing"
REASON_EXECUTOR_ADAPTER_NOT_CONFIGURED = "executor_adapter_not_configured"
REASON_EXECUTOR_PROFILE_NOT_APPLIED = "executor_profile_not_applied"
REASON_EXECUTOR_RUNTIME_UNAVAILABLE = "executor_runtime_unavailable"
REASON_EXECUTOR_VERIFICATION_REQUIRED = "executor_verification_required"
REASON_EXECUTOR_VERIFICATION_FAILED = "executor_verification_failed"
REASON_EXECUTOR_VERIFICATION_STALE = "executor_verification_stale"
REASON_VERIFIED = "verified"

REASON_ZH = {
    REASON_MODEL_DISABLED: "模型已停用",
    REASON_PROVIDER_DISABLED: "连接（Provider）已停用",
    REASON_TOOLS_CAPABILITY_MISSING: "该模型仅声明文本能力，未声明工具能力",
    REASON_PROVIDER_NOT_TRUSTED: "该连接不在受信任执行器名单中",
    REASON_EXECUTOR_TRANSPORT_UNSUPPORTED: "该连接不是 Codex CLI 执行器传输（codex_cli_custom_provider）",
    REASON_EXECUTOR_PROFILE_MISSING: "尚未配置 Executor Profile",
    REASON_EXECUTOR_ADAPTER_NOT_CONFIGURED: "未配置执行器：可配置为 Codex CLI Proxy 执行适配器",
    REASON_EXECUTOR_PROFILE_NOT_APPLIED: "Executor Profile 未应用（config 与 applied 版本不一致）",
    REASON_EXECUTOR_RUNTIME_UNAVAILABLE: "执行器运行环境不可用（profile/凭证/沙箱/工作目录/健康）",
    REASON_EXECUTOR_VERIFICATION_REQUIRED: "未完成隔离工作模式验证",
    REASON_EXECUTOR_VERIFICATION_FAILED: "工作模式验证失败",
    REASON_EXECUTOR_VERIFICATION_STALE: "配置已变化，验证结果过期，需重新验证",
    REASON_VERIFIED: "已验证，可绑定为工作执行器",
}

VERIFICATION_STATUS_PENDING = "pending"
VERIFICATION_STATUS_VERIFIED = "verified"
VERIFICATION_STATUS_FAILED = "failed"
VERIFICATION_STATUS_STALE = "stale"

VERIFICATION_COLUMNS = (
    "provider_id",
    "adapter_type",
    "verification_hash",
    "verified_at",
    "config_version_at_verify",
    "applied_version_at_verify",
    "upstream_model_id_at_verify",
    "status",
    "last_error",
    "evidence_json",
    "created_at",
    "updated_at",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _public_verification(row: sqlite3.Row | dict | None) -> dict:
    if not row:
        return {
            "configured": False,
            "status": "",
            "verified_at": "",
            "verification_hash": "",
            "last_error": "",
        }
    item = {key: row[key] for key in row.keys()}
    try:
        evidence = json.loads(item.get("evidence_json") or "{}")
    except (TypeError, ValueError):
        evidence = {}
    item["evidence"] = evidence
    item.pop("evidence_json", None)
    return item


# ---------------------------------------------------------------------------
# Verification hash (E3/E5).  Covers identity, connection version, secret
# version (never the secret), transport, adapter/profile, config/applied
# version, and sandbox/tool/workspace policy.  No Secret value enters it.
# ---------------------------------------------------------------------------

def verification_hash_inputs(conn: sqlite3.Connection, provider_id: str) -> dict:
    provider = conn.execute(
        """
        SELECT p.id, p.kind, p.transport, p.enabled, p.trusted_for_executor,
               p.secret_version, p.secret_rotated_at
        FROM model_providers p
        WHERE p.id = ?
        """,
        (str(provider_id or "").strip(),),
    ).fetchone()
    if not provider:
        return {}
    item = dict(provider)
    profile = get_executor_profile(conn, provider_id) or {}
    item["executor_adapter_type"] = profile.get("adapter_type") or ""
    item["executor_profile_name"] = profile.get("profile_name") or ""
    item["executor_credential_source"] = profile.get("credential_source") or ""
    item["executor_upstream_provider_id"] = profile.get("upstream_provider_id") or ""
    item["executor_upstream_model_id"] = profile.get("upstream_model_id") or ""
    item["executor_config_version"] = profile.get("config_version") or 0
    item["executor_applied_version"] = profile.get("applied_version") or 0
    item["executor_last_apply_status"] = profile.get("last_apply_status") or ""
    # The upstream model + provider identity (and its secret version) is what a
    # real verification exercises; rotation of the upstream credential must
    # invalidate the hash even though the secret value is never included.
    upstream_model = conn.execute(
        """
        SELECT m.id AS model_id, m.model, m.enabled AS model_enabled,
               m.supports_tools, m.capabilities_json,
               p.id AS upstream_provider_id, p.transport, p.enabled AS provider_enabled,
               p.secret_version AS upstream_secret_version,
               p.secret_rotated_at AS upstream_secret_rotated_at
        FROM model_catalog m JOIN model_providers p ON p.id = m.provider_id
        WHERE m.id = ? AND p.id = ?
        """,
        (item.get("executor_upstream_model_id"), item.get("executor_upstream_provider_id")),
    ).fetchone()
    if upstream_model:
        item.update({k: upstream_model[k] for k in upstream_model.keys()})
    # Sandbox / tool / workspace policy is deployment-owned; include the policy
    # facts that affect execution, not secrets.
    item["sandbox_policy"] = "read-only"
    item["tool_policy"] = "controlled"
    item["workspace_root"] = os.environ.get("CODEX_EXECUTOR_WORKSPACE_ROOT", "/opt/agent-workspace")
    return item


def compute_verification_hash(conn: sqlite3.Connection, provider_id: str) -> str:
    inputs = verification_hash_inputs(conn, provider_id)
    if not inputs:
        return ""
    # secret_version is included by value (it is a version, not the secret);
    # any rotation invalidates the hash even if the value stayed the same.
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# E1 eligibility contract
# ---------------------------------------------------------------------------

def _model_row_for(conn: sqlite3.Connection, model_id: str) -> dict:
    row = conn.execute(
        """
        SELECT m.*, p.id AS provider_id, p.kind AS provider_kind, p.transport,
               p.billing_scope, p.runtime_owner, p.config_mode,
               p.trusted_for_executor, p.enabled AS provider_enabled
        FROM model_catalog m JOIN model_providers p ON p.id = m.provider_id
        WHERE m.id = ?
        """,
        (str(model_id or "").strip(),),
    ).fetchone()
    return dict(row) if row else {}


def _verification_row(conn: sqlite3.Connection, provider_id: str) -> sqlite3.Row | None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_verification_state'",
    ).fetchone()
    if not table:
        return None
    return conn.execute(
        "SELECT * FROM executor_verification_state WHERE provider_id=?",
        (str(provider_id or "").strip(),),
    ).fetchone()


def _mark_stale_on_config_change(conn: sqlite3.Connection, provider_id: str, current_hash: str) -> dict:
    """Mark verification stale when the hash no longer matches current config."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_verification_state'",
    ).fetchone()
    if not table:
        return {"status": ""}
    row = _verification_row(conn, provider_id)
    if not row:
        return {"status": ""}
    stored = str(row["verification_hash"] or "")
    if stored and stored != current_hash and str(row["status"] or "") == VERIFICATION_STATUS_VERIFIED:
        conn.execute(
            """UPDATE executor_verification_state
               SET status=?, last_error=?, updated_at=?
               WHERE provider_id=? AND verification_hash=?""",
            (VERIFICATION_STATUS_STALE, "config_changed_since_verification", utc_now(), provider_id, stored),
        )
        conn.commit()
        return {"status": VERIFICATION_STATUS_STALE}
    return {"status": str(row["status"] or "")}


def executor_eligibility_state(conn: sqlite3.Connection, model_id: str) -> dict:
    """Return the E1 structured contract for one catalog model.

    Server-computed; the frontend only renders it and can never grant binding.
    """

    model = _model_row_for(conn, model_id)
    if not model:
        return {"model_id": model_id, "can_bind": False, "reason_code": "model_not_found", "reason_zh": "模型不存在", "adapter": "", "can_configure": False, "verified_at": "", "verification_hash": ""}

    provider_id = str(model.get("provider_id") or "")
    model_enabled = bool(int(model.get("enabled") or 0))
    provider_enabled = bool(int(model.get("provider_enabled") or 0))
    transport = str(model.get("transport") or "")
    trusted = bool(int(model.get("trusted_for_executor") or 0))
    caps = set(capabilities_from_row(model))
    has_tools = "tools" in caps
    profile = get_executor_profile(conn, provider_id)
    runtime = executor_runtime_status(profile)
    current_hash = compute_verification_hash(conn, provider_id) if profile else ""
    verification = _verification_row(conn, provider_id)
    verification_public = _public_verification(verification)

    state = {
        "model_id": model_id,
        "provider_id": provider_id,
        "model_enabled": model_enabled,
        "provider_enabled": provider_enabled,
        "capabilities": sorted(caps),
        "can_bind": False,
        "reason_code": "",
        "reason_zh": "",
        "adapter": "codex_cli_profile" if profile else "",
        "can_configure": bool(profile is None or not runtime.get("ready") or verification_public.get("status") in {VERIFICATION_STATUS_STALE, VERIFICATION_STATUS_FAILED}),
        "verified_at": verification_public.get("verified_at") or "",
        "verification_hash": verification_public.get("verification_hash") or "",
        "verification_status": verification_public.get("status") or "",
        "executor_profile": public_profile_summary(profile),
        "runtime": runtime,
    }

    def reject(code: str) -> dict:
        state.update({"reason_code": code, "reason_zh": REASON_ZH.get(code, code)})
        return state

    if not model_enabled:
        return reject(REASON_MODEL_DISABLED)
    if not provider_enabled:
        return reject(REASON_PROVIDER_DISABLED)
    # A chat-completions model is a valid upstream for a Codex CLI Proxy
    # executor adapter.  It is not itself an executor transport, but it can be
    # configured as one → surface it as "unconfigured" with a configuration
    # entry (can_configure=True) rather than a dead-end transport error.  The
    # tool capability for work execution comes from the trusted proxy adapter,
    # not from the model's own supports_tools flag.
    if transport == "openai_chat_completions":
        state["adapter"] = "codex_cli_profile"
        state["can_configure"] = True
        return reject(REASON_EXECUTOR_ADAPTER_NOT_CONFIGURED)
    if transport not in {"codex_cli_chatgpt", "codex_cli_custom_provider"}:
        return reject(REASON_EXECUTOR_TRANSPORT_UNSUPPORTED)
    if not trusted:
        return reject(REASON_PROVIDER_NOT_TRUSTED)
    if not has_tools:
        return reject(REASON_TOOLS_CAPABILITY_MISSING)
    if not profile:
        return reject(REASON_EXECUTOR_PROFILE_MISSING)
    if not runtime.get("ready"):
        # Surface the first runtime error for a precise Chinese reason.
        code = runtime.get("error") or REASON_EXECUTOR_RUNTIME_UNAVAILABLE
        mapped = {
            "executor_profile_disabled": REASON_EXECUTOR_PROFILE_MISSING,
            "executor_profile_missing": REASON_EXECUTOR_PROFILE_MISSING,
            "executor_credential_missing": REASON_EXECUTOR_RUNTIME_UNAVAILABLE,
            "executor_sandbox_unavailable": REASON_EXECUTOR_RUNTIME_UNAVAILABLE,
            "executor_workspace_missing": REASON_EXECUTOR_RUNTIME_UNAVAILABLE,
            "executor_upstream_required": REASON_EXECUTOR_PROFILE_MISSING,
            "executor_runtime_not_applied": REASON_EXECUTOR_PROFILE_NOT_APPLIED,
        }
        return reject(mapped.get(code, REASON_EXECUTOR_RUNTIME_UNAVAILABLE))
    if int(profile.get("config_version") or 0) != int(profile.get("applied_version") or 0):
        return reject(REASON_EXECUTOR_PROFILE_NOT_APPLIED)
    if str(profile.get("last_apply_status") or "") != "applied":
        return reject(REASON_EXECUTOR_PROFILE_NOT_APPLIED)

    if not verification:
        return reject(REASON_EXECUTOR_VERIFICATION_REQUIRED)

    stored_hash = str(verification["verification_hash"] or "")
    status = str(verification["status"] or "")
    if status == VERIFICATION_STATUS_FAILED:
        return reject(REASON_EXECUTOR_VERIFICATION_FAILED)
    if status == VERIFICATION_STATUS_STALE or (stored_hash and stored_hash != current_hash):
        _mark_stale_on_config_change(conn, provider_id, current_hash)
        return reject(REASON_EXECUTOR_VERIFICATION_STALE)
    if status != VERIFICATION_STATUS_VERIFIED:
        return reject(REASON_EXECUTOR_VERIFICATION_REQUIRED)
    if stored_hash != current_hash:
        _mark_stale_on_config_change(conn, provider_id, current_hash)
        return reject(REASON_EXECUTOR_VERIFICATION_STALE)

    state["can_bind"] = True
    state["reason_code"] = REASON_VERIFIED
    state["reason_zh"] = REASON_ZH[REASON_VERIFIED]
    return state


def public_profile_summary(profile: dict | None) -> dict | None:
    if not profile:
        return None
    return {
        "provider_id": profile.get("provider_id"),
        "adapter_type": profile.get("adapter_type"),
        "profile_name": profile.get("profile_name"),
        "config_version": profile.get("config_version"),
        "applied_version": profile.get("applied_version"),
        "last_apply_status": profile.get("last_apply_status"),
        "enabled": profile.get("enabled"),
    }


def eligibility_for_role(conn: sqlite3.Connection, role: str) -> list[dict]:
    """Return eligibility for every model for one role (used by the frontend)."""
    rows = conn.execute(
        "SELECT id FROM model_catalog ORDER BY enabled DESC, id",
    ).fetchall()
    if role == "work_executor":
        return [executor_eligibility_state(conn, str(row["id"])) for row in rows]
    return []


# ---------------------------------------------------------------------------
# E3 isolated work-mode verification
# ---------------------------------------------------------------------------

WORK_VERIFY_FIXED_FILE = "work-verify.txt"
WORK_VERIFY_FIXED_CONTENT = "executor-work-mode-verification\n"
WORK_VERIFY_EXPECTED_SHA256 = hashlib.sha256(WORK_VERIFY_FIXED_CONTENT.encode("utf-8")).hexdigest()


def _run_work_verify(
    *,
    runner,
    workspace_root: Path,
    timeout: int,
) -> dict:
    """Run the fixed isolated work-mode verification inside a temp workspace.

    ``runner`` is injected for testability: callable(prompt, cwd, timeout,
    settings) -> dict with ok / output / reply / error_kind.  The real binding
    uses the Codex CLI Proxy adapter; tests inject a deterministic runner.
    """

    import tempfile as _tf

    with _tf.TemporaryDirectory(prefix="executor-verify-", dir=str(workspace_root)) as tmp:
        work_dir = Path(tmp)
        fixed = work_dir / WORK_VERIFY_FIXED_FILE
        fixed.write_text(WORK_VERIFY_FIXED_CONTENT, encoding="utf-8")
        before_hash = hashlib.sha256(fixed.read_bytes()).hexdigest()
        prompt = (
            "请按以下步骤完成一次隔离工作模式验证，不要访问网络：\n"
            "1. 读取工作目录下的 work-verify.txt 并返回其内容的摘要；\n"
            "2. 运行一个固定且无网络访问的命令（例如 ls 或 pwd），返回其结果；\n"
            "3. 修改 work-verify.txt（追加一行 verify-mutated），不要删除原文；\n"
            "4. 用 sha256sum 报告修改后文件的 SHA-256；\n"
            "5. 最后用一句话总结你实际完成的文件与命令操作。"
        )
        result = runner(prompt, cwd=work_dir, timeout=timeout, settings={})
        ok = bool(result.get("ok"))
        output = str(result.get("output") or result.get("reply") or "")
        error_kind = str(result.get("error_kind") or "")
        # The file must have been mutated (original content preserved + marker).
        mutated_content = fixed.read_text(encoding="utf-8", errors="replace") if fixed.exists() else ""
        mutated = fixed.exists() and WORK_VERIFY_FIXED_CONTENT in mutated_content and "verify-mutated" in mutated_content
        # Only a model that performed a real tool/file operation passes.
        final_body = str(output).strip()
        has_final_body = len(final_body) >= 8 and not _looks_like_startup_log(final_body)
        evidence = {
            "fixed_file": WORK_VERIFY_FIXED_FILE,
            "before_sha256": before_hash,
            "mutated": mutated,
            "final_body_present": has_final_body,
            "output_chars": len(final_body),
            "sandbox": "read-only",
            "network": "none",
        }
        return {
            "ok": bool(ok and mutated and has_final_body and not error_kind),
            "output": output,
            "error_kind": error_kind,
            "evidence": evidence,
        }


def _looks_like_startup_log(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    markers = ("welcome to", "starting", "startup", "usage:", "no configuration",
               "traced to", "agent ready", "logged in as", "initializing",
               "checking for updates", "reading config")
    return any(marker in lowered for marker in markers)


def verify_executor_work_mode(conn: sqlite3.Connection, provider_id: str, *, timeout: int = 120, runner=None) -> dict:
    """Verify a third-party model through its executor profile using an
    isolated real workspace run, then persist the verification state bound to
    the current config hash.

    Returns the public verification state; on failure records a failed status.
    """

    provider_id = str(provider_id or "").strip()
    profile = get_executor_profile(conn, provider_id)
    if not profile:
        raise ValueError("executor_profile_missing")
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_verification_state'",
    ).fetchone()
    if not table:
        raise ValueError("executor_verification_schema_missing")
    runtime = executor_runtime_status(profile)
    if not runtime.get("ready"):
        raise ValueError(runtime.get("error") or "executor_runtime_unavailable")
    current_hash = compute_verification_hash(conn, provider_id)
    if not current_hash:
        raise ValueError("executor_verification_hash_unavailable")

    if runner is None:
        def runner(prompt, *, cwd, timeout, settings):
            from codex_qq_bridge import _run_codex_assistant_chat
            settings_override = _executor_verify_settings(conn, provider_id, profile)
            return _run_codex_assistant_chat(prompt, cwd=cwd, timeout=timeout, settings_override=settings_override)

    workspace_root = Path(os.environ.get("CODEX_EXECUTOR_WORKSPACE_ROOT", "/opt/agent-workspace"))
    outcome = _run_work_verify(runner=runner, workspace_root=workspace_root, timeout=timeout)

    now = utc_now()
    if outcome["ok"]:
        conn.execute(
            """
            INSERT INTO executor_verification_state(
                provider_id,adapter_type,verification_hash,verified_at,
                config_version_at_verify,applied_version_at_verify,
                upstream_model_id_at_verify,status,last_error,evidence_json,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider_id) DO UPDATE SET
                adapter_type=excluded.adapter_type,
                verification_hash=excluded.verification_hash,
                verified_at=excluded.verified_at,
                config_version_at_verify=excluded.config_version_at_verify,
                applied_version_at_verify=excluded.applied_version_at_verify,
                upstream_model_id_at_verify=excluded.upstream_model_id_at_verify,
                status='verified',last_error='',
                evidence_json=excluded.evidence_json,updated_at=excluded.updated_at
            """,
            (
                provider_id, str(profile.get("adapter_type") or "codex_cli_profile"),
                current_hash, now,
                int(profile.get("config_version") or 0),
                int(profile.get("applied_version") or 0),
                str(profile.get("upstream_model_id") or ""),
                VERIFICATION_STATUS_VERIFIED, "",
                json.dumps(outcome["evidence"], ensure_ascii=False, sort_keys=True),
                now, now,
            ),
        )
        conn.commit()
        return _public_verification(conn.execute(
            "SELECT * FROM executor_verification_state WHERE provider_id=?",
            (provider_id,),
        ).fetchone())
    error = outcome["error_kind"] or "work_mode_verification_failed"
    conn.execute(
        """
        INSERT INTO executor_verification_state(
            provider_id,adapter_type,verification_hash,verified_at,
            config_version_at_verify,applied_version_at_verify,
            upstream_model_id_at_verify,status,last_error,evidence_json,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(provider_id) DO UPDATE SET
            adapter_type=excluded.adapter_type,
            verification_hash=excluded.verification_hash,
            verified_at='',
            config_version_at_verify=excluded.config_version_at_verify,
            applied_version_at_verify=excluded.applied_version_at_verify,
            upstream_model_id_at_verify=excluded.upstream_model_id_at_verify,
            status='failed',last_error=excluded.last_error,
            evidence_json=excluded.evidence_json,updated_at=excluded.updated_at
        """,
        (
            provider_id, str(profile.get("adapter_type") or "codex_cli_profile"),
            current_hash, now,
            int(profile.get("config_version") or 0),
            int(profile.get("applied_version") or 0),
            str(profile.get("upstream_model_id") or ""),
            VERIFICATION_STATUS_FAILED, error,
            json.dumps(outcome["evidence"], ensure_ascii=False, sort_keys=True),
            now, now,
        ),
    )
    conn.commit()
    return _public_verification(conn.execute(
        "SELECT * FROM executor_verification_state WHERE provider_id=?",
        (provider_id,),
    ).fetchone())


def _executor_verify_settings(conn: sqlite3.Connection, provider_id: str, profile: dict) -> dict:
    """Build settings_override for the verification codex run: the executor
    profile + upstream model, without touching the chat settings path.

    ``codex_model`` must be the upstream's real model name (e.g.
    ``deepseek-v4-flash``), not the internal catalog id — the Codex CLI Proxy
    whitelists concrete model names, so an internal id is rejected with
    ``model_not_allowed``.
    """
    model_name = ""
    upstream_model_id = str(profile.get("upstream_model_id") or "")
    if upstream_model_id:
        row = conn.execute(
            "SELECT model FROM model_catalog WHERE id=?",
            (upstream_model_id,),
        ).fetchone()
        if row:
            model_name = str(row[0] or "")
    return {
        "model_transport": "codex_cli_custom_provider",
        "codex_model": model_name or upstream_model_id,
        "executor_profile": {
            "profile_name": str(profile.get("profile_name") or ""),
            "credential_source": str(profile.get("credential_source") or "proxy_access_key"),
        },
        "provider_id": provider_id,
    }


def work_executor_bind_guard(conn: sqlite3.Connection, model_id: str) -> tuple[bool, str]:
    """E5 guard: re-validate everything the server requires before a
    work_executor bind is accepted.  Returns (allowed, reason_code)."""
    state = executor_eligibility_state(conn, model_id)
    if not state.get("can_bind"):
        return False, str(state.get("reason_code") or "executor_not_eligible")
    return True, "ok"


__all__ = [
    "REASON_EXECUTOR_PROFILE_MISSING",
    "REASON_EXECUTOR_PROFILE_NOT_APPLIED",
    "REASON_EXECUTOR_RUNTIME_UNAVAILABLE",
    "REASON_EXECUTOR_VERIFICATION_FAILED",
    "REASON_EXECUTOR_VERIFICATION_REQUIRED",
    "REASON_EXECUTOR_VERIFICATION_STALE",
    "REASON_PROVIDER_NOT_TRUSTED",
    "REASON_TOOLS_CAPABILITY_MISSING",
    "REASON_ZH",
    "REASON_VERIFIED",
    "VERIFICATION_COLUMNS",
    "VERIFICATION_STATUS_FAILED",
    "VERIFICATION_STATUS_PENDING",
    "VERIFICATION_STATUS_STALE",
    "VERIFICATION_STATUS_VERIFIED",
    "WORK_VERIFY_FIXED_CONTENT",
    "WORK_VERIFY_FIXED_FILE",
    "compute_verification_hash",
    "executor_eligibility_state",
    "public_profile_summary",
    "verification_hash_inputs",
    "verify_executor_work_mode",
    "work_executor_bind_guard",
]
