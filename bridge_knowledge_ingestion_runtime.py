"""Knowledge ingestion runner (C3/C5): scan -> dedupe -> Draft.

The runner is deliberately narrow.  It scans only Owner-configured sources,
compares per-file identity against the operational metadata tables, and for
changed files extracts heading-chunk candidates.  A candidate that duplicates
an existing Draft or conflicts with a Published item becomes a superseding
Draft with conflict metadata — never an in-place overwrite of Published
knowledge.  The runner never publishes anything; publishing stays an Owner
review action.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Mapping

from bridge_knowledge_ingestion import (
    SourceConfig,
    chunk_markdown,
    scan_source,
    validate_source_config,
)
from bridge_knowledge_ingestion_schema import (
    require_knowledge_ingestion_schema,
)
from bridge_knowledge_service import (
    _assistant_id,
    _insert_knowledge,
    list_knowledge,
)
from bridge_living_wiki import content_hash


def _now() -> str:
    from bridge_migrations import utc_now
    return utc_now()


def _upsert_source(
    conn: sqlite3.Connection,
    config: SourceConfig,
) -> str:
    now = _now()
    existing = conn.execute(
        "SELECT id FROM assistant_knowledge_sources WHERE source_type=? AND root_path=? ORDER BY created_at LIMIT 1",
        (config.source_type, config.root),
    ).fetchone()
    source_id = str(existing[0]) if existing else "knsrc-" + uuid.uuid4().hex
    conn.execute(
        """INSERT INTO assistant_knowledge_sources(
               id,source_type,root_path,enabled,config_revision,config_json,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               source_type=excluded.source_type,root_path=excluded.root_path,
               enabled=excluded.enabled,config_revision=excluded.config_revision,
               config_json=excluded.config_json,updated_at=excluded.updated_at""",
        (
            source_id,
            config.source_type,
            config.root,
            1 if config.enabled else 0,
            config.config_revision,
            json.dumps(
                {
                    "root": config.root,
                    "enabled": config.enabled,
                    "allowed_suffixes": list(config.allowed_suffixes),
                    "max_file_bytes": config.max_file_bytes,
                    "max_files": config.max_files,
                    "max_run_bytes": config.max_run_bytes,
                    "max_scan_seconds": config.max_scan_seconds,
                    "max_chunks": config.max_chunks,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            now,
            now,
        ),
    )
    return source_id


def _load_existing_document(conn: sqlite3.Connection, source_id: str, file_path: str) -> dict | None:
    row = conn.execute(
        """SELECT file_sha256,last_content_hash,status FROM assistant_knowledge_source_documents
           WHERE source_id=? AND file_path=?""",
        (source_id, file_path),
    ).fetchone()
    return dict(row) if row else None


def _upsert_document(
    conn: sqlite3.Connection,
    source_id: str,
    file_path: str,
    facts: Mapping,
) -> str:
    now = _now()
    conn.execute(
        """INSERT INTO assistant_knowledge_source_documents(
               source_id,file_path,file_sha256,size_bytes,mtime_iso,
               first_seen_at,last_seen_at,last_content_hash,status,error_kind,
               superseded_item_id,superseded_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_id,file_path) DO UPDATE SET
               file_sha256=excluded.file_sha256,size_bytes=excluded.size_bytes,
               mtime_iso=excluded.mtime_iso,last_seen_at=excluded.last_seen_at,
               last_content_hash=excluded.last_content_hash,status=excluded.status,
               error_kind=excluded.error_kind""",
        (
            source_id,
            file_path,
            str(facts.get("file_sha256") or ""),
            int(facts.get("size_bytes") or 0),
            str(facts.get("mtime_iso") or ""),
            now,
            now,
            str(facts.get("content_hash") or ""),
            str(facts.get("status") or "active"),
            str(facts.get("error_kind") or ""),
            str(facts.get("superseded_item_id") or ""),
            str(facts.get("superseded_at") or ""),
        ),
    )
    return file_path


def _existing_published_hashes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT content_hash FROM assistant_knowledge_items WHERE status IN ('published','draft')"
    ).fetchall()
    return {str(row[0]) for row in rows if str(row[0])}


def _existing_published_sources(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Map source_ref -> list of published/draft knowledge with same origin."""
    rows = conn.execute(
        """SELECT id,title,status,source_ref,content_hash,evidence_refs_json
           FROM assistant_knowledge_items
           WHERE status IN ('published','draft')"""
    ).fetchall()
    result: dict[str, list[dict]] = {}
    for row in rows:
        ref = str(row["source_ref"] or "")
        result.setdefault(ref, []).append(dict(row))
        # Also index by normalized title so an admin-published item with the
        # same heading/title surfaces as a conflict rather than being
        # overwritten silently.
        title = str(row["title"] or "").strip()
        if title:
            result.setdefault("title:" + title.lower(), []).append(dict(row))
    return result


def run_knowledge_ingestion(
    conn: sqlite3.Connection,
    config: Mapping,
    *,
    actor: str = "knowledge_worker",
) -> dict:
    """Run one bounded ingestion pass for one configured source.

    Produces Drafts only.  Returns a run summary that is also persisted to
    ``assistant_knowledge_ingestion_runs``.
    """

    require_knowledge_ingestion_schema(conn)
    normalized = validate_source_config(config)
    if not normalized.enabled:
        return {"ok": False, "reason": "disabled", "drafts": 0, "new_drafts": []}
    started = time.monotonic()
    run_id = "knrun-" + uuid.uuid4().hex
    source_id = _upsert_source(conn, normalized)
    scan = scan_source(normalized)
    published_hashes = _existing_published_hashes(conn)
    published_by_ref = _existing_published_sources(conn)
    changed = unchanged = deleted = failed = 0
    chunks = candidates = drafts = conflicts = rejected = 0
    new_drafts: list[str] = []
    stop_reason = ""
    for facts in scan.get("files") or []:
        file_path = str(facts.get("file_path") or "")
        status = str(facts.get("status") or "")
        if status in {"failed", "over_run_budget", "scan_timeout"}:
            failed += 1
            _upsert_document(
                conn,
                source_id,
                file_path,
                {**facts, "status": "failed", "error_kind": str(facts.get("error_kind") or status)},
            )
            stop_reason = status if status in {"over_run_budget", "scan_timeout"} else stop_reason
            continue
        existing = _load_existing_document(conn, source_id, file_path)
        try:
            body = _read_source_body(normalized.root, file_path, normalized.max_file_bytes)
        except ValueError as exc:
            failed += 1
            _upsert_document(
                conn,
                source_id,
                file_path,
                {**facts, "status": "encoding_invalid", "error_kind": str(exc)[:160]},
            )
            continue
        content_sha = content_hash(body)
        if existing and existing.get("last_content_hash") == content_sha:
            unchanged += 1
            _upsert_document(conn, source_id, file_path, {**facts, "content_hash": content_sha})
            continue
        changed += 1
        _upsert_document(conn, source_id, file_path, {**facts, "content_hash": content_sha})
        file_chunks = chunk_markdown(body, max_chars=normalized.max_chunks)
        chunks += len(file_chunks)
        source_ref = _source_ref(normalized.source_type, source_id, file_path)
        for chunk in file_chunks:
            candidates += 1
            chunk_hash = str(chunk.get("content_sha256") or "")
            if chunk_hash in published_hashes:
                unchanged += 1
                continue
            draft_title = _draft_title(file_path, chunk)
            existing_for_ref = published_by_ref.get(source_ref) or []
            existing_by_title = published_by_ref.get("title:" + draft_title.lower()) or []
            conflict = bool(existing_for_ref or existing_by_title)
            try:
                draft = _insert_knowledge(
                    conn,
                    {
                        "title": _draft_title(file_path, chunk),
                        "content": str(chunk.get("content") or ""),
                        "audience": "all_channels",
                        "kind": "reference",
                        "source_type": normalized.source_type,
                        "source_ref": source_ref,
                        "source_scope_type": "knowledge_source",
                        "consent_basis": "owner_configured_source",
                        "evidence_refs": [
                            f"{source_ref}#{chunk_hash[:16]}",
                        ],
                        "review_note": "knowledge_ingestion_auto_draft" if not conflict else "knowledge_ingestion_conflict_supersedes",
                    },
                    actor=actor,
                    source_type=normalized.source_type,
                    source_ref=source_ref,
                )
            except (ValueError, sqlite3.Error) as exc:
                rejected += 1
                continue
            published_hashes.add(chunk_hash)
            new_drafts.append(str(draft.get("id") or ""))
            drafts += 1
            if conflict:
                conflicts += 1
    # Mark documents not seen in this scan as missing (file deletion only
    # marks stale; it never deletes Published knowledge).
    seen_paths = {
        str(item.get("file_path") or "")
        for item in scan.get("files") or []
        if str(item.get("status") or "") not in {"failed", "over_run_budget", "scan_timeout"}
    }
    missing = conn.execute(
        """SELECT file_path FROM assistant_knowledge_source_documents
           WHERE source_id=? AND status='active' AND file_path NOT IN (%s)"""
        % (",".join("?" for _ in seen_paths)),
        (source_id, *sorted(seen_paths)),
    ).fetchall()
    for row in missing:
        deleted += 1
        conn.execute(
            """UPDATE assistant_knowledge_source_documents
               SET status='missing',last_seen_at=? WHERE source_id=? AND file_path=?""",
            (_now(), source_id, str(row[0])),
        )
    duration = round(time.monotonic() - started, 3)
    conn.execute(
        """INSERT INTO assistant_knowledge_ingestion_runs(
               id,source_id,config_revision,started_at,finished_at,duration_seconds,
               discovered,unchanged,changed,deleted,failed,chunks,candidates,drafts,
               conflicts,rejected,stop_reason,error_kind
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, source_id, normalized.config_revision, _now(), _now(), duration,
            int(scan.get("discovered") or 0), unchanged, changed, deleted, failed,
            chunks, candidates, drafts, conflicts, rejected, stop_reason, "",
        ),
    )
    conn.commit()
    return {
        "ok": True,
        "run_id": run_id,
        "source_id": source_id,
        "discovered": int(scan.get("discovered") or 0),
        "unchanged": unchanged,
        "changed": changed,
        "deleted": deleted,
        "failed": failed,
        "chunks": chunks,
        "candidates": candidates,
        "drafts": drafts,
        "conflicts": conflicts,
        "rejected": rejected,
        "new_drafts": new_drafts,
        "stop_reason": stop_reason,
        "duration_seconds": duration,
    }


def _read_source_body(root: str, file_path: str, max_bytes: int) -> str:
    from pathlib import Path

    root_path = Path(root).resolve()
    target = (root_path / file_path).resolve()
    try:
        target.relative_to(root_path)
    except ValueError:
        raise ValueError("knowledge_source_path_escape") from None
    raw = target.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError("knowledge_source_file_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("knowledge_source_encoding_invalid") from exc


def _source_ref(source_type: str, source_id: str, file_path: str) -> str:
    # Stable, non-leaking identity: source kind + opaque source id + relative
    # file path.  Never exposes the absolute host path.
    return f"{source_type}:{source_id[:12]}:{file_path}"


def _draft_title(file_path: str, chunk: Mapping) -> str:
    heading = str(chunk.get("heading_path") or "").strip()
    base = heading or file_path
    return (base.replace("/", " / ")[:110] or "知识草稿")


__all__ = [
    "run_knowledge_ingestion",
    "_source_ref",
]
