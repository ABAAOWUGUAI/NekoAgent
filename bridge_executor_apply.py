"""Apply provider-owned executor upstream settings to the fixed local proxy."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from bridge_ops_actions import broker_write
from bridge_provider_secrets import resolve_provider_secret
from bridge_executor_profiles import executor_profile_path


# The custom Codex executor has exactly one local proxy process and one
# runtime env file.  Every writer therefore takes an exclusive lock; task
# execution will take a shared lock before using that proxy.  POSIX flock
# protects the production Linux host across bridge processes, while the
# process-local lock keeps Windows local tests deterministic.
_RUNTIME_GATE = threading.Condition(threading.Lock())
_RUNTIME_READERS = 0
_RUNTIME_WRITER_ACTIVE = False
_RUNTIME_WAITING_WRITERS = 0


def _runtime_env_path() -> Path:
    return Path(os.environ.get(
        "CODEX_EXECUTOR_UPSTREAM_ENV_FILE",
        "/var/lib/agent-bridge/executor/proxy.env",
    ))


def _runtime_service() -> str:
    return os.environ.get("CODEX_EXECUTOR_UPSTREAM_SERVICE", "codex-deepseek-proxy.service")


def _runtime_lock_path() -> Path:
    target = _runtime_env_path()
    return target.with_name(f".{target.name}.lock")


def _activation_journal_path() -> Path:
    target = _runtime_env_path()
    return target.with_name(f".{target.name}.activation.json")


_ACTIVATION_JOURNAL_FIELDS = frozenset({
    "version",
    "activation_id",
    "target_provider_id",
    "target_model_id",
    "previous_primary_model_id",
    "previous_fallback_model_id",
})
_ACTIVATION_JOURNAL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")


def _activation_journal_value(value: object, *, allow_empty: bool = True) -> str:
    result = str(value or "").strip()
    if not result and allow_empty:
        return ""
    if not _ACTIVATION_JOURNAL_ID.fullmatch(result):
        raise RuntimeError("executor_activation_journal_invalid")
    return result


def write_executor_activation_journal(
    *,
    activation_id: str,
    target_provider_id: str,
    target_model_id: str,
    previous_primary_model_id: str,
    previous_fallback_model_id: str,
) -> dict:
    """Durably record an in-progress switch without writing secret material.

    The proxy env necessarily contains provider credentials.  The journal does
    not copy it; it retains only exact registry identifiers needed to restore
    the prior binding after an interrupted process is restarted.
    """
    payload = {
        "version": 1,
        "activation_id": _activation_journal_value(activation_id, allow_empty=False),
        "target_provider_id": _activation_journal_value(target_provider_id, allow_empty=False),
        "target_model_id": _activation_journal_value(target_model_id, allow_empty=False),
        "previous_primary_model_id": _activation_journal_value(previous_primary_model_id),
        "previous_fallback_model_id": _activation_journal_value(previous_fallback_model_id),
    }
    path = _activation_journal_path()
    if path.is_symlink():
        raise RuntimeError("executor_activation_journal_symlink_refused")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _write_runtime_file(path, encoded)
    return payload


def read_executor_activation_journal() -> dict | None:
    path = _activation_journal_path()
    if path.is_symlink():
        raise RuntimeError("executor_activation_journal_symlink_refused")
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            raise ValueError("journal_too_large")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError("executor_activation_journal_invalid") from exc
    if not isinstance(value, dict) or set(value) != _ACTIVATION_JOURNAL_FIELDS or value.get("version") != 1:
        raise RuntimeError("executor_activation_journal_invalid")
    return {
        "version": 1,
        "activation_id": _activation_journal_value(value.get("activation_id"), allow_empty=False),
        "target_provider_id": _activation_journal_value(value.get("target_provider_id"), allow_empty=False),
        "target_model_id": _activation_journal_value(value.get("target_model_id"), allow_empty=False),
        "previous_primary_model_id": _activation_journal_value(value.get("previous_primary_model_id")),
        "previous_fallback_model_id": _activation_journal_value(value.get("previous_fallback_model_id")),
    }


def clear_executor_activation_journal() -> None:
    path = _activation_journal_path()
    if path.is_symlink():
        raise RuntimeError("executor_activation_journal_symlink_refused")
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError("executor_activation_journal_clear_failed") from exc


@contextmanager
def _executor_runtime_thread_gate(*, exclusive: bool):
    """In-process reader/writer gate for the singleton runtime.

    ``flock`` is needed for Linux cross-process coordination, but two file
    descriptors in one Python process are not a portable substitute for
    thread-level reader/writer ownership.  Waiting writers block newly
    arriving readers so a continuous task stream cannot starve an explicit
    Owner activation.
    """
    global _RUNTIME_READERS, _RUNTIME_WRITER_ACTIVE, _RUNTIME_WAITING_WRITERS
    with _RUNTIME_GATE:
        if exclusive:
            _RUNTIME_WAITING_WRITERS += 1
            try:
                while _RUNTIME_WRITER_ACTIVE or _RUNTIME_READERS:
                    _RUNTIME_GATE.wait()
            finally:
                _RUNTIME_WAITING_WRITERS -= 1
            _RUNTIME_WRITER_ACTIVE = True
        else:
            while _RUNTIME_WRITER_ACTIVE or _RUNTIME_WAITING_WRITERS:
                _RUNTIME_GATE.wait()
            _RUNTIME_READERS += 1
    try:
        yield
    finally:
        with _RUNTIME_GATE:
            if exclusive:
                _RUNTIME_WRITER_ACTIVE = False
            else:
                _RUNTIME_READERS -= 1
            _RUNTIME_GATE.notify_all()


@contextmanager
def _executor_runtime_lock(*, exclusive: bool):
    """Serialize use and mutation of the singleton custom-executor proxy.

    Production is Linux and uses advisory ``flock`` across processes.  Local
    Windows development has no ``fcntl`` module, so it receives the in-process
    guard needed by tests; it is never a production cross-process guarantee.
    """
    descriptor: int | None = None
    with _executor_runtime_thread_gate(exclusive=exclusive):
        try:
            if os.name == "posix":
                import fcntl

                path = _runtime_lock_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = None
            raise RuntimeError("executor_runtime_lock_unavailable") from exc
        try:
            yield
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@contextmanager
def executor_runtime_exclusive_lock():
    with _executor_runtime_lock(exclusive=True):
        yield


@contextmanager
def executor_runtime_shared_lock():
    with _executor_runtime_lock(exclusive=False):
        yield


def _chat_completions_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def _upstream(conn: sqlite3.Connection, profile: dict) -> dict:
    row = conn.execute(
        """SELECT m.id AS model_id, m.model, m.max_output_tokens,
                  m.enabled AS model_enabled,
                  p.id AS provider_id, p.base_url, p.api_key, p.secret_ref,
                  p.secret_version, p.secret_rotated_at, p.transport,
                  p.enabled AS provider_enabled
           FROM model_catalog m JOIN model_providers p ON p.id=m.provider_id
           WHERE m.id=? AND p.id=?""",
        (profile.get("upstream_model_id"), profile.get("upstream_provider_id")),
    ).fetchone()
    if not row or not int(row["model_enabled"] or 0) or not int(row["provider_enabled"] or 0):
        raise RuntimeError("executor_upstream_model_unavailable")
    item = dict(row)
    if item.get("transport") != "openai_chat_completions":
        raise RuntimeError("executor_upstream_transport_unsupported")
    item["api_key"] = resolve_provider_secret(conn, item)
    if not item.get("api_key"):
        raise RuntimeError("executor_upstream_credential_missing")
    if not item.get("base_url") or not item.get("model"):
        raise RuntimeError("executor_upstream_incomplete")
    return item


def provider_runtime_dependency_fingerprint(conn: sqlite3.Connection, provider_id: str) -> tuple | None:
    """Fields of an upstream Provider that can change proxy runtime behavior."""
    row = conn.execute(
        "SELECT base_url,enabled,transport,secret_ref,secret_version FROM model_providers WHERE id=?",
        (str(provider_id or "").strip(),),
    ).fetchone()
    if not row:
        return None
    return tuple(row[key] for key in ("base_url", "enabled", "transport", "secret_ref", "secret_version"))


def model_runtime_dependency_fingerprint(conn: sqlite3.Connection, model_id: str) -> tuple | None:
    """Fields of an upstream Model that can change proxy runtime behavior."""
    row = conn.execute(
        "SELECT provider_id,model,max_output_tokens,enabled FROM model_catalog WHERE id=?",
        (str(model_id or "").strip(),),
    ).fetchone()
    if not row:
        return None
    return tuple(row[key] for key in ("provider_id", "model", "max_output_tokens", "enabled"))


def _write_runtime_file(target: Path, content: bytes) -> None:
    if target.is_symlink():
        raise RuntimeError("executor_runtime_env_symlink_refused")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.apply-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _runtime_env_snapshot() -> tuple[Path, bytes | None]:
    target = _runtime_env_path()
    if target.is_symlink():
        raise RuntimeError("executor_runtime_env_symlink_refused")
    if not target.exists():
        return target, None
    try:
        return target, target.read_bytes()
    except OSError as exc:
        raise RuntimeError("executor_runtime_env_backup_failed") from exc


def _runtime_env_model(content: bytes | None) -> str:
    if not content:
        return ""
    for line in content.decode("utf-8", errors="replace").splitlines():
        if line.startswith("DEEPSEEK_MODEL="):
            return line.split("=", 1)[1].strip()[:200]
    return ""


def _runtime_env_identity(content: bytes | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not content:
        return values
    for line in content.decode("utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "DEEPSEEK_MODEL",
            "CODEX_EXECUTOR_PROVIDER_ID",
            "CODEX_EXECUTOR_CONFIG_VERSION",
        }:
            values[key] = value.strip()[:200]
    return values


def _proxy_health_matches(expected_model: str) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/healthz", timeout=2) as response:
            payload = json.load(response)
        return bool(response.status == 200 and payload.get("ok") and payload.get("model") == expected_model)
    except Exception:
        return False


def executor_runtime_identity_matches(
    provider_id: str,
    config_version: int | str,
    model_name: str,
) -> bool:
    """Check both persisted singleton identity and the live proxy health.

    The env file is the non-secret identity record written by activation;
    health confirms that the process has actually loaded that model rather than
    merely observing a file written for a future restart.
    """
    try:
        target, content = _runtime_env_snapshot()
    except RuntimeError:
        return False
    del target
    identity = _runtime_env_identity(content)
    expected_provider = str(provider_id or "").strip()
    expected_version = str(config_version if config_version is not None else "").strip()
    expected_model = str(model_name or "").strip()
    if not expected_provider or not expected_version or not expected_model:
        return False
    if identity.get("CODEX_EXECUTOR_PROVIDER_ID") != expected_provider:
        return False
    if identity.get("CODEX_EXECUTOR_CONFIG_VERSION") != expected_version:
        return False
    if identity.get("DEEPSEEK_MODEL") != expected_model:
        return False
    return _proxy_health_matches(expected_model)


def _restore_runtime_env(target: Path, previous: bytes | None) -> str:
    if target.is_symlink():
        raise RuntimeError("executor_runtime_env_symlink_refused")
    if previous is None:
        if target.exists():
            target.unlink()
        return ""
    _write_runtime_file(target, previous)
    return _runtime_env_model(previous)


def _write_runtime_env(
    upstream: dict,
    *,
    provider_id: str,
    config_version: int,
    target: Path | None = None,
) -> None:
    target = target or _runtime_env_path()
    values = (str(upstream["api_key"]), str(upstream["model"]), str(upstream["base_url"]))
    if any("\n" in value or "\r" in value for value in values):
        raise RuntimeError("executor_upstream_value_invalid")
    content = "\n".join((
        f"DEEPSEEK_API_KEY={upstream['api_key']}",
        f"DEEPSEEK_MODEL={upstream['model']}",
        f"DEEPSEEK_URL={_chat_completions_url(upstream['base_url'])}",
        f"DEEPSEEK_MAX_TOKENS={max(1, min(int(upstream.get('max_output_tokens') or 900), 8192))}",
        # These identifiers are deliberately non-secret.  They make the
        # singleton proxy's actual target auditable by the bridge and prevent
        # a task snapshot from silently running through a different draft
        # adapter than the one recorded in SQLite.
        f"CODEX_EXECUTOR_PROVIDER_ID={str(provider_id or '').strip()}",
        f"CODEX_EXECUTOR_CONFIG_VERSION={max(0, int(config_version or 0))}",
        "DEEPSEEK_DEBUG=0",
        "",
    )).encode("utf-8")
    _write_runtime_file(target, content)


def _apply_error_code(exc: Exception) -> str:
    value = str(exc or "").strip()
    if value.startswith("executor_") and value.replace("_", "").isalnum() and len(value) <= 120:
        return value
    return "executor_runtime_apply_failed"


def _restart_and_verify(expected_model: str) -> None:
    service = _runtime_service().removesuffix(".service")
    broker_write("service_restart", service, idempotency_key=f"executor-apply-{uuid.uuid4().hex}")
    last_error = "executor_proxy_health_timeout"
    for _ in range(20):
        try:
            with urllib.request.urlopen("http://127.0.0.1:5000/healthz", timeout=2) as response:
                payload = json.load(response)
            if response.status == 200 and payload.get("ok") and payload.get("model") == expected_model:
                return
            last_error = "executor_proxy_model_mismatch"
        except Exception:
            last_error = "executor_proxy_health_unavailable"
        time.sleep(0.25)
    raise RuntimeError(last_error)


def _restart_without_runtime() -> None:
    """Restart the proxy after a bootstrap verification removed its env.

    There is intentionally no health-success condition here: a first custom
    executor has no formerly active custom runtime to restore.  A successful
    systemd restart after removal ensures the staged candidate process is no
    longer serving its credentials; the following activation remains gated by
    a fresh explicit switch.
    """
    service = _runtime_service().removesuffix(".service")
    broker_write("service_restart", service, idempotency_key=f"executor-stage-reset-{uuid.uuid4().hex}")


def apply_executor_profile(conn: sqlite3.Connection, provider_id: str) -> dict:
    """Apply one profile only from an explicit activation/reconciliation path."""
    with executor_runtime_exclusive_lock():
        return _apply_executor_profile_locked(conn, provider_id)


def _apply_executor_profile_locked(conn: sqlite3.Connection, provider_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM model_executor_profiles WHERE provider_id=?",
        (provider_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("executor_profile_missing")
    profile = dict(row)
    version = int(profile.get("config_version") or 0)
    previous: bytes | None = None
    target: Path | None = None
    wrote_runtime = False
    try:
        upstream = _upstream(conn, profile)
        target, previous = _runtime_env_snapshot()
        _write_runtime_env(
            upstream,
            provider_id=provider_id,
            config_version=version,
            target=target,
        )
        wrote_runtime = True
        _restart_and_verify(str(upstream["model"]))
    except Exception as exc:
        error = _apply_error_code(exc)
        rollback = "not_needed"
        if wrote_runtime and target is not None:
            try:
                previous_model = _restore_runtime_env(target, previous)
                if previous_model:
                    _restart_and_verify(previous_model)
                    rollback = "restored_and_verified"
                elif previous is None:
                    _restart_without_runtime()
                    rollback = "new_runtime_removed_and_proxy_reset"
                else:
                    rollback = "restored_unverified"
            except Exception:
                error = "executor_runtime_rollback_failed"
                rollback = "rollback_failed"
        conn.execute(
            "UPDATE model_executor_profiles SET last_apply_status='failed',last_error=?,updated_at=datetime('now') WHERE provider_id=?",
            (error, provider_id),
        )
        return {"provider_id": provider_id, "ok": False, "error": error, "rollback": rollback}
    conn.execute(
        """UPDATE model_executor_profiles
           SET applied_version=config_version,last_apply_status='applied',last_error='',updated_at=datetime('now')
           WHERE provider_id=?""",
        (provider_id,),
    )
    return {"provider_id": provider_id, "ok": True, "applied_version": version}


@contextmanager
def staged_executor_profile(conn: sqlite3.Connection, provider_id: str):
    """Temporarily route the singleton proxy to a candidate and always restore.

    This is only for isolated verification.  A candidate is never marked
    ``applied`` here and cannot become the work executor merely by being
    tested.  Without a prior custom runtime, the candidate is explicitly
    removed and the proxy restarted after verification; it cannot be left
    live merely because the check completed.
    """
    provider_id = str(provider_id or "").strip()
    with executor_runtime_exclusive_lock():
        row = conn.execute(
            "SELECT * FROM model_executor_profiles WHERE provider_id=?",
            (provider_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("executor_profile_missing")
        profile = dict(row)
        upstream = _upstream(conn, profile)
        target, previous = _runtime_env_snapshot()
        previous_model = _runtime_env_model(previous)
        wrote_candidate = False
        try:
            _write_runtime_env(
                upstream,
                provider_id=provider_id,
                config_version=int(profile.get("config_version") or 0),
                target=target,
            )
            wrote_candidate = True
            _restart_and_verify(str(upstream["model"]))
            yield {
                "provider_id": provider_id,
                "config_version": int(profile.get("config_version") or 0),
                "model": str(upstream["model"] or ""),
            }
        finally:
            if wrote_candidate:
                try:
                    _restore_runtime_env(target, previous)
                    if previous_model:
                        _restart_and_verify(previous_model)
                    else:
                        _restart_without_runtime()
                except Exception as exc:
                    raise RuntimeError("executor_runtime_stage_restore_failed") from exc


def _toml_quote(value: object) -> str:
    """Quote a value as a TOML basic string (JSON escaping is compatible)."""
    return json.dumps(str(value or ""), ensure_ascii=False)


def _ensure_executor_profile_file(conn: sqlite3.Connection, profile_row: sqlite3.Row) -> bool:
    """Write the codex CLI profile config for a Draft so that isolated
    verification can run before activation.

    A saved custom-provider profile is a Draft (``pending``) and its codex
    ``--profile`` config file must exist before ``executor_candidate_runtime_status``
    and the staged verification runner can proceed.  This function is file-only:
    it never marks the profile applied and never touches the shared proxy
    runtime.  Content matches the deployed deepseek-proxy profile shape.
    """
    profile = dict(profile_row)
    profile_name = str(profile.get("profile_name") or "").strip()
    provider_id = str(profile.get("provider_id") or "").strip()
    if not profile_name or not provider_id:
        return False
    # The codex CLI profile authenticates to the local singleton proxy with the
    # proxy access key (injected into the process env by codex_exec_env), not
    # the upstream API key, which lives only in the proxy's own env file.
    provider_key = provider_id.replace("-", "_")
    upstream = _upstream(conn, profile)
    model_name = str(upstream.get("model") or "").strip()
    if not model_name:
        return False
    content = "".join((
        f"model_provider = {_toml_quote(provider_key)}\n",
        f"model = {_toml_quote(model_name)}\n",
        "\n",
        f"[model_providers.{provider_key}]\n",
        f"name = {_toml_quote(provider_key)}\n",
        "base_url = \"http://127.0.0.1:5000/v1\"\n",
        "wire_api = \"responses\"\n",
        "env_key = \"CODEX_PROXY_ACCESS_KEY\"\n",
        "request_max_retries = 1\n",
        "stream_max_retries = 1\n",
        "timeout_ms = 120000\n",
    ))
    path = executor_profile_path(profile_name)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return True
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.release-tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def apply_profiles_for_dependency(
    conn: sqlite3.Connection, *, provider_id: str = "", model_id: str = "", provider_runtime_changed: bool = True,
    model_runtime_changed: bool = True,
) -> list[dict]:
    clauses, params = [], []
    if provider_id:
        clauses.extend(("provider_id=?", "upstream_provider_id=?"))
        params.extend((provider_id, provider_id))
    if model_id:
        clauses.append("upstream_model_id=?")
        params.append(model_id)
    if not clauses:
        return []
    rows = conn.execute(
        "SELECT provider_id,profile_name,upstream_provider_id,upstream_model_id,config_version,applied_version,last_apply_status FROM model_executor_profiles WHERE " + " OR ".join(clauses),
        tuple(params),
    ).fetchall()
    results = []
    for row in rows:
        provider_is_upstream = bool(
            provider_id
            and str(row["upstream_provider_id"] or "") == provider_id
            and str(row["provider_id"] or "") != provider_id
        )
        dependency_changed = bool(
            (model_id and model_runtime_changed and str(row["upstream_model_id"] or "") == model_id)
            or (provider_is_upstream and provider_runtime_changed)
        )
        if dependency_changed:
            conn.execute(
                """UPDATE model_executor_profiles
                   SET config_version=config_version+1,last_apply_status='pending',updated_at=datetime('now')
                   WHERE provider_id=?""",
                (row["provider_id"],),
            )
        # A Draft still needs its codex CLI profile file so that the isolated
        # verification step can stage and run it before activation.
        _ensure_executor_profile_file(conn, row)
        # This hook is invoked by ordinary Provider/Model saves.  It may mark
        # affected profiles as Draft/pending, but it must never rewrite the
        # one shared proxy runtime.  Only the explicit work-executor activation
        # operation may call apply_executor_profile().
    return results
