"""Filesystem-backed Provider API keys with strict reference validation."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
SECRET_REF_RE = re.compile(r"^provider:([a-z0-9][a-z0-9_-]{1,63}):v([1-9][0-9]*)$")


class ProviderSecretError(RuntimeError):
    pass


class ProviderSecretStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    @classmethod
    def for_connection(cls, conn: sqlite3.Connection) -> "ProviderSecretStore":
        configured = str(os.environ.get("PROVIDER_SECRET_DIR") or "").strip()
        if configured:
            return cls(configured)
        database_path = ""
        for row in conn.execute("PRAGMA database_list").fetchall():
            if str(row[1]) == "main":
                database_path = str(row[2] or "")
                break
        if database_path:
            return cls(Path(database_path).resolve().parent / "provider-secrets")
        return cls(
            Path(tempfile.gettempdir())
            / "agent-provider-secrets-tests"
            / str(os.getpid())
            / f"conn-{id(conn):x}"
        )

    @staticmethod
    def _provider_id(value: object) -> str:
        provider_id = str(value or "").strip()
        if not PROVIDER_ID_RE.fullmatch(provider_id):
            raise ProviderSecretError("invalid_provider_secret_id")
        return provider_id

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ProviderSecretError("provider_secret_dir_invalid")
        os.chmod(self.root, 0o700)

    def _path_for_ref(self, provider_id: object, secret_ref: object) -> Path:
        provider_id = self._provider_id(provider_id)
        match = SECRET_REF_RE.fullmatch(str(secret_ref or "").strip())
        if not match or match.group(1) != provider_id:
            raise ProviderSecretError("provider_secret_ref_invalid")
        version = int(match.group(2))
        path = self.root / f"{provider_id}.v{version}.key"
        if path.parent != self.root:
            raise ProviderSecretError("provider_secret_path_invalid")
        return path

    def write(self, provider_id: object, secret: object, version: int) -> str:
        provider_id = self._provider_id(provider_id)
        value = str(secret or "").strip()
        if not value or len(value.encode("utf-8")) > 65536 or "\x00" in value:
            raise ProviderSecretError("provider_secret_invalid")
        version = int(version)
        if version < 1:
            raise ProviderSecretError("provider_secret_version_invalid")
        secret_ref = f"provider:{provider_id}:v{version}"
        self._ensure_root()
        path = self._path_for_ref(provider_id, secret_ref)
        if path.exists() or path.is_symlink():
            raise ProviderSecretError("provider_secret_version_exists")
        temporary = self.root / f".{provider_id}.{uuid.uuid4().hex}.tmp"
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = None
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return secret_ref

    def read(self, provider_id: object, secret_ref: object) -> str:
        path = self._path_for_ref(provider_id, secret_ref)
        if path.is_symlink():
            raise ProviderSecretError("provider_secret_symlink_not_allowed")
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            raise ProviderSecretError("provider_secret_missing") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65537:
            raise ProviderSecretError("provider_secret_file_invalid")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ProviderSecretError("provider_secret_permissions_invalid")
        return path.read_text(encoding="utf-8").strip()

    def delete_ref(self, provider_id: object, secret_ref: object) -> bool:
        path = self._path_for_ref(provider_id, secret_ref)
        if path.is_symlink():
            raise ProviderSecretError("provider_secret_symlink_not_allowed")
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def prune(self, active_refs: set[str]) -> int:
        if not self.root.exists():
            return 0
        if self.root.is_symlink() or not self.root.is_dir():
            raise ProviderSecretError("provider_secret_dir_invalid")
        removed = 0
        for path in self.root.iterdir():
            match = re.fullmatch(r"([a-z0-9][a-z0-9_-]{1,63})\.v([1-9][0-9]*)\.key", path.name)
            if not match:
                continue
            secret_ref = f"provider:{match.group(1)}:v{match.group(2)}"
            if secret_ref not in active_refs:
                if path.is_symlink():
                    raise ProviderSecretError("provider_secret_symlink_not_allowed")
                path.unlink()
                removed += 1
        return removed


def ensure_provider_secret_columns(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_providers)").fetchall()}
    definitions = {
        "secret_ref": "TEXT NOT NULL DEFAULT ''",
        "secret_version": "INTEGER NOT NULL DEFAULT 0",
        "secret_rotated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in definitions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE model_providers ADD COLUMN {name} {definition}")


def resolve_provider_secret(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> str:
    item = dict(row)
    secret_ref = str(item.get("secret_ref") or "").strip()
    if secret_ref:
        return ProviderSecretStore.for_connection(conn).read(item.get("provider_id") or item.get("id"), secret_ref)
    return str(item.get("api_key") or "").strip()


def provider_secret_public(conn: sqlite3.Connection, row: sqlite3.Row | dict, mask) -> dict:
    item = dict(row)
    plaintext = str(item.pop("api_key", "") or "")
    secret_ref = str(item.get("secret_ref") or "")
    available = bool(plaintext or secret_ref)
    if secret_ref:
        try:
            plaintext = resolve_provider_secret(conn, item)
        except Exception:
            available, plaintext = False, ""
    item.update(
        api_key_set=bool(secret_ref or plaintext),
        secret_available=available,
        api_key_preview=mask(plaintext),
    )
    return item


def prepare_provider_secret_update(
    conn: sqlite3.Connection,
    provider_id: str,
    api_key: str,
    clear: bool,
    now: str,
) -> tuple[str, int, str, str]:
    existing = conn.execute(
        "SELECT secret_ref,secret_version,secret_rotated_at FROM model_providers WHERE id=?",
        (provider_id,),
    ).fetchone()
    current_ref, current_version, current_rotated = (
        (str(existing[0] or ""), int(existing[1] or 0), str(existing[2] or ""))
        if existing else ("", 0, "")
    )
    if clear:
        return "", 0, "", ""
    if not api_key:
        return current_ref, current_version, current_rotated, ""
    version = current_version + 1
    new_ref = ProviderSecretStore.for_connection(conn).write(provider_id, api_key, version)
    return new_ref, version, now, new_ref


def prune_unreferenced_provider_secrets(conn: sqlite3.Connection) -> int:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_providers)").fetchall()}
    if "secret_ref" not in columns:
        return 0
    refs = {
        str(row[0])
        for row in conn.execute("SELECT secret_ref FROM model_providers WHERE secret_ref <> ''").fetchall()
    }
    return ProviderSecretStore.for_connection(conn).prune(refs)


def migrate_plaintext_provider_rows(
    conn: sqlite3.Connection,
    store: ProviderSecretStore,
) -> list[str]:
    """Move legacy plaintext rows to versioned files inside one DB transaction."""

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_providers)").fetchall()}
    required = {"api_key", "secret_ref", "secret_version", "secret_rotated_at"}
    if not required.issubset(columns):
        raise ProviderSecretError("provider_secret_schema_not_ready")
    rows = conn.execute(
        "SELECT id,api_key,secret_version FROM model_providers WHERE api_key <> '' ORDER BY id",
    ).fetchall()
    created: list[tuple[str, str]] = []
    migrated: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            provider_id = str(row[0])
            secret = str(row[1] or "")
            version = int(row[2] or 0) + 1
            secret_ref = store.write(provider_id, secret, version)
            created.append((provider_id, secret_ref))
            if store.read(provider_id, secret_ref) != secret:
                raise ProviderSecretError("provider_secret_verification_failed")
            conn.execute(
                """
                UPDATE model_providers
                SET api_key='',secret_ref=?,secret_version=?,secret_rotated_at=?
                WHERE id=? AND api_key=?
                """,
                (
                    secret_ref,
                    version,
                    datetime.now(timezone.utc).isoformat(),
                    provider_id,
                    secret,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ProviderSecretError("provider_secret_concurrent_change")
            migrated.append(provider_id)
        conn.commit()
    except Exception:
        conn.rollback()
        for provider_id, secret_ref in created:
            store.delete_ref(provider_id, secret_ref)
        raise
    return migrated


__all__ = [
    "ProviderSecretError",
    "ProviderSecretStore",
    "ensure_provider_secret_columns",
    "prune_unreferenced_provider_secrets",
    "resolve_provider_secret",
    "migrate_plaintext_provider_rows",
    "prepare_provider_secret_update",
    "provider_secret_public",
]
